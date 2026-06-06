import time
import datetime
import threading
import pandas as pd
import os
import ccxt
from concurrent.futures import ThreadPoolExecutor, as_completed
from modules.cryptano.utils.common import format_price as fmt_p
from modules.cryptano.utils.crypto_utils import calculate_rsi, exchange, get_top_coins
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.regime import detect_market_regime
from modules.cryptano.utils.indicators import get_market_state
from modules.cryptano.utils.storage import load_json
from modules.cryptano.history import save_signal

# ================= Настройки фильтров =================
TIMEFRAME = "4h"
FILTER_VOL_NORMAL = 1.5
FILTER_VOL_ANOMALY = 2.0
FILTER_ZONE_BOTTOM = 25
FILTER_ZONE_TOP = 80
FILTER_RSI_OVERSOLD = 35
SCAN_COINS_LIMIT = 150  # Количество топ-монет по объему для сканирования
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
    time.sleep(0.1)
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

        market_data = get_market_state(df, current_price, channel_lookback=40)
        trend = market_data["trend"]
        trend_code = market_data.get("trend_code", "RANGE")
        pos_pct = market_data["pos_pct"]
        
        # === ДИНАМИЧЕСКАЯ МАТРИЦА НАСТРОЕК ===
        if trend_code == "BULL":
            filter_rsi_oversold = 45
            filter_rsi_overbought = 80
            filter_vol_normal = 1.5
            filter_zone_bottom = 25
            filter_zone_top = 75
            trend = f"{trend} (🟢 Приоритет LONG)"
        elif trend_code == "BEAR":
            filter_rsi_oversold = 25
            filter_rsi_overbought = 65
            filter_vol_normal = 2.0
            filter_zone_bottom = 15
            filter_zone_top = 85
            trend = f"{trend} (🔴 Приоритет SHORT)"
        else:  # RANGE
            filter_rsi_oversold = 35
            filter_rsi_overbought = 65
            filter_vol_normal = 1.3
            filter_zone_bottom = 25
            filter_zone_top = 75
        # =====================================
        
        if filter_zone_bottom < pos_pct < filter_zone_top:
            return None

        vol_ratio = market_data["vol_ratio"]
        ma30 = market_data["ma30"]
        strong_support = market_data["strong_support"]
        strong_resistance = market_data["strong_resistance"]
        nearest_support = market_data["nearest_support"]
        nearest_resistance = market_data["nearest_resistance"]
        
        if vol_ratio < filter_vol_normal:
            return None

        # --- Мягкие условия Радара ---
        is_near_support = (pos_pct <= filter_zone_bottom) and (rsi <= filter_rsi_oversold)
        is_near_res = (pos_pct >= filter_zone_top) and (rsi >= filter_rsi_overbought)
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

def format_light_signal(setup):
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

    # Формируем статус, иконку и комментарий на основе pos
    if pos >= 80:
        strategy_text = "Приоритет — Шорт от сопротивления."
        target_zone = "SHORT ZONE"
        zone_name = "SHORT ZONE"
        icon = "🔴"
        target_value = strong_resistance
        target_pullback = fmt_p(target_value)
        comment_body = f"Искать подтверждение разворота вблизи ~{target_pullback}."
        readiness_pct = pos
    else:
        strategy_text = "Приоритет — Лонг от поддержки."
        target_zone = "LONG ZONE"
        zone_name = "LONG ZONE"
        icon = "🟢"
        target_value = strong_support
        target_pullback = fmt_p(target_value)
        comment_body = f"Искать подтверждение разворота вблизи ~{target_pullback}."
        readiness_pct = 100 - pos

    distance_pct = abs(current_price - target_value) / target_value * 100 if target_value > 0 else 0
    distance_str = f"📍 До {target_zone}: {distance_pct:.0f}%\n\n" if readiness_pct < 90 else "\n"

    msg = (
        f"⚡️ LIGHT SIGNAL | #{coin_name} | {icon}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{price_text}\n"
        f"📊 Объем: x{setup['vol_ratio']:.1f}\n"
        f"🌡 RSI: {setup['rsi']:.1f}\n"
        f"--------------------------------\n"
        f"📊 Тренд: {setup['trend']}\n"
        f"⚡️ СТАТУС: {zone_name} ({readiness_pct:.0f}%)\n\n"
        f"👀 {strategy_text}\n"
        f"{comment_body}\n\n"
        f"❗️ НЕ сигнал на вход"
    )
    return msg

def _execute_scan_cycle(bot, admin_chat_id, is_auto=False):
    prefix = "[ЛАЙТ-РАДАР АВТО]" if is_auto else "[ЛАЙТ-РАДАР РУЧНОЙ]"
    try:
        found_something = False
        
        exchange = ccxt.bybit({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        load_markets_cached(exchange, ttl_seconds=86400)

        coins = get_top_coins(limit=SCAN_COINS_LIMIT)
        start_time = time.time()
        total_processed_coins = len(coins) if coins else 0
        api_queries = total_processed_coins + 1
        now = datetime.datetime.now()

        eligible_symbols = []
        if is_auto:
            # Очистка старых записей из кэша (утечка памяти)
            keys_to_delete = [k for k, v in cooldown_cache.items() if (now - v).total_seconds() >= (COOLDOWN_HOURS * 3600)]
            for k in keys_to_delete:
                del cooldown_cache[k]

            for symbol in coins:
                coin_name = symbol.split("/")[0]
                if coin_name in cooldown_cache:
                    if (now - cooldown_cache[coin_name]).total_seconds() < (COOLDOWN_HOURS * 3600):
                        continue
                eligible_symbols.append(symbol)
        else:
            eligible_symbols = coins

        worker_count = min(MAX_LIGHT_SCAN_WORKERS, max(1, len(eligible_symbols)))
        setups = []

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_light_setup, symbol) for symbol in eligible_symbols]
            for future in as_completed(futures):
                setup = future.result()
                if setup:
                    setup["source"] = "LIGHT"  # Бирка радара
                    
                    # ИСПРАВЛЕННАя ЛОГИКА (Цели и стопы на своих местах)
                    if setup["pos_pct"] >= 80:
                        setup["type"] = "SHORT_PUMP"
                        setup["take_profit"] = setup.get("strong_support", 0) # Для шорта цель ВНИЗУ
                        setup["stop_loss"] = setup.get("strong_resistance", 0) * 1.05 # Стоп за хай
                    else:
                        setup["type"] = "LONG_ROLLBACK"
                        setup["take_profit"] = setup.get("strong_resistance", 0) # Для лонга цель ВВЕРХУ
                        setup["stop_loss"] = setup.get("strong_support", 0) * 0.95 # Стоп за дно
                        
                    setup["price"] = setup.get("current_price", 0)
                    
                    save_signal(setup)         # Сохранение в базу
                    setups.append(setup)

        for setup in setups:
            if not setup:
                continue
            coin_name = setup["coin"]
            found_something = True
            
            msg = format_light_signal(setup)
            bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

            if is_auto:
                cooldown_cache[coin_name] = now
                
        elapsed_time = time.time() - start_time
        
        if not is_auto:
            if not found_something:
                print("[SCANNER LOG] Монет по фильтру Light не найдено.")
                print(f"\n{prefix} 📊 Скан завершен за {elapsed_time:.1f} сек.")
                print(f"{prefix} 🪙 Монет обработано: {total_processed_coins} | Найдено сетапов: {len(setups)}")
                print(f"{prefix} 🌐 Запросов к API Bybit: {api_queries}\n")
                bot.send_message(admin_chat_id, "ℹ️ По легким фильтрам интересных монет сейчас нет. Рынок спокойный.")
            else:
                print("[SCANNER LOG] Ручной сканер Light успешно завершил работу.")
                print(f"\n{prefix} 📊 Скан завершен за {elapsed_time:.1f} сек.")
                print(f"{prefix} 🪙 Монет обработано: {total_processed_coins} | Найдено сетапов: {len(setups)}")
                print(f"{prefix} 🌐 Запросов к API Bybit: {api_queries}\n")
                bot.send_message(admin_chat_id, "✅ Ручной экспресс-скан Light завершен!")
        else:
            print(f"\n{prefix} 📊 Скан завершен за {elapsed_time:.1f} сек.")
            print(f"{prefix} 🪙 Монет обработано: {total_processed_coins} | Найдено сетапов: {len(setups)}")
            print(f"{prefix} 🌐 Запросов к API Bybit: {api_queries}\n")
            
    except Exception as e:
        print(f"[SCANNER ERROR] Ошибка внутри Light-скана: {e}")
        bot.send_message(admin_chat_id, f"❌ Ошибка при сканировании: {e}")

def run_manual_light_scan(bot, admin_chat_id):
    """ОДНОРАЗОВЫЙ ручной запуск легкого сканера по кнопке."""
    print("[SCANNER LOG] Начало ручного экспресс-сканирования Light...")
    
    if not _scan_lock.acquire(blocking=False):
        print("[SCANNER WARNING] Сканер занят фоновым потоком!")
        bot.send_message(admin_chat_id, "⚠️ Сканер сейчас занят фоновым мониторингом. Подожди минуту.")
        return

    try:
        print("[SCANNER LOG] Начинаю перебор монет по единым условиям Light...")
        _execute_scan_cycle(bot, admin_chat_id, is_auto=False)
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
            _execute_scan_cycle(bot, admin_chat_id, is_auto=True)
        finally:
            _scan_lock.release()

        time.sleep(SCAN_INTERVAL)
