import pandas as pd
import numpy as np
import time
from modules.cryptano.utils.crypto_utils import calculate_rsi
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

# =========================================================================
# НОВАЯ ФУНКЦИЯ ИСКЛЮЧИТЕЛЬНО ДЛЯ WATCHER (15M СНАЙПЕР С ОБЪЕМОМ И ТЕНЬЮ)
# =========================================================================
def analyze_extreme_pattern(df, direction, current_price, price_precision, source="Manual", macro_levels=None):
    if macro_levels is None: macro_levels = {}
    is_scalper = source in ["MOMENTUM_PUMP", "MOMENTUM_DUMP"]
    
    if len(df) < 22:
        return {"trigger_fired": False, "rsi_filter_passed": False, "volume_climax": False, "trigger_type": "НЕТ"}
    df = df.copy()
        
    df["atr"] = calculate_atr(df, 14)
    current_candle = df.iloc[-1]
    candle_close = float(current_candle["close"])
    candle_open = float(current_candle["open"])
    candle_high = float(current_candle["high"])
    candle_low = float(current_candle["low"])
    current_atr = float(current_candle["atr"]) if not pd.isna(current_candle["atr"]) else candle_close * 0.005
    current_rsi = float(current_candle.get("rsi", 50.0))
    
    # 1. Расчет объема (SMA 20)
    avg_vol = df["volume"].iloc[-21:-1].mean()
    current_vol = float(current_candle["volume"])
    vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0
    volume_climax = vol_ratio >= 1.5 

    # 2. Математика теней (Выкуп > 50%)
    full_size = max(candle_high - candle_low, 0.000001)
    upper_shadow = candle_high - max(candle_close, candle_open)
    lower_shadow = min(candle_close, candle_open) - candle_low
    upper_shadow_ratio = upper_shadow / full_size
    lower_shadow_ratio = lower_shadow / full_size

    swing_high, swing_low = pandas_get_local_structure(df, lookback=15)
    trigger_fired = False
    trigger_type = "НЕТ"
    
    # Фильтр целей (Убирает слипшиеся тейки на дешевых монетах)
    def filter_targets(raw_targets, precision, is_short):
        merged = []
        raw_targets = sorted(raw_targets, reverse=is_short)
        min_distance = current_atr * 0.5 
        for t in raw_targets:
            if t <= 0: continue
            if not merged:
                merged.append(t)
                continue
            if not any(abs(t - m) < min_distance for m in merged):
                merged.append(t)
        return merged

    # --- SHORT ---
    if direction == "SHORT":
        nearest_res = swing_high
        if macro_levels and macro_levels.get("resistances"):
            above_res = [r["min"] for r in macro_levels["resistances"] if r["min"] > candle_close]
            if above_res: nearest_res = min(above_res)

        is_pierced = candle_high >= (nearest_res * 0.999) and candle_close < nearest_res
        is_rejection = upper_shadow_ratio >= 0.45
        
        if is_pierced and is_rejection and volume_climax:
            trigger_fired, trigger_type = True, "Институциональный Пинбар (V2)"
            
        sl_price = max(candle_high, nearest_res) + (current_atr * 0.1)
        risk = max(sl_price - candle_close, current_atr * 0.5)
        
        tp1_price = swing_low
        if tp1_price >= candle_close: tp1_price = candle_close - (risk * 1.5)

        raw_targets = [tp1_price]
        if not is_scalper and macro_levels:
            for s in macro_levels.get("supports", []):
                if s["max"] < tp1_price: raw_targets.append(s["max"])
                    
        valid_targets = filter_targets(raw_targets, price_precision, is_short=True)
        while len(valid_targets) < 3:
            valid_targets.append(valid_targets[-1] - risk)

        tp1_price, tp2_price, tp3_price = valid_targets[0], valid_targets[1], valid_targets[2]

    # --- LONG ---
    elif direction == "LONG":
        nearest_sup = swing_low
        if macro_levels and macro_levels.get("supports"):
            below_sup = [s["max"] for s in macro_levels["supports"] if s["max"] < candle_close]
            if below_sup: nearest_sup = max(below_sup)

        is_pierced = candle_low <= (nearest_sup * 1.001) and candle_close > nearest_sup
        is_rejection = lower_shadow_ratio >= 0.45
        
        if is_pierced and is_rejection and volume_climax:
            trigger_fired, trigger_type = True, "Институциональный Пинбар (V2)"
            
        sl_price = min(candle_low, nearest_sup) - (current_atr * 0.1)
        risk = max(candle_close - sl_price, current_atr * 0.5)
        
        tp1_price = swing_high
        if tp1_price <= candle_close: tp1_price = candle_close + (risk * 1.5)

        raw_targets = [tp1_price]
        if not is_scalper and macro_levels:
            for r in macro_levels.get("resistances", []):
                if r["min"] > tp1_price: raw_targets.append(r["min"])
                    
        valid_targets = filter_targets(raw_targets, price_precision, is_short=False)
        while len(valid_targets) < 3:
            valid_targets.append(valid_targets[-1] + risk)

        tp1_price, tp2_price, tp3_price = valid_targets[0], valid_targets[1], valid_targets[2]

    if trigger_fired:
        reward = abs(tp1_price - candle_close)
        trade_risk = abs(candle_close - sl_price)
        rr = reward / trade_risk if trade_risk > 0 else 0
        if rr < 3.0:
            trigger_fired = False
            trigger_type = f"ПРОПУСК: Плохой R/R (1:{round(rr, 2)})"

    return {
        "trigger_fired": trigger_fired, "rsi_filter_passed": True, "volume_climax": volume_climax,
        "trigger_type": trigger_type, "sl_price": round(sl_price, price_precision),
        "tp1_price": round(tp1_price, price_precision), "tp2_price": round(tp2_price, price_precision),
        "tp3_price": round(tp3_price, price_precision), "rsi_value": round(current_rsi, 1), "vol_ratio": round(vol_ratio, 1)
    }