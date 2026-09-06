import os
import telebot
import logging
from dotenv import load_dotenv
import keyboards
from telebot import types
from background_tasks import start_all_background_tasks
from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.filters.critical_filter import process_crypto_command
from modules.footballnogoal.football import check_live_matches
from modules.playerpropsbasket.player_props import check_nba_injuries
from modules.cryptano.history import check_and_update, format_history
from modules.cryptano.market_overview import analyze_coin
from modules.cryptano.strategy.manual_sfp import check_manual_extreme
from modules.cryptano.filters.light_filter import run_manual_light_scan
from modules.cryptano.live_scan import manage_watchlist, show_watchlist
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

@bot.message_handler(commands=['rebuild_levels'])
def rebuild_levels(message):
    """Ручной форс-запуск построения macro_levels.json, не дожидаясь расписания."""
    if str(message.chat.id) != str(ADMIN_CHAT_ID): return

    bot.send_message(message.chat.id, "⏳ Запускаю построение уровней вручную (может занять пару минут)...")

    def _run():
        from modules.cryptano.swing_hunter import build_macro_levels
        try:
            result = build_macro_levels(bot, ADMIN_CHAT_ID)
            coins_count = len([k for k in result if k != "_meta"]) if result else 0
            bot.send_message(message.chat.id, f"✅ Готово. Уровни построены для {coins_count} монет.")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка при построении уровней: {e}")

    threading.Thread(target=_run, daemon=True).start()

@bot.message_handler(content_types=['text'])
def handle_text(message):
    if str(message.chat.id) != str(ADMIN_CHAT_ID): return

    text = message.text
    chat_id = message.chat.id

    # Перехват команд Снайпера (+BTC, -BTC)
    if manage_watchlist(text, bot, chat_id):
        return
        
    if text in ["🎯Watchlist", "Watchlist"]:
        show_watchlist(bot, chat_id)
        return

    # --- ГЛАВНОЕ МЕНЮ И СПОРТ ---
    if message.text == "🪙 Крипта":
        bot.send_message(message.chat.id, "🪙 Крипто-сканер:", reply_markup=keyboards.get_crypto_menu())

    elif message.text == "⬅️ Главное меню":
        bot.send_message(message.chat.id, "Главное меню:", reply_markup=keyboards.get_main_menu())

    elif message.text in ["🏀 NBA", "⚽️ Футбол"]:
        sport = "nba" if "NBA" in message.text else "football"
        config = load_config()
        bot.send_message(message.chat.id, f"{message.text}: {config[sport]['status']}",
                         reply_markup=keyboards.get_sport_menu(sport))

    # --- УПРАВЛЕНИЕ АВТОБОТОМ И FASTTRADE ---
    elif message.text == "🤖 Автобот":
        config = load_config()
        status = config.get("crypto", {}).get("status", "STOPPED")
        current_mode = config.get("crypto", {}).get("market_mode", "swap").upper()
        bot.send_message(
            message.chat.id, 
            f"🤖 **Автобот:** `{status}`\n⚙️ **Рынок:** `{current_mode}`", 
            reply_markup=keyboards.get_autobot_menu(config),
            parse_mode="Markdown"
        )

    elif message.text == "⚡ FastTrade":
        config = load_config()
        ft_status = "ON" if config.get("crypto", {}).get("fasttrade", True) else "OFF"
        bot.send_message(message.chat.id, f"⚡ FastTrade: {ft_status}", reply_markup=keyboards.get_fasttrade_menu())
    
    # --- РУЧНЫЕ АНАЛИЗАТОРЫ ---
    elif message.text == "📈 Анализ":
        msg = bot.send_message(message.chat.id, "Введи чистый тикер монеты для стандартного анализа (например: BTC или SOL):")
        bot.register_next_step_handler(msg, process_normal_analysis)

    elif message.text == "👀 Watcher":
        msg = bot.send_message(message.chat.id, 
            "Введи монету и направление.\nФормат: `BTC LONG или SHORT`", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_manual_extreme_check)  
        
    # --- ОСНОВНЫЕ СКАНЕРЫ ---
    elif message.text == "🚀 Critical фильтр":
        print("[MAIN LOG] Нажата кнопка 🚀 Critical фильтр. Передаю в cryptano.py")
        process_crypto_command("⚡️ Critical фильтр", bot, message.chat.id) # Передаем старый текст для обратной совместимости в фильтре

    elif message.text == "💎 Light фильтр":
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

    elif message.text in ["🔥 RSI > 70", "🥶 RSI < 30", "💰 Volume > x2"]:
        process_crypto_command(message.text, bot, message.chat.id)

    # --- РЕЗУЛЬТАТЫ ---
    elif message.text == "📊 Результаты":
        bot.send_message(message.chat.id, "⏳ Опрашиваю биржу (проверка открытых сделок)...", parse_mode="Markdown")
        
        def run_check():
            check_and_update(bot, message.chat.id)
            bot.send_message(message.chat.id, "✅ База обновлена! Выбери период для отчета:", reply_markup=keyboards.get_history_keyboard())
            
        import threading
        threading.Thread(target=run_check).start()

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
    
    # =========================================================
    # БЛОК: ПОЛНАЯ ОЧИСТКА WATCHLIST
    # =========================================================
    if call.data == "clear_entire_watchlist":
        try:
            wl_path = os.path.join("modules", "cryptano", "watchlist.json")
            save_json_atomic(wl_path, {})  # Полностью перезаписываем в пустой JSON
            
            bot.answer_callback_query(call.id, "Watchlist успешно очищен!")
            bot.edit_message_text(
                "📋 Мой watchlist пуст.\nДобавь монеты командой `+BTC` или `+ETH SHORT`", 
                call.message.chat.id, 
                call.message.message_id,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"[ERROR] Ошибка полной очистки watchlist: {e}")
            bot.answer_callback_query(call.id, f"Ошибка: {e}")
        return
    
    # =========================================================
    # БЛОК 1: УПРАВЛЕНИЕ АВТОБОТОМ (ВКЛ / ВЫКЛ)
    # =========================================================
    if call.data in ["start_autobot", "stop_autobot"]:
        config = load_config()
        if "crypto" not in config: config["crypto"] = {}
        
        if call.data == "start_autobot":
            config["crypto"]["status"] = "RUNNING"
            # Строку с принудительным fasttrade = True полностью удалили
            save_config(config)
            
            # Обновляем текст кнопки на актуальный статус
            bot.edit_message_text(
                f"🤖 Автобот: RUNNING", 
                call.message.chat.id, 
                call.message.message_id, 
                reply_markup=keyboards.get_autobot_menu()
            )
            
            bot.send_message(
                call.message.chat.id, 
                "🚀 **Автобот запущен!**\nФильтры начнут включаться по каскадному расписанию.\n⚙️ _FastTrade управляется отдельно вручную._", 
                parse_mode="Markdown"
            )
            
        else:
            config["crypto"]["status"] = "STOPPED"
            save_config(config)
            
            bot.edit_message_text(
                f"🤖 Автобот: STOPPED", 
                call.message.chat.id, 
                call.message.message_id, 
                reply_markup=keyboards.get_autobot_menu()
            )
            bot.send_message(
                call.message.chat.id, 
                "⏹ **Автобот остановлен.**\nВсе фоновые проверки полностью прекращены. Автоматика спит."
            )
        return

    # =========================================================
    # БЛОК 2: УПРАВЛЕНИЕ РЕЖИМОМ FASTTRADE (СКАЛЬПИНГ ТОП-20)
    # =========================================================
    if call.data in ["start_fasttrade", "stop_fasttrade"]:
        config = load_config()
        if "crypto" not in config: config["crypto"] = {}
        
        if call.data == "start_fasttrade":
            config["crypto"]["fasttrade"] = True
            save_config(config)
            
            bot.edit_message_text(
                f"⚡ FastTrade: ON", 
                call.message.chat.id, 
                call.message.message_id, 
                reply_markup=keyboards.get_fasttrade_menu()
            )
            bot.send_message(
                call.message.chat.id, 
                "⚡ **FastTrade включен.**\nГенератор монет добавит первые 20 волатильных пар через 10 минут после старта Автобота."
            )
        else:
            config["crypto"]["fasttrade"] = False
            save_config(config)
            
            # 🧹 РЕЖИМ ДВОРНИКА: Мгновенно выметаем скальпинг-монеты из watchlist.json, не дожидаясь таймеров
            try:
                wl_path = os.path.join("modules", "cryptano", "watchlist.json")
                wl = load_json(wl_path, default={})
                # Ищем монеты, добавленные генератором пампа/дампа
                keys_to_delete = [k for k, v in wl.items() if v.get("source") in ["MOMENTUM_PUMP", "MOMENTUM_DUMP"]]
                for k in keys_to_delete:
                    del wl[k]
                save_json_atomic(wl_path, wl)
                print(f"[FASTTRADE] 🧹 Мгновенная очистка кнопкой. Удалено {len(keys_to_delete)} монет.")
            except Exception as e:
                print(f"[ERROR] Ошибка мгновенной очистки watchlist: {e}")
                
            bot.edit_message_text(
                f"⚡ FastTrade: OFF", 
                call.message.chat.id, 
                call.message.message_id, 
                reply_markup=keyboards.get_fasttrade_menu()
            )
            bot.send_message(
                call.message.chat.id, 
                "🧹 **FastTrade выключен.**\nСкальпинг-монеты лидеров дня мгновенно удалены из списка слежения Watcher."
            )
        return

    if call.data == "status_football":
        check_live_matches(bot, call.message.chat.id, silent=False)
        return

    if call.data == "status_nba":
        check_nba_injuries(bot, call.message.chat.id, silent=False)
        return

    # =========================================================
    # БЛОК: ИСТОРИЯ И ОТЧЕТЫ (Короткий отчет + кнопка TXT)
    # =========================================================
    if call.data in ["hist_day", "hist_week", "hist_month", "hist_all"]:
        period = call.data.split("_")[1]
        bot.edit_message_text("⏳ Считываю статистику...", call.message.chat.id, call.message.message_id)
        
        try:
            # Отправляем только короткий текст
            short_report = format_history(period)
            txt_markup = types.InlineKeyboardMarkup()
            txt_markup.add(types.InlineKeyboardButton("📄 Скачать полный отчет (.txt)", callback_data=f"gettxt_{period}"))
            
            bot.send_message(call.message.chat.id, short_report, parse_mode="Markdown", reply_markup=txt_markup)
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка вывода истории: {e}")
            
        bot.delete_message(call.message.chat.id, call.message.message_id)
        return

    # =========================================================
    # БЛОК: СКАЧИВАНИЕ ПОЛНОГО ОТЧЕТА ПО ЗАПРОСУ
    # =========================================================
    if call.data.startswith("gettxt_"):
        period = call.data.split("_")[1]
        bot.answer_callback_query(call.id, "Генерирую файл отчета...")
        
        try:
            file_path = generate_report_file(period)
            if file_path and os.path.exists(file_path):
                with open(file_path, "rb") as f:
                    bot.send_document(call.message.chat.id, f, caption=f"📄 Детализация за период: {period.upper()}")
            else:
                bot.send_message(call.message.chat.id, "⚠️ Не удалось сформировать файл. Возможно, сделок нет.")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка отправки файла: {e}")
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
                    
    # =========================================================
    # БЛОК: ПЕРЕКЛЮЧЕНИЕ РЫНКА (СПОТ / ФЬЮЧЕРСЫ)
    # =========================================================
    if call.data in ["market_spot", "market_swap"]:
        new_mode = "spot" if call.data == "market_spot" else "swap"
        
        config = load_config()
        if "crypto" not in config: config["crypto"] = {}
        config["crypto"]["market_mode"] = new_mode
        save_config(config)
        
        # Вызываем нашу новую функцию из Единого Центра
        from modules.cryptano.utils.crypto_utils import switch_market_mode
        switch_market_mode(new_mode)
        
        status = config["crypto"].get("status", "STOPPED")
        bot.edit_message_text(
            f"🤖 **Автобот:** `{status}`\n⚙️ **Рынок:** `{new_mode.upper()}`", 
            call.message.chat.id, 
            call.message.message_id, 
            reply_markup=keyboards.get_autobot_menu(config),
            parse_mode="Markdown"
        )
        
        mode_ru = "СПОТ" if new_mode == "spot" else "ФЬЮЧЕРСЫ"
        bot.send_message(
            call.message.chat.id, 
            f"✅ **Рынок переключен на {mode_ru}.**\nВсе сканеры, генератор и Watcher теперь анализируют этот рынок.",
            parse_mode="Markdown"
        )
        return               

if __name__ == "__main__":
    config = load_config()
    if "crypto" not in config:
        config["crypto"] = {"status": "STOPPED", "fasttrade": False}
    else:
        config["crypto"]["status"] = "STOPPED"
        # Если ключа fasttrade вообще не было в файле, создаем его как выключенный по умолчанию
        if "fasttrade" not in config["crypto"]:
            config["crypto"]["fasttrade"] = False
            
    save_config(config)

    if ADMIN_CHAT_ID:
        start_all_background_tasks(bot, ADMIN_CHAT_ID)
    else:
        print("[MAIN WARNING] ВНИМАНИЕ: ADMIN_CHAT_ID не найден в .env. Фоновые потоки не запущены.")

    print("✅ Бот включен. Сканеры спят и ждут команды 'Автобот Старт' в Telegram.")
    bot.infinity_polling(timeout=15, long_polling_timeout=5, logger_level=logging.ERROR)