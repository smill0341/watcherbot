import pandas as pd
import numpy as np
import time
from modules.cryptano.utils.common import calculate_rsi
from modules.cryptano.utils.price_action import get_market_structure
from modules.cryptano.utils.common import format_price as fmt_p

# ================= НАСТРОЙКИ ИНДИКАТОРОВ =================
VOL_LIMIT = 2.5        
RSI_LOW = 30           
RSI_HIGH = 70          
LEVELS_PERIOD = 20     
CHANNEL_PERIOD = 40    
MA_PERIOD = 30         
# ======================================================

# Внутренняя функция для расчета уровней 
def pandas_get_local_structure(df, lookback=15):
    context_df = df.iloc[-lookback-1:-1]
    swing_low = float(context_df['low'].min())
    swing_high = float(context_df['high'].max())
    return swing_high, swing_low

def calculate_atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_cp = (df["high"] - df["close"].shift()).abs()
    low_cp = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calculate_pivot_points(df):
    last_row = df.iloc[-2]
    pivot = (last_row["high"] + last_row["low"] + last_row["close"]) / 3
    return pivot, (2 * pivot) - last_row["low"], (2 * pivot) - last_row["high"]

# =========================================================================
# СТАРЫЕ ФУНКЦИИ ДЛЯ LIVE_SCAN, LIGHT_FILTER И CRITICAL_FILTER (НЕ ТРОГАЕМ)
# =========================================================================
def get_market_state(df, current_price, channel_lookback=120):
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    last_row = df.iloc[-1]
    ema9 = float(last_row["ema9"])
    ema21 = float(last_row["ema21"])
    ema200 = float(last_row["ema200"])

    recent_volume = df["volume"].iloc[-1]
    avg_volume = df["volume"].iloc[-25:-5].mean()
    
    tf_ms = df["timestamp"].iloc[-1] - df["timestamp"].iloc[-2]
    tf_minutes = tf_ms / 60000.0 if tf_ms > 0 else 60.0
    
    elapsed_ms = (time.time() * 1000) - df["timestamp"].iloc[-1]
    elapsed_minutes = elapsed_ms / 60000.0
    if elapsed_minutes <= 0: elapsed_minutes = 1.0
        
    expected_progress = min(elapsed_minutes / tf_minutes, 1.0)
    adjusted_ratio = float(recent_volume / (avg_volume * expected_progress) if avg_volume > 0 else 1.0)
    vol_ratio = adjusted_ratio

    recent_channel = df.tail(channel_lookback)
    strong_resistance = float(recent_channel["high"].max())
    strong_support = float(recent_channel["low"].min())
    range_size = strong_resistance - strong_support
    
    pos_pct = 50.0 if range_size == 0 else ((current_price - strong_support) / range_size) * 100

    recent_short = df.tail(20)
    nearest_support = float(recent_short["low"].min())
    nearest_resistance = float(recent_short["high"].max())

    structure = get_market_structure(df, lookback=120)
    bull_bias = current_price > ema200
    bear_bias = current_price < ema200
    local_up = ema9 > ema21
    local_down = ema9 < ema21

    if bull_bias and (local_up or structure == 'bullish'):
        trend_code, trend_text = "BULL", "📈 Глобальный Бычий"
    elif bear_bias and (local_down or structure == 'bearish'):
        trend_code, trend_text = "BEAR", "📉 Глобальный Медвежий"
    else:
        dist_to_ema200 = abs(current_price - ema200) / ema200
        if dist_to_ema200 < 0.02:
            trend_code, trend_text = "RANGE", "📊 Боковик"
        elif bull_bias:
            trend_code, trend_text = "BULL", "📈 Глобальный Бычий"
        else:
            trend_code, trend_text = "BEAR", "📉 Глобальный Медвежий"

    return {
        "trend": trend_text, "trend_code": trend_code, "pos_pct": pos_pct,
        "vol_ratio": vol_ratio, "ema9": ema9, "ema21": ema21, "ema200": ema200,
        "ma30": ema21, "strong_support": strong_support, "strong_resistance": strong_resistance,
        "nearest_support": nearest_support, "nearest_resistance": nearest_resistance
    }

def get_cryptano_signal(df, current_price, price_precision, scan_type, rsi_high=75, rsi_low=25, volume_multiplier=4.0):
    df["rsi"] = calculate_rsi(df)
    last_row = df.iloc[-1]
    rsi = float(last_row["rsi"])

    recent_closed_vols = df["volume"].iloc[-6:-1]
    max_vol = float(recent_closed_vols.max())
    avg_volume = float(df["volume"].iloc[-30:-6].mean())
    vol_ratio = float(max_vol / avg_volume if avg_volume > 0 else 1.0)
    
    current_vol = float(df["volume"].iloc[-1])
    is_volume_fading = current_vol < max_vol

    is_rsi_high_trigger = (scan_type == "rsi_high" and rsi >= rsi_high)
    is_short_pump_trigger = ((scan_type in ["auto", "volume"] and rsi >= rsi_high and vol_ratio >= volume_multiplier and is_volume_fading) or is_rsi_high_trigger)

    if is_short_pump_trigger:
        impulse_low = float(df.tail(15)["low"].min())
        impulse_high = float(df.tail(15)["high"].max()) 
        wave_size = impulse_high - impulse_low
        stop_loss = impulse_high * 1.03 
        return {
            "type": "SHORT_PUMP", "price": current_price, "rsi": rsi, "vol_ratio": vol_ratio,
            "entry_market": current_price, "take_profit": impulse_high - (wave_size * 0.236),
            "take_profit_2": impulse_high - (wave_size * 0.382), "take_profit_3": impulse_high - (wave_size * 0.5),
            "stop_loss": stop_loss,
        }

    is_rsi_trigger = (scan_type in ["rsi", "rsi_low"] and rsi <= rsi_low)
    is_vol_trigger = (scan_type == "volume" and vol_ratio >= volume_multiplier and is_volume_fading)
    is_auto_trigger = (scan_type == "auto" and rsi <= rsi_low and vol_ratio >= volume_multiplier and is_volume_fading)

    if is_rsi_trigger or is_vol_trigger or is_auto_trigger:
        impulse_high = float(df.tail(15)["high"].max())
        impulse_low = float(df.tail(15)["low"].min()) 
        wave_size = impulse_high - impulse_low
        fib_0618 = impulse_low + (wave_size * 0.618)
        stop_loss = impulse_low * 0.97 
        return {
            "type": "LONG_ROLLBACK", "price": current_price, "rsi": rsi, "vol_ratio": vol_ratio,
            "entry_limit": fib_0618 if fib_0618 < current_price else current_price,
            "take_profit": impulse_low + (wave_size * 0.236), "take_profit_2": impulse_low + (wave_size * 0.382),
            "take_profit_3": impulse_low + (wave_size * 0.5), "stop_loss": stop_loss,
        }
    return None

