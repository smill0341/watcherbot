import threading
import time
import datetime
import os

# Импорт базовых инструментов
from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.utils.common import KNOWN_TICKER_ALIASES
from modules.cryptano.utils.paths import MACRO_LEVELS_FILE, WATCHER_HISTORY_FILE, ACTIVE_WATCHERS_FILE
from modules.cryptano.strategy.bounce_manager import SHORT_MODES
from modules.cryptano.filters.critical_filter import scan_market, format_results
# from modules.cryptano.light_filter import _execute_scan_cycle  # ОТКЛЮЧЕНО: light-фильтр выключен из пайплайна
from modules.cryptano.utils.coin_generators import update_momentum_watchlist
from modules.cryptano.swing_hunter import start_swing_hunter

# Импорт спортивных модулей
from modules.footballnogoal.football import run_football_monitor
from modules.playerpropsbasket.player_props import run_nba_monitor

# level_id всегда имеет вид "{TAG}_{trade_type}_{min}_{max}" (см. _level_id
# в vbottom_manager.py / bounce_parent.py) — TAG однозначно говорит, какая
# это стратегия, независимо от того, есть ли метаданные в текущем
# macro_levels.json. "BC" — BounceParent._level_id (см. bounce_parent.py).
_STRATEGY_BY_TAG = {"VB": "V_BOTTOM", "VGB": "V_GREEN_BOTTOM", "VRT": "V_RED_TOP", "BC": "BOUNCE"}


def _resolve_watcher_meta(level_id, watcher, level_id_meta):
    """
    Надёжно достаёт coin/direction/strategy для вотчера — сначала из
    level_id_meta (свежий скан, самый точный источник), а если там пусто
    (уровень в этот скан не встретился в macro_levels.json — не значит,
    что вотчер "ничей"), достраивает из самого вотчера и из level_id.
    Раньше при пустой meta coin/strategy улетали в None и на дашборде
    превращались в "?", хотя вотчер прекрасно знает, кто он.
    """
    meta = level_id_meta.get(level_id, {})
    coin = meta.get("coin") or getattr(watcher, "coin", None)
    direction = meta.get("direction") or getattr(watcher, "trade_type", None)
    strategy = meta.get("strategy") or _STRATEGY_BY_TAG.get(level_id.split("_", 1)[0])
    return coin, direction, strategy

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
                        from modules.cryptano.live_scan import auto_add_to_watchlist, is_in_watchlist
                        # Отсекаем монеты, которые уже в списке слежения — по ним сообщение не нужно
                        res = [r for r in res if not is_in_watchlist(r["coin"])]

                    if res:
                        msg = format_results(res, "⏰ Авто-находка: Сильный RSI + Аномальный объем!")
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
            # ОТКЛЮЧЕНО: light-фильтр выключен из пайплайна (light_filter.py не удалён,
            # просто больше не вызывается в цикле скана).
            # if elapsed >= 120 and (time.time() - last_light >= 1800 or last_light == 0):
            #     print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [DISPATCHER] ⏱ Каскад: Запуск Light фильтра...")
            #     last_light = time.time()
            #     try:
            #         _execute_scan_cycle(bot, admin_chat_id, is_auto=True)
            #     except Exception as e:
            #         print(f"[DISPATCHER ERROR] Ошибка внутри Light цикла: {e}")

            # =========================================================
            # 3. ОЧЕРЕДЬ: 👀 WATCHER SCAN (Старт на 5-й минуте, далее —
            # синхронизировано с закрытием 15м свечи на бирже + 1 минута
            # запас, а не "каждые 900 сек от старта бота". Раньше скан
            # мог попасть на любой момент внутри ещё не закрытой свечи —
            # вотчер видел один и тот же формирующийся бар по несколько
            # раз с разными high/close, что давало задвоенные/лишние
            # события (NEW_PEAK и т.п.) на одной и той же свече.
            # Теперь: следующий запуск — это ближайшая граница 15м
            # (:00/:15/:30/:45 по UTC, ровно как биржевые свечи) + 60 сек,
            # чтобы биржа гарантированно успела закрыть и отдать свечу.
            # ============================================================
            _WATCHER_QUARTER_SEC = 900
            _WATCHER_CLOSE_BUFFER_SEC = 60
            _now_ts = time.time()
            _next_watcher_boundary = (_now_ts // _WATCHER_QUARTER_SEC) * _WATCHER_QUARTER_SEC + _WATCHER_CLOSE_BUFFER_SEC
            if elapsed >= 300 and _now_ts >= _next_watcher_boundary and (last_watcher < _next_watcher_boundary or last_watcher == 0):
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [DISPATCHER] ⏱ Начало анализа Watcher списка...")
                last_watcher = time.time()
                
                try:
                    from modules.cryptano.live_scan import _load_watchlist, _save_watchlist, watcher_cooldown_cache, _watcher_lock, AUTO_REMOVE_AFTER_SIGNAL, COOLDOWN_HOURS, v_bottom_mgr, bounce_mgr, tracked_origin_levels, tracked_origin_levels_vrt, save_watcher_state
                    from modules.cryptano.watcher_plan import check_v_bottom, check_v_green_bottom, check_v_red_top, check_bounce
                    
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
                                vbottom_signals = 0
                                vbottom_levels_checked = 0
                                vgb_signals = 0
                                vgb_levels_checked = 0
                                vrt_signals = 0
                                vrt_levels_checked = 0
                                bounce_signals = 0
                                bounce_levels_checked = 0
                                active_level_ids = set()  # для clear_dead_watchers в конце скана (VB/VGB/VRT)
                                bc_active_level_ids = set()  # то же самое, но для BOUNCE (шаг 5)
                                level_id_meta = {}  # level_id -> {"coin":..., "direction":...} для экспорта дашборду
                                macro_db = load_json(MACRO_LEVELS_FILE, default={})
                                
                                for coin, data in list(wl.items()):
                                    total_scanned += 1

                                    # --- НОВЫЙ БЛОК: Проверка флага ручного рескана ---
                                    flag_path = os.path.join(os.path.dirname(__file__), "modules", "cryptano", f"rescan_{coin}.flag")
                                    if os.path.exists(flag_path):
                                        print(f"[DISPATCHER] 🔄 Запрошен ручной рескан для {coin}. Сбрасываем память...")
                                        v_bottom_mgr.remove_watchers_by_coin(coin)
                                        try:
                                            os.remove(flag_path)
                                        except:
                                            pass

                                    # Достаем реальный источник (Swing, Momentum, Manual)
                                    source = data.get("source", "Manual") 
                                    
                                    dirs = ["LONG", "SHORT"] if data["direction"] == "ANY" else [data["direction"]]

                                    # Собираем актуальные level_id этой монеты (не зависит от кулдауна —
                                    # даже если сейчас скипнем по кулдауну, уровень всё равно "живой")
                                    coin_macro = macro_db.get(coin) or macro_db.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})
                                    if coin_macro:
                                        if "LONG" in dirs:
                                            for lvl in coin_macro.get("supports", []):
                                                vb_id = f"VB_LONG_{lvl['min']}_{lvl['max']}"
                                                vgb_id = f"VGB_LONG_{lvl['min']}_{lvl['max']}"
                                                active_level_ids.add(vb_id)
                                                active_level_ids.add(vgb_id)
                                                level_id_meta[vb_id] = {"coin": coin, "direction": "LONG", "strategy": "V_BOTTOM"}
                                                level_id_meta[vgb_id] = {"coin": coin, "direction": "LONG", "strategy": "V_GREEN_BOTTOM"}
                                        if "SHORT" in dirs:
                                            for lvl in coin_macro.get("resistances", []):
                                                vrt_id = f"VRT_SHORT_{lvl['min']}_{lvl['max']}"
                                                active_level_ids.add(vrt_id)
                                                level_id_meta[vrt_id] = {"coin": coin, "direction": "SHORT", "strategy": "V_RED_TOP"}
                                        # BOUNCE (шаг 5 очистки) — те же supports/resistances, но
                                        # id строятся ЧЕРЕЗ bounce_mgr._level_id, чтобы формат 1-в-1
                                        # совпадал с тем, что реально кладётся в bounce_mgr._watchers
                                        # (BounceParent._level_id), а не дублировался руками и не разъехался.
                                        if "LONG" in dirs:
                                            for lvl in coin_macro.get("supports", []):
                                                bc_active_level_ids.add(bounce_mgr._level_id(lvl, "LONG"))
                                        if "SHORT" in dirs:
                                            for lvl in coin_macro.get("resistances", []):
                                                base_bc_id = bounce_mgr._level_id(lvl, "SHORT")
                                                for m in SHORT_MODES:
                                                    bc_active_level_ids.add(f"{base_bc_id}__{m}")

                                    # --- 4. BOUNCE стратегия ---
                                    # В отличие от VB/VGB/VRT, BOUNCE не тянет один уровень за раз
                                    # через tracked_levels — process_candle() внутри bounce_mgr сам
                                    # ведёт реестр всех активных зон (дедуп/кладбище/фокус), поэтому
                                    # вызывается ОДИН раз на монету, а не внутри "for d in dirs".
                                    # Сознательно НЕ участвует в watcher_cooldown_cache и
                                    # coins_to_remove/AUTO_REMOVE_AFTER_SIGNAL ниже — свой лимит
                                    # сделок на уровень (MAX_TRADES_PER_LEVEL) уже внутри BounceWatcher,
                                    # а принудительное снятие монеты с watchlist после одного сигнала
                                    # убило бы остальные ещё живые BOUNCE-вотчеры на этой же монете.
                                    if f"{coin}_LONG" not in watcher_cooldown_cache or f"{coin}_SHORT" not in watcher_cooldown_cache:
                                        bc_count, bc_reports, bc_levels = check_bounce(
                                            coin, "LONG" in dirs, "SHORT" in dirs, bounce_mgr
                                        )
                                        bounce_levels_checked += bc_levels
                                        for bc_report in bc_reports:
                                            signals_found += 1
                                            bounce_signals += 1
                                            bot.send_message(admin_chat_id, bc_report, parse_mode="Markdown")

                                    for d in dirs:
                                        if f"{coin}_{d}" in watcher_cooldown_cache: continue
                                        
                                        coin_signal_found = False

                                        # --- 1. V-BOTTOM стратегия ---
                                        # Проверяем НЕЗАВИСИМО от результата SFP — если сработали обе, шлём обе
                                        v_is_ready, v_report, v_levels = check_v_bottom(coin, d, v_bottom_mgr, tracked_origin_levels)
                                        vbottom_levels_checked += v_levels
                                        if v_report and not v_report.startswith("❌") and not v_report.startswith("⚠️") and v_is_ready:
                                            signals_found += 1
                                            vbottom_signals += 1
                                            coin_signal_found = True
                                            bot.send_message(admin_chat_id, v_report, parse_mode="Markdown")

                                        # --- 2. V-GREEN-BOTTOM стратегия (в паре с V-BOTTOM, один менеджер на двоих) ---
                                        # Только LONG — функция сама пропускает SHORT без обращения к бирже
                                        vgb_is_ready, vgb_report, vgb_levels = check_v_green_bottom(coin, d, v_bottom_mgr, tracked_origin_levels)
                                        vgb_levels_checked += vgb_levels
                                        if vgb_report and not vgb_report.startswith("❌") and not vgb_report.startswith("⚠️") and vgb_is_ready:
                                            signals_found += 1
                                            vgb_signals += 1
                                            coin_signal_found = True
                                            bot.send_message(admin_chat_id, vgb_report, parse_mode="Markdown")

                                        # --- 3. V-RED-TOP стратегия ---
                                        # Только SHORT — функция сама пропускает LONG без обращения к бирже.
                                        # Свой персистентный tracked_origin_levels_vrt (см. live_scan.py) —
                                        # один уровень может дать несколько сигналов подряд, поэтому
                                        # untrack происходит только когда вотчер реально завершился.
                                        vrt_is_ready, vrt_report, vrt_levels = check_v_red_top(coin, d, v_bottom_mgr, tracked_origin_levels_vrt)
                                        vrt_levels_checked += vrt_levels
                                        if vrt_report and not vrt_report.startswith("❌") and not vrt_report.startswith("⚠️") and vrt_is_ready:
                                            signals_found += 1
                                            vrt_signals += 1
                                            coin_signal_found = True
                                            bot.send_message(admin_chat_id, vrt_report, parse_mode="Markdown")

                                        if coin_signal_found:
                                            watcher_cooldown_cache[f"{coin}_{d}"] = now_dt
                                            if AUTO_REMOVE_AFTER_SIGNAL: coins_to_remove.append(coin)
                                            break
                                    time.sleep(1.2) # Защитная пауза между монетами
                                
                                if coins_to_remove:
                                    current_wl = _load_watchlist()
                                    for c in set(coins_to_remove):
                                        if c in current_wl: del current_wl[c]
                                    _save_watchlist(current_wl)

                                # Чистим вотчеров мёртвых/отработавших уровней, которых больше нет
                                # в актуальном macro_levels.json — иначе память растёт бесконечно.
                                # clear_dead_watchers теперь ВОЗВРАЩАЕТ удалённых — успеваем забрать
                                # их путь (event_log) в архив, прежде чем объект будет потерян навсегда.
                                before_count = v_bottom_mgr.watcher_count()
                                removed_watchers = v_bottom_mgr.clear_dead_watchers(active_level_ids)
                                cleared_count = before_count - v_bottom_mgr.watcher_count()

                                # То же самое для BOUNCE (шаг 5) — свой отдельный реестр (bounce_mgr),
                                # свой набор актуальных id (bc_active_level_ids, собран выше по тому же
                                # принципу, что VB/VGB/VRT). Возврат archive-словарь тут не забираем —
                                # у BounceWatcher нет history_log/event_log (см. NOTE у экспорта ниже),
                                # архивировать пока просто нечего.
                                bc_before_count = bounce_mgr.watcher_count()
                                bounce_mgr.clear_dead_watchers(bc_active_level_ids)
                                bc_cleared_count = bc_before_count - bounce_mgr.watcher_count()

                                # 📚 Архив последнего умершего/сработавшего вотчера по каждой паре
                                # монета+стратегия (не журнал на века — только последний слепок,
                                # перезаписывается при следующей смерти по этому же ключу).
                                if removed_watchers:
                                    try:
                                        history_db = load_json(WATCHER_HISTORY_FILE, default={})
                                        for level_id, watcher in removed_watchers.items():
                                            coin, direction, strategy = _resolve_watcher_meta(level_id, watcher, level_id_meta)
                                            if not coin or not strategy:
                                                continue  # ни meta, ни сам вотчер не знают, кто это — совсем битый случай
                                            hist_key = f"{coin}_{strategy}"
                                            history_db[hist_key] = {
                                                "coin": coin,
                                                "direction": direction,
                                                "strategy": strategy,
                                                "final_state": getattr(watcher, "state", None),
                                                "history_log": getattr(watcher, "history_log", ""),
                                                "level_min": getattr(watcher, "min", None),
                                                "level_max": getattr(watcher, "max", None),
                                                "events": getattr(watcher, "event_log", []),
                                                "died_at": now_dt.isoformat(),
                                            }
                                        save_json_atomic(WATCHER_HISTORY_FILE, history_db)
                                    except Exception as e:
                                        print(f"⚠️ [DASHBOARD EXPORT] Не удалось сохранить watcher_history.json: {e}")

                                # 📤 Экспорт активных вотчеров для веб-дашборда (только чтение снаружи,
                                # сама торговая логика/состояние это никак не меняет — просто снимок).
                                # NOTE: у BounceWatcher нет history_log/event_log (только
                                # state/coin/min/max/trade_type) — эти два поля в экспорте
                                # всегда будут пустыми для BOUNCE, пока отдельно не заведём
                                # такой лог в bounce_watcher.py. Само появление уровня в
                                # подсветке ("активный", толстая линия) уже работает.
                                try:
                                    export = {}
                                    for level_id, watcher in v_bottom_mgr._watchers.items():
                                        coin, direction, strategy = _resolve_watcher_meta(level_id, watcher, level_id_meta)
                                        export[level_id] = {
                                            "coin": coin,
                                            "direction": direction,
                                            "strategy": strategy,
                                            "state": getattr(watcher, "state", None),
                                            "breach_count": getattr(watcher, "breach_count", 0),
                                            "history_log": getattr(watcher, "history_log", ""),
                                            "level_min": getattr(watcher, "min", None),
                                            "level_max": getattr(watcher, "max", None),
                                            "events": getattr(watcher, "event_log", []),
                                            "updated_at": now_dt.isoformat(),
                                        }
                                    # BOUNCE — тот же самый export-словарь, тот же файл (дашборд читает
                                    # один active_watchers.json на все стратегии). coin/strategy тут
                                    # достаются из _resolve_watcher_meta через сам вотчер (watcher.coin,
                                    # watcher.trade_type), т.к. level_id_meta для BC не заполняется —
                                    # префикс "BC_" в level_id и так однозначно резолвится в _STRATEGY_BY_TAG.
                                    for level_id, watcher in bounce_mgr._watchers.items():
                                        coin, direction, strategy = _resolve_watcher_meta(level_id, watcher, level_id_meta)
                                        export[level_id] = {
                                            "coin": coin,
                                            "direction": direction,
                                            "strategy": strategy,
                                            "mode": getattr(watcher, "mode", None),
                                            "state": getattr(watcher, "state", None),
                                            "breach_count": getattr(watcher, "pierce_count", 0),
                                            "history_log": getattr(watcher, "history_log", ""),
                                            "level_min": getattr(watcher, "min", None),
                                            "level_max": getattr(watcher, "max", None),
                                            "events": getattr(watcher, "event_log", []),
                                            "updated_at": now_dt.isoformat(),
                                        }
                                    save_json_atomic(ACTIVE_WATCHERS_FILE, export)

                                    # Персистентность: сохраняем реальное состояние вотчеров
                                    # (не только event_log для дашборда, а весь прогресс паттерна)
                                    # + оба tracked-словаря + BOUNCE (вотчеры/graveyard/pierced_count),
                                    # чтобы рестарт бота не обнулял прогресс ни у одной стратегии.
                                    save_watcher_state()
                                except Exception as e:
                                    print(f"⚠️ [DASHBOARD EXPORT] Не удалось сохранить active_watchers.json: {e}")
                                    
                                # 🚀 ФИНАЛЬНЫЙ ПРИНТ СО СТАТИСТИКОЙ (общий + отдельно по каждой стратегии)
                                print(f"✅ [DISPATCHER] Анализ прошел. Монет просканировано: {total_scanned} | Сигналов найдено: {signals_found}")
                                print(f"   -> V_BOTTOM: Уровней оценено: {vbottom_levels_checked} | Сделок найдено: {vbottom_signals}")
                                print(f"   -> V_GREEN_BOTTOM: Уровней оценено: {vgb_levels_checked} | Сделок найдено: {vgb_signals}")
                                print(f"   -> V_RED_TOP: Уровней оценено: {vrt_levels_checked} | Сделок найдено: {vrt_signals}")
                                print(f"   -> BOUNCE: Уровней оценено: {bounce_levels_checked} | Сделок найдено: {bounce_signals}")
                                print(f"   -> Очистка VB/VGB/VRT: удалено {cleared_count}, осталось в памяти {v_bottom_mgr.watcher_count()}")
                                print(f"   -> Очистка BOUNCE: удалено {bc_cleared_count}, осталось в памяти {bounce_mgr.watcher_count()}")
                                
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