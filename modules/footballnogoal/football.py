import os
import requests
import time
from datetime import datetime
from dotenv import load_dotenv
from modules.storage import load_json, save_json_atomic

# Явно указываем путь к .env
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ==================================================
#                 НАСТРОЙКИ ФИЛЬТРОВ
# ==================================================
# 1. Настройки времени
START_MINUTE = 60  # С какой минуты начинаем ловить матч
END_MINUTE = 72    # До какой минуты ведем поиск

# 2. Частота опроса API (в секундах)
SLEEP_TIME = 180   # 180 секунд = 3 минуты

# 3. ID Актуальных и Топ-лиг
TOP_LEAGUES = {
    # Топ-Европа (оставляем на случай, когда сезон возобновится)
    39: "АПЛ (Англия)", 
    140: "Ла Лига (Испания)", 
    78: "Бундеслига (Германия)", 
    135: "Серия А (Италия)", 
    61: "Лига 1 (Франция)", 
    2: "Лига Чемпионов",
    
    # Летние активные и забивные чемпионаты
    253: "MLS (США)",
    71: "Серия А (Бразилия)",
    103: "Элитсерия (Норвегия)",
    113: "Аллсвенскан (Швеция)",
    98: "Джей-лига 1 (Япония)",
    292: "К-Лига 1 (Южная Корея)"
}

# 4. НАСТРОЙКИ ФИЛЬТРОВ ДАВЛЕНИЯ
MIN_TOTAL_XG = 2.0        # Минимальный суммарный xG обеих команд для общего гола
MIN_TOTAL_CHANCES = 3     # Минимальное суммарное количество голевых шансов

# Пороги для доп. прогноза (индивидуальный гол конкретной команды)
TEAM_MIN_TOUCHES = 35     # Дотиков в штрафной соперника
TEAM_MIN_SHOTS = 12       # Всего ударов команды
TEAM_MIN_XG = 1.5         # Индивидуальный xG команды

# ==================================================

# Множество, чтобы не дублировать сигналы в Telegram
sent_matches = set()

def parse_stat_value(stat_list, type_name):
    """Помощник для безопасного извлечения числовых значений из статистики API"""
    for stat in stat_list:
        if stat.get('type') == type_name:
            val = stat.get('value')
            if val is None:
                return 0
            if isinstance(val, str):
                val = val.replace('%', '').strip()
            return float(val) if '.' in str(val) else int(val)
    return 0

def send_telegram_signal_advanced(bot, chat_id, match_data, minute, ht_home, ht_away, total_xg, total_chances, 
                                  home_team, away_team, home_touches, away_touches, 
                                  home_shots, away_shots, additional_pred):
    league_name = match_data['league']['name']
    current_home = match_data['goals']['home']
    current_away = match_data['goals']['away']
    
    message = (
        f"⚽️ **СИГНАЛ: АНОМАЛЬНОЕ ДАВЛЕНИЕ (ГОЛ НАЗРЕВАЕТ)** ⚽️\n\n"
        f"🏆 Лига: {league_name}\n"
        f"⏱ Минута: {minute}'\n"
        f"⚔️ Матч: **{home_team}** {current_home}:{current_away} **{away_team}**\n"
        f" halftime Счет 1-го тайма: ({ht_home}:{ht_away})\n\n"
        f"📊 **Live-метрики матча:**\n"
        f"• Суммарный xG: `{total_xg:.2f}` (порог: >={MIN_TOTAL_XG})\n"
        f"• Голевые шансы: `{int(total_chances)}` (порог: >={MIN_TOTAL_CHANCES})\n"
        f"• Дотики в штрафной: `{int(home_touches)}` vs `{int(away_touches)}`\n"
        f"• Удары по воротам: `{int(home_shots)}` vs `{int(away_shots)}`\n\n"
        f"🔥 **ОСНОВНАЯ СТАВКА:** Общий ТБ (0.5) в матче / ТБ во 2-м тайме\n\n"
        f"🎯 **ДОПОЛНИТЕЛЬНЫЙ ПРОГНОЗ:**\n`{additional_pred}`"
    )
    
    try:
        # Отправляем через объект bot из main.py
        bot.send_message(chat_id, message, parse_mode='Markdown')
    except Exception as e:
        print(f"❌ Не удалось отправить в Telegram: {e}")

def check_live_matches(bot, chat_id, silent=False):
    current_time = datetime.now().strftime("%H:%M:%S")
    print(f"[{current_time}] Отправляю запрос к API футбола...")
    
    # Берем ключ API из безопасного .env
    api_key = os.getenv("API_FOOTBALL_KEY")
    if not api_key:
        print("❌ ОШИБКА: API_FOOTBALL_KEY не найден в файле .env")
        return

    url = "https://v3.football.api-sports.io/fixtures?live=all"
    headers = {
        'x-rapidapi-host': "v3.football.api-sports.io",
        'x-rapidapi-key': api_key
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10).json()
        fixtures = response.get('response', [])
        
        total_live = len(fixtures)
        top_leagues_live = 0
        matches_found = 0
        
        for item in fixtures:
            league_id = item['league']['id']
            fixture_id = item['fixture']['id']
            
            # Фильтр 1: Только топ-лиги
            if league_id in TOP_LEAGUES:
                top_leagues_live += 1
                status = item['fixture']['status']
                elapsed = status.get('elapsed')  # Текущая минута
                
                # Фильтр 2: Временной интервал
                if elapsed and START_MINUTE <= elapsed <= END_MINUTE:
                    
                    halftime_home = item['score']['halftime']['home']
                    halftime_away = item['score']['halftime']['away']
                    current_home = item['goals']['home']
                    current_away = item['goals']['away']
                    
                    if all(v is not None for v in [halftime_home, halftime_away, current_home, current_away]):
                        
                        # Фильтр 3: Сухой второй тайм
                        if current_home == halftime_home and current_away == halftime_away:
                            
                            # === БЛОК: ФИЛЬТРАЦИЯ ПО ДАВЛЕНИЮ ===
                            stats_url = f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture_id}"
                            stats_res = requests.get(stats_url, headers=headers, timeout=10).json()
                            stats_data = stats_res.get('response', [])
                            
                            if len(stats_data) >= 2:
                                # Разбираем статистику Хозяев (index 0) и Гостей (index 1)
                                home_stats = stats_data[0].get('statistics', [])
                                away_stats = stats_data[1].get('statistics', [])
                                
                                # Извлекаем метрики xG, голевые шансы, дотики и удары
                                home_xg = parse_stat_value(home_stats, "Expected Goals")
                                away_xg = parse_stat_value(away_stats, "Expected Goals")
                                home_chances = parse_stat_value(home_stats, "Big Chances")
                                away_chances = parse_stat_value(away_stats, "Big Chances")
                                home_touches = parse_stat_value(home_stats, "Touches in Opposition Box")
                                away_touches = parse_stat_value(away_stats, "Touches in Opposition Box")
                                home_shots = parse_stat_value(home_stats, "Total Shots")
                                away_shots = parse_stat_value(away_stats, "Total Shots")
                                
                                # ПЛЮС: Парсим удары в створ (для запасного фильтра летних лиг)
                                home_on_target = parse_stat_value(home_stats, "Shots on Goal")
                                away_on_target = parse_stat_value(away_stats, "Shots on Goal")
                                total_on_target = home_on_target + away_on_target
                                
                                total_xg = home_xg + away_xg
                                total_chances = home_chances + away_chances
                                total_shots = home_shots + away_shots
                                
                                # Проверяем, есть ли продвинутая стата в этой лиге
                                has_advanced_stats = total_xg > 0 or total_chances > 0
                                is_pressure_match = False
                                
                                # Логика прохода: либо по xG (топ-лиги), либо по ударам (если xG недоступен)
                                if has_advanced_stats:
                                    if total_xg >= MIN_TOTAL_XG or total_chances >= MIN_TOTAL_CHANCES:
                                        is_pressure_match = True
                                else:
                                    # Запасной фильтр для летних лиг: минимум 13 ударов всего и 4 в створ
                                    if total_shots >= 13 and total_on_target >= 4:
                                        is_pressure_match = True
                                
                                if is_pressure_match:
                                    matches_found += 1
                                    
                                    if fixture_id not in sent_matches:
                                        home_team = item['teams']['home']['name']
                                        away_team = item['teams']['away']['name']
                                        
                                        # Определяем, есть ли перекос на индивидуальный гол одной из команд
                                        additional_pred = "Отсутствует (игра равная)"
                                        
                                        if has_advanced_stats:
                                            if home_touches >= TEAM_MIN_TOUCHES and home_shots >= TEAM_MIN_SHOTS and home_xg >= TEAM_MIN_XG:
                                                additional_pred = f"🔥 ИТБ1 (+0.5) — Гол команды {home_team}"
                                            elif away_touches >= TEAM_MIN_TOUCHES and away_shots >= TEAM_MIN_SHOTS and away_xg >= TEAM_MIN_XG:
                                                additional_pred = f"🔥 ИТБ2 (+0.5) — Гол команды {away_team}"
                                        else:
                                            # Индивидуальный прогноз по ударам, если нет xG
                                            if home_on_target >= 4 and home_shots >= 9:
                                                additional_pred = f"🔥 ИТБ1 (+0.5) — Гол команды {home_team} (по ударам)"
                                            elif away_on_target >= 4 and away_shots >= 9:
                                                additional_pred = f"🔥 ИТБ2 (+0.5) — Гол команды {away_team} (по ударам)"
                                        
                                        print(f"🔥 НАЙДЕН ЖИРНЫЙ МАТЧ ({TOP_LEAGUES[league_id]}): {home_team} {current_home}:{current_away} {away_team} ({elapsed}') -> В Telegram!")
                                        
                                        send_telegram_signal_advanced(
                                            bot, chat_id, item, elapsed, halftime_home, halftime_away, 
                                            total_xg, total_chances, home_team, away_team, 
                                            home_touches, away_touches, home_shots, away_shots, 
                                            additional_pred
                                        )
                                        sent_matches.add(fixture_id)
                                        
        print(f"📊 Итог проверки: Всего в лайве: {total_live} | Из них ТОП-лиг: {top_leagues_live} | Подходят под критерии: {matches_found}")
    
        if not silent:
            bot.send_message(chat_id, f"📊 Лайв: {total_live} | ТОП-лиг: {top_leagues_live} | Сигналов: {matches_found}")
                                
    except Exception as e:
        print(f"❌ Ошибка при сканировании: {e}\n")
                                


# ================= ИНТЕГРАЦИЯ С MAIN.PY =================
def run_football_monitor(bot, chat_id):
    """
    Бесконечный фоновый цикл. Управляется через config.json
    """
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.json")
    
    print(" Бот-сканер НАИГРАННОГО ГОЛА инициализирован")

    while True:
        try:
            if os.path.exists(config_path):
                config = load_json(config_path, default={})
                
                # Проверка автостопа
                auto_stop = config.get("football", {}).get("auto_stop", "00:00")
                current_time = datetime.now().strftime("%H:%M")
                
                if current_time == auto_stop and config.get("football", {}).get("status") == "RUNNING":
                    config["football"]["status"] = "STOPPED"
                    save_json_atomic(config_path, config, indent=4)
                    print(f"[ФУТБОЛ] ⏹ Автостоп в {auto_stop}. Статус → STOPPED.")
                    time.sleep(SLEEP_TIME)
                    continue
                
                # Запускаем проверку матчей ТОЛЬКО если статус RUNNING
                if config.get("football", {}).get("status") == "RUNNING":
                    check_live_matches(bot, chat_id, silent=True)
                    
        except Exception as e:
            print(f"❌ Ошибка в цикле футбола: {e}")
            try:
                bot.send_message(chat_id, f"❌ [ФУТБОЛ] Ошибка:\n`{e}`", parse_mode="Markdown")
            except:
                pass

        time.sleep(SLEEP_TIME)     
