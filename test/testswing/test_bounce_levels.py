"""
test_bounce_levels_mae_mfe.py
==============================
Без стопов. Вместо гонки стоп/тейк — просто честно смотрим, что случилось
после касания уровня за ФИКСИРОВАННЫЙ срок (по умолчанию 14 дней — грубо
"пара недель", можно поменять HORIZON_DAYS):

- MFE% (Max Favorable Excursion) — максимальный ход В НАШУ СТОРОНУ за этот
  срок. "Насколько выросло/упало после касания, в лучший момент".
- MAE% (Max Adverse Excursion)   — максимальный ход ПРОТИВ нас за тот же
  срок. "Насколько было больно пересидеть, прежде чем пошло в нужную сторону
  (если вообще пошло)".

Уровень (свинг) по-прежнему ищем на недельном/месячном графике — это то,
ЧТО считается уровнем. А вот РЕАКЦИЮ на касание меряем на ДНЕВНЫХ свечах —
иначе "срок в 14 дней" на недельном графике это грубо 2 бара, слишком мало
точек для честной картины движения внутри этого окна.

Как и раньше: не пересекающиеся сделки (пока не истёк горизонт предыдущей
сделки по тому же направлению — новую не открываем), и то же самое честное
предупреждение про survivorship bias (топ-N по обороту — сегодняшний, не
point-in-time), см. соответствующий докстринг в test_bounce_levels_honest.py.

Использование:
    python3 test_bounce_levels_mae_mfe.py
"""
import ccxt
import pandas as pd
import numpy as np
import time

# ==========================================
# НАСТРОЙКИ ТЕСТА
# ==========================================
MIN_VOLUME_USD = 5_000_000
TOP_COINS = 150
TIMEFRAMES = {
    '1w': 150,
    '1M': 60,
}
SWING_WINDOW = 5

HORIZON_DAYS = 14        # срок замера реакции после касания — ДНИ, не бары исходного ТФ
DAILY_HISTORY_LIMIT = 1000  # сколько дневных свечей тянуть на монету (~2.7 года)
# ==========================================

exchange = ccxt.bybit({'enableRateLimit': True})


def get_top_markets():
    print("⏳ Запрашиваем тикеры с Bybit...")
    tickers = exchange.fetch_tickers()
    markets = []
    for sym, tick in tickers.items():
        if sym.endswith(':USDT'):
            vol = float(tick.get('quoteVolume') or 0)
            if vol >= MIN_VOLUME_USD:
                markets.append((sym, vol))

    markets.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [m[0] for m in markets[:TOP_COINS]]
    print(f"✅ Найдено ликвидных монет. Тестируем Топ-{TOP_COINS} "
          f"(⚠️ сегодняшний топ по обороту, не point-in-time)")
    return top_symbols


def fetch_ohlcv(symbol, tf, limit):
    for attempt in range(3):
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception:
            time.sleep(2)
    return pd.DataFrame()


def _find_swings(df):
    df = df.copy()
    df['is_swing_low'] = (df['low'] == df['low'].rolling(window=SWING_WINDOW, center=True).min())
    df['is_swing_high'] = (df['high'] == df['high'].rolling(window=SWING_WINDOW, center=True).max())
    return df


def _find_touch(df, i, level, direction):
    """Первое касание уровня ПОСЛЕ того, как свинг закрылся (i+3, как в исходном тесте)."""
    for j in range(i + 3, len(df)):
        if direction == 'LONG' and df['low'].iloc[j] <= level:
            return j
        if direction == 'SHORT' and df['high'].iloc[j] >= level:
            return j
    return None


def _mae_mfe_on_daily(df_daily, touch_time, entry, direction, horizon_days):
    """
    Ищет ближайшую дневную свечу к моменту касания, смотрит на horizon_days
    вперёд, считает MFE%/MAE% относительно entry. Возвращает (mfe_pct, mae_pct, exit_time)
    или None, если дневных данных после касания не хватает на весь горизонт
    (свежее касание у самого края доступной истории — отбрасываем, не режем горизонт втихую).
    """
    start_idx = df_daily['timestamp'].searchsorted(touch_time)
    if start_idx >= len(df_daily):
        return None
    end_idx = start_idx + horizon_days
    if end_idx > len(df_daily):
        return None  # не хватает дневных данных на полный горизонт — не подмешиваем неполные окна

    window = df_daily.iloc[start_idx:end_idx]
    if window.empty:
        return None

    if direction == 'LONG':
        mfe_pct = (window['high'].max() - entry) / entry * 100
        mae_pct = (entry - window['low'].min()) / entry * 100
    else:
        mfe_pct = (entry - window['low'].min()) / entry * 100
        mae_pct = (window['high'].max() - entry) / entry * 100

    return mfe_pct, mae_pct, window['timestamp'].iloc[-1]


def run_backtest(df, df_daily, symbol, tf_name):
    if len(df) < SWING_WINDOW or df_daily.empty:
        return []

    df = _find_swings(df)
    results = []
    busy_until_time = {'LONG': None, 'SHORT': None}

    for i in range(2, len(df) - 2):
        for direction, is_swing_col in (('LONG', 'is_swing_low'), ('SHORT', 'is_swing_high')):
            if not df[is_swing_col].iloc[i]:
                continue

            level = df['low'].iloc[i] if direction == 'LONG' else df['high'].iloc[i]
            touch_idx = _find_touch(df, i, level, direction)
            if touch_idx is None:
                continue

            touch_time = df['timestamp'].iloc[touch_idx]
            if busy_until_time[direction] is not None and touch_time <= busy_until_time[direction]:
                continue  # предыдущая сделка этого направления ещё "внутри горизонта"

            outcome = _mae_mfe_on_daily(df_daily, touch_time, level, direction, HORIZON_DAYS)
            if outcome is None:
                continue
            mfe_pct, mae_pct, exit_time = outcome

            results.append({
                'symbol': symbol, 'tf': tf_name, 'type': direction,
                'level': level, 'mfe_pct': mfe_pct, 'mae_pct': mae_pct,
            })
            busy_until_time[direction] = exit_time

    return results


def main():
    symbols = get_top_markets()
    all_trades = []

    for idx, symbol in enumerate(symbols):
        print(f"[{idx + 1}/{len(symbols)}] Сканируем {symbol}...")
        df_daily = fetch_ohlcv(symbol, '1d', DAILY_HISTORY_LIMIT)
        time.sleep(0.1)
        if df_daily.empty:
            continue
        for tf, limit in TIMEFRAMES.items():
            df = fetch_ohlcv(symbol, tf, limit)
            trades = run_backtest(df, df_daily, symbol, tf)
            all_trades.extend(trades)
            time.sleep(0.1)

    df_res = pd.DataFrame(all_trades)

    if df_res.empty:
        print("Сделок не найдено.")
        return

    print("\n" + "=" * 60)
    print(f"📊 РЕЗУЛЬТАТ БЕЗ СТОПОВ: касание уровня → MFE/MAE за {HORIZON_DAYS} дней")
    print("=" * 60)

    for tf in TIMEFRAMES.keys():
        tf_df = df_res[df_res['tf'] == tf]
        if tf_df.empty:
            continue

        total = len(tf_df)
        n_symbols = tf_df['symbol'].nunique()

        print(f"\n⚡ ТАЙМФРЕЙМ: {tf.upper()}")
        print(f"Всего касаний (не пересекающихся): {total}  |  Монет в выборке: {n_symbols}")
        print(f"MFE (движение В нашу сторону): среднее {tf_df['mfe_pct'].mean():.2f}%, "
              f"медиана {tf_df['mfe_pct'].median():.2f}%")
        print(f"MAE (просадка ПРОТИВ нас):     среднее {tf_df['mae_pct'].mean():.2f}%, "
              f"медиана {tf_df['mae_pct'].median():.2f}%")
        print("-" * 30)
        for pct in (3, 5, 10, 20):
            share = (tf_df['mfe_pct'] >= pct).mean() * 100
            print(f"Дали движение в нашу сторону >{pct}%:  {share:.1f}% касаний")
        print("-" * 30)
        for pct in (3, 5, 10, 20):
            share = (tf_df['mae_pct'] >= pct).mean() * 100
            print(f"Просадка против нас была >{pct}%:  {share:.1f}% касаний")

        top_contributors = tf_df['symbol'].value_counts().head(5)
        print("Топ-5 монет по числу сделок (проверка на перекос):")
        for sym, cnt in top_contributors.items():
            print(f"   {sym}: {cnt} сделок ({cnt / total * 100:.1f}% выборки)")


if __name__ == "__main__":
    main()