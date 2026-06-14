import threading
import time
import datetime
import os

# Импорт базовых инструментов
from modules.cryptano.utils.storage import load_json
from modules.cryptano.critical_filter import scan_market, format_results
from modules.cryptano.light_filter import _execute_scan_cycle
from modules.cryptano.utils.coin_generators import update_momentum_watchlist
from modules.cryptano.swing_hunter import start_swing_hunter

# Импорт спортивных модулей
from modules.footballnogoal.football import run_football_monitor
from modules.playerpropsbasket.player_props import run_nba_monitor

def crypto_orchestrator(bot, admin_chat_id):
    """
    Единый каскадный Диспетчер автоматики Крипты.
    Управляет очередью, задержками и строго следит за кнопками ON/OFF.
    """
    print("🪙  Watcher Crypto инициализирован!")
    
    # Храним таймштампы последнего успешного запуска фильтров (в секундах)
    last_critical = 0
    last_light = 0
    last_watcher = 0
    last_generator = 0
    
    # Переменные контроля текущей сессии запуска
    cascade_initialized = False
    session_start_time = 0
    
    # Путь к файлу конфигурации в корне
    config_path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))

    while True:
        time.sleep(1) # Проверка пульса каждую секунду без нагрузки на процессор
        
        try:
            config = load_json(config_path, default={})
            status = config.get("crypto", {}).get("status", "STOPPED")
            fasttrade_on = config.get("crypto", {}).get("fasttrade", False)
            
            # 📴 РЕЖИМ STOPPED: Если автобот выключен — глушим всю автоматику
            if status != "RUNNING":
                if cascade_initialized:
                    print("[DISPATCHER] 💤 Автобот переведен в STOPPED. Диспетчер уходит в режим ожидания.")
                    cascade_initialized = False
                continue
                
            # 🟢 ТОЧКА ЗАПУСКА: Если статус RUNNING, но сессия еще не зафиксирована
            if not cascade_initialized:
                print("[DISPATCHER] 🚀 Обнаружен запуск Автобота! Включаю каскадный отсчет времени...")
                session_start_time = time.time()
                cascade_initialized = True
                
                # Сбрасываем таймеры в ноль, чтобы принудительно прогнать стартовую каскадную очередь
                last_critical = 0
                last_light = 0
                last_watcher = 0
                last_generator = 0
            
            # Считаем, сколько секунд прошло с момента нажатия кнопки "Старт"
            elapsed = time.time() - session_start_time
            
            # =========================================================
            # 1. ОЧЕРЕДЬ: 🚀 CRITICAL (Старт на 1-й минуте, далее каждый 1 час)
            # =========================================================
            if elapsed >= 60 and (time.time() - last_critical >= 3600 or last_critical == 0):
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [DISPATCHER] ⏱ Каскад: Запуск Critical фильтра...")
                last_critical = time.time()
                
                # Выполняем изолированный цикл Критикала
                try:
                    res = scan_market(scan_type="auto")
                    if res:
                        msg = format_results(res, "⏰ Авто-находка: Сильный RSI + Аномальный объем!")
                        from modules.cryptano.live_scan import auto_add_to_watchlist 
                        added_coins = []
                        for r in res:
                            coin = r["coin"]
                            direction = "SHORT" if r["type"] == "SHORT_PUMP" else "LONG"
                            if auto_add_to_watchlist(coin, direction):
                                added_coins.append(coin)
                        if added_coins:
                            msg += f"\n🤖 *Переданы в Watcher:* {', '.join(added_coins)}"
                        bot.send_message(admin_chat_id, msg, parse_mode="Markdown")
                except Exception as e:
                    print(f"[DISPATCHER ERROR] Ошибка внутри Critical цикла: {e}")

            # =========================================================
            # 2. ОЧЕРЕДЬ: 💎 LIGHT (Старт на 2-й минуте, далее каждые 30 минут)
            # =========================================================
            if elapsed >= 120 and (time.time() - last_light >= 1800 or last_light == 0):
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [DISPATCHER] ⏱ Каскад: Запуск Light фильтра...")
                last_light = time.time()
                try:
                    _execute_scan_cycle(bot, admin_chat_id, is_auto=True)
                except Exception as e:
                    print(f"[DISPATCHER ERROR] Ошибка внутри Light цикла: {e}")

            # =========================================================
            # 3. ОЧЕРЕДЬ: 👀 WATCHER SCAN (Старт на 5-й минуте, далее каждые 15 минут)
            # =========================================================
            if elapsed >= 300 and (time.time() - last_watcher >= 900 or last_watcher == 0):
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [DISPATCHER] ⏱ Каскад: Запуск сканирования Watcher списка...")
                last_watcher = time.time()
                
                try:
                    from modules.cryptano.live_scan import _load_watchlist, _save_watchlist, watcher_cooldown_cache, _watcher_lock, AUTO_REMOVE_AFTER_SIGNAL, COOLDOWN_HOURS
                    from modules.cryptano.watcher_plan import check_manual_extreme
                    
                    wl = _load_watchlist()
                    if wl:
                        now_dt = datetime.datetime.now()
                        keys_to_del = [k for k, v in watcher_cooldown_cache.items() if (now_dt - v).total_seconds() >= (COOLDOWN_HOURS * 3600)]
                        for k in keys_to_del: del watcher_cooldown_cache[k]

                        if _watcher_lock.acquire(blocking=False):
                            try:
                                coins_to_remove = []
                                total_scanned = 0
                                signals_found = 0
                                
                                for coin, data in list(wl.items()):
                                    total_scanned += 1
                                    # Достаем реальный источник (Swing, Momentum, Manual)
                                    source = data.get("source", "Manual") 
                                    
                                    # ПЕЧАТАЕМ КАЖДЫЙ ШАГ, ЧТОБЫ ВИДЕТЬ, ЧТО БОТ НЕ ВИСНЕТ
                                    print(f"   -> [WATCHER] Анализирую монету: {coin} ({total_scanned}/{len(wl)})...")
                                    
                                    dirs = ["LONG", "SHORT"] if data["direction"] == "ANY" else [data["direction"]]
                                    for d in dirs:
                                        if f"{coin}_{d}" in watcher_cooldown_cache: continue
                                        
                                        # Передаем source в функцию
                                        is_ready, report = check_manual_extreme(coin, d, source)
                                        if not report or report.startswith("❌") or report.startswith("⚠️"): continue
                                        
                                        if is_ready:
                                            signals_found += 1
                                            bot.send_message(admin_chat_id, report, parse_mode="Markdown")
                                            watcher_cooldown_cache[f"{coin}_{d}"] = now_dt
                                            if AUTO_REMOVE_AFTER_SIGNAL: coins_to_remove.append(coin)
                                            break
                                    time.sleep(1.2) # Защитная пауза между монетами
                                
                                if coins_to_remove:
                                    current_wl = _load_watchlist()
                                    for c in set(coins_to_remove):
                                        if c in current_wl: del current_wl[c]
                                    _save_watchlist(current_wl)
                                    
                                # 🚀 ФИНАЛЬНЫЙ ПРИНТ СО СТАТИСТИКОЙ
                                print(f"✅ [DISPATCHER] Скан завершен. Просканировано: {total_scanned} | Найдено точек входа: {signals_found}")
                                
                            finally:
                                _watcher_lock.release()
                    else:
                        print("✅ [DISPATCHER] Watchlist пуст. Скан отменен.")
                        
                except Exception as e:
                    print(f"[DISPATCHER ERROR] Ошибка внутри Watcher цикла: {e}")
            # =========================================================
            # 4. ОЧЕРЕДЬ: 🚀 COIN GENERATOR / FASTTRADE (Скальпинг)
            # =========================================================
            if fasttrade_on:
                if last_generator == 0:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [DISPATCHER] ⚡️ FastTrade включен! Первый скан начнется через 60 секунд...")
                    last_generator = time.time() - 7200 + 60 
                
                elif time.time() - last_generator >= 7200:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [DISPATCHER] ⏱ Каскад: Запуск генератора ТОП-20 монет...")
                    try:
                        update_momentum_watchlist(bot, admin_chat_id)
                    except Exception as e:
                        print(f"[DISPATCHER ERROR] Ошибка генератора: {e}")
                    
                    last_generator = time.time()
            else:
                last_generator = 0  # <--- Просто тихо держим на нуле, БЕЗ print

        except Exception as e:
            print(f"[CRITICAL DISPATCHER ERROR] Сбой главного диспетчера задач: {e}")

def start_all_background_tasks(bot, admin_chat_id):
    """
    Запускает спортивные мониторы и наш единый Каскадный Диспетчер крипты.
    """
    # 🪙 Запуск Единого Диспетчера Крипты в один поток
    threading.Thread(target=crypto_orchestrator, args=(bot, admin_chat_id), daemon=True).start()
    
    # 🌪 Запуск Свинг Хантера (Генератор уровней + Минутный дозор)
    start_swing_hunter(bot, admin_chat_id)
    
    # ⚽️🏀 Спортивные мониторы (остались без изменений)
    threading.Thread(target=run_football_monitor, args=(bot, admin_chat_id), daemon=True).start()
    threading.Thread(target=run_nba_monitor, args=(bot, admin_chat_id), daemon=True).start()