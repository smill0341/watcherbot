import time
import datetime
import threading
import pandas as pd
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from modules.cryptano.utils.common import format_price as fmt_p
from modules.cryptano.utils.crypto_utils import calculate_rsi, exchange, get_top_coins
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.regime import detect_market_regime
from modules.cryptano.utils.indicators import get_market_state
from modules.cryptano.utils.storage import load_json

# ================= Настройки фильтров =================
TIMEFRAME = "4h"
FILTER_VOL_NORMAL = 1.7
FILTER_VOL_ANOMALY = 3.0
FILTER_ZONE_BOTTOM = 15
FILTER_ZONE_TOP = 85
FILTER_RSI_OVERSOLD = 35
SCAN_COINS_LIMIT = 100  # Количество топ-монет по объему для сканирования
FILTER_RSI_OVERBOUGHT = 65
COOLDOWN_HOURS = 4       # Не спамить одной монетой 4 часа после сигнала
SCAN_INTERVAL = 1800      # Запуск сканирования каждые 30 минут (1800 сек)
MAX_LIGHT_SCAN_WORKERS = 8

cooldown_cache = {}
_scan_lock = threading.Lock()

def _light_setup(symbol):
    """
    Единая функция анализа (Радар) для автобота и ручного сканера.
    """
    try:
        coin_name = symbol.split("/")[0]
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=250)
        if len(ohlcv) < 35:
            return None

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])

        df["rsi"] = calculate_rsi(df)

        last_row = df.iloc[-1]
        current_price = float(last_row["close"])
        rsi = float(last_row["rsi"])

        market_data = get_market_state(df, current_price)
        trend = market_data["trend"]
        pos_pct = market_data["pos_pct"]
        vol_ratio = market_data["vol_ratio"]
        ma30 = market_data["ma30"]
        strong_support = market_data["strong_support"]
        strong_resistance = market_data["strong_resistance"]
        nearest_support = market_data["nearest_support"]
        nearest_resistance = market_data["nearest_resistance"]
        
        if vol_ratio < FILTER_VOL_NORMAL:
            return None

        # --- Мягкие условия Радара ---
        is_near_support = (pos_pct <= FILTER_ZONE_BOTTOM) and (rsi <= FILTER_RSI_OVERSOLD)
        is_near_res = (pos_pct >= FILTER_ZONE_TOP) and (rsi >= FILTER_RSI_OVERBOUGHT)
        is_anomaly = (vol_ratio >= FILTER_VOL_ANOMALY) and (rsi > 60 or rsi < 40)

        if is_near_support:
            setup_info = f"Near Support (Bottom {pos_pct:.0f}%)"
        elif is_near_res:
            setup_info = f"Near Resistance (Top {pos_pct:.0f}%)"
        elif is_anomaly:
            setup_info = "Volume Anomaly / Momentum"
        else:
            return None

        market_regime = detect_market_regime(current_price, rsi, vol_ratio, ma30)

        return {
            "coin": coin_name,
            "trend": trend,
            "setup_info": setup_info,
            "pos_pct": pos_pct,
            "vol_ratio": vol_ratio,
            "rsi": rsi,
            "market_regime": market_regime,
            "current_price": current_price,
            "strong_support": strong_support,
            "strong_resistance": strong_resistance,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
        }
    except Exception as e:
        # Убрал принт с ошибкой парсинга, чтобы не засорять терминал
        return None

def run_manual_light_scan(bot, admin_chat_id):
    """ОДНОРАЗОВЫЙ ручной запуск легкого сканера по кнопке."""
    print("[SCANNER LOG] Начало ручного экспресс-сканирования Light...")
    
    if not _scan_lock.acquire(blocking=False):
        print("[SCANNER WARNING] Сканер занят фоновым потоком!")
        bot.send_message(admin_chat_id, "⚠️ Сканер сейчас занят фоновым мониторингом. Подожди минуту.")
        return

    try:
        found_something = False
        
        print("[SCANNER LOG] Начинаю перебор монет по единым условиям Light...")

        symbols = get_top_coins(limit=SCAN_COINS_LIMIT)
        start_time = time.time()
        total_processed_coins = len(symbols) if symbols else 0
        api_queries = total_processed_coins + 1
        worker_count = min(MAX_LIGHT_SCAN_WORKERS, max(1, total_processed_coins))
        setups = []

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            # Вызываем ЕДИНУЮ функцию
            futures = [executor.submit(_light_setup, symbol) for symbol in symbols]
            for future in as_completed(futures):
                setup = future.result()
                if setup:
                    setups.append(setup)

        for setup in setups:
            coin_name = setup["coin"]
            found_something = True

            current_price = setup.get('current_price', 0)
            strong_resistance = setup.get('strong_resistance', 0)
            strong_support = setup.get('strong_support', 0)
            nearest_support = setup.get('nearest_support', strong_support)
            nearest_resistance = setup.get('nearest_resistance', strong_resistance)
            pos = setup['pos_pct']
            if 20 <= pos <= 80:
                continue
            # Общие расчеты процентов изменения цены от локального минимума/максимума
            pump_pct = ((current_price - strong_support) / strong_support) * 100 if strong_support > 0 else 0
            dump_pct = ((strong_resistance - current_price) / strong_resistance) * 100 if strong_resistance > 0 else 0

            # Формируем price_text для отображения
            price_text = f"💰 Цена: {fmt_p(current_price)} (🔻 -{dump_pct:.0f}% от пика {fmt_p(strong_resistance)})"

            # Определяем тренд
            trend_str = setup['trend']
            if "бычий" in trend_str.lower():
                trend_type = "BULLISH"
            elif "медвежий" in trend_str.lower():
                trend_type = "BEARISH"
            else:
                trend_type = "FLAT"

            # Рассчитываем readiness_pct
            if trend_type == "BULLISH":
                readiness_pct = 100 - pos
            elif trend_type == "BEARISH":
                readiness_pct = pos
            else:
                readiness_pct = max(pos, 100 - pos)

            # Формируем статус, иконку и комментарий
            if trend_type == "BULLISH":
                if readiness_pct >= 85:
                    icon = "🟢"
                    status = "Long Zone"
                    comment = "👀 Цена в зоне покупок. Ищем сетап в лонг на младшем ТФ."
                elif readiness_pct >= 65:
                    icon = "🟡"
                    status = "Long Interest"
                    comment = "👀 Цена подходит к зоне покупок. Готовимся к поиску лонга."
                else:
                    icon = "⏳"
                    status = "Wait for Pullback"
                    if setup['vol_ratio'] > 2.0 and setup['rsi'] > 70.0:
                        target_pullback = fmt_p(nearest_support)
                        comment = f"👀 Сильный импульс. Ждем локальный ретест, не FOMO. Ближайшая поддержка {target_pullback}."
                    else:
                        target_pullback = fmt_p(strong_support)
                        comment = f"👀 Цена на импульсе. Ждем откат к сильной поддержке {target_pullback}."
            elif trend_type == "BEARISH":
                if readiness_pct >= 85:
                    icon = "🔴"
                    status = "Short Zone"
                    comment = "👀 Цена в зоне продаж. Ищем сетап в шорт на младшем ТФ."
                elif readiness_pct >= 65:
                    icon = "🟡"
                    status = "Short Interest"
                    comment = "👀 Цена подходит к зоне продаж. Готовимся к поиску шорта."
                else:
                    icon = "⏳"
                    status = "Wait for Bounce"
                    if setup['vol_ratio'] > 2.0 and setup['rsi'] < 30.0:
                        target_bounce = fmt_p(nearest_resistance)
                        comment = f"👀 Сильное падение. Ждем быстрый ретест. Ближайшее сопротивление {target_bounce}."
                    else:
                        target_bounce = fmt_p(strong_resistance)
                        comment = f"👀 Цена на локальном дне. Ждем отскок к сильному сопротивлению {target_bounce}."
            else:
                if pos >= 85:
                    icon = "🔴"
                    status = "Short Zone"
                    comment = "👀 Цена у верхней границы боковика."
                elif pos >= 65:
                    icon = "🟡"
                    status = "Short Interest"
                    comment = "👀 Подход к верхней границе боковика."
                elif pos <= 15:
                    icon = "🟢"
                    status = "Long Zone"
                    comment = "👀 Цена у нижней границы боковика."
                elif pos <= 35:
                    icon = "🟡"
                    status = "Long Interest"
                    comment = "👀 Подход к нижней границе боковика."
                else:
                    icon = "⚖️"
                    status = "Mid-Range"
                    comment = "👀 Цена в середине боковика. Точек входа нет."

            msg = (
                f"⚡️ LIGHT SIGNAL | #{coin_name} | {icon}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{price_text}\n"
                f"📊 Объем: x{setup['vol_ratio']:.1f}\n"
                f"🌡 RSI: {setup['rsi']:.1f}\n"
                f"--------------------------------\n"
                f"Тренд: {setup['trend']}\n"
                f"⚡️ СТАТУС: {status} ({readiness_pct:.0f}% готовность)\n\n"
                f"{comment}\n\n"
                f"❗️ Это НЕ сигнал на вход."
            )
            bot.send_message(admin_chat_id, msg, parse_mode="Markdown")
            
        if not found_something:
            print("[SCANNER LOG] Монет по фильтру Light не найдено.")
            elapsed_time = time.time() - start_time
            print(f"\n[ЛАЙТ-РАДАР РУЧНОЙ] 📊 Скан завершен за {elapsed_time:.1f} сек.")
            print(f"[ЛАЙТ-РАДАР РУЧНОЙ] 🪙 Монет обработано: {total_processed_coins}")
            print(f"[ЛАЙТ-RADAR РУЧНОЙ] 🌐 Запросов к API Bybit: {api_queries}\n")
            bot.send_message(admin_chat_id, "ℹ️ По легким фильтрам интересных монет сейчас нет. Рынок спокойный.")
        else:
            print("[SCANNER LOG] Ручной сканер Light успешно завершил работу.")
            elapsed_time = time.time() - start_time
            print(f"\n[ЛАЙТ-РАДАР РУЧНОЙ] 📊 Скан завершен за {elapsed_time:.1f} сек.")
            print(f"[ЛАЙТ-РАДАР РУЧНОЙ] 🪙 Монет обработано: {total_processed_coins}")
            print(f"[ЛАЙТ-RADAR РУЧНОЙ] 🌐 Запросов к API Bybit: {api_queries}\n")
            bot.send_message(admin_chat_id, "✅ Ручной экспресс-скан Light завершен!")
            
    except Exception as e:
        print(f"[SCANNER ERROR] Ошибка внутри ручного Light-скана: {e}")
        bot.send_message(admin_chat_id, f"❌ Ошибка при сканировании: {e}")
    finally:
        _scan_lock.release()

def run_light_scanner(bot, admin_chat_id):
    """ФОНОВЫЙ АВТОБОТ."""
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "config.json"))
 
    print("  🟡 Light скринер инициализирован!")

    while True:
        try:
            config = load_json(config_path, default={})
            if config.get("crypto", {}).get("status") != "RUNNING":
                time.sleep(30)
                continue
        except Exception as e:
            print(f"[LIGHT SCANNER] ❌ Ошибка чтения конфига: {e}")
            time.sleep(30)
            continue

        if not _scan_lock.acquire(blocking=False):
            time.sleep(30)
            continue

        try:
            coins = get_top_coins(limit=SCAN_COINS_LIMIT)
            start_time = time.time()
            total_processed_coins = len(coins) if coins else 0
            api_queries = total_processed_coins + 1
            now = datetime.datetime.now()

            eligible_symbols = []
            for symbol in coins:
                coin_name = symbol.split("/")[0]
                if coin_name in cooldown_cache:
                    if (now - cooldown_cache[coin_name]).total_seconds() < (COOLDOWN_HOURS * 3600):
                        continue
                eligible_symbols.append(symbol)

            worker_count = min(MAX_LIGHT_SCAN_WORKERS, max(1, len(eligible_symbols)))
            setups = []

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                # Вызываем ТУ ЖЕ САМУЮ ЕДИНУЮ функцию
                futures = [executor.submit(_light_setup, symbol) for symbol in eligible_symbols]
                for future in as_completed(futures):
                    setup = future.result()
                    if setup:
                        setups.append(setup)

            for setup in setups:
                coin_name = setup["coin"]

                current_price = setup.get('current_price', 0)
                strong_resistance = setup.get('strong_resistance', 0)
                strong_support = setup.get('strong_support', 0)
                nearest_support = setup.get('nearest_support', strong_support)
                nearest_resistance = setup.get('nearest_resistance', strong_resistance)
                pos = setup['pos_pct']
                # Общие расчеты процентов изменения цены от локального минимума/максимума
                pump_pct = ((current_price - strong_support) / strong_support) * 100 if strong_support > 0 else 0
                dump_pct = ((strong_resistance - current_price) / strong_resistance) * 100 if strong_resistance > 0 else 0

                # Формируем price_text для отображения
                price_text = f"💰 Цена: {fmt_p(current_price)} (🔻 -{dump_pct:.0f}% от пика {fmt_p(strong_resistance)})"

                # Определяем тренд
                trend_str = setup['trend']
                if "бычий" in trend_str.lower():
                    trend_type = "BULLISH"
                elif "медвежий" in trend_str.lower():
                    trend_type = "BEARISH"
                else:
                    trend_type = "FLAT"

                # Рассчитываем readiness_pct
                if trend_type == "BULLISH":
                    readiness_pct = 100 - pos
                elif trend_type == "BEARISH":
                    readiness_pct = pos
                else:
                    readiness_pct = max(pos, 100 - pos)

                # Формируем статус, иконку и комментарий
                if trend_type == "BULLISH":
                    if readiness_pct >= 85:
                        icon = "🟢"
                        status = "Long Zone"
                        comment = "👀 Цена в зоне покупок. Ищем сетап в лонг на младшем ТФ."
                    elif readiness_pct >= 65:
                        icon = "🟡"
                        status = "Long Interest"
                        comment = "👀 Цена подходит к зоне покупок. Готовимся к поиску лонга."
                    else:
                        icon = "⏳"
                        status = "Wait for Pullback"
                        if setup['vol_ratio'] > 2.0 and setup['rsi'] > 70.0:
                            target_pullback = fmt_p(nearest_support)
                            comment = f"👀 Сильный импульс. Ждем локальный ретест, не FOMO. Ближайшая поддержка {target_pullback}."
                        else:
                            target_pullback = fmt_p(strong_support)
                            comment = f"👀 Цена на импульсе. Ждем откат к сильной поддержке {target_pullback}."
                elif trend_type == "BEARISH":
                    if readiness_pct >= 85:
                        icon = "🔴"
                        status = "Short Zone"
                        comment = "👀 Цена в зоне продаж. Ищем сетап в шорт на младшем ТФ."
                    elif readiness_pct >= 65:
                        icon = "🟡"
                        status = "Short Interest"
                        comment = "👀 Цена подходит к зоне продаж. Готовимся к поиску шорта."
                    else:
                        icon = "⏳"
                        status = "Wait for Bounce"
                        if setup['vol_ratio'] > 2.0 and setup['rsi'] < 30.0:
                            target_bounce = fmt_p(nearest_resistance)
                            comment = f"👀 Сильное падение. Ждем быстрый ретест. Ближайшее сопротивление {target_bounce}."
                        else:
                            target_bounce = fmt_p(strong_resistance)
                            comment = f"👀 Цена на локальном дне. Ждем отскок к сильному сопротивлению {target_bounce}."
                else:
                    if pos >= 85:
                        icon = "🔴"
                        status = "Short Zone"
                        comment = "👀 Цена у верхней границы боковика."
                    elif pos >= 65:
                        icon = "🟡"
                        status = "Short Interest"
                        comment = "👀 Подход к верхней границе боковика."
                    elif pos <= 15:
                        icon = "🟢"
                        status = "Long Zone"
                        comment = "👀 Цена у нижней границы боковика."
                    elif pos <= 35:
                        icon = "🟡"
                        status = "Long Interest"
                        comment = "👀 Подход к нижней границе боковика."
                    else:
                        icon = "⚖️"
                        status = "Mid-Range"
                        comment = "👀 Цена в середине боковика. Точек входа нет."

                msg = (
                    f"⚡️ LIGHT SIGNAL | #{coin_name} | {icon}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{price_text}\n"
                    f"📊 Объем: x{setup['vol_ratio']:.1f}\n"
                    f"🌡 RSI: {setup['rsi']:.1f}\n"
                    f"--------------------------------\n"
                    f"Тренд: {setup['trend']}\n"
                    f"⚡️ СТАТУС: {status} ({readiness_pct:.0f}% готовность)\n\n"
                    f"{comment}\n\n"
                    f"❗️ Это НЕ сигнал на вход."
                )
                bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

                cooldown_cache[coin_name] = now
        finally:
            elapsed_time = time.time() - start_time
            print(f"\n[ЛАЙТ-РАДАР АВТО] 📊 Скан завершен за {elapsed_time:.1f} сек.")
            print(f"[ЛАЙТ-РАДАР АВТО] 🪙 Монет обработано: {total_processed_coins}")
            print(f"[ЛАЙТ-РАДАР АВТО] 🌐 Запросов к API Bybit: {api_queries}\n")
            _scan_lock.release()

        time.sleep(SCAN_INTERVAL)