import pandas as pd
from modules.cryptano.utils.crypto_utils import calculate_rsi
from modules.cryptano.utils.price_action import get_market_structure

VOL_LIMIT = 2.5
RSI_LOW = 30
RSI_HIGH = 70

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
        trend_code = "BULL"
        trend_text = "📈 Глобальный Бычий"
    elif bear_bias and (local_down or structure == 'bearish'):
        trend_code = "BEAR"
        trend_text = "📉 Глобальный Медвежий"
    else:
        # Цена под/над EMA200, но структура ranging или против тренда
        dist_to_ema200 = abs(current_price - ema200) / ema200
        # Если цена трется вокруг EMA200 (например, отклонение < 2%) — это истинный боковик
        if dist_to_ema200 < 0.02:
            trend_code = "RANGE"
            trend_text = "📊 Боковик"
        elif bull_bias:
            trend_code = "BULL"
            trend_text = "📈 Глобальный Бычий"
        else:
            trend_code = "BEAR"
            trend_text = "📉 Глобальный Медвежий"



    return {
        "trend": trend_text,
        "trend_code": trend_code,
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


def get_cryptano_signal(df, current_price, price_precision, scan_type, rsi_high=75, rsi_low=25, volume_multiplier=4.0):
    df["rsi"] = calculate_rsi(df)
    
    last_row = df.iloc[-1]
    rsi = float(last_row["rsi"])

    # 1. ПРОВЕРКА ОБЪЕМОВ И ЗАТУХАНИЯ ПАНИКИ
    # Ищем пиковый объем на последних 5 закрытых свечах
    recent_closed_vols = df["volume"].iloc[-6:-1]
    max_vol = float(recent_closed_vols.max())
    avg_volume = float(df["volume"].iloc[-30:-6].mean())
    
    vol_ratio = float(max_vol / avg_volume if avg_volume > 0 else 1.0)
    
    # Проверка затухания: текущий объем должен быть МЕНЬШЕ пикового.
    # Это подтверждает, что аномалия прошла пик и продавцы/покупатели выдохлись.
    current_vol = float(df["volume"].iloc[-1])
    is_volume_fading = current_vol < max_vol

    # 🔴 ЛОГИКА ДЛЯ ПАМПА (SHORT)
    is_rsi_high_trigger = (scan_type == "rsi_high" and rsi >= rsi_high)
    is_short_pump_trigger = (
        (scan_type in ["auto", "volume"] and rsi >= rsi_high and vol_ratio >= volume_multiplier and is_volume_fading)
        or is_rsi_high_trigger
    )

    if is_short_pump_trigger:
        # Ищем экстремумы импульса за последние 50 свечей
        impulse_low = float(df.tail(50)["low"].min())
        impulse_high = float(df.tail(50)["high"].max()) # Это самый пик пампа
        wave_size = impulse_high - impulse_low
        
        # Считаем уровни отката Фибоначчи ВНИЗ
        fib_0382 = impulse_high - (wave_size * 0.382)
        fib_0500 = impulse_high - (wave_size * 0.5)

        # Жесткий стоп-лосс: прячем на 3% ВЫШЕ абсолютного пика пампа
        stop_loss = impulse_high * 1.03 

        return {
            "type": "SHORT_PUMP",
            "price": current_price,
            "rsi": rsi,
            "vol_ratio": vol_ratio,
            "entry_market": current_price, 
            "take_profit": fib_0382, # Главная цель: откат 0.382
            "take_profit_2": fib_0500, # Вторая цель: откат 0.5
            "stop_loss": stop_loss,
        }

    # 🟢 ЛОГИКА ДЛЯ ДАМПА (LONG)
    is_rsi_trigger = (scan_type in ["rsi", "rsi_low"] and rsi <= rsi_low)
    is_vol_trigger = (scan_type == "volume" and vol_ratio >= volume_multiplier and is_volume_fading)
    is_auto_trigger = (scan_type == "auto" and rsi <= rsi_low and vol_ratio >= volume_multiplier and is_volume_fading)

    if is_rsi_trigger or is_vol_trigger or is_auto_trigger:
        # Ищем экстремумы импульса за последние 50 свечей
        impulse_high = float(df.tail(50)["high"].max())
        impulse_low = float(df.tail(50)["low"].min()) # Это абсолютное дно дампа
        wave_size = impulse_high - impulse_low
        
        # Считаем уровни отскока Фибоначчи ВВЕРХ
        fib_0382 = impulse_low + (wave_size * 0.382)
        fib_0500 = impulse_low + (wave_size * 0.5)
        fib_0618 = impulse_low + (wave_size * 0.618)

        # Жесткий стоп-лосс: прячем на 3% НИЖЕ абсолютного дна. Пробьет — значит скам.
        stop_loss = impulse_low * 0.97 

        return {
            "type": "LONG_ROLLBACK",
            "price": current_price,
            "rsi": rsi,
            "vol_ratio": vol_ratio,
            "entry_limit": fib_0618 if fib_0618 < current_price else current_price, # Золотой карман
            "take_profit": fib_0382, # Главная цель: отскок 0.382
            "take_profit_2": fib_0500, # Вторая цель: отскок 0.5
            "stop_loss": stop_loss,
        }

    return None


def analyze_extreme_pattern(df, direction, current_price, price_precision):
    """Анализ экстремального паттерна на M15. Возвращает только числа и детали."""
    score = 0
    details = []
    sl_price = 0.0
    tp1_price = 0.0

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
            details.append("✅ Есть разворотная свеча")
        else:
            details.append("❌ Нет разворотной свечи")

        avg_vol = df["volume"].mean()
        peak_vol_mult = float(peak_candle["volume"] / avg_vol if avg_vol > 0 else 1.0)
        current_vol_mult = float(df.iloc[-1]["volume"] / avg_vol if avg_vol > 0 else 1.0)

        if peak_vol_mult > 0:
            drop_percent = ((peak_vol_mult - current_vol_mult) / peak_vol_mult) * 100
        else:
            drop_percent = 0

        if peak_vol_mult >= VOL_LIMIT and drop_percent >= 60:
            score += 1
            details.append(f"✅ Объёмы остывают: x{peak_vol_mult:.1f} -> x{current_vol_mult:.1f} ↓")
        elif peak_vol_mult >= VOL_LIMIT and drop_percent < 60:
            details.append(f"❌ Обьемы еще большие: x{peak_vol_mult:.1f} -> x{current_vol_mult:.1f} ↑")
        else:
            details.append(f"❌ Нет объёма, ждём движений: x{peak_vol_mult:.1f} -> x{current_vol_mult:.1f} ≈")

        current_rsi = round(float(df.iloc[-1]["rsi"]), 1)
        peak_rsi = round(float(peak_candle["rsi"]), 1)

        if peak_rsi >= RSI_HIGH and current_rsi < peak_rsi:
            score += 1
            details.append(f"✅ RSI развернулся: {peak_rsi:.1f} -> {current_rsi:.1f} ↓")
        elif peak_rsi >= RSI_HIGH and current_rsi >= peak_rsi:
            details.append(f"❌ RSI давит дальше: {peak_rsi:.1f} -> {current_rsi:.1f} ↑")
        else:
            details.append(f"❌ RSI без экстрима: {peak_rsi:.1f} -> {current_rsi:.1f} ≈")

        level_price = float(peak_candle["low"])
        if current_price < level_price:
            score += 1
            details.append(f"✅ Есть слом структуры: <{level_price:.1f}")
        else:
            details.append(f"❌ Нет слома: пробой <{level_price:.1f}")

        sl_price = float(peak_candle["high"])
        impulse_low = float(df.loc[:peak_idx].tail(90)["low"].min())
        wave_size = sl_price - impulse_low
        tp1_price = round(sl_price - (wave_size * 0.382), price_precision)

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
            details.append("✅ Есть разворотная свеча")
        else:
            details.append("❌ Нет разворотной свечи")

        avg_vol = df["volume"].mean()
        peak_vol_mult = float(low_candle["volume"] / avg_vol if avg_vol > 0 else 1.0)
        current_vol_mult = float(df.iloc[-1]["volume"] / avg_vol if avg_vol > 0 else 1.0)

        if peak_vol_mult > 0:
            drop_percent = ((peak_vol_mult - current_vol_mult) / peak_vol_mult) * 100
        else:
            drop_percent = 0

        if peak_vol_mult >= VOL_LIMIT and drop_percent >= 60:
            score += 1
            details.append(f"✅ Объёмы остывают: x{peak_vol_mult:.1f} -> x{current_vol_mult:.1f} ↓")
        elif peak_vol_mult >= VOL_LIMIT and drop_percent < 60:
            details.append(f"❌ Обьемы еще большие: x{peak_vol_mult:.1f} -> x{current_vol_mult:.1f} ↑")
        else:
            details.append(f"❌ Нет объёма, ждём движений: x{peak_vol_mult:.1f} -> x{current_vol_mult:.1f} ≈")

        current_rsi = round(float(df.iloc[-1]["rsi"]), 1)
        peak_rsi = round(float(low_candle["rsi"]), 1)

        if peak_rsi <= RSI_LOW and current_rsi > peak_rsi:
            score += 1
            details.append(f"✅ RSI развернулся: {peak_rsi:.1f} -> {current_rsi:.1f} ↑")
        elif peak_rsi <= RSI_LOW and current_rsi <= peak_rsi:
            details.append(f"❌ RSI давит дальше: {peak_rsi:.1f} -> {current_rsi:.1f} ↓")
        else:
            details.append(f"❌ RSI без экстрима: {peak_rsi:.1f} -> {current_rsi:.1f} ≈")

        level_price = float(low_candle["high"])
        if current_price > level_price:
            score += 1
            details.append(f"✅ Есть слом структуры: >{level_price:.1f}")
        else:
            details.append(f"❌ Нет слома: пробой >{level_price:.1f}")

        sl_price = float(low_candle["low"])
        impulse_high = float(df.loc[:low_idx].tail(90)["high"].max())
        wave_size = impulse_high - sl_price
        tp1_price = round(sl_price + (wave_size * 0.382), price_precision)

    # 5. Проверка дивергенции (история за 22 часа до текущих 15 свечей)
    past_df = df.iloc[-90:-15].dropna(subset=['rsi'])

    if len(past_df) < 10:
        details.append("❌ Недостаточно истории для дивергенции")
    else:
        if direction == "SHORT":
            past_peak_idx = past_df["high"].idxmax()
            past_peak_candle = past_df.loc[past_peak_idx]
            
            price1 = round(float(past_peak_candle["high"]), price_precision)
            rsi1 = round(float(past_peak_candle["rsi"]), 1)
            price2 = round(float(peak_candle["high"]), price_precision)
            rsi2 = round(float(peak_candle["rsi"]), 1)
            
            if price2 > price1 and rsi2 < rsi1:
                score += 1
                details.append(f"✅ Есть дивергенция: {price1:.{price_precision}f}/{rsi1:.1f} -> {price2:.{price_precision}f}/{rsi2:.1f} ↓")
            elif price2 > price1 and rsi2 >= rsi1:
                details.append(f"❌ Нет дивергенции: {price1:.{price_precision}f}/{rsi1:.1f} -> {price2:.{price_precision}f}/{rsi2:.1f} ↑")
            else:
                details.append(f"❌ Дивергенция не сформирована: {price1:.{price_precision}f}/{rsi1:.1f} -> {price2:.{price_precision}f}/{rsi2:.1f} ≈")
                
        elif direction == "LONG":
            past_low_idx = past_df["low"].idxmin()
            past_low_candle = past_df.loc[past_low_idx]
            
            price1 = round(float(past_low_candle["low"]), price_precision)
            rsi1 = round(float(past_low_candle["rsi"]), 1)
            price2 = round(float(low_candle["low"]), price_precision)
            rsi2 = round(float(low_candle["rsi"]), 1)
            
            if price2 < price1 and rsi2 > rsi1:
                score += 1
                details.append(f"✅ Есть дивергенция: {price1:.{price_precision}f}/{rsi1:.1f} -> {price2:.{price_precision}f}/{rsi2:.1f} ↑")
            elif price2 < price1 and rsi2 <= rsi1:
                details.append(f"❌ Нет дивергенции: {price1:.{price_precision}f}/{rsi1:.1f} -> {price2:.{price_precision}f}/{rsi2:.1f} ↓")
            else:
                details.append(f"❌ Дивергенция не сформирована: {price1:.{price_precision}f}/{rsi1:.1f} -> {price2:.{price_precision}f}/{rsi2:.1f} ≈")

    return {
        "score": score,
        "details": details,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
    }
