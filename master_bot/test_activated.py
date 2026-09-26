# -*- coding: utf-8 -*-
"""
test_macro_atr_cap.py — берёт РЕАЛЬНЫЙ вывод lb._extract_macro_swings()
(настоящая боевая функция, без переделок) и показывает, как выглядели бы
эти же самые зоны, если ограничить их размах не процентом от цены (как
сейчас compress_fat_zones), а множителем ATR(1d) — для нескольких вариантов
множителя разом, чтобы подобрать разумный на реальных цифрах, а не на глаз.

Направление зоны (от тени в сторону тела) сохраняется как есть — capped_span
просто ограничивает, СКОЛЬКО она может пройти в эту сторону, вместо жёсткого
процента от цены.

НИЧЕГО не пишет на диск, не меняет levels_builder.py/swing_hunter.py.

Запуск:
    python test_macro_atr_cap.py AERO DASH
    python test_macro_atr_cap.py AERO DASH BTC ETH SOL
"""
import sys
import pandas as pd

from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.common import resolve_symbol
from modules.cryptano.utils import levels_builder as lb

DEFAULT_COINS = ["zen"]
K_CANDIDATES = [1.5, 3.0, 5.0, 8.0]  # во сколько раз ATR(1d) — варианты для сравнения


def main():
    coins = sys.argv[1:] or DEFAULT_COINS

    if not exchange.markets:
        exchange.load_markets(reload=False)
    markets = exchange.markets or {}

    for coin in coins:
        print(f"\n{'=' * 100}\n{coin}\n{'=' * 100}")
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            print(f"  нет рынка для {coin}")
            continue

        try:
            ohlcv_1M = exchange.fetch_ohlcv(symbol, timeframe="1M", limit=60)
            ohlcv_1d = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=365)
        except Exception as e:
            print(f"  ошибка загрузки: {e}")
            continue

        df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if len(df_1M) < 5 or len(df_1d) < 20:
            print("  мало данных, пропускаю")
            continue

        current_idx = len(df_1d) - 1
        current_price = float(df_1d['close'].iloc[current_idx])
        atr_1d = lb.calculate_atr(df_1d, 14).iloc[current_idx]
        if pd.isna(atr_1d) or atr_1d == 0:
            atr_1d = current_price * 0.05
        max_distance = lb._calc_weekly_atr(df_1d, current_idx) * lb.ATR_DISTANCE_MULTIPLIER * 2.5

        print(f"  current_price={current_price:.6g}  atr_1d={atr_1d:.6g}\n")

        # РЕАЛЬНЫЙ вызов боевой функции — сырые кандидаты, без единой правки
        raw = lb._extract_macro_swings(df_1M, current_price, max_distance, 5.0, "1M_MACRO")

        if not raw:
            print("  боевая _extract_macro_swings ничего не нашла для этой монеты")
            continue

        header = f"  {'дата':<12} {'сторона':<10} {'raw ширина%':>12}"
        for k in K_CANDIDATES:
            header += f"   K={k:<4}"
        print(header)

        for z in raw:
            is_sup = z['_is_support']
            side = "SUPPORT" if is_sup else "RESISTANCE"
            date_str = z['date']
            raw_min, raw_max = z['min'], z['max']
            raw_span = raw_max - raw_min
            raw_width_pct = raw_span / ((raw_min + raw_max) / 2) * 100

            line = f"  {date_str:<12} {side:<10} {raw_width_pct:>11.2f}%"
            for k in K_CANDIDATES:
                cap = atr_1d * k
                capped_span = min(raw_span, cap)
                capped_width_pct = capped_span / ((raw_min + raw_max) / 2) * 100 if (raw_min + raw_max) else 0
                hit = "*" if capped_span < raw_span else " "
                line += f"   {capped_width_pct:>5.2f}%{hit}"
            print(line)

        print("\n  (* = в этом варианте K реальный размах обрезан до предела ATR*K)")


if __name__ == "__main__":
    main()