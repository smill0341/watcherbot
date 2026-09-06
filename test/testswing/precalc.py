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

from swing_hunter import build_macro_levels, get_top_symbols, build_full_history_cache

# --- СПИСОК МЕСЯЦЕВ ДЛЯ ПРЕДРАСЧЕТА ---
# Можешь добавлять сюда или удалять любые нужные периоды
MONTHS_TO_CALC = [
   
    {"start": "2026-08-01", "end": "2026-08-31"}    
]

def build_timeline_for_month(start_date, end_date, cache, valid_symbols):
    # Превращаем стартовую дату в метку для имени файла (например, "2026_02")
    month_label = pd.to_datetime(start_date).strftime("%Y_%m")
    
    dates = pd.date_range(start=start_date, end=end_date, freq='12h')
    timeline = {}

    for dt in dates:
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        print(f"⏳ Сбор уровней на момент: {time_str}")

        # cache=... значит build_macro_levels работает целиком в памяти,
        # без единого сетевого запроса к бирже на этом шаге
        levels_dict = build_macro_levels(target_time_str=time_str, cache=cache, valid_symbols=valid_symbols)
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

    # 1. Список монет фиксируем ОДИН раз на весь прогон (раньше пересчитывался
    #    под каждую дату отдельно, каждый раз по СЕГОДНЯШНЕМУ объёму — теперь
    #    явно один список для всех периодов сразу).
    valid_symbols = get_top_symbols()

    # 2. Считаем, насколько глубоко назад нужна история 4h-свечей, чтобы хватило
    #    даже на самую раннюю дату из MONTHS_TO_CALC (200 свечей запаса на неё) +
    #    сам охватываемый период.
    earliest_start = min(pd.to_datetime(p["start"]) for p in MONTHS_TO_CALC)
    days_span = max((pd.Timestamp.now() - earliest_start).days, 0)
    extra_candles = max(1500, (days_span + 40) * 6)  # 6 = 4h-свечей в сутках

    # 3. Качаем всю историю по каждой монете ОДИН раз (вместо сети под каждую
    #    из 12-часовых точек ниже).
    cache = build_full_history_cache(valid_symbols, extra_candles=extra_candles)

    for period in MONTHS_TO_CALC:
        print(f"==============================================")
        print(f"📅 ОБРАБОТКА ПЕРИОДА: {period['start']} -> {period['end']}")
        print(f"==============================================")
        build_timeline_for_month(period['start'], period['end'], cache, valid_symbols)
    
    print("🎉 ВСЕ МЕСЯЦЫ УСПЕШНО ЗАГРУЖЕНЫ!")