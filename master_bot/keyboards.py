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
    markup.add("📈 Анализ", "👀 Watcher")

    # Ряд 2: Ручные экспресс-скринеры всего рынка
    markup.add("🚀 Critical фильтр", "💎 Light фильтр")

    # Ряд 3: Снайпер и История сигналов
    markup.add("🎯Watchlist", "📊 Результаты")

    # Ряд 4: Две отдельные кнопки как ты просил
    markup.add("🤖 Автобот", "⚡ FastTrade")

    # Ряд 5: Доп. фильтры и Выход
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
    # Ряд 1: Сами фильтры
    markup.add("🔥 RSI > 70", "🥶 RSI < 30", "💰 Volume > x2")
    # Ряд 2: Кнопка возврата в крипто-меню (название должно совпадать с main.py)
    markup.add("⬅️ Назад в меню Крипты")
    return markup

def get_autobot_menu(config=None):
    if config is None:
        # Безопасная подгрузка конфига, если не передали
        import json
        try:
            with open("config.json", "r") as f:
                config = json.load(f)
        except:
            config = {}

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("▶️ Старт", callback_data="start_autobot"),
        InlineKeyboardButton("⏹ Стоп", callback_data="stop_autobot")
    )
    
    # Кнопка переключения рынка
    current_mode = config.get("crypto", {}).get("market_mode", "swap") # swap по умолчанию
    btn_text = "🔄 Рынок: СПОТ" if current_mode == "spot" else "🔄 Рынок: ФЬЮЧЕРСЫ"
    toggle_data = "market_spot" if current_mode == "swap" else "market_swap"
    
    # Добавляем широкую кнопку снизу
    markup.add(InlineKeyboardButton(btn_text, callback_data=toggle_data))
    return markup

def get_fasttrade_menu():
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("▶️ ВКЛ", callback_data="start_fasttrade"),
        InlineKeyboardButton("⏹ ВЫКЛ", callback_data="stop_fasttrade")
    )
    return markup