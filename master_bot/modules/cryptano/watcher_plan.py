# modules/cryptano/watcher_plan.py

import time
import os
from modules.cryptano.utils.storage import load_json
import pandas as pd
import gc
import traceback
from modules.cryptano.utils.common import calculate_rsi, exchange, format_price as fmt_p, price_precision_from_market, resolve_symbol, KNOWN_TICKER_ALIASES
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.indicators import pandas_get_local_structure, calculate_atr
from modules.cryptano.utils.watcher_logic import analyze_extreme_pattern
from modules.cryptano.utils.vbottom_manager import VBottomManager

SCAN_COINS_LIMIT = 150

def check_manual_extreme(coin, direction, source="Manual"):
    """
    Делает умный срез графика M15. Ищет пик/дно в последних свечах 
    и проверяет, не произошел ли разворот на основе SFP (ликвидности).
    """
    try:
        start_time = time.time()
        api_queries = 2
        time.sleep(0.3)

        coin = coin.upper().replace("USDT", "").replace("/", "").strip()
        
        markets = load_markets_cached(exchange)
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            print(f"[WATCHER DEBUG] ❌ Ошибка: {coin}/USDT не найдена на бирже.")
            return False, f"❌ Монета *{coin}* не найдена на Bybit."

        market_info = markets[symbol]
        price_precision = price_precision_from_market(market_info)

        ohlcv = None
        for attempt in range(3):
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=120)
                break
            except Exception as e:
                if "Rate Limit" in str(e) or "10006" in str(e):
                    print(f"[WATCHER WARNING] Bybit лимиты забиты на {coin}. Пауза 1.5 сек...")
                    time.sleep(1.5)
                else:
                    raise e

        if not ohlcv or len(ohlcv) < 16:
            print(f"[WATCHER DEBUG] ⚠️ Недостаточно данных ({len(ohlcv) if ohlcv else 0} свечей)")
            return False, f"⚠️ Недостаточно данных по монете {coin} на M15."

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["rsi"] = calculate_rsi(df)

        current_candle = df.iloc[-1] # Самая последняя свеча (онлайн)
        current_price = float(current_candle["close"])
        
        # Отрезаем незакрытую (живую) свечу, работаем только с фактами
        df_closed = df.iloc[:-1]
        
        # --- НОВОЕ: ЗАГРУЗКА МАКРО-УРОВНЕЙ ---
        
        macro_path = os.path.join(os.path.dirname(__file__), "macro_levels.json")
        macro_db = load_json(macro_path, default={})
        coin_macro = macro_db.get(coin) or macro_db.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})
        # ------------------------------------
        
        # Передаем source и coin_macro
        result = analyze_extreme_pattern(df_closed, direction, current_price, price_precision, source, coin_macro)

        trigger_fired = result.get("trigger_fired", False)
        rsi_filter_passed = result.get("rsi_filter_passed", False)
        volume_climax = result.get("volume_climax", False)
        trigger_type = result.get("trigger_type", "НЕТ")
        rsi_value = result.get("rsi_value", 50.0)
        vol_ratio = result.get("vol_ratio", 1.0)

        sl_price = result.get("sl_price", 0.0)
        tp1_price = result.get("tp1_price", 0.0)
        tp2_price = result.get("tp2_price", 0.0)
        tp3_price = result.get("tp3_price", 0.0)

        # 🚀 ГЛАВНОЕ ПРАВИЛО ВХОДА: Жесткий фильтр RSI для ВСЕХ источников
        is_ready = trigger_fired and rsi_filter_passed
        
        icon = "🔴" if direction == "SHORT" else "🟢"
        status_text = "ВХОД! УСЛОВИЯ ВЫПОЛНЕНЫ" if is_ready else "ЖДЕМ (Слабость не подтверждена)"
        
        # Короткие метки для источников
        source_map = {
            "Manual": "MAN",
            "Critical": "CRIT",
            "Light": "LIGHT",
            "Swing Hunter": "SWING"
        }
        src_label = source_map.get(source, source).upper()

        # Строка без лишнего мусора, только суть
        report = f"{icon} {coin} | WATCHER {direction} | {src_label}\n\n"
        
        # 1. Триггер
        trigger_icon = "⚡️" if trigger_fired else "⏳"
        report += f"{trigger_icon} Триггер: {trigger_type}\n"
        
        # 2. RSI Предохранитель
        rsi_icon = "🔥" if rsi_filter_passed else "🧊"
        rsi_status = "Пройден" if rsi_filter_passed else "Остыл/Не дошел"
        report += f"{rsi_icon} RSI: {rsi_value} ({rsi_status})\n"
        
        # 3. Объемный усилитель
        vol_icon = "💥" if volume_climax else "📊"
        vol_text = f"Аномалия x{vol_ratio}" if volume_climax else f"Обычный x{vol_ratio}"
        report += f"{vol_icon} Объем: {vol_text}\n\n"
        
        if is_ready:
            report += f"📝 WATCHER PLAN:\n"
            report += f"• Entry: {fmt_p(current_price)}\n"
            
            # Оставляем только уникальные тейки (если впереди было мало зон)
            tps = [fmt_p(tp1_price)]
            if tp2_price != tp1_price: tps.append(fmt_p(tp2_price))
            if tp3_price != tp2_price: tps.append(fmt_p(tp3_price))
            
            report += f"• TP: {' | '.join(tps)}\n"
            report += f"• Sl: {fmt_p(sl_price)}\n"

            # ----------------------------------------------------
            # НОВЫЙ БЛОК: Сохраняем сигнал Ватчера в общую историю
            # ----------------------------------------------------
            try:
                from modules.cryptano.history import save_signal
                signal_data = {
                    "coin": coin,
                    "type": "SHORT_PUMP" if direction == "SHORT" else "LONG_ROLLBACK", 
                    "price": current_price,
                    "take_profit": tp1_price,  # TP1: главная цель для подсчета ✅
                    "target_2": tp2_price if tp2_price != tp1_price else None,
                    "target_3": tp3_price if tp3_price != tp2_price else None,
                    "stop_loss": sl_price,
                    "source": "WATCHER"        # Бирка для фильтрации в отчете
                }
                save_signal(signal_data)
            except Exception as e:
                print(f"[WATCHER ERROR] Не удалось сохранить сигнал в историю: {e}")
            # ----------------------------------------------------

        elapsed_time = time.time() - start_time

        del df
        gc.collect()

        return is_ready, report

    except Exception as e:
        print(f"\n[WATCHER ERROR] ❌ КРИТИЧЕСКАЯ ОШИБКА В РУЧНОМ СКАНЕ ({coin}): {e}")
        traceback.print_exc()  # Печатает полный стек вызовов ошибки в терминал
        return False, f"❌ Ошибка ручного экспресс-анализа {coin}: {e}"
    


def check_v_bottom(coin, direction, vbottom_mgr=None):
    """
    Проверяет V-BOTTOM паттерн на уровнях по 15-минутным свечам.
    Возвращает (is_ready, report_text, levels_checked) — третье значение
    нужно для статистики скана (сколько уровней вообще оценили).
    """
    try:
        time.sleep(0.3)

        coin = coin.upper().replace("USDT", "").replace("/", "").strip()
        
        # Резолвим символ на бирже (учитывает алиасы тикеров типа TON/TONCOIN)
        markets = load_markets_cached(exchange)
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            return False, f"❌ Монета *{coin}* не найдена на Bybit.", 0

        # Тянем 15m-свечи (120 свечей = 30 часов истории, достаточно для 52-свечи baseline)
        ohlcv = None
        for attempt in range(3):
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=120)
                break
            except Exception as e:
                if "Rate Limit" in str(e) or "10006" in str(e):
                    print(f"[V_BOTTOM WARNING] Bybit rate limit на {coin}. Пауза 1.5 сек...")
                    time.sleep(1.5)
                else:
                    raise e

        if not ohlcv or len(ohlcv) < 52:
            return False, f"⚠️ Недостаточно данных V-BOTTOM для {coin} ({len(ohlcv) if ohlcv else 0} свечей).", 0

        # Формируем DataFrame с индексом по времени для VBottomManager
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)

        # Загружаем макро-уровни
        macro_path = os.path.join(os.path.dirname(__file__), "macro_levels.json")
        macro_db = load_json(macro_path, default={})
        coin_macro = macro_db.get(coin) or macro_db.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})

        if not coin_macro:
            return False, f"⚠️ Нет уровней для {coin} в macro_levels.json.", 0

        # Инициализируем менеджер если не передан
        if vbottom_mgr is None:
            vbottom_mgr = VBottomManager()

        # Берём уровни поддержки для LONG, сопротивления для расчёта TP
        supports = coin_macro.get("supports", [])
        resistances = coin_macro.get("resistances", [])

        if not supports and direction == "LONG":
            return False, f"⚠️ Нет поддержек для V-BOTTOM на {coin}.", 0

        signals = []
        levels_checked = 0

        # Проверяем каждый уровень поддержки для LONG
        if direction == "LONG":
            for level in supports:
                levels_checked += 1
                result = vbottom_mgr.evaluate_v_bottom(
                    level, df, "LONG", resistances,
                    trend="UNKNOWN", c_atr=None
                )
                if result.get('allow'):
                    signals.append(result)

        # Для SHORT аналогично, но на сопротивлениях
        elif direction == "SHORT":
            for level in resistances:
                levels_checked += 1
                result = vbottom_mgr.evaluate_v_bottom(
                    level, df, "SHORT", supports,
                    trend="UNKNOWN", c_atr=None
                )
                if result.get('allow'):
                    signals.append(result)

        if not signals:
            return False, None, levels_checked

        # Если есть сигналы — берём первый (обычно один)
        signal = signals[0]
        entry_price = signal.get('entry_price', 0.0)
        sl = signal.get('sl', 0.0)
        tp = signal.get('tp', 0.0)
        history_log = signal.get('history_log', '')
        level_id = signal.get('level_id', 'unknown')

        # Форматируем отчёт для телеграма
        icon = "🔴" if direction == "SHORT" else "🟢"
        report = (
            f"{icon} *V-BOTTOM {direction}* _{coin}_\n\n"
            f"Entry: `{entry_price:.8f}`\n"
            f"SL: `{sl:.8f}`\n"
            f"TP: `{tp:.8f}`\n"
            f"R/R: `{(tp-entry_price)/(entry_price-sl) if entry_price > sl else 0:.2f}`\n\n"
            f"📊 {history_log}\n"
            f"Level: `{level_id}`"
        )

        return True, report, levels_checked

    except Exception as e:
        print(f"\n[V_BOTTOM ERROR] ❌ ОШИБКА ПРИ ПРОВЕРКЕ V-BOTTOM ({coin}): {e}")
        traceback.print_exc()
        return False, f"❌ Ошибка V-BOTTOM анализа {coin}: {e}", 0

def check_v_green_bottom(coin, direction, vbottom_mgr=None):
    """
    Проверяет V-GREEN-BOTTOM паттерн (лестница ям + режим кульминации)
    на 15-минутных свечах. Работает только для LONG.
    Возвращает (is_ready, report_text, levels_checked) — тот же формат,
    что и check_v_bottom, чтобы использовать один менеджер на пару стратегий.
    """
    if direction != "LONG":
        return False, None, 0

    try:
        time.sleep(0.3)

        coin = coin.upper().replace("USDT", "").replace("/", "").strip()

        # Резолвим символ на бирже (учитывает алиасы тикеров типа TON/TONCOIN)
        markets = load_markets_cached(exchange)
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            return False, f"❌ Монета *{coin}* не найдена на Bybit.", 0

        # Тянем 15m-свечи (120 свечей = 30 часов истории, достаточно для 52-свечи baseline)
        ohlcv = None
        for attempt in range(3):
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=120)
                break
            except Exception as e:
                if "Rate Limit" in str(e) or "10006" in str(e):
                    print(f"[V_GREEN_BOTTOM WARNING] Bybit rate limit на {coin}. Пауза 1.5 сек...")
                    time.sleep(1.5)
                else:
                    raise e

        if not ohlcv or len(ohlcv) < 52:
            return False, f"⚠️ Недостаточно данных V-GREEN-BOTTOM для {coin} ({len(ohlcv) if ohlcv else 0} свечей).", 0

        # Формируем DataFrame с индексом по времени
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)

        # V-GREEN-BOTTOM реально использует ATR (в отличие от V-BOTTOM) — считаем по-настоящему
        atr_series = calculate_atr(df, 14)
        c_atr = float(atr_series.iloc[-1]) if not atr_series.empty and atr_series.iloc[-1] == atr_series.iloc[-1] else None

        # Загружаем макро-уровни
        macro_path = os.path.join(os.path.dirname(__file__), "macro_levels.json")
        macro_db = load_json(macro_path, default={})
        coin_macro = macro_db.get(coin) or macro_db.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})

        if not coin_macro:
            return False, f"⚠️ Нет уровней для {coin} в macro_levels.json.", 0

        if vbottom_mgr is None:
            vbottom_mgr = VBottomManager()

        supports = coin_macro.get("supports", [])
        resistances = coin_macro.get("resistances", [])

        if not supports:
            return False, f"⚠️ Нет поддержек для V-GREEN-BOTTOM на {coin}.", 0

        signals = []
        levels_checked = 0

        for level in supports:
            levels_checked += 1
            result = vbottom_mgr.evaluate_v_green_bottom(
                level, df, "LONG", resistances,
                trend="UNKNOWN", c_atr=c_atr
            )
            if result.get('allow'):
                signals.append(result)

        if not signals:
            return False, None, levels_checked

        signal = signals[0]
        entry_price = signal.get('entry_price', 0.0)
        sl = signal.get('sl', 0.0)
        tp = signal.get('tp', 0.0)
        history_log = signal.get('history_log', '')
        level_id = signal.get('level_id', 'unknown')

        report = (
            f"🟢 *V-GREEN-BOTTOM LONG* _{coin}_\n\n"
            f"Entry: `{entry_price:.8f}`\n"
            f"SL: `{sl:.8f}`\n"
            f"TP: `{tp:.8f}`\n"
            f"R/R: `{(tp-entry_price)/(entry_price-sl) if entry_price > sl else 0:.2f}`\n\n"
            f"📊 {history_log}\n"
            f"Level: `{level_id}`"
        )

        return True, report, levels_checked

    except Exception as e:
        print(f"\n[V_GREEN_BOTTOM ERROR] ❌ ОШИБКА ПРИ ПРОВЕРКЕ V-GREEN-BOTTOM ({coin}): {e}")
        traceback.print_exc()
        return False, f"❌ Ошибка V-GREEN-BOTTOM анализа {coin}: {e}", 0