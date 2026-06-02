import pandas as pd
from modules.cryptano.utils.crypto_utils import calculate_rsi
from modules.cryptano.utils.price_action import get_market_structure

def get_market_state(df, current_price, channel_lookback=120):
    """
    Принимает график (df) и текущую цену. 
    Возвращает готовые расчеты тренда, позиции в канале и объема.
    Основано СТРОГО на логике из scanner.py
    """
    
    # Считаем EMA
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    last_row = df.iloc[-1]
    ema9 = float(last_row["ema9"])
    ema21 = float(last_row["ema21"])
    ema200 = float(last_row["ema200"])

    # Считаем объем
    recent_volume = df["volume"].iloc[-1]
    avg_volume = df["volume"].iloc[-25:-5].mean()
    vol_ratio = float(recent_volume / avg_volume if avg_volume > 0 else 1.0)

    # Считаем канал и позицию (pos_pct)
    recent_channel = df.tail(channel_lookback)
    strong_resistance = float(recent_channel["high"].max())
    strong_support = float(recent_channel["low"].min())
    range_size = strong_resistance - strong_support
    
    if range_size == 0:
        pos_pct = 50.0
    else:
        pos_pct = ((current_price - strong_support) / range_size) * 100

    # Считаем ближайшие уровни за последние 7 свечей
    recent_short = df.tail(7)
    nearest_support = float(recent_short["low"].min())
    nearest_resistance = float(recent_short["high"].max())

    # Ищем структуру рынка: Swing Highs / Swing Lows (вынесено в price_action.py)
    structure = get_market_structure(df, lookback=120)

    # Определяем глобальный и локальный тренд
    bull_bias = current_price > ema200
    bear_bias = current_price < ema200
    local_up = ema9 > ema21
    local_down = ema9 < ema21

    if bull_bias and (local_up or structure == 'bullish'):
        trend = "📈 Глобальный Бычий"
    elif bear_bias and (local_down or structure == 'bearish'):
        trend = "📉 Глобальный Медвежий"
    else:
        # Цена под/над EMA200, но структура ranging или против тренда
        dist_to_ema200 = abs(current_price - ema200) / ema200
        # Если цена трется вокруг EMA200 (например, отклонение < 2%) — это истинный боковик
        if dist_to_ema200 < 0.02:
            trend = "📊 Боковик"
        elif bull_bias:
            trend = "📈 Глобальный Бычий"
        else:
            trend = "📉 Глобальный Медвежий"

    return {
        "trend": trend,
        "pos_pct": pos_pct,
        "vol_ratio": vol_ratio,
        "ema9": ema9,
        "ema21": ema21,
        "ema200": ema200,
        "ma30": ema21,
        "strong_support": strong_support,
        "strong_resistance": strong_resistance,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance
    }


def calculate_atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_cp = (df["high"] - df["close"].shift()).abs()
    low_cp = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calculate_pivot_points(df):
    last_row = df.iloc[-2]
    high = last_row["high"]
    low = last_row["low"]
    close = last_row["close"]
    
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    
    return pivot, r1, s1


def get_cryptano_signal(df, current_price, price_precision, scan_type, rsi_high, rsi_low, volume_multiplier):
    df["rsi"] = calculate_rsi(df)
    df["atr"] = calculate_atr(df)
    df["ma30"] = df["close"].rolling(window=30).mean()
    df["ma200"] = df["close"].rolling(window=200).mean()

    last_row = df.iloc[-1]
    rsi = float(last_row["rsi"])
    atr = float(last_row["atr"])
    ma30 = float(last_row["ma30"])

    recent_volume = df["volume"].iloc[-1]
    avg_volume = df["volume"].iloc[-25:-5].mean()
    vol_ratio = float(recent_volume / avg_volume if avg_volume > 0 else 1.0)

    is_rsi_high_trigger = (scan_type == "rsi_high" and rsi >= rsi_high)
    is_short_pump_trigger = (
        (scan_type in ["auto", "volume"] and rsi > 75.0 and vol_ratio >= 4.0)
        or is_rsi_high_trigger
    )

    if is_short_pump_trigger:
        entry_market = current_price
        entry_limit = current_price + (atr * 1.5)
        stop_loss = entry_limit + (atr * 0.5)
        take_profit = ma30

        return {
            "type": "SHORT_PUMP",
            "price": current_price,
            "rsi": rsi,
            "vol_ratio": vol_ratio,
            "entry_market": entry_market,
            "entry_limit": entry_limit,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
        }

    pivot, r1, s1 = calculate_pivot_points(df)
    s1 = float(s1)
    r1 = float(r1)
    stop_loss_long = s1 * 0.95

    is_rsi_trigger = (scan_type in ["rsi", "rsi_low"] and rsi <= rsi_low)
    is_vol_trigger = (scan_type == "volume" and vol_ratio >= volume_multiplier)
    is_auto_trigger = (scan_type == "auto" and rsi <= 35.0 and vol_ratio >= volume_multiplier)

    if is_rsi_trigger or is_vol_trigger or is_auto_trigger:
        ma200 = float(last_row["ma200"]) if not pd.isna(last_row["ma200"]) else 0
        if current_price > s1 and current_price > ma200:
            return {
                "type": "LONG_ROLLBACK",
                "price": current_price,
                "rsi": rsi,
                "vol_ratio": vol_ratio,
                "s1": s1,
                "r1": r1,
                "stop_loss": stop_loss_long,
            }

    return None


def analyze_extreme_pattern(df, direction, current_price, price_precision):
    """Анализ экстремального паттерна на M15. Возвращает только числа и детали."""
    score = 0
    details = []
    sl_price = 0.0

    if direction == "SHORT":
        recent_df = df.tail(15)
        peak_idx = recent_df["high"].idxmax()
        peak_candle = df.loc[peak_idx]

        body = abs(peak_candle["close"] - peak_candle["open"])
        upper_shadow = peak_candle["high"] - max(peak_candle["close"], peak_candle["open"])
        is_pin = upper_shadow > (body * 1.5)

        is_engulf = False
        if peak_idx + 1 < len(df):
            next_c = df.loc[peak_idx + 1]
            is_engulf = (
                (peak_candle["close"] > peak_candle["open"]) and
                (next_c["close"] < next_c["open"]) and
                (next_c["close"] < peak_candle["open"])
            )

        if is_pin or is_engulf:
            score += 1
            details.append("✅ Разворотный паттерн на самом пике пампа")
        else:
            details.append("❌ На пике не было явной разворотной свечи")

        if df.iloc[-1]["volume"] < peak_candle["volume"]:
            score += 1
            details.append("✅ Объемы покупателей иссякли после пика")
        else:
            details.append("❌ Объемы все еще аномально высокие")

        if df.iloc[-1]["rsi"] < peak_candle["rsi"] and peak_candle["rsi"] > 70:
            score += 1
            details.append(f"✅ RSI упал от пика ({peak_candle['rsi']:.1f} -> {df.iloc[-1]['rsi']:.1f})")
        else:
            details.append(f"❌ RSI не показывает разворот ({df.iloc[-1]['rsi']:.1f})")

        if current_price < peak_candle["low"]:
            score += 1
            details.append("✅ Слом структуры (Пробит лой пиковой свечи)")
        else:
            details.append("❌ Слом локальной структуры отсутствует")

        sl_price = float(peak_candle["high"])

    elif direction == "LONG":
        recent_df = df.tail(15)
        low_idx = recent_df["low"].idxmin()
        low_candle = df.loc[low_idx]

        body = abs(low_candle["close"] - low_candle["open"])
        lower_shadow = min(low_candle["close"], low_candle["open"]) - low_candle["low"]
        is_hammer = lower_shadow > (body * 1.5)

        is_bull_engulf = False
        if low_idx + 1 < len(df):
            next_c = df.loc[low_idx + 1]
            is_bull_engulf = (
                (low_candle["close"] < low_candle["open"]) and
                (next_c["close"] > next_c["open"]) and
                (next_c["close"] > low_candle["open"])
            )

        if is_hammer or is_bull_engulf:
            score += 1
            details.append("✅ Разворотный паттерн (Молот/Поглощение) на дне")
        else:
            details.append("❌ На дне не было явной разворотной свечи")

        avg_vol = df["volume"].mean()
        if low_candle["volume"] > (avg_vol * 2.5):
            score += 1
            details.append("✅ Кульминация продаж (Кит выкупил дно на объеме)")
        else:
            details.append("❌ Нет кульминационного объема на дне")

        if df.iloc[-1]["rsi"] > low_candle["rsi"] and low_candle["rsi"] < 30:
            score += 1
            details.append(f"✅ RSI отскочил от дна ({low_candle['rsi']:.1f} -> {df.iloc[-1]['rsi']:.1f})")
        else:
            details.append(f"❌ RSI все еще на дне ({df.iloc[-1]['rsi']:.1f})")

        if current_price > low_candle["close"] and df.iloc[-1]["low"] >= low_candle["low"]:
            score += 1
            details.append("✅ Формируется база (Дно больше не обновляется)")
        else:
            details.append("❌ Цена продолжает давить вниз")

        sl_price = float(low_candle["low"])

    return {
        "score": score,
        "details": details,
        "sl_price": sl_price,
    }
