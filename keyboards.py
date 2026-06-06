from telebot.types import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton

def get_main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🏀 NBA", "⚽️ Футбол", "🪙 Крипта")
    return markup

def get_sport_menu(sport):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("▶️ Старт", callback_data=f"start_{sport}"),
        InlineKeyboardButton("⏹ Стоп", callback_data=f"stop_{sport}")
    )
    if sport == "football":
        markup.add(InlineKeyboardButton("📊 Статус", callback_data="status_football"))
    if sport == "nba":
        markup.add(InlineKeyboardButton("📊 Статус", callback_data="status_nba"))
    return markup

def get_crypto_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("📉 RSI", callback_data="cryp_rsi"),
        InlineKeyboardButton("💰 Объем", callback_data="cryp_vol"),
        InlineKeyboardButton("🔄 Все", callback_data="cryp_all")
    )
    return keyboard

def get_crypto_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)

    # Ряд 1: Ручной разбор монеты в зависимости от фазы
    markup.add("📈 Анализ Normal", "🔍 Watcher Plan")

    # Ряд 2: Ручные экспресс-скринеры всего рынка
    markup.add("⚡️ Critical фильтр", "💎 Light фильтр")

    # Ряд 3: Снайпер и История сигналов
    markup.add("🎯 Мой Watchlist", "📊 Результаты")

    # Ряд 4: Управление фоновой автоматикой
    markup.add("🤖 Автобот Старт", "📴 Автобот Стоп")

    # Ряд 5: Доп. фильтры и Выход (в один ряд)
    markup.add("🛠 Другие фильтры", "⬅️ Главное меню")

    return markup

def get_history_keyboard():
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("📅 День", callback_data="hist_day"),
        InlineKeyboardButton("📅 Неделя", callback_data="hist_week")
    )
    markup.add(
        InlineKeyboardButton("📅 Месяц", callback_data="hist_month"),
        InlineKeyboardButton("📅 Всё", callback_data="hist_all")
    )
    return markup

def get_other_filters_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("⬅️ Назад в Крипту")
    return markup
    markup.add("🔥 RSI > 70", "🥶 RSI < 30", "💰 Volume > x2")
    
    # Ряд 2: Кнопка возврата в крипто-меню
    markup.add("⬅️ Назад в меню Крипты")
    
    return markup