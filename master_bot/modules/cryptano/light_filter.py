import time
import datetime
import threading
import pandas as pd
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from modules.cryptano.utils.common import format_price as fmt_p, calculate_rsi
from modules.cryptano.utils.crypto_utils import exchange, get_top_coins
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.regime import detect_market_regime
from modules.cryptano.utils.indicators import get_market_state
from modules.cryptano.utils.storage import load_json
from modules.cryptano.history import save_signal

# ================= Настройки фильтров ================= 
TIMEFRAME = "4h"
SCAN_COINS_LIMIT = 150    # Количество топ-монет по объему для сканирования
COOLDOWN_HOURS = 4         # Не спамить одной монетой 4 часа после сигнала
SCAN_INTERVAL = 1800        # Запуск сканирования каждые 30 минут (1800 сек)
MAX_LIGHT_SCAN_WORKERS = 3
AUTO_ADD_TO_WATCHER = True  # Разрешить легкому фильтру самому добавлять монеты в Watchlist после нахождения сигнала

cooldown_cache = {}
_scan_lock = threading.Lock()

def _light_setup(symbol):
    """
    Единая функция анализа (Радар) с умной матрицей и возвратом причин отбраковки.
    """
    time.sleep(0.5)
    try:
        coin_name = symbol.split("/")[0]
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=250)
        if len(ohlcv) < 35:
            return 'reject_data' # Не хватает данных свечей

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["rsi"] = calculate_rsi(df)

        last_row = df.iloc[-1]
        current_price = float(last_row["close"])
        rsi = float(last_row["rsi"])

        market_data = get_market_state(df, current_price, channel_lookback=40)
        trend_text = market_data["trend"]
        trend_code = market_data.get("trend_code", "RANGE")
        pos_pct = market_data["pos_pct"]
        vol_ratio = market_data["vol_ratio"]
        
        nearest_support = market_data["nearest_support"]
        nearest_resistance = market_data["nearest_resistance"]
        strong_support = market_data["strong_support"]
        strong_resistance = market_data["strong_resistance"]
        ma30 = market_data["ma30"]

        # 1. Определяем направление по каналу
        if pos_pct > 50:
            # Цена в верхней части (у потолка). Ищем шорт от сопротивления.
            scan_direction = "SHORT"
            # 🛠 ИСПРАВЛЕНО: Убрано дублирование if current_price > 0
            distance_to_level = ((nearest_resistance - current_price) / current_price) * 100 if current_price > 0 else 999
        else:
            # Цена в нижней части (у дна). Ищем лонг от поддержки.
            scan_direction = "LONG"
            # 🛠 ИСПРАВЛЕНО: Убрано дублирование if current_price > 0
            distance_to_level = ((current_price - nearest_support) / current_price) * 100 if current_price > 0 else 999

        # 2. Адаптивная матрица порогов под текущий тренд
        if trend_code == "BULL":
            if scan_direction == "LONG":
                req_rsi_max, req_rsi_min = 45, 0
                req_vol = 0.6
                req_dist = 5.0
                risk_tag = "✅ ПРИОРИТЕТ (По тренду)"
            else:
                req_rsi_max, req_rsi_min = 100, 80
                req_vol = 1.8
                req_dist = 4.0
                risk_tag = "⚠️ КОНТРТРЕНД (Шорт на сильном рынке)"
                
        elif trend_code == "BEAR":
            if scan_direction == "SHORT":
                req_rsi_max, req_rsi_min = 100, 55
                req_vol = 0.6
                req_dist = 5.0
                risk_tag = "✅ ПРИОРИТЕТ (По тренду)"
            else:
                req_rsi_max, req_rsi_min = 25, 0
                req_vol = 1.8
                req_dist = 4.0
                risk_tag = "⚠️ КОНТРТРЕНД (Ловим отскок / пролив)"
                
        else:  # RANGE / FLAT
            risk_tag = "🟡 БОКОВИК (Работа от границ)"
            req_vol = 0.5
            req_dist = 5.0
            if scan_direction == "LONG":
                req_rsi_max, req_rsi_min = 40, 0
            else:
                req_rsi_max, req_rsi_min = 100, 60

        # 3. Фильтрация с возвратом ТОЧНОЙ причины (для сводного лога)
        if vol_ratio < req_vol:
            return 'reject_volume'
            
        if rsi > req_rsi_max or rsi < req_rsi_min:
            return 'reject_rsi'
            
        if distance_to_level > req_dist:
            return 'reject_distance'

        # Если дошли сюда, сигнал идеален
        if scan_direction == "LONG":
            setup_info = f"Near Support (Bottom {pos_pct:.0f}%)"
        else:
            setup_info = f"Near Resistance (Top {pos_pct:.0f}%)"

        market_regime = detect_market_regime(current_price, rsi, vol_ratio, ma30)

        return {
            "coin": coin_name,
            "trend": trend_text,
            "trend_code": trend_code,
            "scan_direction": scan_direction,
            "distance_to_level": distance_to_level,
            "risk_tag": risk_tag,
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
        return 'reject_error'

def format_light_signal(setup):
    coin_name = setup["coin"]
    current_price = setup.get('current_price', 0)
    strong_resistance = setup.get('strong_resistance', 0)
    strong_support = setup.get('strong_support', 0)
    nearest_support = setup.get('nearest_support', strong_support)
    nearest_resistance = setup.get('nearest_resistance', strong_resistance)
    pos = setup['pos_pct']
    
    scan_direction = setup.get("scan_direction", "LONG" if pos > 50 else "SHORT")
    
    # Чистим название тренда
    trend_clean = setup['trend'].replace("Глобальный ", "").replace("Глобальная ", "")

    pump_pct = ((current_price - strong_support) / strong_support) * 100 if strong_support > 0 else 0
    dump_pct = ((strong_resistance - current_price) / strong_resistance) * 100 if strong_resistance > 0 else 0

    # Формируем логику строк Приоритет/Риск
    if scan_direction == "SHORT":
        icon = "🔴"
        zone_name = "ШОРТ-ЗОНА"
        price_context = f"🔻 -{dump_pct:.0f}% от хая"
        action_main = f"👀 Приоритет: Шорт от сопротивления ~{fmt_p(nearest_resistance)}"
        action_risk = f"⚖️ Риск: Лонг от поддержки ~{fmt_p(nearest_support)}"
    else:
        icon = "🟢"
        zone_name = "ЛОНГ-ЗОНА"
        price_context = f"📈 +{pump_pct:.0f}% от дна"
        action_main = f"👀 Приоритет: Лонг от поддержки ~{fmt_p(nearest_support)}"
        action_risk = f"⚖️ Риск: Шорт от сопротивления ~{fmt_p(nearest_resistance)}"

    msg = (
        f"⚡️ LIGHT FILTR | #{coin_name} {icon}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Цена: {fmt_p(current_price)} ({price_context})\n"
        f"📊 Объем: x{setup['vol_ratio']:.1f}  |  🌡 RSI: {setup['rsi']:.1f}\n"
        f"--------------------------------\n"
        f"{trend_clean} | {icon} {zone_name}\n\n"
        f"{action_main}\n"
        f"{action_risk}\n\n"
        f"❗️ НЕ сигнал на вход"
    )
    return msg

def _execute_scan_cycle(bot, admin_chat_id, is_auto=False):
    prefix = "[LIGHT FILTER AUTO]" if is_auto else "[LIGHT FILTER MANUAL]"
    try:
        found_something = False
        
        load_markets_cached(exchange, ttl_seconds=86400)

        coins = get_top_coins(limit=SCAN_COINS_LIMIT)
        start_time = time.time()
        total_processed_coins = len(coins) if coins else 0
        api_queries = total_processed_coins + 1
        now = datetime.datetime.now()

        eligible_symbols = []
        if is_auto:
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
        
        reject_stats = {'volume': 0, 'rsi': 0, 'distance': 0, 'data': 0, 'error': 0}

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_light_setup, symbol) for symbol in eligible_symbols]
            for future in as_completed(futures):
                setup = future.result()
                
                if not isinstance(setup, dict):
                    if isinstance(setup, str) and setup.startswith('reject_'):
                        reason = setup.split('_')[1]
                        if reason in reject_stats:
                            reject_stats[reason] += 1
                    else:
                        reject_stats['error'] += 1
                    continue

                setup["source"] = "LIGHT"
                
                if setup["pos_pct"] > 50:
                    setup["type"] = "SHORT_PUMP"
                    setup["take_profit"] = setup.get("strong_support", 0)
                    setup["stop_loss"] = setup.get("strong_resistance", 0) * 1.05
                else:
                    setup["type"] = "LONG_ROLLBACK"
                    setup["take_profit"] = setup.get("strong_resistance", 0)
                    setup["stop_loss"] = setup.get("strong_support", 0) * 0.95
                    
                setup["price"] = setup.get("current_price", 0)
                
                save_signal(setup)
                setups.append(setup)

        # === ЕДИНСТВЕННЫЙ ЦИКЛ ОТПРАВКИ И ДОБАВЛЕНИЯ ===
        sent_to_watchlist_count = 0

        for setup in setups:
            coin_name = setup["coin"]
            found_something = True
            
            msg = format_light_signal(setup)
            bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

            if is_auto:
                cooldown_cache[coin_name] = now
                
                if AUTO_ADD_TO_WATCHER:
                    try:
                        from modules.cryptano.live_scan import auto_add_to_watchlist
                        w_dir = "SHORT" if setup["type"] == "SHORT_PUMP" else "LONG"
                        
                        if auto_add_to_watchlist(coin_name, w_dir, source="Light"):
                            sent_to_watchlist_count += 1
                    except Exception as e:
                        print(f"[LIGHT] Ошибка передачи в Watcher: {e}")

        elapsed_time = time.time() - start_time
        
        stats_str = f"🚫 Отбраковано -> Объем: {reject_stats['volume']} | RSI: {reject_stats['rsi']} | Дистанция: {reject_stats['distance']} | Нехватка данных: {reject_stats['data']}"
        
        if is_auto:
            print(f"\n{prefix} 📊 Скан завершен за {elapsed_time:.1f} сек.")
            print(f"{prefix} 🪙 Монет обработано: {total_processed_coins} | Найдено сетапов: {len(setups)}")
            print(f"{prefix} 📥 Отправлено в Watchlist: {sent_to_watchlist_count} шт.")
            print(f"{prefix} {stats_str}")
            print(f"{prefix} 🌐 Запросов к API Bybit: {api_queries}\n")
        else:
            if not found_something:
                print("[SCANNER LOG] Монет по фильтру Light не найдено.")
                print(f"\n{prefix} 📊 Скан завершен за {elapsed_time:.1f} сек.")
                print(f"{prefix} 🪙 Монет обработано: {total_processed_coins} | Найдено сетапов: {len(setups)}")
                print(f"{prefix} {stats_str}")
                print(f"{prefix} 🌐 Запросов к API Bybit: {api_queries}\n")
                bot.send_message(admin_chat_id, "ℹ️ По легким фильтрам интересных монет сейчас нет. Рынок спокойный.")
            else:
                print("[SCANNER LOG] Ручной сканер Light успешно завершил работу.")
                print(f"\n{prefix} 📊 Скан завершен за {elapsed_time:.1f} сек.")
                print(f"{prefix} 🪙 Монет обработано: {total_processed_coins} | Найдено сетапов: {len(setups)}")
                print(f"{prefix} {stats_str}")
                print(f"{prefix} 🌐 Запросов к API Bybit: {api_queries}\n")
                bot.send_message(admin_chat_id, "✅ Ручной экспресс-скан Light завершен!")
            
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