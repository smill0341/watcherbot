import os
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import pandas as pd
from modules.playerpropsbasket.update_base import run_auto_update
from dotenv import load_dotenv
from master_bot.modules.cryptano.utils.storage import load_json, save_json_atomic

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ==================================================
#                 НАСТРОЙКИ NBA
# ==================================================
SLEEP_TIME = 1800  # Как часто чекать сайт (1800 сек = 30 минут)

current_dir = os.path.dirname(os.path.abspath(__file__))
file_healthy = os.path.join(current_dir, "healthyplayers.csv")

team_map = {
    'Celtics': 'BOS', 'Nets': 'BKN', 'Knicks': 'NYK', '76ers': 'PHI', 'Raptors': 'TOR',
    'Bulls': 'CHI', 'Cavaliers': 'CLE', 'Pistons': 'DET', 'Pacers': 'IND', 'Bucks': 'MIL',
    'Hawks': 'ATL', 'Hornets': 'CHA', 'Heat': 'MIA', 'Magic': 'ORL', 'Wizards': 'WAS',
    'Nuggets': 'DEN', 'Timberwolves': 'MIN', 'Thunder': 'OKC', 'Blazers': 'POR', 'Jazz': 'UTA',
    'Warriors': 'GSW', 'Clippers': 'LAC', 'Lakers': 'LAL', 'Suns': 'PHO', 'Kings': 'SAC',
    'Mavericks': 'DAL', 'Rockets': 'HOU', 'Grizzlies': 'MEM', 'Pelicans': 'NOP', 'Spurs': 'SAS'
}

# Множество для отправленных сигналов, чтобы бот не спамил одно и то же каждые 30 минут
sent_signals = set()

def load_data():
    try:
        return pd.read_csv(file_healthy)
    except Exception:
        return None

def analyze_star_absence_live(df, star_name, team_abc):
    team_games = df[df['Team'] == team_abc]
    if team_games.empty: return None
    
    all_game_dates = team_games['Date'].unique()
    star_played_dates = team_games[team_games['Player'] == star_name]['Date'].unique()
    star_out_dates = [d for d in all_game_dates if d not in star_played_dates]
    
    if len(star_out_dates) < 2: return None
    
    teammates_out = team_games[team_games['Date'].isin(star_out_dates) & (team_games['Player'] != star_name)]
    teammates_in = team_games[team_games['Date'].isin(star_played_dates) & (team_games['Player'] != star_name)]
    
    if teammates_out.empty or teammates_in.empty: return None
    
    avg_out = teammates_out.groupby('Player')[['PTS', 'FGA']].mean()
    avg_out['games_out'] = teammates_out.groupby('Player')['PTS'].count()
    avg_in = teammates_in.groupby('Player')[['PTS', 'FGA']].mean()
    
    merged = avg_out[avg_out['games_out'] >= 2].merge(avg_in, on='Player', suffixes=('_OUT', '_IN'))
    merged['diff_pts'] = merged['PTS_OUT'] - merged['PTS_IN']
    
    if merged.empty: return None
    best_player = merged['diff_pts'].idxmax()
    row = merged.loc[best_player]
    
    if row['diff_pts'] >= 3.5:
        return {
            'player': best_player,
            'pts_in': round(row['PTS_IN'], 1),
            'pts_out': round(row['PTS_OUT'], 1),
            'diff': round(row['diff_pts'], 1),
            'games_count': len(star_out_dates)
        }
    return None

def check_nba_injuries(bot, chat_id, silent=False):
    current_time = datetime.now().strftime("%H:%M:%S")
    print(f"[{current_time}] 🏀 NBA: Сканирование составов Rotowire...")
    
    # Загружаем свежую базу прямо перед проверкой
    df = load_data()
    if df is None:
        print(f"⚠️ ОШИБКА: Файл {file_healthy} не найден!")
        bot.send_message(chat_id, "⚠️ **NBA:** База `healthyplayers.csv` не найдена! Запустите скрипт обновления базы.", parse_mode="Markdown")
        return

    url = "https://www.rotowire.com/basketball/nba-lineups.php"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        lineup_boxes = soup.find_all('div', class_='lineup__box')
        
        any_signals = False
        
        # Очищаем кэш сигналов раз в сутки, чтобы он не раздувался
        if len(sent_signals) > 100:
            sent_signals.clear()

        for box in lineup_boxes:
            teams = box.find_all('div', class_='lineup__mteam')
            if len(teams) < 2: continue
            
            home_abc, away_abc = team_map.get(teams[0].text.strip()), team_map.get(teams[1].text.strip())
            injury_list = box.find('ul', class_='lineup__injuries')
            
            if injury_list:
                for player in injury_list.find_all('li'):
                    name_node, status_node = player.find('a'), player.find('span', class_='lineup__injury-status')
                    if name_node and status_node:
                        p_name, status = name_node.text.strip(), status_node.text.strip()
                        
                        if status in ["Out", "GTD"]:
                            trend = None
                            if away_abc: trend = analyze_star_absence_live(df, p_name, away_abc)
                            if not trend and home_abc: trend = analyze_star_absence_live(df, p_name, home_abc)
                            
                            if trend:
                                # Уникальный ID сигнала, чтобы не спамить одно и то же
                                signal_id = f"{p_name}_{status}_{trend['player']}"
                                
                                if signal_id not in sent_signals:
                                    any_signals = True
                                    
                                    # Формируем сообщение для Телеграма
                                    msg = (
                                        f"🔥 **РЕАЛЬНЫЙ ВАЛУЙНЫЙ СИГНАЛ (NBA):**\n\n"
                                        f"🏀 **Матч:** {teams[1].text.strip()} @ {teams[0].text.strip()}\n"
                                        f"❌ **Травма/Отдых:** {p_name} ({status})\n"
                                        f"🎯 **СТАВКА:** {trend['player']} -> **ТОТАЛ БОЛЬШЕ**\n\n"
                                        f"📊 Со звездой: `{trend['pts_in']} PTS` | БЕЗ НЕЕ: `{trend['pts_out']} PTS`\n"
                                        f"📈 Прирост: `+{trend['diff']}` очков (выборка: {trend['games_count']} матчей)"
                                    )
                                    
                                    bot.send_message(chat_id, msg, parse_mode="Markdown")
                                    sent_signals.add(signal_id)
                                    print(f"🔥 Отправлен сигнал в ТГ: {trend['player']} (Травма: {p_name})")
                                    
        if not silent:
            if any_signals:
                bot.send_message(chat_id, "✅ [NBA]: Сканирование завершено. Сигналы отправлены выше.")
            else:
                bot.send_message(chat_id, "🏀 [NBA]: Матчей с травмами не найдено.")
            
    except Exception as e:
        print(f"❌ Ошибка парсинга NBA: {e}")


# ================= ИНТЕГРАЦИЯ С MAIN.PY =================
def run_nba_monitor(bot, chat_id):
    """
    Бесконечный фоновый цикл. Управляется через config.json
    """
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.json")

    print(" Бот NBA injures инициализирован! ")
 
    while True:
        try:
            if os.path.exists(config_path):
                config = load_json(config_path, default={})
                
                # Проверка автостопа
                auto_stop = config.get("nba", {}).get("auto_stop", "06:00")
                current_time = datetime.now().strftime("%H:%M")
                
                if current_time == auto_stop and config.get("nba", {}).get("status") == "RUNNING":
                    config["nba"]["status"] = "STOPPED"
                    save_json_atomic(config_path, config, indent=4)
                    print(f"[NBA] ⏹ Автостоп в {auto_stop}. Статус → STOPPED.")
                    time.sleep(SLEEP_TIME)
                    continue
                
                # Запускаем логику ТОЛЬКО если статус RUNNING
                if config.get("nba", {}).get("status") == "RUNNING":
                    
                    # --- БЛОК АВТООБНОВЛЕНИЯ БАЗЫ ---
                    need_update = False
                    if not os.path.exists(file_healthy):
                        need_update = True
                    else:
                        file_age_hours = (time.time() - os.path.getmtime(file_healthy)) / 3600
                        if file_age_hours > 18:
                            need_update = True
                    
                    if need_update:
                        print("[NBA] Локальная база игроков устарела. Запускаю обновление...")
                        success = run_auto_update()
                        if success:
                            print("[NBA] База успешно обновлена.")
                        else:
                            print("[NBA] ⚠️ Не удалось обновить базу.")
                    
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🏀 NBA: Сканирование...")
                    check_nba_injuries(bot, chat_id, silent=True)       
                    
        except Exception as e:
            print(f"Ошибка в цикле монитора NBA: {e}")
            try:
                bot.send_message(chat_id, f"❌ [NBA] Ошибка:\n`{e}`", parse_mode="Markdown")
            except:
                pass
            
        time.sleep(SLEEP_TIME)
