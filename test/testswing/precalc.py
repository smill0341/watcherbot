import os
import sys
import pandas as pd
import json

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. ПУТЬ К ОСНОВНОМУ БОТУ (D:\bot\master_bot)
MASTER_BOT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../master_bot"))
if MASTER_BOT_ROOT not in sys.path:
    sys.path.insert(0, MASTER_BOT_ROOT)

# 2. ПУТЬ К ПАПКЕ TEST (D:\bot\test) - куда будем сохранять JSON файлы
TEST_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../"))
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from swing_hunter import build_macro_levels

# --- СПИСОК МЕСЯЦЕВ ДЛЯ ПРЕДРАСЧЕТА ---
# Можешь добавлять сюда или удалять любые нужные периоды
MONTHS_TO_CALC = [
    {"start": "2026-01-01", "end": "2026-01-31"},
    {"start": "2026-02-01", "end": "2026-02-28"},
    {"start": "2026-03-01", "end": "2026-03-31"},
    {"start": "2026-04-01", "end": "2026-04-30"},
    {"start": "2026-05-01", "end": "2026-05-31"},
    {"start": "2026-06-01", "end": "2026-06-30"},
    {"start": "2026-07-01", "end": "2026-07-31"}    
]

def build_timeline_for_month(start_date, end_date):
    # Превращаем стартовую дату в метку для имени файла (например, "2026_02")
    month_label = pd.to_datetime(start_date).strftime("%Y_%m")
    
    dates = pd.date_range(start=start_date, end=end_date, freq='12h')
    timeline = {}

    for dt in dates:
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        print(f"⏳ Сбор уровней на момент: {time_str}")
        
        levels_dict = build_macro_levels(target_time_str=time_str)
        timeline[time_str] = levels_dict

    # Динамическое имя файла: levels_timeline_2026_02.json
    filename = f'levels_timeline_{month_label}.json'
    
    # Сохраняем прямо в папку test
    output_path = os.path.join(TEST_ROOT, filename)
    with open(output_path, 'w') as f:
        json.dump(timeline, f, indent=4)
        
    print(f"\n✅ МЕСЯЦ {month_label} СОХРАНЕН В: {output_path}\n")

if __name__ == "__main__":
    print("🚀 СТАРТ МАССОВОЙ ЗАГРУЗКИ УРОВНЕЙ...")
    for period in MONTHS_TO_CALC:
        print(f"==============================================")
        print(f"📅 ОБРАБОТКА ПЕРИОДА: {period['start']} -> {period['end']}")
        print(f"==============================================")
        build_timeline_for_month(period['start'], period['end'])
    
    print("🎉 ВСЕ МЕСЯЦЫ УСПЕШНО ЗАГРУЖЕНЫ!")