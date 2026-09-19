"""
Быстрая проверка новой логики activated_at на реальных данных монеты,
без запуска всего build_macro_levels. Запускать из корня master_bot:

    python -m modules.cryptano.utils.test_activated_at BTC
"""
import sys
import pandas as pd

from modules.cryptano.utils.common import exchange
from modules.cryptano.utils.levels_builder import build_levels


def fetch(symbol, tf, limit):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    return pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])


def main():
    coin = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    symbol = f"{coin}/USDT:USDT"  # своп, не спот — так же, как в build_macro_levels

    df_1M = fetch(symbol, "1M", 60)
    df_1W = fetch(symbol, "1W", 150)
    df_1d = fetch(symbol, "1d", 365)
    df_4h = fetch(symbol, "4h", 200)

    levels = build_levels(df_1M, df_1W, df_1d, df_4h, coin)

    print(f"\n=== {coin} ===")
    for side, items in (("SUPPORT", levels["supports"]), ("RESIST ", levels["resistances"])):
        for z in items:
            print(f"  [{side}] date={z.get('date'):>10}  activated_at={z.get('activated_at'):>10}  "
                  f"type={z.get('type')[:50]}")


if __name__ == "__main__":
    main()