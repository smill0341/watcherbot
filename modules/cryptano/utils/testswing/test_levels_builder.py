"""
test_levels_builder.py
=======================
Отдельный тестовый скрипт. НЕ трогает боевую логику.
Качает реальные 1D и 4H данные с биржи через существующий exchange,
прогоняет через build_levels() из levels_builder.py и печатает результат.

Запуск:
    python modules/cryptano/utils/testswing/test_levels_builder.py
    python modules/cryptano/utils/testswing/test_levels_builder.py NEAR
    python modules/cryptano/utils/testswing/test_levels_builder.py BTC ETH SOL
"""

import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import json
import pandas as pd

from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.testswing.levels_builder import build_levels


def fetch_df(symbol, timeframe, limit):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return df


def run_for_coin(coin):
    symbol = f"{coin}/USDT"
    print(f"\n{'=' * 60}")
    print(f"  {coin}")
    print(f"{'=' * 60}")

    try:
        print("Качаю 1D (limit=365)...")
        df_1d = fetch_df(symbol, "1d", 365)
        print(f"  -> получено {len(df_1d)} дневных свечей, "
              f"с {pd.to_datetime(df_1d['timestamp'].iloc[0], unit='ms').date()} "
              f"по {pd.to_datetime(df_1d['timestamp'].iloc[-1], unit='ms').date()}")

        print("Качаю 4H (limit=200)...")
        df_4h = fetch_df(symbol, "4h", 200)
        print(f"  -> получено {len(df_4h)} 4H свечей")

        current_price = float(df_1d['close'].iloc[-1])
        print(f"Текущая цена (close последней дневной свечи): {current_price}")

        if len(df_1d) < 50:
            print("⚠️  Менее 50 дневных свечей - результат может быть пустым, это нормально для новых монет.")

        result = build_levels(df_1d, df_4h, coin)

        print("\n--- РЕЗУЛЬТАТ ---")
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))

        n_total = len(result["supports"]) + len(result["resistances"])
        print(f"\nИтого: {n_total} зон (supports={len(result['supports'])}, resistances={len(result['resistances'])})")

    except Exception as e:
        import traceback
        print(f"❌ ОШИБКА для {coin}: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    coins = sys.argv[1:] if len(sys.argv) > 1 else ["NEAR"]
    for c in coins:
        run_for_coin(c.upper())