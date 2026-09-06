import time
import pandas as pd
import gc
from datetime import datetime
from modules.cryptano.utils.common import calculate_rsi, exchange, format_price as fmt_p
from modules.cryptano.utils.market_cache import load_markets_cached, get_ohlcv_cached
from modules.cryptano.utils.price_action import check_live_confirmation
from modules.cryptano.utils.regime import detect_market_regime
from modules.cryptano.strategy.manual_sfp import check_manual_extreme
from modules.cryptano.utils.indicators import calculate_atr

# ==========================================
# БЛОК 1: Настройки и Импорты
# ==========================================
MIN_RR = 2.5

def fmt_z(z): return fmt_p((z['min'] + z['max']) / 2) if z else "Нет"

def clean_price(price_str):
    """Возвращает строку без обрезки нулей (исправление для фикса целых чисел)"""
    return str(price_str)

# ==========================================
# БЛОК 3: Оценка Сетапа (Quality и Confirmation)
# ==========================================
def evaluate_setup(direction, current_price, rsi_4h, vol_ratio, market_mode, range_position, rr, zone_score):
    # 1. Setup Quality (Качество самой зоны по истории и математика RR)
    quality = 50 + min(zone_score * 5, 30)
    if rr >= 3: quality += 20
    elif rr >= 2: quality += 10
    elif rr > 0 and rr < 1.5: quality -= 30
    elif rr == 0: quality -= 50
    quality = max(0, min(100, int(quality)))

    # 2. Entry Confirmation (Живой контекст и реакция рынка)
    confirm = 50
    reasons = []

    # Пространственная позиция цены относительно рабочих уровней
    if direction == "LONG":
        if range_position <= 25: confirm += 20
        elif range_position > 45: 
            confirm -= 25; reasons.append("• цена слишком высоко для лонга")
    elif direction == "SHORT":
        if range_position >= 75: confirm += 20
        elif range_position < 55: 
            confirm -= 25; reasons.append("• цена слишком низко для шорта")

    # Объемы (Главное рыночное подтверждение)
    if vol_ratio >= 1.5: confirm += 20
    elif vol_ratio < 0.8: 
        confirm -= 20; reasons.append("• нет подтверждения объемом (слабая активность)")

    # Фильтр направления глобального тренда
    if "Strong Bear" in market_mode and direction == "LONG":
        confirm -= 35; reasons.append("• лонг против сильного падающего тренда")
    elif "Strong Bull" in market_mode and direction == "SHORT":
        confirm -= 35; reasons.append("• шорт против сильного бычьего тренда")
        
    # RSI как фильтр истощения / Modifier (Мягкое влияние вместо жестких блокировок)
    if direction == "LONG":
        if rsi_4h <= 35: 
            confirm += 10 # Топливо для bounce
        elif rsi_4h > 65: 
            confirm -= 20; reasons.append("• RSI в зоне перегрева, покупка опасна")
    elif direction == "SHORT":
        if rsi_4h >= 65: 
            confirm += 10 # Топливо для падения от хаев
        elif rsi_4h < 35: 
            confirm -= 10 # Не запрет, но предупреждаем о риске отскока
            reasons.append("⚠️ RSI в перепроданности — возможен отскок")

    if not reasons and confirm < 60:
        reasons.append("• нет явных признаков защиты зоны ценой")

    confirm = max(15, min(100, int(confirm))) # Нижний лимит удержан на 15 баллах
    return quality, confirm, reasons

# ==========================================
# БЛОК 4: Поиск Order Block зон (MTF) + Pivots
# ==========================================
def get_order_blocks(df, price_precision=6):
    df = df.copy()
    df.reset_index(drop=True, inplace=True)

    df['body'] = (df['close'] - df['open']).abs()
    df['avg_body'] = df['body'].rolling(20, min_periods=1).mean()
    df['avg_vol'] = df['volume'].rolling(20, min_periods=1).mean()

    raw_supports = []
    raw_resistances = []

    # 1. ОРДЕР-БЛОКИ (Следы крупного объема - старая база)
    for i in range(1, len(df)):
        if df['body'].iat[i] > 2 * df['avg_body'].iat[i] and df['volume'].iat[i] > 2 * df['avg_vol'].iat[i]:
            if df['close'].iat[i] > df['open'].iat[i]:
                j = i - 1
                while j >= 0 and df['close'].iat[j] >= df['open'].iat[j]:
                    j -= 1
                if j >= 0:
                    raw_supports.append({'min': float(df['low'].iat[j]), 'max': float(df['high'].iat[j]), 'score': 1})
            elif df['close'].iat[i] < df['open'].iat[i]:
                j = i - 1
                while j >= 0 and df['close'].iat[j] <= df['open'].iat[j]:
                    j -= 1
                if j >= 0:
                    raw_resistances.append({'min': float(df['low'].iat[j]), 'max': float(df['high'].iat[j]), 'score': 1})

    # 2. ПИВОТЫ (Классические Swing High / Swing Low толпы)
    window = 10 # Смотрим 10 свечей влево и 10 вправо
    for i in range(window, len(df) - window):
        high_val = df['high'].iat[i]
        low_val = df['low'].iat[i]
        
        # Если это локальный максимум
        if high_val == df['high'].iloc[i-window:i+window+1].max():
            raw_resistances.append({
                'min': float(max(df['open'].iat[i], df['close'].iat[i])), # Низ зоны - тело свечи
                'max': float(high_val),                                   # Верх зоны - шпилька
                'score': 2 # Пивотам даем вес x2
            })
        
        # Если это локальный минимум
        if low_val == df['low'].iloc[i-window:i+window+1].min():
            raw_supports.append({
                'min': float(low_val),                                    # Низ зоны - шпилька
                'max': float(min(df['open'].iat[i], df['close'].iat[i])), # Верх зоны - тело свечи
                'score': 2
            })

    # 3. КЛАСТЕРИЗАЦИЯ (Склейка зон, которые стоят рядом)
    def cluster_zones(zones):
        clusters = []
        threshold = 0.015 # Если уровни ближе чем на 1.5% - клеим их в один монолит
        
        for zone in sorted(zones, key=lambda x: (x['min'] + x['max']) / 2):
            zone_avg = (zone['min'] + zone['max']) / 2
            added = False
            for cluster in clusters:
                cluster_avg = (cluster['min'] + cluster['max']) / 2
                if abs(zone_avg - cluster_avg) / cluster_avg <= threshold:
                    cluster['min'] = min(cluster['min'], zone['min'])
                    cluster['max'] = max(cluster['max'], zone['max'])
                    cluster['score'] += zone['score'] # Зона становится сильнее
                    added = True
                    break
            if not added:
                clusters.append(zone.copy())
                
        # Возвращаем только подтвержденные уровни (где совпало хотя бы 2 фактора)
        return [
            {'min': float(c['min']), 'max': float(c['max']), 'score': c['score']}
            for c in clusters if c['score'] >= 2
        ]

    supports = cluster_zones(raw_supports)
    resistances = cluster_zones(raw_resistances)
    return supports, resistances
# ==========================================
# БЛОК 5: Основная Функция Анализа
# ==========================================
def analyze_coin(ticker_input: str) -> str:
    msg = "" 
    try:
        start_time = time.time()
        api_queries = 2
        coin = ticker_input.upper().replace("USDT", "").replace("/", "").strip()
        symbol = f"{coin}/USDT"
        
        markets = load_markets_cached(exchange)
        if symbol not in markets:
            symbol_fut = f"{coin}/USDT:USDT"
            if symbol_fut in markets: symbol = symbol_fut
            else: return f"❌ Монета *{coin}* не найдена на Bybit."

        ohlcv_1h = get_ohlcv_cached(exchange, symbol, timeframe="1h", limit=336)
        df_1h = pd.DataFrame(ohlcv_1h, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df_1h["rsi"] = calculate_rsi(df_1h)

        ohlcv_4h = get_ohlcv_cached(exchange, symbol, timeframe="4h", limit=360)
        df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df_4h["rsi"] = calculate_rsi(df_4h)
        df_4h["atr"] = calculate_atr(df_4h)

        last_4h = df_4h.iloc[-1]
        current_price = float(last_4h["close"])
        rsi_4h = int(round(last_4h["rsi"])) 
        atr = float(last_4h["atr"])
        
        # Ищем максимальный всплеск объема за последние 16 часов (4 свечи), чтобы не пропустить памп
        recent_vol = float(df_4h["volume"].iloc[-5:-1].max())
        avg_vol = float(df_4h["volume"].iloc[-30:-5].mean())
        vol_ratio = round(recent_vol / avg_vol if avg_vol > 0 else 1.0, 2)
        # ----------------------------------

        ohlcv_daily = get_ohlcv_cached(exchange, symbol, timeframe="1d", limit=365)
        df_daily = pd.DataFrame(ohlcv_daily, columns=["timestamp", "open", "high", "low", "close", "volume"])
        last_daily_vol = df_daily["volume"].iloc[-1]
        last_daily_close = df_daily["close"].iloc[-1]
        daily_turnover = last_daily_vol * last_daily_close 
        
        if daily_turnover < 1000000:
            return f"⚠️ Монета *{coin}* отфильтрована: низкий ликвид (объем ${int(daily_turnover):,})"
         
        df_daily["ma7"] = df_daily["close"].rolling(7).mean()
        df_daily["ma30"] = df_daily["close"].rolling(30).mean()
        df_daily["ma200"] = df_daily["close"].rolling(200).mean()
        df_daily["rsi"] = calculate_rsi(df_daily)

        last_d = df_daily.iloc[-1]
        ma7 = last_d["ma7"]
        ma30 = last_d["ma30"]
        ma200 = last_d["ma200"]
        rsi_daily = int(round(last_d["rsi"])) if not pd.isna(last_d["rsi"]) else 50

        # =========================================================
        # 🌋 ЛОГИКА EXTREME MODE И АВТОПЕРЕХОДА (2 из 3)
        # =========================================================
        old_price = float(df_4h["close"].iloc[-20])
        price_change_pct = ((current_price - old_price) / old_price) * 100

        pump_triggers = 0
        if price_change_pct > 15.0: pump_triggers += 1
        if vol_ratio >= 2.0: pump_triggers += 1  
        if rsi_4h >= 75: pump_triggers += 1

        dump_triggers = 0
        if price_change_pct < -15.0: dump_triggers += 1
        if vol_ratio >= 2.0: dump_triggers += 1
        if rsi_4h <= 25: dump_triggers += 1

        is_pump = pump_triggers >= 2
        is_dump = dump_triggers >= 2

        # ==========================================
        # БЛОК 6: Определение Контекста Тренда (По MA)
        # ==========================================
        if not pd.isna(ma7) and not pd.isna(ma30):
            # Если ma200 еще не отрисовалась, используем ma30 как глобальную базу
            ma200_val = ma200 if not pd.isna(ma200) else ma30
            
            if rsi_4h <= 20 or rsi_daily <= 25:
                market_mode, trend_label = "Capitulation", "🚨 TREND: Capitulation (Панический слив, шортить поздно!)"
            # Приоритет 1: Цена выше MA200 = Глобальный бычий фон
            elif current_price > ma200_val:
                if current_price > ma7 and ma7 > ma30 and ma30 >= ma200_val:
                    market_mode = "Strong Bull"
                else:
                    market_mode = "Bullish Pullback"
            # Приоритет 2: Цена ниже MA200 = Глобальный медвежий фон
            elif current_price < ma200_val:
                if current_price < ma7 and ma7 < ma30 and ma30 <= ma200_val:
                    market_mode = "Strong Bear"
                else:
                    market_mode = "Weak Bear"
            else:
                market_mode = "Range"
        else:
            market_mode = "Range"

        trend_map = {
            "Strong Bull": "📈 Бычий.",
            "Bullish Pullback": "📈 Бычий. Локально коррекция.",
            "Strong Bear": "📉 Медвежий.",
            "Weak Bear": "📉 Медвежий.",
            "Range": "⚖️ Боковик.",
            "Capitulation": "📉 Медвежий.",
            "Bearish Rally": "📉 Медвежий. Локально отскок."
        }
        trend_label = trend_map.get(market_mode, "⚖️ Боковик.")

        # ==========================================
        # БЛОК 7: Пространственная Иерархия Зон
        # ==========================================
        local_supports, local_resistances = get_order_blocks(df_1h, price_precision=6)
        swing_supports, swing_resistances = get_order_blocks(df_4h, price_precision=6)
        macro_supports, macro_resistances = get_order_blocks(df_daily, price_precision=6)

        def nearest_below(zones):
            return min((z for z in zones if z['max'] < current_price), key=lambda z: current_price - z['max'], default=None)

        def nearest_above(zones):
            return min((z for z in zones if z['min'] > current_price), key=lambda z: z['min'] - current_price, default=None)

        local_sup = nearest_below(local_supports)
        local_res = nearest_above(local_resistances)
        swing_sup = nearest_below(swing_supports)
        swing_res = nearest_above(swing_resistances)
        macro_sup = nearest_below(macro_supports)
        macro_res = nearest_above(macro_resistances)

        window_min = float(df_4h['low'].min())
        window_max = float(df_4h['high'].max())

        if not local_sup:
            local_sup = {"min": window_min, "max": window_min, "score": 1}
        if not local_res:
            local_res = {"min": window_max, "max": window_max, "score": 1}
        if not macro_sup:
            macro_sup = {"min": float(df_daily['low'].min()), "max": float(df_daily['low'].min()), "score": 1}
        if not macro_res:
            macro_res = {"min": float(df_daily['high'].max()), "max": float(df_daily['high'].max()), "score": 1}

        macro_zones = []
        if macro_sup:
            macro_zones.append(macro_sup)
        if macro_res:
            macro_zones.append(macro_res)

        range_position = ((current_price - window_min) / (window_max - window_min)) * 100 if window_max > window_min else 50
        
        if local_sup and local_res:
            dist_long_pct = ((current_price - local_sup['max']) / current_price) * 100
            dist_short_pct = ((local_res['min'] - current_price) / current_price) * 100
        elif local_sup and not local_res:
            dist_long_pct = ((current_price - local_sup['max']) / current_price) * 100
            dist_short_pct = 0
        elif local_res and not local_sup:
            dist_long_pct = 0
            dist_short_pct = ((local_res['min'] - current_price) / current_price) * 100
        else:
            dist_long_pct, dist_short_pct = 0, 0

        dist_l = abs(dist_long_pct)
        if dist_l < 1:
            long_dist_text = f"{fmt_p(dist_l)}% (очень близко)"
        elif dist_l < 3:
            long_dist_text = f"{fmt_p(dist_l)}% (близко)"
        else:
            long_dist_text = f"{fmt_p(dist_l)}% (далеко)"

        dist_s = abs(dist_short_pct)
        if dist_s < 1:
            short_dist_text = f"{fmt_p(dist_s)}% (очень близко)"
        elif dist_s < 3:
            short_dist_text = f"{fmt_p(dist_s)}% (близко)"
        else:
            short_dist_text = f"{fmt_p(dist_s)}% (далеко)"

        if range_position <= 30: pos_text = f"🟢 Близко к зоне поддержки"
        elif range_position >= 70: pos_text = f"🔴 Близко к сопротивлению"
        else: pos_text = f"⛔ Между рабочими уровнями (No Trade Zone)"

        # ==========================================
        # БЛОК 8: Формирование Шапки
        # ==========================================
        def get_rsi_label(rsi):
            if rsi <= 35: return "давление продавца"
            if rsi >= 65: return "давление покупателя"
            return "нейтрально"

        def get_vol_label(vol):
            if vol < 0.8: return "📉 Слабая активность"
            if vol < 1.2: return "📊 Нормальный"
            if vol < 2.0: return "📈 Повышенный (Выше обычного)"
            if vol < 3.0: return "🔥 Высокий"
            return "🌋 Аномальный (Кульминация)"

        if is_dump:
            header_title = f"🔍 {coin} | DUMP 🚨"
            price_label = "📉 Сильный импульс вниз."
        elif is_pump:
            header_title = f"🔍 {coin} | PUMP 🚨"
            price_label = "📈 Сильный импульс вверх."
        else:
            header_title = f"🔍 {coin} | OVERVIEW"
            price_label = trend_label

        msg = (
            f"{header_title}\n"
            f"💰 Цена: {clean_price(fmt_p(current_price))} | {price_label}\n\n"
            f"📊 *СОСТОЯНИЕ РЫНКА:*\n"
            f"• RSI: `{rsi_daily}-{rsi_4h}` (d1, h4) → {get_rsi_label(rsi_4h)}\n"
            f"• Объём: `x{vol_ratio}` → {get_vol_label(vol_ratio)}\n\n"
        )

        if not (is_pump or is_dump):
            msg += (
                f"📊 *ДНЕВНЫЕ МЕТРИКИ:*\n"
                f"🔴 MA7: `{clean_price(fmt_p(ma7))}` | 🔴 MA30: `{clean_price(fmt_p(ma30))}` | 🔴 MA200: `{clean_price(fmt_p(ma200))}`\n"
                f"━━━━━━━━━━━━━━━\n"
            )

        # ==========================================
        # БЛОК 9: Контекст и Уровни
        # ==========================================
        # Сортируем макро-зоны по цене для красивого вывода
        macro_sorted = sorted(macro_zones, key=lambda x: x["max"])
        macro_text = " | ".join([f"`{fmt_z(z)}`" for z in macro_sorted]) if macro_sorted else "Формируется"

        if not (is_pump or is_dump):
            msg += (
                f"📌 *КЛЮЧЕВЫЕ УРОВНИ*\n"
                f"🔹 Local: `{clean_price(fmt_z(local_sup))}` | `{clean_price(fmt_z(local_res))}`\n"
                f"🔹 Swing: `{clean_price(fmt_z(swing_sup))}` | `{clean_price(fmt_z(swing_res))}`\n"
                f"🔹 Macro: {macro_text}\n\n"
            )
        else:
            msg += (
                f"📌 *КЛЮЧЕВЫЕ УРОВНИ*\n"
                f"🔹 Macro: {macro_text}\n"
                f"━━━━━━━━━━━━━━━\n"
            )

        if is_dump:
            msg += "➡️ Запущен глубокий анализ Watcher...\n━━━━━━━━━━━━━━━\n"
            # При дампе логично искать точку разворота вверх (LONG)
            _, watcher_report = check_manual_extreme(coin, "LONG")
            if watcher_report:
                msg += f"{watcher_report}\n"
        elif is_pump:
            msg += "➡️ Запущен глубокий анализ Watcher...\n━━━━━━━━━━━━━━━\n"
            # При пампе логично искать точку разворота вниз (SHORT)
            _, watcher_report = check_manual_extreme(coin, "SHORT")
            if watcher_report:
                msg += f"{watcher_report}\n"
        else:
            no_trade_zone = 30 <= range_position <= 70
            idea_present = not no_trade_zone

            if not idea_present:
                msg += "🎯 ТОРГОВЫЙ ПЛАН: Вне рынка (Нет интересных точек входа)\n"
            else:
                if dist_l <= dist_s:
                    main_dir = "LONG"
                    alt_dir = "SHORT"
                else:
                    main_dir = "SHORT"
                    alt_dir = "LONG"

                # Собираем ВСЕ сопротивления и поддержки с 1H, 4H, 1D в одну Карту Ликвидности
                all_resistances = local_resistances + swing_resistances + macro_resistances
                all_supports = local_supports + swing_supports + macro_supports

                def get_clean_targets(zones, is_long):
                    targets = sorted([z['min'] if is_long else z['max'] for z in zones])
                    if not is_long: targets.reverse()
                    
                    clean = []
                    for t in targets:
                        if (is_long and t > current_price) or (not is_long and t < current_price):
                            if not clean or abs(t - clean[-1]) / clean[-1] > 0.015:
                                clean.append(t)
                    return clean

                long_targets = get_clean_targets(all_resistances, is_long=True)
                short_targets = get_clean_targets(all_supports, is_long=False)

                if main_dir == "LONG":
                    l_entry = local_sup["max"]
                    l_sl = local_sup["min"] - atr
                    
                    l_tp1 = long_targets[0] if len(long_targets) > 0 else l_entry * 1.03
                    l_tp2 = long_targets[1] if len(long_targets) > 1 else l_tp1 * 1.05
                    l_tp3 = long_targets[2] if len(long_targets) > 2 else l_tp2 * 1.08
                    
                    l_rr = round((l_tp1 - l_entry) / (l_entry - l_sl), 1) if l_entry > l_sl else 0
                    l_qual, l_conf, l_reasons = evaluate_setup("LONG", current_price, rsi_4h, vol_ratio, market_mode, range_position, l_rr, local_sup["score"])
                    risk_label = "(По тренду)" if l_qual >= 65 else "(Контртренд)"

                    msg += (
                        f"🎯 LONG IDEA {risk_label}\n"
                        f"📍 Вход: {clean_price(fmt_p(l_entry))}\n"
                        f"🛡 СТОП (SL): {clean_price(fmt_p(l_sl))}\n"
                        f"💰 ЦЕЛИ (TP): {clean_price(fmt_p(l_tp1))} | {clean_price(fmt_p(l_tp2))} | {clean_price(fmt_p(l_tp3))}\n"
                    )
                else:
                    s_entry = local_res["min"]
                    s_sl = local_res["max"] + atr
                    
                    s_tp1 = short_targets[0] if len(short_targets) > 0 else s_entry * 0.97
                    s_tp2 = short_targets[1] if len(short_targets) > 1 else s_tp1 * 0.95
                    s_tp3 = short_targets[2] if len(short_targets) > 2 else s_tp2 * 0.92

                    s_rr = round((s_entry - s_tp1) / (s_sl - s_entry), 1) if s_sl > s_entry else 0
                    s_qual, s_conf, s_reasons = evaluate_setup("SHORT", current_price, rsi_4h, vol_ratio, market_mode, range_position, s_rr, local_res["score"])
                    risk_label = "(По тренду)" if s_qual >= 65 else "(Контртренд)"

                    msg += (
                        f"🎯 SHORT IDEA {risk_label}\n"
                        f"📍 Вход: {clean_price(fmt_p(s_entry))}\n"
                        f"🛡 СТОП (SL): {clean_price(fmt_p(s_sl))}\n"
                        f"💰 ЦЕЛИ (TP): {clean_price(fmt_p(s_tp1))} | {clean_price(fmt_p(s_tp2))} | {clean_price(fmt_p(s_tp3))}\n"
                    )
                # ==========================================
                # БЛОК 11: Триггер входа
                # ==========================================
                msg += "━━━━━━━━━━━━━━━\n"
                if main_dir == "SHORT" and local_res:
                    is_conf, score, details = check_live_confirmation(df_4h, "SHORT", local_res['min'], local_res['max'], vol_ratio)
                elif main_dir == "LONG" and local_sup:
                    is_conf, score, details = check_live_confirmation(df_4h, "LONG", local_sup['min'], local_sup['max'], vol_ratio)
                else:
                    is_conf, score, details = False, 0, []

                trigger_status = "✅ ЕСТЬ ПОДТВЕРЖДЕНИЕ" if is_conf else "⛔ ЖДЕМ ПОДТВЕРЖДЕНИЯ"
                vol_ok = any(d.startswith("✅ Volume Spike") for d in details)
                rejection_ok = any(d.startswith("✅ Rejection Candle") for d in details)
                hold_ok = any(d.startswith("✅ Удержание уровня") for d in details)

                msg += f"🧭 ТРИГГЕР ВХОДА: {trigger_status}\n"
                msg += f"• Объем (x1.5+): {'✅ Да' if vol_ok else f'❌ Нет (сейчас x{vol_ratio})'}\n"
                msg += f"• Форма свечи: {'✅ Реверс-бар' if rejection_ok else '❌ Нет Rejection'}\n"
                msg += f"• Защита уровня: {'✅ Уровень удерживают' if hold_ok else '❌ Нет удержания'}\n\n"

                if alt_dir == "SHORT":
                    res_zone = f"{clean_price(fmt_p(local_res['min']))} - {clean_price(fmt_p(local_res['max']))}" if local_res else "Не определена"
                    msg += f"🎯 SHORT IDEA: Искать в зоне {res_zone}\n"
                else:
                    sup_zone = f"{clean_price(fmt_p(local_sup['min']))} - {clean_price(fmt_p(local_sup['max']))}" if local_sup else "Не определена"
                    msg += f"🎯 LONG IDEA: Искать в зоне {sup_zone}\n"

    except Exception as e:
        return f"❌ Произошла ошибка при анализе {ticker_input}: {e}"
        
    elapsed_time = time.time() - start_time
    print(f"\n[ПОЛНЫЙ АНАЛИЗ] 📊 Полный анализ завершен за {elapsed_time:.2f} сек.")
    print(f"[ПОЛНЫЙ АНАЛИЗ] 🌐 Запросов к API Bybit: {api_queries}\n")
    
    try:
        del df_1h, df_4h, df_daily
    except UnboundLocalError:
        pass
    gc.collect()
    
    return msg