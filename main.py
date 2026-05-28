import os
import json
import threading
import telebot
from dotenv import load_dotenv
import keyboards
from modules.cryptano.cryptano import auto_scheduler as run_crypto, process_crypto_command
from modules.footballnogoal.football import run_football_monitor, check_live_matches
from modules.playerpropsbasket.player_props import run_nba_monitor, check_nba_injuries
from modules.cryptano.history import check_and_update, format_history
from modules.cryptano.analyzer import analyze_coin
from modules.cryptano.scanner import run_light_scanner

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CONFIG_FILE = "config.json"

bot = telebot.TeleBot(TOKEN)

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f: return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(config, f, indent=4)

@bot.message_handler(commands=['start'])
def start(message):
    if str(message.chat.id) == str(ADMIN_CHAT_ID):
        bot.send_message(message.chat.id, "Главное меню:", reply_markup=keyboards.get_main_menu())

@bot.message_handler(content_types=['text'])
def handle_text(message):
    if str(message.chat.id) != str(ADMIN_CHAT_ID): return

    if message.text == "🪙 Крипта":
        bot.send_message(message.chat.id, "🪙 Крипто-сканер:", reply_markup=keyboards.get_crypto_menu())

    elif message.text in ["🔥 Перекупленные (RSI > 70)", "🥶 Перепроданные (RSI < 30)",
                          "💰 Аномальный объем", "🔄 Полный фильтр (RSI + Vol)"]:
        process_crypto_command(message.text, bot, message.chat.id)

    elif message.text == "🔍 Анализ монеты":
        bot.send_message(message.chat.id, "Введи тикер монеты (например: ETH, BTC, SOL):")
        bot.register_next_step_handler(message, handle_coin_analysis)

    elif message.text == "📊 Результаты сигналов":
        check_and_update(bot, message.chat.id)
        bot.send_message(message.chat.id, "Выбери период:", reply_markup=keyboards.get_history_keyboard())

    elif message.text == "⬅️ Назад в Главное меню":
        bot.send_message(message.chat.id, "Главное меню:", reply_markup=keyboards.get_main_menu())

    elif message.text in ["🏀 NBA", "⚽️ Футбол"]:
        sport = "nba" if "NBA" in message.text else "football"
        config = load_config()
        bot.send_message(message.chat.id, f"{message.text}: {config[sport]['status']}",
                         reply_markup=keyboards.get_sport_menu(sport))

    elif message.text == "🤖 Автобот Старт":
        config = load_config()
        config["crypto"]["status"] = "RUNNING"
        save_config(config)
        bot.send_message(message.chat.id, "✅ Автобот запущен. Сканирование каждый час.")

    elif message.text == "⏹ Автобот Стоп":
        config = load_config()
        config["crypto"]["status"] = "STOPPED"
        save_config(config)
        bot.send_message(message.chat.id, "⏹ Автобот остановлен.")


def handle_coin_analysis(message):
    if str(message.chat.id) != str(ADMIN_CHAT_ID): return
    bot.send_message(message.chat.id, "⏳ Анализирую...")
    result = analyze_coin(message.text.strip())
    bot.send_message(message.chat.id, result, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def handle_inline(call):
    if call.data.startswith("hist_"):
        period = call.data.replace("hist_", "")
        msg = format_history(period)
        bot.send_message(call.message.chat.id, msg, parse_mode="Markdown")
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
    # Твой старый аномальный автобот
    threading.Thread(target=run_crypto, args=(bot, ADMIN_CHAT_ID), daemon=True).start()
    
    # ТВОЙ НОВЫЙ УПРОЩЕННЫЙ СКРИНЕР (Добавляем эту строку)
    threading.Thread(target=run_light_scanner, args=(bot, ADMIN_CHAT_ID), daemon=True).start()
    
    # Остальные твои спортивные боты
    threading.Thread(target=run_football_monitor, args=(bot, ADMIN_CHAT_ID), daemon=True).start()
    threading.Thread(target=run_nba_monitor, args=(bot, ADMIN_CHAT_ID), daemon=True).start()
    
    bot.polling(none_stop=True)