import time
from telebot import types
import datetime
import os
import threading
import re
from modules.cryptano.watcher_plan import check_manual_extreme, check_v_bottom
from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.utils.vbottom_manager import VBottomManager
import json

# ================= НАСТРОЙКИ WATCHER =================
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
COOLDOWN_HOURS = 4     # Заморозка повторных сигналов по монете

# 🆕 НОВЫЕ РУБИЛЬНИКИ АВТОМАТИЗАЦИИ
AUTO_ADD_FROM_CRITICAL = True   # Разрешить Critical фильтру самому добавлять монеты
AUTO_REMOVE_AFTER_SIGNAL = True # Удалять монету из Watchlist после успешного сигнала

watcher_cooldown_cache = {}
v_bottom_mgr = VBottomManager()  # Менеджер V-BOTTOM стратегии
_watcher_lock = threading.Lock()

def is_in_watchlist(coin):
    """Проверяет, есть ли монета уже в списке слежения (чтобы не дублировать сообщения)."""
    wl = _load_watchlist()
    return coin in wl

def auto_add_to_watchlist(coin, direction, source="Critical"):
    """Универсальное автодобавление монет из фильтров"""
    # Блокируем ТОЛЬКО если пришло от Critical и его рубильник выключен
    if source == "Critical" and not AUTO_ADD_FROM_CRITICAL:
        return False
        
    wl = _load_watchlist()
    if coin not in wl:
        macro_path = os.path.join(os.path.dirname(__file__), "macro_levels.json")
        macro_levels = load_json(macro_path, default={})
        if coin not in macro_levels:
            from modules.cryptano.swing_hunter import build_levels_for_single_coin
            build_levels_for_single_coin(coin)

        wl[coin] = {
            "direction": direction, 
            "added_at": datetime.datetime.now().isoformat(),
            "source": source
        }
        _save_watchlist(wl)
        return True
    return False

def _load_watchlist():
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        time.sleep(0.5)
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

def _save_watchlist(data):
    save_json_atomic(WATCHLIST_FILE, data)

def extract_watcher_data(report):
    """
    Парсит длинный отчет из watcher_plan и достает только самое важное
    для короткого watcher алерта. Безопасен для новой SFP логики.
    """
    if "ВХОД! УСЛОВИЯ ВЫПОЛНЕНЫ" not in report:
        return None
    
    # Регулярки для вытаскивания данных из нового формата текста
    price_match = re.search(r"Текущая цена:\s*([\d.]+)", report)
    price = price_match.group(1) if price_match else "N/A"
    
    dir_match = re.search(r"WATCHER\s*(SHORT|LONG)", report)
    direction = dir_match.group(1) if dir_match else "N/A"
    
    trigger_match = re.search(r"Триггер:\s*([^\n]+)", report)
    trigger_str = trigger_match.group(1).strip() if trigger_match else "SFP"
    
    sl_match = re.search(r"Стоп-лосс:\s*([\d.]+)", report)
    sl = sl_match.group(1) if sl_match else "N/A"
    
    return {
        "price": price,
        "direction": direction,
        "score": trigger_str,  # Вместо баллов передаем тип триггера (SFP/Пинбар)
        "sl": sl
    }

def manage_watchlist(command, bot, chat_id):
    """
    Обработчик команд из Телеграма: +BTC, +BTC LONG, -BTC
    Возвращает True, если команда была распознана и обработана.
    """
    cmd = command.strip().upper()
    if not (cmd.startswith('+') or cmd.startswith('-')):
        return False

    parts = cmd.split()
    action_coin = parts[0]
    action = action_coin[0]  # '+' или '-'
    coin = action_coin[1:].replace("USDT", "").replace("/", "").strip()
    
    if not coin:
        return False

    direction = "ANY"
    if len(parts) > 1:
        if parts[1] in ["LONG", "SHORT"]:
            direction = parts[1]

    wl = _load_watchlist()

    if action == '+':
        wl[coin] = {
            "direction": direction, 
            "added_at": datetime.datetime.now().isoformat(),
            "source": "Manual"  # 🆕 Отметка ручного добавления
        }
        _save_watchlist(wl)
        
        mode_text = "Любое" if direction == "ANY" else direction
        bot.send_message(
            chat_id, 
            f"✅ *{coin}* добавлена в watchlist.\n"
            f"Режим: *{mode_text}*", 
            parse_mode="Markdown"
        )
        return True
        
    elif action == '-':
        if coin in wl:
            del wl[coin]
            _save_watchlist(wl)
            bot.send_message(chat_id, f"❌ Монета *{coin}* удалена из watchlist.", parse_mode="Markdown")
        else:
            bot.send_message(chat_id, f"⚠️ Монеты *{coin}* нет в списке.", parse_mode="Markdown")
        return True
        
    return False

def show_watchlist(bot, chat_id):
    """Показывает текущий список слежения с группировкой SWING монет"""
    from telebot import types
    wl = _load_watchlist()
    if not wl:
        bot.send_message(chat_id, "📋 Мой watchlist пуст.\nДобавь монеты командой `+BTC`", parse_mode="Markdown")
        return
    
    msg = "📋 Мой watchlist:\n\n"
    swing_hunter_count = 0
    
    # 1. Сначала выводим построчно все монеты, кроме SWING
    for coin, data in wl.items():
        source = data.get('source', 'Manual')
        
        # Если монета от Swing Hunter — просто считаем её и пропускаем поштучный вывод
        if source == "Swing Hunter":
            swing_hunter_count += 1
            continue
            
        # 1. Защита от пустых значений (None)
        safe_source = source if source else "Manual"
        
        # 2. Полный словарь со всеми метками
        source_map = {
            "Manual": "MAN", 
            "Critical": "CRIT", 
            "Light": "LIGHT",
            "Swing Hunter": "SWING"
        }
        
        # 3. Безопасное форматирование
        src_label = source_map.get(safe_source, str(safe_source)).upper()
        
        direction_text = data['direction'].capitalize() if data['direction'] != "ANY" else "ANY"
        msg += f"• {coin} | {direction_text} | {src_label}\n"
    
    # 2. В самом конце выводим ОДНУ строку со счетом для Swing Hunter
    if swing_hunter_count > 0:
        import datetime
        now_str = datetime.datetime.now().strftime("%H:%M , %d.%m")
        msg += f"\n`{now_str}` — добавлено {swing_hunter_count} монет от swinghunter\n"
    
    msg += "\nДля удаления напиши: `-ТИКЕР` (например, `-BTC`)"
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗑 Полная очистка списка", callback_data="clear_entire_watchlist"))
    
    bot.send_message(chat_id, msg, parse_mode="Markdown", reply_markup=markup)

def run_live_scanner(bot, admin_chat_id):
    """
    Главный фоновый цикл Watcher. Проверяет монеты раз в 15 минут.
    """
    print(" 📡 Watcher live scan инициализирован! Синхронизация с 15m свечами включена.")
    
    while True:
        now = datetime.datetime.utcnow()
        mins_past = now.minute

        if mins_past % 15 == 0 and now.second < 35:
            sleep_time = 35 - now.second
        else:
            next_mark = ((mins_past // 15) + 1) * 15
            sleep_time = (next_mark - mins_past) * 60 - now.second + 35

        current_local_time = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{current_local_time}] 📡 [WATCHER] Ждем {int(sleep_time)} сек. (синхронизация с 15m свечой Bybit + 35 сек)...")
        time.sleep(sleep_time)
        
        try:
            wl = _load_watchlist()
            if not wl:
                continue

            now = datetime.datetime.now()

            # Очистка старых записей из кэша (защита от утечки памяти)
            keys_to_delete = [k for k, v in watcher_cooldown_cache.items() if (now - v).total_seconds() >= (COOLDOWN_HOURS * 3600)]
            for k in keys_to_delete:
                del watcher_cooldown_cache[k]

            if not _watcher_lock.acquire(blocking=False):
                time.sleep(30)
                continue

            try:
                # 🆕 Список для монет, которые отработали и подлежат удалению
                coins_to_remove = []

                # 🆕 Безопасная итерация через list(), чтобы словарь не менял размер в процессе
                for coin, data in list(wl.items()):
                    time.sleep(0.3)
                    # Если ANY - проверяем оба направления. Иначе - только заданное.
                    directions_to_check = ["LONG", "SHORT"] if data["direction"] == "ANY" else [data["direction"]]
                    
                    for d in directions_to_check:
                        cache_key = f"{coin}_{d}"
                        
                        if cache_key in watcher_cooldown_cache:
                            continue  # Монета в кулдауне, пропускаем
                        
                        # Вызываем готовую логику с передачей источника
                        is_ready, report = check_manual_extreme(coin, d, source=data.get("source", "Manual"))
                        
                        signal_found = False
                        
                        if report and not report.startswith("❌") and not report.startswith("⚠️"):
                            if is_ready:
                                # SFP Сигнал подтвержден! Пересылаем готовый отчет
                                bot.send_message(admin_chat_id, report, parse_mode="Markdown")
                                signal_found = True
                        
                        # Если SFP не дал сигнал — пытаемся V-BOTTOM (параллельная стратегия)
                        if not signal_found:
                            v_is_ready, v_report, v_levels = check_v_bottom(coin, d, v_bottom_mgr)
                            if v_report and not v_report.startswith("❌") and not v_report.startswith("⚠️"):
                                if v_is_ready:
                                    bot.send_message(admin_chat_id, v_report, parse_mode="Markdown")
                                    signal_found = True
                                    report = v_report  # Для логирования
                        
                        if signal_found:
                            # Замораживаем, чтобы не спамить
                            watcher_cooldown_cache[cache_key] = now
                            
                            # 🆕 Отмечаем монету на удаление, если включена настройка
                            if AUTO_REMOVE_AFTER_SIGNAL:
                                coins_to_remove.append(coin)
                                
                            break  # Если нашли одну сторону, вторую не проверяем

                        # Микро-пауза между запросами разных монет (внутри цикла for)
                        time.sleep(1) 
                
                # 🆕 Авто-удаление отработавших монет
                if coins_to_remove:
                    # Загружаем свежий список, чтобы не затереть ручные добавления за время скана
                    current_wl = _load_watchlist()
                    # Используем set, чтобы избежать дублей (если вдруг монета добавилась дважды)
                    unique_coins_to_remove = set(coins_to_remove)
                    
                    for c in unique_coins_to_remove:
                        if c in current_wl:
                            del current_wl[c]
                            
                    _save_watchlist(current_wl)
                    print(f"[WATCHER] 🗑 Отработавшие монеты удалены из списка: {', '.join(unique_coins_to_remove)}")
                
                # Пульс для консоли
                current_time = datetime.datetime.now().strftime("%H:%M:%S")
                # Выводим длину заново загруженного списка (уже без удаленных)
                print(f"[{current_time}] 📡 [WATCHER] Скан завершен. Монет в прицеле: {len(_load_watchlist())}")
                        
            finally:
                _watcher_lock.release()

        except Exception as e:
            print(f"[WATCHER ERROR] Ошибка в цикле live_scan: {e}")