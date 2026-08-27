# -*- coding: utf-8 -*-
"""
volume_avg_by_date.py
======================
Просто: монета, дата, типичный объём этого дня. Одна строка = один день.

Типичный объём = медиана объёма за предыдущие WINDOW_DAYS дней (устойчива к
разовым выбросам-монстрам — обычное среднее ими портится, поэтому не оно).

Ничего не меняет в существующем коде бота, только читает свечи из data_cache/
и пишет один CSV.
"""

import os
import glob
import numpy as np
import pandas as pd

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
CONFIG = {
    'COIN': 'AGI',              # тикер, например 'STX', или 'ALL' — все монеты из data_cache/
    'TIMEFRAME': '15m',

    'DATE_START': '2026-08-01',
    'DATE_END':   '2026-08-31',

    'WINDOW_DAYS': 10,          # за сколько дней назад считаем ориентир (пробуйте 7 / 10 / 14 / 30)
    'PERCENTILE': 97,           # 50 = медиана (совсем не видит крупные объёмы).
                                 # 90/95 — учитывает верхний хвост, но не даёт одному
                                 # монстру задрать порог, как это делает max/среднее.
    'GREEN_ONLY': False,        # True = считать только по зелёным свечам (Close > Open)

    'DATA_CACHE_FOLDER': 'data_cache',
    'OUTPUT_FILE': 'volume_avg_by_date.csv',
}


def _find_cached_csv(coin, timeframe, cache_folder):
    matches = glob.glob(os.path.join(cache_folder, f"cache_{coin.lower()}_{timeframe}_*.csv"))
    if not matches:
        matches = glob.glob(os.path.join(cache_folder, f"*{coin.lower()}*{timeframe}*.csv"))
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def compute_avg_by_date(coin, cfg):
    path = _find_cached_csv(coin, cfg['TIMEFRAME'], cfg['DATA_CACHE_FOLDER'])
    if path is None:
        print(f"[{coin}] ❌ кэш не найден в {cfg['DATA_CACHE_FOLDER']}/ — пропуск")
        return []

    df = pd.read_csv(path, index_col=0, parse_dates=True).sort_index()
    if cfg.get('GREEN_ONLY'):
        df = df[df['Close'] > df['Open']]

    rows = []
    for day in pd.date_range(cfg['DATE_START'], cfg['DATE_END'], freq='D'):
        window_start = day - pd.Timedelta(days=cfg['WINDOW_DAYS'])
        window_vol = df.loc[(df.index >= window_start) & (df.index < day), 'Volume']

        if len(window_vol) > 0:
            avg_volume = float(np.percentile(window_vol, cfg['PERCENTILE']))
        else:
            avg_volume = np.nan

        rows.append({
            'coin': coin,
            'date': day.date(),
            'avg_volume': round(avg_volume, 2) if not np.isnan(avg_volume) else '',
        })

    return rows


def main():
    cfg = CONFIG
    all_rows = []

    if cfg['COIN'].upper() == 'ALL':
        pattern = os.path.join(cfg['DATA_CACHE_FOLDER'], f"cache_*_{cfg['TIMEFRAME']}_*.csv")
        coins = sorted({os.path.basename(p).split('_')[1].upper() for p in glob.glob(pattern)})
        if not coins:
            print(f"❌ В {cfg['DATA_CACHE_FOLDER']}/ нет ни одного кэша для {cfg['TIMEFRAME']}.")
            return
    else:
        coins = [cfg['COIN'].upper()]

    for coin in coins:
        all_rows.extend(compute_avg_by_date(coin, cfg))

    if not all_rows:
        print("❌ Ничего не посчитано.")
        return

    out_df = pd.DataFrame(all_rows)
    out_df.to_csv(cfg['OUTPUT_FILE'], index=False)
    print("Готово.")


if __name__ == "__main__":
    main()