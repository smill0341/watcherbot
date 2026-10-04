import pandas as pd


def calculate_ema(df, period=50, source_col="close"):
    """EMA по колонке source_col (по умолчанию 'close', в нижнем регистре —
    так же, как называются колонки в df внутри watcher_plan.py). Нужна
    для V_RED_TOP (фильтр 'уровень должен быть выше EMA')."""
    return df[source_col].ewm(span=period, adjust=False).mean()


def calculate_atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_cp = (df["high"] - df["close"].shift()).abs()
    low_cp = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = ema_up / ema_down
    return 100 - (100 / (1 + rs))