import json
import pandas as pd

with open('levels_timeline.json') as f:
    timeline = json.load(f)

keys = list(timeline.keys())
print('Pervye 3 klyucha v timeline:', keys[:3])
print('Poslednie 3 klyucha:', keys[-3:])
print('Vsego klyuchey:', len(keys))

TEST_START_DATE = '2026-04-01 00:00:00'
period_key = pd.to_datetime(TEST_START_DATE).floor('12h').strftime('%Y-%m-%d %H:%M:%S')
print('Test ischet klyuch:', period_key)
print('Etot klyuch est v timeline?', period_key in timeline)

if period_key in timeline:
    coin_data = timeline[period_key].get('APT', {})
    print('Dannye APT na etot moment:', coin_data)
    all_coins = list(timeline[period_key].keys())
    print('Vse monety v etom periode:', all_coins)
    print('Skolko vsego monet:', len(all_coins))
