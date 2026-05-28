import math
import pandas as pd
import ccxt
from datetime import datetime
from modules.cryptano.price_action import check_live_confirmation

# ==========================================
# БЛОК 1: Настройки и Импорты
# ==========================================
exchange = ccxt.bybit({'enableRateLimit': True})
MIN_RR = 2.5

# Умный форматер чисел (убирает визуальный шум до запятой для тяжелых монет)
def fmt_p(val):
    if val is None or pd.isna(val): return "Нет"
    val = float(val)
    if val >= 100: return f"{int(val)}" # Для BTC, ETH убираем копейки
    if val >= 1: return f"{round(val, 2)}"
    return f"{val}" # Для дешевых альткоинов оставляем точность

def fmt_z(z): return f"{fmt_p(z['min'])}–{fmt_p(z['max'])}" if z else "Нет"

# ==========================================
# БЛОК 2: Базовые Индикаторы
# ==========================================
def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = ema_up / ema_down
    return 100 - (100 / (1 + rs))

def calculate_atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_cp = (df["high"] - df["close"].shift()).abs()
    low_cp = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

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
# БЛОК 4: Поиск Зон Ликвидности (Market Profile)
# ==========================================
def get_all_zones(df_daily, price_precision, days=180):
    df = df_daily.tail(days).copy()
    df.reset_index(drop=True, inplace=True)
    
    pivots = []
    for i in range(3, len(df) - 3):
        window_high = df['high'].iloc[i-3:i+4]
        window_low = df['low'].iloc[i-3:i+4]
        if df['high'].iloc[i] == window_high.max(): pivots.append(df['high'].iloc[i])
        if df['low'].iloc[i] == window_low.min(): pivots.append(df['low'].iloc[i])

    ZONE_THRESHOLD = 0.02
    clusters = []
    for p in sorted(pivots):
        added = False
        for cluster in clusters:
            cluster_avg = sum(cluster) / len(cluster)
            if abs(p - cluster_avg) / cluster_avg <= ZONE_THRESHOLD:
                cluster.append(p)
                added = True
                break
        if not added: clusters.append([p])

    zones = []
    for c in clusters:
        touches = len(c)
        if touches >= 2: 
            zones.append({
                "min": round(min(c), price_precision), 
                "max": round(max(c), price_precision), 
                "score": touches 
            })
    return zones

# ==========================================
# БЛОК 5: Основная Функция Анализа
# ==========================================
def analyze_coin(ticker_input: str) -> str:
    msg = "" 
    try:
        coin = ticker_input.upper().replace("USDT", "").replace("/", "").strip()
        symbol = f"{coin}/USDT"
        
        markets = exchange.load_markets()
        if symbol not in markets:
            symbol_fut = f"{coin}/USDT:USDT"
            if symbol_fut in markets: symbol = symbol_fut
            else: return f"❌ Монета *{coin}* не найдена на Bybit."
                
        market_info = exchange.market(symbol)
        price_precision = market_info.get('precision', {}).get('price', 4)
        if isinstance(price_precision, float) and price_precision < 1:
            price_precision = int(round(-math.log10(price_precision)))
        elif not isinstance(price_precision, int): price_precision = 4

        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=100)
        df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df_4h["rsi"] = calculate_rsi(df_4h)
        df_4h["atr"] = calculate_atr(df_4h)

        last_4h = df_4h.iloc[-1]
        current_price = round(float(last_4h["close"]), price_precision)
        rsi_4h = int(round(last_4h["rsi"])) 
        atr = float(last_4h["atr"])
        recent_vol = float(last_4h["volume"])
        avg_vol = float(df_4h["volume"].iloc[-25:-1].mean())
        vol_ratio = round(recent_vol / avg_vol if avg_vol > 0 else 1.0, 2)

        ohlcv_daily = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=365)
        df_daily = pd.DataFrame(ohlcv_daily, columns=["timestamp", "open", "high", "low", "close", "volume"])
        last_daily_vol = df_daily["volume"].iloc[-1]
        last_daily_close = df_daily["close"].iloc[-1]
        daily_turnover = last_daily_vol * last_daily_close 
        
        if daily_turnover < 5000000:
             return f"⚠️ Монета *{coin}* отфильтрована: низкий ликвид (объем ${int(daily_turnover):,})"
         
        df_daily["ma7"] = df_daily["close"].rolling(7).mean()
        df_daily["ma30"] = df_daily["close"].rolling(30).mean()
        df_daily["ma200"] = df_daily["close"].rolling(200).mean()
        df_daily["rsi"] = calculate_rsi(df_daily)

        last_d = df_daily.iloc[-1]
        ma7 = last_d["ma7"]
        ma30 = last_d["ma30"]
        ma200 = last_d["ma200"]
        rsi_daily = int(round(last_d["rsi"])) 

        # ==========================================
        # БЛОК 6: Определение Контекста Тренда (По MA)
        # ==========================================
        # Если монета относительно новая и ma200 еще нет, считаем тренд по ma7 и ma30
        if not pd.isna(ma7) and not pd.isna(ma30):
            ma200_val = ma200 if not pd.isna(ma200) else ma30
            if rsi_4h <= 20 or rsi_daily <= 25:
                market_mode, trend_label = "Capitulation", "🚨 TREND: Capitulation (Панический слив, шортить поздно!)"
            elif current_price > ma7 and ma7 > ma30 and ma30 > ma200:
                market_mode, trend_label = "Strong Bull", "🚀 TREND: Strong Bull (Сильный рост, работаем от откатов)"
            elif current_price > ma30 and current_price < ma200:
                market_mode, trend_label = "Weak Bull", "📈 TREND: Weak Bull (Глобально медведи, но локально отскок)"
            elif current_price < ma7 and ma7 < ma30 and ma30 < ma200:
                market_mode, trend_label = "Strong Bear", "🩸 TREND: Strong Bear (работаем только от short setups)"
            elif current_price < ma30 and current_price > ma200:
                market_mode, trend_label = "Weak Bear", "📉 TREND: Weak Bear (Слабый рынок, работаем от откатов)"
            else:
                market_mode, trend_label = "Range", "↔️ TREND: Range Mode (Боковик, торгуем от границ канала)"
        else:
            market_mode, trend_label = "Range", "↔️ TREND: Недостаточно истории для MA"

        # ==========================================
        # БЛОК 7: Пространственная Иерархия Зон
        # ==========================================
        all_zones = get_all_zones(df_daily, price_precision, days=180)
        
        # Фильтруем и сортируем зоны строго по пространственному расстоянию от цены
        supports = sorted([z for z in all_zones if z["max"] < current_price], key=lambda x: current_price - x["max"])
        resistances = sorted([z for z in all_zones if z["min"] > current_price], key=lambda x: x["min"] - current_price)

        # 1. Local (Ближайший уровень ликвидности сверху и снизу)
        local_sup = supports[0] if len(supports) > 0 else None
        local_res = resistances[0] if len(resistances) > 0 else None

        # 2. Swing (Следующий эшелон зон)
        swing_sup = supports[1] if len(supports) > 1 else None
        swing_res = resistances[1] if len(resistances) > 1 else None

        # 3. Macro (Топ-3 сильнейших исторических зон по числу касаний в истории за год)
        macro_zones = sorted(all_zones, key=lambda x: x["score"], reverse=True)[:3]

        # Расчет процентиля нахождения цены строго МЕЖДУ рабочими зонами
        if local_sup and local_res:
            s_val = local_sup["max"]
            r_val = local_res["min"]
            range_position = ((current_price - s_val) / (r_val - s_val)) * 100
            dist_long = f"-{round(((current_price - s_val) / current_price) * 100)}"
            dist_short = round(((r_val - current_price) / current_price) * 100)
        else:
            range_position, dist_long, dist_short = 50, 0, 0

        # Корректный пространственный статус (Синхронизировано с Блоком 11: 30/70)
        if range_position <= 30: pos_text = f"🟢 Близко к зоне поддержки"
        elif range_position >= 70: pos_text = f"🔴 Близко к зоне сопротивления"
        else: pos_text = f"⛔ Между рабочими уровнями (No Trade Zone)"

        # ==========================================
        # БЛОК 8: Формирование Шапки
        # ==========================================
        def get_rsi_label(rsi):
            if rsi <= 35: return "давление продавца"
            if rsi >= 65: return "давление покупателя"
            return "нейтрально"

        def get_vol_label(vol):
            if vol < 0.8: return "слабая активность"
            if vol >= 1.5: return "аномально высокий"
            return "нормальный"

        msg = (
            f"🔍 *{coin} | АНАЛИЗ*\n"
            f"💰 Цена: `{fmt_p(current_price)}` | {trend_label}\n"
            f"📊 *СОСТОЯНИЕ РЫНКА:*\n"
            f"• RSI: `{rsi_daily}-{rsi_4h}` (d1, h4) → {get_rsi_label(rsi_4h)}\n"
            f"• Объём: `x{vol_ratio}` → {get_vol_label(vol_ratio)}\n\n"
            f"📊 *ТРЕНД (MA D1):*\n"
            f"🔴 MA7: `{fmt_p(ma7)}` | 🔴 MA30: `{fmt_p(ma30)}` | 🔴 MA200: `{fmt_p(ma200)}`\n"
            f"━━━━━━━━━━━━━━━\n"
        )

        # ==========================================
        # БЛОК 9: Контекст и Уровни
        # ==========================================
        # ==========================================
        # БЛОК 9: Контекст и Уровни
        # ==========================================
        # Сортируем макро-зоны по цене для красивого вывода
        macro_sorted = sorted(macro_zones, key=lambda x: x["max"])
        macro_text = " | ".join([f"`{fmt_z(z)}`" for z in macro_sorted]) if macro_sorted else "Формируется"

        msg += (
            f"🔮 *КОНТЕКСТ РЫНКА*\n"
            f"📍 Позиция: `{int(range_position)}%` | {pos_text}\n\n"
            f"📌 *КЛЮЧЕВЫЕ УРОВНИ*\n"
            f"🔹 Local: `{fmt_z(local_sup)}` | `{fmt_z(local_res)}`\n"
            f"🔹 Swing: `{fmt_z(swing_sup)}` | `{fmt_z(swing_res)}`\n"
            f"🔹 Macro: {macro_text}\n"
            f"📍 До long-зоны: `{dist_long}%` | 📍 До short-зоны: `+{dist_short}%`\n\n"
        )

        if "Bull" in market_mode:
            msg += f"📈 Сценарий: Тренд восходящий. Ждем скидку и ищем лонги от `{fmt_z(local_sup)}`.\n❌ Отмена: потеря уровня `{fmt_p(local_sup['min']) if local_sup else ''}`\n"
        elif "Bear" in market_mode:
            msg += f"📉 Сценарий: Давление продавца сохраняется. Вероятен тест зоны `{fmt_z(local_sup)}`.\n❌ Отмена: закреп выше `{fmt_p(local_res['max']) if local_res else ''}`\n"
        elif "Capitulation" in market_mode:
            msg += f"🚨 Сценарий: Паника на рынке. Шортить поздно. Ищем признаки остановки и защиты у макро уровней.\n"
        else:
            msg += f"↔️ Сценарий: Чистый боковик. Работаем строго от границ: лонг у `{fmt_p(local_sup['max']) if local_sup else ''}`, шорт у `{fmt_p(local_res['min']) if local_res else ''}`.\n"

        msg += "\n🎯 *ПЛАН*\n\n"

        # ==========================================
        # БЛОК 10: Логика Сделок (LONG / SHORT)
        # ==========================================
        if local_sup:
            l_entry = local_sup["max"]
            l_sl = round(local_sup["min"] - atr, price_precision)
            l_tp1 = local_res["min"] if local_res else round(l_entry * 1.05, price_precision)
            l_tp2 = swing_res["min"] if swing_res else round(l_tp1 * 1.05, price_precision)
            l_rr = round((l_tp1 - l_entry) / (l_entry - l_sl), 1) if l_entry > l_sl else 0
            
            l_qual, l_conf, l_reasons = evaluate_setup("LONG", current_price, rsi_4h, vol_ratio, market_mode, range_position, l_rr, local_sup["score"])
            
            msg += (
                f"🟢 *LONG IDEA* Зона: `{fmt_z(local_sup)}`\n"
                f"🎯 TP1: `{fmt_p(l_tp1)}` | 🎯 TP2: `{fmt_p(l_tp2)}` | 🛡 SL: `{fmt_p(l_sl)}`\n"
                f"📊 Setup Quality: `{l_qual}/100` | 📊 Entry Confirmation: `{l_conf}/100`\n"
            )
            if l_conf >= 65: msg += "✅ СЕТАП АКТИВЕН: Отличные условия для входа.\n\n"
            else:
                msg += "⛔ *Сейчас входа нет:*\n"
                for r in l_reasons: msg += f"  {r}\n"
                msg += "\n"

        if local_res:
            s_entry = local_res["min"]
            s_sl = round(local_res["max"] + atr, price_precision)
            s_tp1 = local_sup["max"] if local_sup else round(s_entry * 0.95, price_precision)
            s_tp2 = swing_sup["max"] if swing_sup else round(s_tp1 * 0.95, price_precision)
            s_rr = round((s_entry - s_tp1) / (s_sl - s_entry), 1) if s_sl > s_entry else 0
            
            s_qual, s_conf, s_reasons = evaluate_setup("SHORT", current_price, rsi_4h, vol_ratio, market_mode, range_position, s_rr, local_res["score"])
            
            msg += (
                f"🔴 *SHORT IDEA* Зона: `{fmt_z(local_res)}`\n"
                f"🎯 TP1: `{fmt_p(s_tp1)}` | 🎯 TP2: `{fmt_p(s_tp2)}` | 🛡 SL: `{fmt_p(s_sl)}`\n"
                f"📊 Setup Quality: `{s_qual}/100` | 📊 Entry Confirmation: `{s_conf}/100`\n"
            )
            if s_conf >= 65: msg += "✅ СЕТАП АКТИВЕН: Отличные условия для входа.\n\n"
            else:
                msg += "⛔ *Сейчас входа нет:*\n"
                for r in s_reasons: msg += f"  {r}\n"
                msg += "\n"

        # ==========================================
        # БЛОК 11: Финальный Приоритет (Price Action Снайпер)
        # ==========================================
        msg += "━━━━━━━━━━━━━━━\n🧭 *ЧТО ДЕЛАТЬ СЕЙЧАС*\n"
        
        if 30 <= range_position <= 70:
            msg += f"⛔ *ЖДАТЬ.* Цена между рабочими уровнями.\n"
            
        elif range_position > 70 and local_res:
            # Запускаем микро-анализ для Шорта
            is_conf, score, details = check_live_confirmation(df_4h, "SHORT", local_res['min'], local_res['max'], vol_ratio)
            
            if is_conf:
                msg += f"🔴 *SHORT SETUP ACTIVE (ВХОД РАЗРЕШЕН)*\n"
            else:
                msg += f"⛔ *ПОДТВЕРЖДЕНИЯ НЕТ (Ждем форму свечи)*\n"
                
            msg += f"📍 Зона: `{fmt_z(local_res)}`\n🔍 *Live Confirmation ({score}/3):*\n"
            for d in details: msg += f"  {d}\n"
            
        elif range_position < 30 and local_sup:
            # Запускаем микро-анализ для Лонга
            is_conf, score, details = check_live_confirmation(df_4h, "LONG", local_sup['min'], local_sup['max'], vol_ratio)
            
            if is_conf:
                msg += f"🟢 *LONG SETUP ACTIVE (ВХОД РАЗРЕШЕН)*\n"
            else:
                msg += f"⛔ *ПОДТВЕРЖДЕНИЯ НЕТ (Ждем форму свечи)*\n"
                
            msg += f"📍 Зона: `{fmt_z(local_sup)}`\n🔍 *Live Confirmation ({score}/3):*\n"
            for d in details: msg += f"  {d}\n"
            
        else:
            msg += "⚠️ Наблюдение за структурой на текущих отметках.\n"

    except Exception as e:
        return f"❌ Произошла ошибка при анализе {ticker_input}: {e}"
        
    return msg