import time
import datetime
import os
import threading
import re
from modules.cryptano.watcher_plan import check_manual_extreme
from modules.cryptano.utils.storage import load_json, save_json_atomic

# ================= НАСТРОЙКИ WATCHER =================
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
WATCH_INTERVAL = 900  # Интервал проверок: 900 секунд (15 минут)
COOLDOWN_HOURS = 4     # Заморозка повторных сигналов по монете

watcher_cooldown_cache = {}
_watcher_lock = threading.Lock()

def _load_watchlist():
    return load_json(WATCHLIST_FILE, default={})

def _save_watchlist(data):
    save_json_atomic(WATCHLIST_FILE, data)

def extract_watcher_data(report):
    """
    Парсит длинный отчет из watcher_plan и достает только самое важное
    для короткого watcher алерта.
    """
    if "🔥 СИГНАЛ АКТИВЕН" not in report:
        return None
    
    # Регулярки для вытаскивания данных из текста отчета
    price_match = re.search(r"Цена онлайн: `([^`]+)`", report)
    price = price_match.group(1) if price_match else "N/A"
    
    dir_match = re.search(r"Направление: \*([^*]+)\*", report)
    direction = dir_match.group(1) if dir_match else "N/A"
    
    score_match = re.search(r"Набрано: `(\d+ из \d+ баллов)`", report)
    score_str = score_match.group(1) if score_match else "4+ из 5"
    
    sl_match = re.search(r"Стоп-лосс: `([^`]+)`", report)
    sl = sl_match.group(1) if sl_match else "N/A"
    
    return {
        "price": price,
        "direction": direction,
        "score": score_str,
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
    print(" 📡 Watcher live scan инициализирован!")
    
    while True:
        try:
            wl = _load_watchlist()
            if not wl:
                time.sleep(WATCH_INTERVAL)
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
                for coin, data in wl.items():
                    # Если ANY - проверяем оба направления. Иначе - только заданное.
                    directions_to_check = ["LONG", "SHORT"] if data["direction"] == "ANY" else [data["direction"]]
                    
                    for d in directions_to_check:
                        cache_key = f"{coin}_{d}"
                        
                        if cache_key in watcher_cooldown_cache:
                            continue  # Монета в кулдауне, пропускаем
                        
                        # Вызываем готовую логику из trade_plan.py
                        report = check_manual_extreme(coin, d)
                        
                        if not report or report.startswith("❌") or report.startswith("⚠️"):
                            continue
                        
                        watcher_data = extract_watcher_data(report)
                        
                        if watcher_data:
                            # Сигнал подтвержден! Формируем боевой алерт.
                            icon = "🟢" if watcher_data['direction'] == "LONG" else "🔴"
                            msg = (
                                f"📡 **WATCHER ALERT | {coin}** {icon}\n"
                                f"📈 Приоритет: **{watcher_data['direction']}** ({watcher_data['score']})\n\n"
                                f"💰 Текущая цена: `{watcher_data['price']}`\n"
                                f"🛡 Рекомендуемый Стоп: `{watcher_data['sl']}`\n\n"
                                f"⚡️ Условия разворота подтверждены. Точка входа активна!"
                            )
                            bot.send_message(admin_chat_id, msg, parse_mode="Markdown")
                            
                            # Замораживаем, чтобы не спамить
                            watcher_cooldown_cache[cache_key] = now
                            break  # Если нашли Лонг, Шорт уже не проверяем (и наоборот)

                        # Микро-пауза между запросами разных монет, чтобы не злить биржу
                        time.sleep(1) 
                
                # Пульс для консоли
                current_time = datetime.datetime.now().strftime("%H:%M:%S")
                print(f"[{current_time}] 📡 [WATCHER] Скан завершен. Монет в прицеле: {len(wl)}")
                        
            finally:
                _watcher_lock.release()

        except Exception as e:
            print(f"[WATCHER ERROR] Ошибка в цикле live_scan: {e}")
        
        time.sleep(WATCH_INTERVAL)