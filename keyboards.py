from telebot import types

def get_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🏀 NBA", "⚽️ Футбол", "🪙 Крипта")
    return markup

def get_sport_menu(sport):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("▶️ Старт", callback_data=f"start_{sport}"),
        types.InlineKeyboardButton("⏹ Стоп", callback_data=f"stop_{sport}")
    )
    if sport == "football":
        markup.add(types.InlineKeyboardButton("📊 Статус", callback_data="status_football"))
    if sport == "nba":
        markup.add(types.InlineKeyboardButton("📊 Статус", callback_data="status_nba"))
    return markup

def get_crypto_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton("📉 RSI", callback_data="cryp_rsi"),
        types.InlineKeyboardButton("💰 Объем", callback_data="cryp_vol"),
        types.InlineKeyboardButton("🔄 Все", callback_data="cryp_all")
    )
    return keyboard

def get_crypto_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔥 Перекупленные (RSI > 70)", "🥶 Перепроданные (RSI < 30)")
    markup.add("💰 Аномальный объем", "🔄 Полный фильтр (RSI + Vol)")
    markup.add("🤖 Автобот Старт", "⏹ Автобот Стоп")
    markup.add("🔍 Анализ монеты")  
    markup.add("📊 Результаты сигналов")
    markup.add("⬅️ Назад в Главное меню")
    return markup

def get_history_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("📅 День", callback_data="hist_day"),
        types.InlineKeyboardButton("📅 Неделя", callback_data="hist_week")
    )
    markup.add(
        types.InlineKeyboardButton("📅 Месяц", callback_data="hist_month"),
        types.InlineKeyboardButton("📅 Всё", callback_data="hist_all")
    )
    return markup