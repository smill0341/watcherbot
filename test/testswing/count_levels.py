# -*- coding: utf-8 -*-
"""
count_levels.py
=================
Счётчик уровней за ПЕРИОД — без бэктеста, без вотчеров, без сделок.
Вызывает build_levels() на каждом 12-часовом шаге внутри заданного периода
(та же частота, с которой уровни обновляются у настоящего бота — см.
TIME_ASIAN_CLOSE/TIME_US_OPEN в swing_hunter.py) и печатает/сохраняет
реальные min/max каждой зоны на каждом шаге.

Цель — увидеть дрожание координат одного и того же реального уровня между
соседними пересчётами (тот самый источник дублей), не гоняя весь тестер и
не сверяя числа по логам вручную.

Результат:
  - в консоль: краткая сводка по каждой дате (сколько уровней, где именно)
  - в CSV (count_levels_out.csv): полная таблица — по строке на каждую
    зону на каждую дату, с реальными min/max. Открываете в Excel,
    сортируете по min — почти одинаковые зоны на соседних датах лягут
    рядом, дрожание видно сразу.

Запуск: правите CONFIG ниже, затем python count_levels.py
"""

import os
import sys
import time
import datetime
import csv

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

MASTER_BOT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../master_bot"))
if MASTER_BOT_ROOT not in sys.path:
    sys.path.insert(0, MASTER_BOT_ROOT)
TEST_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../"))
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

import pandas as pd
from modules.cryptano.utils.crypto_utils import exchange
from testswing.levels_builder import build_levels, compress_fat_zones, CONFLUENCE_BONUS, MIN_SCORE_MONTH_WEEK_CONFLUENCE

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
CONFIG = {
    'COINS': ['UNI'],                     # список монет
    'DATE_START': "2026-07-01 00:00:00",  # начало периода
    'DATE_END': "2026-07-31 00:00:00",    # конец периода
    'STEP_HOURS': 12,                     # шаг пересчёта — 12ч = как у настоящего бота
    'OUTPUT_CSV': "count_levels_out.csv",
    # Допуск на слияние БЛИЗКИХ (не обязательно физически пересекающихся) зон,
    # % от цены. Обычный merge_overlapping_zones внутри build_levels сливает
    # зоны только если они пересекаются — если между ними есть зазор, они так
    # и остаются двумя отдельными записями, даже если это дрожание одного и
    # того же уровня между соседними 12ч-пересчётами. Этот скрипт считает
    # ВТОРОЙ, отдельный проход слияния поверх обычного результата — просто
    # чтобы на цифрах увидеть, насколько сильно это сократило бы число зон,
    # ничего не меняя в боевом levels_builder.py.
    'ZONE_MERGE_DISTANCE_PCT': 4.0,
}


def merge_by_distance(zones, tolerance_pct):
    """Второй проход слияния ПОВЕРХ уже готовых зон (после обычного
    merge_overlapping_zones внутри build_levels). Та же логика подсчёта
    score, что и в levels_builder.merge_overlapping_zones — но условие
    слияния ослаблено: не только 'пересекается', а 'зазор между зонами
    меньше tolerance_pct% от цены соседней зоны'."""
    if not zones:
        return []

    sorted_zones = sorted(zones, key=lambda x: x['min'])

    for z in sorted_zones:
        z['base_score'] = z.get('score', 0.0)
        z['_has_peak'] = 'extreme_peak' in z.get('type', '')
        z['_has_calendar'] = any(t in z.get('type', '') for t in ('PMH', 'PML', 'PWH', 'PWL'))
        z['_has_month_cal'] = any(t in z.get('type', '') for t in ('PMH', 'PML'))
        z['_has_week_cal'] = any(t in z.get('type', '') for t in ('PWH', 'PWL'))

    merged = [sorted_zones[0]]

    for current in sorted_zones[1:]:
        last = merged[-1]
        gap_tolerance = last['max'] * (tolerance_pct / 100.0)
        if current['min'] <= last['max'] + gap_tolerance:
            last['max'] = max(last['max'], current['max'])
            last['base_score'] = max(last['base_score'], current['base_score'])
            last['_has_peak'] = last['_has_peak'] or current['_has_peak']
            last['_has_calendar'] = last['_has_calendar'] or current['_has_calendar']
            last['_has_month_cal'] = last['_has_month_cal'] or current['_has_month_cal']
            last['_has_week_cal'] = last['_has_week_cal'] or current['_has_week_cal']

            confluence = CONFLUENCE_BONUS if (last['_has_peak'] and last['_has_calendar']) else 0.0
            last['score'] = round(last['base_score'] + confluence, 2)

            if last['_has_month_cal'] and last['_has_week_cal']:
                last['score'] = max(last['score'], MIN_SCORE_MONTH_WEEK_CONFLUENCE)

            if current.get('type') and last.get('type') and current['type'] not in last['type']:
                last['type'] = f"{last['type']} + {current['type']}"
            last['reaction_count'] = max(last.get('reaction_count', 0), current.get('reaction_count', 0))
        else:
            merged.append(current)

    for m in merged:
        m.pop('base_score', None)
        m.pop('_has_peak', None)
        m.pop('_has_calendar', None)
        m.pop('_has_month_cal', None)
        m.pop('_has_week_cal', None)

    return merged


# =====================================================================
# СБОР ДАННЫХ (тот же паттерн, что в swing_hunter.py)
# =====================================================================
def safe_fetch_ohlcv(sym, tf, lim, params):
    for attempt in range(5):
        try:
            return exchange.fetch_ohlcv(sym, timeframe=tf, limit=lim, params=params)
        except Exception as e:
            if "10006" in str(e) or "Rate Limit" in str(e) or "Too many visits" in str(e):
                print(f"   ⚠️ Rate limit — жду 4 сек (попытка {attempt + 1}/5)...")
                time.sleep(4.0)
            else:
                raise e
    raise Exception("Биржа заблокировала запросы после 5 попыток.")


def _resolve_symbol(coin):
    if not exchange.markets:
        exchange.load_markets()
    symbol_perp = f"{coin.upper()}/USDT:USDT"
    symbol_spot = f"{coin.upper()}/USDT"
    return symbol_perp if symbol_perp in exchange.markets else symbol_spot # type: ignore


def _fetch_levels_at(symbol, coin, dt):
    fetch_params = {'endTime': int(dt.timestamp() * 1000)}

    ohlcv_1M = safe_fetch_ohlcv(symbol, "1M", 60, fetch_params)
    ohlcv_1W = safe_fetch_ohlcv(symbol, "1W", 150, fetch_params)
    ohlcv_1d = safe_fetch_ohlcv(symbol, "1d", 365, fetch_params)
    ohlcv_4h = safe_fetch_ohlcv(symbol, "4h", 200, fetch_params)

    if len(ohlcv_1d) < 50:
        return None

    df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1M) >= 5 else None
    df_1W = pd.DataFrame(ohlcv_1W, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1W) >= 5 else None
    df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_4h) >= 50 else None

    return build_levels(df_1M, df_1W, df_1d, df_4h, coin)


# =====================================================================
# ОСНОВНОЙ ПРОГОН
# =====================================================================
def run():
    date_start = datetime.datetime.strptime(CONFIG['DATE_START'], "%Y-%m-%d %H:%M:%S")
    date_end = datetime.datetime.strptime(CONFIG['DATE_END'], "%Y-%m-%d %H:%M:%S")
    step = datetime.timedelta(hours=CONFIG['STEP_HOURS'])

    steps = []
    dt = date_start
    while dt <= date_end:
        steps.append(dt)
        dt += step

    print(f"Период: {date_start} .. {date_end}, шаг {CONFIG['STEP_HOURS']}ч ({len(steps)} точек на монету)")

    rows = []  # для CSV: одна строка на (дата, монета, сторона, зона)

    for coin in CONFIG['COINS']:
        symbol = _resolve_symbol(coin)
        print(f"\n=== {coin} ({symbol}) ===")

        for dt in steps:
            try:
                levels = _fetch_levels_at(symbol, coin, dt)
            except Exception as e:
                print(f"  {dt}: ❌ Ошибка: {e}")
                continue

            if levels is None:
                print(f"  {dt}: недостаточно данных — пропуск")
                continue

            supports_before = levels.get("supports", [])
            resistances_before = levels.get("resistances", [])

            tolerance = CONFIG['ZONE_MERGE_DISTANCE_PCT']
            supports_after = compress_fat_zones(
                merge_by_distance([dict(z) for z in supports_before], tolerance), coin
            )
            resistances_after = compress_fat_zones(
                merge_by_distance([dict(z) for z in resistances_before], tolerance), coin
            )

            # Краткая консольная сводка — только цифры, отсортировано по цене,
            # плюс было/стало после доп. слияния по расстоянию.
            sup_str = ", ".join(f"{z['min']:.4f}-{z['max']:.4f}[{z.get('type','?')}]" for z in sorted(supports_before, key=lambda z: z['min']))
            res_str = ", ".join(f"{z['min']:.4f}-{z['max']:.4f}[{z.get('type','?')}]" for z in sorted(resistances_before, key=lambda z: z['min']))
            print(f"  {dt} | supports({len(supports_before)}->{len(supports_after)}): {sup_str or '-'}")
            print(f"  {' ' * len(str(dt))} | resistances({len(resistances_before)}->{len(resistances_after)}): {res_str or '-'}")

            for stage, supports, resistances in (('before', supports_before, resistances_before),
                                                  ('after', supports_after, resistances_after)):
                for z in supports:
                    rows.append({'date': dt, 'coin': coin, 'stage': stage, 'side': 'support',
                                 'min': z['min'], 'max': z['max'],
                                 'mid': (z['min'] + z['max']) / 2,
                                 'score': z.get('score', 0), 'type': z.get('type', '?'),
                                 'class': z.get('class', 'regular')})
                for z in resistances:
                    rows.append({'date': dt, 'coin': coin, 'stage': stage, 'side': 'resistance',
                                 'min': z['min'], 'max': z['max'],
                                 'mid': (z['min'] + z['max']) / 2,
                                 'score': z.get('score', 0), 'type': z.get('type', '?'),
                                 'class': z.get('class', 'regular')})

            time.sleep(0.3)  # чтобы не улететь в rate limit

    if rows:
        out_path = os.path.join(CURRENT_DIR, CONFIG['OUTPUT_CSV'])
        with open(out_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['date', 'coin', 'stage', 'side', 'min', 'max', 'mid', 'score', 'type', 'class'])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nСохранено: {out_path} ({len(rows)} строк)")
        print(f"Допуск слияния ZONE_MERGE_DISTANCE_PCT = {CONFIG['ZONE_MERGE_DISTANCE_PCT']}%")
        print("Колонка 'stage': 'before' — как сейчас у бота, 'after' — с доп. слиянием близких зон.")
        print("Откройте в Excel, отсортируйте по 'coin' -> 'side' -> 'stage' -> 'mid' — "
              "видно и дрожание между датами, и разницу before/after сразу.")

    print("\nГотово.")


if __name__ == "__main__":
    run()