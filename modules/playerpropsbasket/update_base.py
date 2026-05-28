import os
import pandas as pd
from datetime import datetime
from nba_api.stats.endpoints import leaguegamelog


def run_auto_update():
    print("Запуск автообновления базы игроков NBA через API...")
    try:
        now = datetime.now()
        # Если сейчас октябрь или позже, значит сезон начался в этом году. Иначе - в прошлом.
        start_year = now.year if now.month >= 10 else now.year - 1
        
        api_season_str = f"{start_year}-{str(start_year+1)[-2:]}"
        full_season_str = f"{start_year}-{start_year+1}"
        
        print(f"Вычислен текущий сезон для обновления: {api_season_str}")

        # Скачиваем свежую статистику через API
        log = leaguegamelog.LeagueGameLog(season=api_season_str, player_or_team_abbreviation='P')
        df_raw = log.get_data_frames()[0]
        
        df_new = pd.DataFrame()
        df_new['Player'] = df_raw['PLAYER_NAME']
        df_new['Date'] = df_raw['GAME_DATE']
        df_new['Team'] = df_raw['TEAM_ABBREVIATION']
        df_new['PTS'] = df_raw['PTS']
        df_new['FGA'] = df_raw['FGA']
        df_new['MP'] = df_raw['MIN']
        df_new['Season'] = full_season_str
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        output_path = os.path.join(current_dir, "healthyplayers.csv")
        
        df_new.to_csv(output_path, index=False)
        print(f"🔥 База успешно обновлена и сохранена в: {output_path}")
        return True
    except Exception as e:
        print(f"❌ Ошибка при обновлении базы NBA: {e}")
        return False

# Этот блок позволяет запускать файл вручную, как и раньше
if __name__ == "__main__":
    run_auto_update()