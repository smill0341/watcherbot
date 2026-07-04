import os
import sys

# 1. ЖЕСТКО УКАЗЫВАЕМ ПУТЬ К КОРНЮ ПРОЕКТА (Чтобы Python видел папку modules)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pandas as pd
import json
# Теперь импорт сработает без ошибок из любой папки
from modules.cryptano.utils.testswing.swing_hunter import build_macro_levels

START_DATE = "2026-01-01"
END_DATE = "2026-06-30"

def build_timeline():
    # freq='12h' нарежет месяц на даты: 01.04 00:00, 01.04 12:00, 02.04 00:00 и т.д.
    dates = pd.date_range(start=START_DATE, end=END_DATE, freq='12h')
    timeline = {}

    for dt in dates:
        # Превращаем в формат "YYYY-MM-DD HH:MM:SS" как любит твой скрипт
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n==============================================")
        print(f"⏳ Сбор уровней на момент: {time_str}")
        print(f"==============================================\n")
        
        # Вызываем твоего Хантера, передавая ему время среза
        levels_dict = build_macro_levels(target_time_str=time_str)
        timeline[time_str] = levels_dict

    # Сохраняем файл прямо в корень проекта, чтобы тестер его сразу увидел
    output_path = os.path.join(PROJECT_ROOT, 'levels_timeline.json')
    with open(output_path, 'w') as f:
        json.dump(timeline, f, indent=4)
        
    print(f"\n✅ ГОТОВО! Файл сохранен в: {output_path}")

if __name__ == "__main__":
    build_timeline()