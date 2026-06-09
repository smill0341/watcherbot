import time
import datetime
import os
import threading
import re
from modules.cryptano.watcher_plan import check_manual_extreme
from modules.cryptano.utils.storage import load_json, save_json_atomic
import json

# ================= НАСТРОЙКИ WATCHER =================
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
WATCH_INTERVAL = 900  # Интервал проверок: 900 секунд (15 минут)
COOLDOWN_HOURS = 4     # Заморозка повторных сигналов по монете

# 🆕 НОВЫЕ РУБИЛЬНИКИ АВТОМАТИЗАЦИИ
AUTO_ADD_FROM_CRITICAL = True   # Разрешить Critical фильтру самому добавлять монеты
AUTO_REMOVE_AFTER_SIGNAL = True # Удалять монету из Watchlist после успешного сигнала

watcher_cooldown_cache = {}
_watcher_lock = threading.Lock()

def auto_add_to_watchlist(coin, direction):
    """Вызывается из critical_filter.py для автоматического добавления монет"""
    if not AUTO_ADD_FROM_CRITICAL:
        return False
        
    wl = _load_watchlist()
    if coin not in wl:
        wl[coin] = {"direction": direction, "added_at": datetime.datetime.now().isoformat()}
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
        wl[coin] = {"direction": direction, "added_at": datetime.datetime.now().isoformat()}
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
    """Показывает текущий список слежения"""
    wl = _load_watchlist()
    if not wl:
        bot.send_message(chat_id, "📋 Мой watchlist пуст.\nДобавь монеты командой `+BTC` или `+ETH SHORT`", parse_mode="Markdown")
        return
    
    msg = "📋 **Мой watchlist:**\n\n"
    for coin, data in wl.items():
        mode_text = "ANY (Лонг и Шорт)" if data['direction'] == "ANY" else data['direction']
        msg += f"• *{coin}* | Режим: `{mode_text}`\n"
    
    msg += "\n_Для удаления напиши: `-ТИКЕР` (например, `-BTC`)_"
    bot.send_message(chat_id, msg, parse_mode="Markdown")

def run_live_scanner(bot, admin_chat_id):
    """
    Главный фоновый цикл Watcher. Проверяет монеты раз в 15 минут.
    """
    print(f" 📡 Watcher live scan инициализирован! Первый запуск через {WATCH_INTERVAL // 60} минут.")
    
    while True:
        # Пауза стоит СНАЧАЛА, поэтому бот не сканирует сразу при запуске
        time.sleep(WATCH_INTERVAL)
        
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
                    # Если ANY - проверяем оба направления. Иначе - только заданное.
                    directions_to_check = ["LONG", "SHORT"] if data["direction"] == "ANY" else [data["direction"]]
                    
                    for d in directions_to_check:
                        cache_key = f"{coin}_{d}"
                        
                        if cache_key in watcher_cooldown_cache:
                            continue  # Монета в кулдауне, пропускаем
                        
                        # Вызываем готовую логику
                        is_ready, report = check_manual_extreme(coin, d)
                        
                        if not report or report.startswith("❌") or report.startswith("⚠️"):
                            continue
                        
                        if is_ready:
                            # Сигнал подтвержден! Пересылаем готовый отчет
                            bot.send_message(admin_chat_id, report, parse_mode="Markdown")
                            
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