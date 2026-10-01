"""Индикаторы для графика (только отображение, к торговой логике не привязано).

Supertrend / ADX / HMA — готовые функции pandas-ta-classic.
VWAP (сброс каждый день, UTC) и Envelope (SMA ± %) — простая арифметика,
готовых аналогов, совпадающих с биржевыми, в библиотеках нет.
Каждый индикатор считается изолированно: если функции нет или она упала —
этот индикатор просто не приходит, остальные работают.
"""
import math

import numpy as np

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None
try:
    import pandas_ta_classic as ta
except Exception:  # pragma: no cover
    ta = None

ST_LEN, ST_MULT = 14, 3.0
ADX_LEN = 14
HMA_LEN = 21
ENV_LEN, ENV_PCT = 20, 1.4


def _arr(x):
    """Series/список/None -> numpy float-массив (None -> пустой массив)."""
    if x is None:
        return np.array([], dtype=float)
    return np.asarray(x, dtype=float)


def _pts(times, values, keep=None, gaps=False):
    """gaps=True: вместо пропущенных точек — пустая точка {time}, чтобы линия рвалась."""
    out = []
    for i in range(min(len(times), len(values))):
        v = float(values[i])
        bad = math.isnan(v) or (keep is not None and not bool(keep[i]))
        if bad:
            if gaps:
                out.append({"time": times[i]})
            continue
        out.append({"time": times[i], "value": v})
    return out


def compute_indicators(candles):
    """candles: список dict {time, open, high, low, close, volume} по возрастанию времени."""
    res = {}
    if pd is None or not candles or len(candles) < 30:
        return res
    times = [c["time"] for c in candles]
    secs = np.array([t / 1000 if t > 9999999999 else t for t in times], dtype=float)
    high = pd.Series([float(c["high"]) for c in candles])
    low = pd.Series([float(c["low"]) for c in candles])
    close = pd.Series([float(c["close"]) for c in candles])
    vol = np.array([float(c.get("volume") or 0.0) for c in candles], dtype=float)

    if ta is not None:
        try:
            st = ta.supertrend(high, low, close, length=ST_LEN, multiplier=ST_MULT)
            if st is not None:
                line = _arr(st[f"SUPERT_{ST_LEN}_{ST_MULT}"])
                dirs = _arr(st[f"SUPERTd_{ST_LEN}_{ST_MULT}"])
                res["supertrend_up"] = _pts(times, line, dirs == 1, gaps=True)
                res["supertrend_dn"] = _pts(times, line, dirs == -1, gaps=True)
        except Exception:
            pass
        try:
            adx = ta.adx(high, low, close, length=ADX_LEN)
            if adx is not None:
                res["adx"] = _pts(times, _arr(adx[f"ADX_{ADX_LEN}"]))
        except Exception:
            pass
        try:
            hma = ta.hma(close, length=HMA_LEN)
            if hma is not None:
                res["hma"] = _pts(times, _arr(hma))
        except Exception:
            pass

    try:
        tp = (_arr(high) + _arr(low) + _arr(close)) / 3.0
        day = (secs // 86400).astype(np.int64)
        pv_cum = np.zeros(len(tp))
        v_cum = np.zeros(len(tp))
        vwap = np.full(len(tp), np.nan)
        prev_day = None
        pv = 0.0
        vv = 0.0
        for i in range(len(tp)):
            if day[i] != prev_day:  # новый день: сброс, первая свеча дня — разрыв линии
                pv, vv, prev_day = 0.0, 0.0, day[i]
                pv += tp[i] * vol[i]
                vv += vol[i]
                continue
            pv += tp[i] * vol[i]
            vv += vol[i]
            if vv > 0:
                vwap[i] = pv / vv
        res["vwap"] = _pts(times, vwap, gaps=True)
    except Exception:
        pass

    try:
        sma = _arr(close.rolling(ENV_LEN).mean())
        res["env_up"] = _pts(times, sma * (1 + ENV_PCT / 100))
        res["env_lo"] = _pts(times, sma * (1 - ENV_PCT / 100))
    except Exception:
        pass
    return res