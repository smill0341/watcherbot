import os
import telebot
import logging
from dotenv import load_dotenv
import keyboards
from telebot import types
from background_tasks import start_all_background_tasks
from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.critical_filter import process_crypto_command
from modules.footballnogoal.football import check_live_matches
from modules.playerpropsbasket.player_props import check_nba_injuries
from modules.cryptano.history import check_and_update, format_history
from modules.cryptano.market_overview import analyze_coin
from modules.cryptano.watcher_plan import check_manual_extreme
from modules.cryptano.light_filter import run_manual_light_scan
from modules.cryptano.live_scan import manage_watchlist, show_watchlist, run_live_scanner
from modules.cryptano.history import check_and_update, format_history, generate_report_file
import threading
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CONFIG_FILE = "config.json"

bot = telebot.TeleBot(TOKEN)

def load_config():
    return load_json(CONFIG_FILE, default={})

def save_config(config):
    save_json_atomic(CONFIG_FILE, config, indent=4)

@bot.message_handler(commands=['start'])
def start(message):
    if str(message.chat.id) == str(ADMIN_CHAT_ID):
        bot.send_message(message.chat.id, "Главное меню:", reply_markup=keyboards.get_main_menu())

@bot.message_handler(content_types=['text'])
def handle_text(message):
    if str(message.chat.id) != str(ADMIN_CHAT_ID): return

    text = message.text
    chat_id = message.chat.id

    # Перехват команд Снайпера (+BTC, -BTC)
    if manage_watchlist(text, bot, chat_id):
        return
        
    if text in ["🎯 Мой Watchlist", "Watchlist"]:
        show_watchlist(bot, chat_id)
        return

    # --- ГЛАВНОЕ МЕНЮ И СПОРТ ---
    if message.text == "🪙 Крипта":
        bot.send_message(message.chat.id, "🪙 Крипто-сканер:", reply_markup=keyboards.get_crypto_menu())

    elif message.text == "🪙 Critical фильтр":
        bot.send_message(message.chat.id, "🪙 Крипто-сканер:", reply_markup=keyboards.get_crypto_menu())

    elif message.text == "⬅️ Главное меню":
        bot.send_message(message.chat.id, "Главное меню:", reply_markup=keyboards.get_main_menu())

    elif message.text in ["🏀 NBA", "⚽️ Футбол"]:
        sport = "nba" if "NBA" in message.text else "football"
        config = load_config()
        bot.send_message(message.chat.id, f"{message.text}: {config[sport]['status']}",
                         reply_markup=keyboards.get_sport_menu(sport))

    # --- УПРАВЛЕНИЕ АВТОБОТОМ ---
    elif message.text == "🤖 Автобот Старт":
        config = load_config()
        config["crypto"]["status"] = "RUNNING"
        save_config(config)
        bot.send_message(message.chat.id, "✅ Автобот запущен.")

    elif message.text == "📴 Автобот Стоп":
        config = load_config()
        config["crypto"]["status"] = "STOPPED"
        save_config(config)
        bot.send_message(message.chat.id, "⏹ Автобот остановлен.")
    
    # --- РУЧНЫЕ АНАЛИЗАТОРЫ (Normal и Pump/Dump) ---
    elif message.text == "📈 Анализ Normal":
        msg = bot.send_message(message.chat.id, "Введи чистый тикер монеты для стандартного анализа (например: BTC или SOL):")
        bot.register_next_step_handler(msg, process_normal_analysis)

    elif message.text == "🔍 Watcher Plan":
        msg = bot.send_message(message.chat.id, 
            "Введи монету и направление.\nФормат: `BTC LONG или SHORT`", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_manual_extreme_check)  
        
    # --- ОСНОВНЫЕ СКАНЕРЫ ---
    elif message.text == "⚡️ Critical фильтр":
        print("[MAIN LOG] Нажата кнопка ⚡️ Critical фильтр. Передаю в cryptano.py")
        process_crypto_command(message.text, bot, message.chat.id)

    elif message.text == "💎 Light 👑 фильтр" or message.text == "💎 Light фильтр":
        print("[MAIN LOG] Нажата кнопка 💎 Light фильтр. Вызываю scanner.py")
        bot.send_message(message.chat.id, "⏳ Запускаю легкий поиск уровней (Light)...")
        try:
            run_manual_light_scan(bot, message.chat.id)
            print("[MAIN LOG] Light сканер успешно отработал.")
        except Exception as e:
            print(f"[MAIN ERROR] Ошибка в Light сканере: {e}")
            bot.send_message(message.chat.id, f"❌ Ошибка Light сканера: {e}")

    # --- ПОДМЕНЮ "ДРУГИЕ ФИЛЬТРЫ" И ЕГО КНОПКИ ---
    elif message.text == "🛠 Другие фильтры":
        print("[LOG] Нажата кнопка: 🛠 Другие фильтры")
        bot.send_message(
            message.chat.id, 
            "Выбери интересующий экспресс-фильтр рынка:", 
            reply_markup=keyboards.get_other_filters_menu()
        )

    elif message.text == "⬅️ Назад в меню Крипты":
        bot.send_message(
            message.chat.id, 
            "Возвращаю в главное управление криптой.", 
            reply_markup=keyboards.get_crypto_menu()
        )    

    # Исправленный список 
    elif message.text in ["🔥 RSI > 70", "🥶 RSI < 30", "💰 Volume > x2"]:
        process_crypto_command(message.text, bot, message.chat.id)

    # --- РЕЗУЛЬТАТЫ ---
    elif message.text == "📊 Результаты":
        check_and_update(bot, message.chat.id)
        bot.send_message(message.chat.id, "Выбери период:", reply_markup=keyboards.get_history_keyboard())  


def process_normal_analysis(message):
    # 1. ЗАЩИТА ОТ "ЗАЛИПАНИЯ": Позволяем выйти в меню, если передумали
    if message.text in ["⬅️ Главное меню", "🪙 Крипта", "🛠 Другие фильтры"]:
        handle_text(message) # Возвращаем в обработчик главного меню
        return

    try:
        coin = message.text.strip().upper()
        bot.send_message(message.chat.id, f"⏳ Запрашиваю данные Bybit и строю уровни для {coin}...")
        
        # Получаем отчет из анализатора
        report = analyze_coin(coin) 
        
        # 2. ЗАЦИКЛИВАНИЕ ПРИ ОШИБКЕ ВВОДА: Если монета не найдена на бирже
        if report and str(report).startswith("❌"):
            msg = bot.send_message(message.chat.id, f"{report}\n\nПопробуй ввести тикер еще раз (например: BTC):")
            bot.register_next_step_handler(msg, process_normal_analysis) # Зацикливаем ожидание
            return
        
        # --- АВТОМАТИЧЕСКИЙ ПЕРЕХОД В PUMP/DUMP ---
        if report and str(report).startswith("AUTO_PUMPDUMP:"):
            parts = str(report).split(":")
            coin_name = parts[1]
            reason = parts[2]
            
            warning_msg = (
                f"❌ *СТАНДАРТНЫЙ АНАЛИЗ ОТМЕНЕН*\n"
                f"🪙 Монета: `#{coin_name}`\n"
                f"⚠️ Причина: Обнаружен *{reason}*\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🤖 *Автоматически запускаю алгоритм Pump/Dump...*"
            )
            bot.send_message(message.chat.id, warning_msg, parse_mode="Markdown")
            
            # Если это памп (рост) — значит, ищем точку для шорта (SHORT).
            # Если дамп (падение) — ищем точку для лонга (LONG).
            direction = "SHORT" if "ПАМП" in reason else "LONG"
            
            # Запускаем функцию экстремального анализа напрямую!
            extreme_report = check_manual_extreme(coin_name, direction)
            bot.send_message(message.chat.id, extreme_report, parse_mode="Markdown")
            return
        # ------------------------------------------

        # Если монета нормальная — просто отправляем обычный отчет
        bot.send_message(message.chat.id, report, parse_mode="Markdown")
        
    except Exception as e:
        # 3. ЗАЦИКЛИВАНИЕ ПРИ СИСТЕМНОЙ ОШИБКЕ
        msg = bot.send_message(message.chat.id, f"❌ Ошибка анализа: {e}\n\nПопробуй ввести тикер еще раз:")
        bot.register_next_step_handler(msg, process_normal_analysis) # Зацикливаем ожидание

def process_manual_extreme_check(message):
    # Позволяем выйти из режима ввода, если передумали и нажали кнопку меню
    if message.text in ["⬅️ Главное меню", "🪙 Крипта", "🛠 Другие фильтры"]:
        handle_text(message)
        return
        
    try:
        parts = message.text.strip().upper().split()
        if len(parts) != 2 or parts[1] not in ["LONG", "SHORT"]:
            msg = bot.send_message(message.chat.id, "❌ Неверный формат. Нужно: BTC LONG или SHORT\n\nПопробуй ввести еще раз (или нажми кнопку меню для выхода):")
            bot.register_next_step_handler(msg, process_manual_extreme_check) # Зацикливаем
            return
            
        coin = parts[0]
        direction = parts[1]
        
        bot.send_message(message.chat.id, f"⏳ Ожидайте, делаю анализ для {coin}...")
        
        _, report = check_manual_extreme(coin, direction)
        
        # Если монета не найдена или другая ошибка от анализатора
        if report and str(report).startswith("❌"):
            msg = bot.send_message(message.chat.id, f"{report}\n\nПопробуй ввести другой тикер и направление:")
            bot.register_next_step_handler(msg, process_manual_extreme_check) # Зацикливаем
            return

        bot.send_message(message.chat.id, report, parse_mode="Markdown")
    except Exception as e:
        msg = bot.send_message(message.chat.id, f"❌ Ошибка: {e}\n\nПопробуй ввести данные еще раз:")
        bot.register_next_step_handler(msg, process_manual_extreme_check) # Зацикливаем

def handle_coin_analysis(message):
    if str(message.chat.id) != str(ADMIN_CHAT_ID): return
    bot.send_message(message.chat.id, "⏳ Анализирую...")
    result = analyze_coin(message.text.strip())
    bot.send_message(message.chat.id, result, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def handle_inline(call):
    if call.data.startswith("hist_"):
        period = call.data.replace("hist_", "")
        short_msg = format_history(period)
        
        # Создаем кнопку для скачивания файла
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📄 Скачать полный отчет (.txt)", callback_data=f"getrep_{period}"))
        
        bot.send_message(call.message.chat.id, short_msg, parse_mode="Markdown", reply_markup=markup)
        return

    # НОВЫЙ БЛОК: Отправка самого файла по клику на кнопку
    if call.data.startswith("getrep_"):
        period = call.data.replace("getrep_", "")
        bot.send_message(call.message.chat.id, "⏳ Формирую отчет, секунду...")
        
        filepath = generate_report_file(period)
        if filepath and os.path.exists(filepath):
            with open(filepath, 'rb') as f:
                bot.send_document(call.message.chat.id, f, caption="📂 Подробная статистика по сделкам")
            try:
                os.remove(filepath) # Удаляем с сервера после отправки
            except:
                pass
        else:
            bot.send_message(call.message.chat.id, "🤷‍♂️ Ошибка: нет данных для формирования отчета.")
        return

    if call.data == "status_football":
        check_live_matches(bot, call.message.chat.id, silent=False)
        return

    if call.data == "status_nba":
        check_nba_injuries(bot, call.message.chat.id, silent=False)
        return

    if "_" in call.data:
        parts = call.data.split("_")
        if len(parts) == 2:
            action, sport = parts
            config = load_config()
            if sport in config:
                config[sport]["status"] = "RUNNING" if action == "start" else "STOPPED"
                save_config(config)
                bot.edit_message_text(f"{sport.upper()}: {config[sport]['status']}",
                                      call.message.chat.id, call.message.message_id)

                if action == "start" and sport == "football":
                    check_live_matches(bot, call.message.chat.id, silent=False)

                if action == "start" and sport == "nba":
                    check_nba_injuries(bot, call.message.chat.id, silent=False)

if __name__ == "__main__":
    config = load_config()
    if "crypto" in config:
        config["crypto"]["status"] = "STOPPED"
        save_config(config)

    if ADMIN_CHAT_ID:
        start_all_background_tasks(bot, ADMIN_CHAT_ID)
        sniper_thread = threading.Thread(target=run_live_scanner, args=(bot, ADMIN_CHAT_ID), daemon=True)
        sniper_thread.start()
    else:
        print("[MAIN WARNING] ВНИМАНИЕ: ADMIN_CHAT_ID не найден в .env. Фоновые потоки не запущены.")

    print("✅ Бот включен. Сканеры спят и ждут команды 'Автобот Старт' в Telegram.")
    bot.infinity_polling(timeout=15, long_polling_timeout=5, logger_level=logging.ERROR)