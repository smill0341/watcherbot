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
from testswing.levels_builder import build_levels

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
CONFIG = {
    'COINS': ['XRP'],                     # список монет
    'DATE_START': "2026-07-01 00:00:00",  # начало периода
    'DATE_END': "2026-07-31 00:00:00",    # конец периода
    'STEP_HOURS': 12,                     # шаг пересчёта — 12ч = как у настоящего бота
    'OUTPUT_CSV': "count_levels_out.csv",
}


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
    return symbol_perp if symbol_perp in exchange.markets else symbol_spot


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

            supports = levels.get("supports", [])
            resistances = levels.get("resistances", [])

            # Краткая консольная сводка — только цифры, отсортировано по цене
            sup_str = ", ".join(f"{z['min']:.4f}-{z['max']:.4f}[{z.get('type','?')}]" for z in sorted(supports, key=lambda z: z['min']))
            res_str = ", ".join(f"{z['min']:.4f}-{z['max']:.4f}[{z.get('type','?')}]" for z in sorted(resistances, key=lambda z: z['min']))
            print(f"  {dt} | supports({len(supports)}): {sup_str or '-'}")
            print(f"  {' ' * len(str(dt))} | resistances({len(resistances)}): {res_str or '-'}")

            for z in supports:
                rows.append({'date': dt, 'coin': coin, 'side': 'support',
                             'min': z['min'], 'max': z['max'],
                             'mid': (z['min'] + z['max']) / 2,
                             'score': z.get('score', 0), 'type': z.get('type', '?'),
                             'class': z.get('class', 'regular')})
            for z in resistances:
                rows.append({'date': dt, 'coin': coin, 'side': 'resistance',
                             'min': z['min'], 'max': z['max'],
                             'mid': (z['min'] + z['max']) / 2,
                             'score': z.get('score', 0), 'type': z.get('type', '?'),
                             'class': z.get('class', 'regular')})

            time.sleep(0.3)  # чтобы не улететь в rate limit

    if rows:
        out_path = os.path.join(CURRENT_DIR, CONFIG['OUTPUT_CSV'])
        with open(out_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['date', 'coin', 'side', 'min', 'max', 'mid', 'score', 'type', 'class'])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nСохранено: {out_path} ({len(rows)} строк)")
        print("Откройте в Excel, отсортируйте по 'coin' -> 'side' -> 'mid' — "
              "почти одинаковые зоны на соседних датах лягут рядом, дрожание видно сразу.")

    print("\nГотово.")


if __name__ == "__main__":
    run()