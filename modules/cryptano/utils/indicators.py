import pandas as pd
from modules.cryptano.utils.crypto_utils import calculate_rsi
from modules.cryptano.utils.price_action import get_market_structure
from modules.cryptano.utils.common import format_price as fmt_p
import time

# ================= Настройки фильтров ================= 

# ================= НАСТРОЙКИ ИНДИКАТОРОВ =================
VOL_LIMIT = 2.5        # Порог для базового статуса "Аномальный объем"
RSI_LOW = 30           # Порог для базового статуса "Перепроданность"
RSI_HIGH = 70          # Порог для базового статуса "Перекупленность"

# --- Настройки периодов для уровней и тренда ---
LEVELS_PERIOD = 20     # Сколько последних свечей берем для поиска ближайших поддержки/сопротивления
CHANNEL_PERIOD = 40    # Период для расчета глобального канала (откуда берется pos_pct)
MA_PERIOD = 30         # Период скользящей средней (MA) для определения глобального тренда

# ======================================================

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
    
    
    
    # 1. Автоматически определяем размер свечи в минутах (вычитаем время открытия двух соседних свечей)
    tf_ms = df["timestamp"].iloc[-1] - df["timestamp"].iloc[-2]
    tf_minutes = tf_ms / 60000.0 if tf_ms > 0 else 60.0
    
    # 2. Вычисляем, сколько минут прошло с момента ОТКРЫТИЯ текущей свечи
    elapsed_ms = (time.time() * 1000) - df["timestamp"].iloc[-1]
    elapsed_minutes = elapsed_ms / 60000.0
    
    if elapsed_minutes <= 0:
        elapsed_minutes = 1.0
        
    # 3. Считаем честный прогресс свечи (min нужен, чтобы прогресс не превысил 100%, если свеча только закрылась)
    expected_progress = min(elapsed_minutes / tf_minutes, 1.0)
    
    adjusted_ratio = float(recent_volume / (avg_volume * expected_progress) if avg_volume > 0 else 1.0)
    vol_ratio = adjusted_ratio

    # Считаем канал и позицию (pos_pct)
    recent_channel = df.tail(channel_lookback)
    strong_resistance = float(recent_channel["high"].max())
    strong_support = float(recent_channel["low"].min())
    range_size = strong_resistance - strong_support
    
    if range_size == 0:
        pos_pct = 50.0
    else:
        pos_pct = ((current_price - strong_support) / range_size) * 100

    # Считаем ближайшие уровни за последние 20 свечей
    recent_short = df.tail(20)
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
        # Ищем экстремумы импульса за последние 15 свечей
        impulse_low = float(df.tail(15)["low"].min())
        impulse_high = float(df.tail(15)["high"].max()) # Это самый пик пампа
        wave_size = impulse_high - impulse_low
        
        fib_0236 = impulse_high - (wave_size * 0.236)
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
            "take_profit": fib_0236,
            "take_profit_2": fib_0382,
            "take_profit_3": fib_0500,
            "stop_loss": stop_loss,
        }

    # 🟢 ЛОГИКА ДЛЯ ДАМПА (LONG)
    is_rsi_trigger = (scan_type in ["rsi", "rsi_low"] and rsi <= rsi_low)
    is_vol_trigger = (scan_type == "volume" and vol_ratio >= volume_multiplier and is_volume_fading)
    is_auto_trigger = (scan_type == "auto" and rsi <= rsi_low and vol_ratio >= volume_multiplier and is_volume_fading)

    if is_rsi_trigger or is_vol_trigger or is_auto_trigger:

        # Ищем экстремумы импульса за последние 15 свечей
        impulse_high = float(df.tail(15)["high"].max())
        impulse_low = float(df.tail(15)["low"].min()) # Это абсолютное дно дампа
        wave_size = impulse_high - impulse_low
        
        fib_0236 = impulse_low + (wave_size * 0.236)
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
            "take_profit": fib_0236,
            "take_profit_2": fib_0382,
            "take_profit_3": fib_0500,
            "stop_loss": stop_loss,
        }

    return None


def analyze_extreme_pattern(df, direction, current_price, price_precision):
    """
    Анализ разворота на основе SFP (Свип ликвидности) и Разворотных свечей.
    Возвращает четкие флаги триггеров и фильтров вместо старой системы баллов.
    """
    if len(df) < 16:
        return {
            "trigger_fired": False, "rsi_filter_passed": False, "volume_climax": False,
            "trigger_type": "НЕТ", "sl_price": 0.0, "tp1_price": 0.0, "tp2_price": 0.0, "tp3_price": 0.0,
            "rsi_value": 50.0, "vol_ratio": 1.0
        }

    # Последняя свеча (которую мы оцениваем на наличие разворота)
    current_candle = df.iloc[-1]
    # Предыдущие 15 свечей для поиска локальных максимумов/минимумов
    context_df = df.iloc[-16:-1]
    
    candle_open = float(current_candle["open"])
    candle_high = float(current_candle["high"])
    candle_low = float(current_candle["low"])
    candle_close = float(current_candle["close"])
    
    current_rsi = float(current_candle["rsi"]) if "rsi" in current_candle else 50.0
    
    avg_vol = context_df["volume"].mean()
    current_vol = float(current_candle["volume"])
    vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0
    
    # Бонус-усилитель: Кульминация объема (объем > х4 от среднего)
    volume_climax = vol_ratio >= 4.0

    trigger_fired = False
    rsi_filter_passed = False
    trigger_type = "НЕТ"
    sl_price = 0.0
    tp1_price = tp2_price = tp3_price = 0.0

    body = abs(candle_close - candle_open)
    if body == 0: body = 0.000001

    if direction == "SHORT":
        local_high = float(context_df["high"].max())
        
        # 1. ТРИГГЕР: SFP (Цена пробила прошлый хай, но закрылась ниже него)
        is_sfp = (candle_high > local_high) and (candle_close < local_high)
        
        # 2. АЛЬТ. ТРИГГЕР: Пинбар (Верхняя тень в 1.5 раза больше тела)
        upper_shadow = candle_high - max(candle_close, candle_open)
        is_pinbar = upper_shadow > (body * 1.5)
        
        if is_sfp:
            trigger_fired = True
            trigger_type = "SFP (Ложный пробой максимума)"
            # 🔥 Мягкий фильтр: пробой уровня сам по себе сильный сигнал
            rsi_filter_passed = current_rsi >= 65.0  
        elif is_pinbar:
            trigger_fired = True
            trigger_type = "Агрессивный Пинбар"
            # 🧊 Жесткий фильтр: просто тень без пробоя требует сильного перегрева
            rsi_filter_passed = current_rsi >= 72.0  
            
        # РАСЧЕТ ЦЕЛЕЙ
        sl_price = candle_high if trigger_fired else local_high
        impulse_low = float(df.tail(90)["low"].min())
        wave_size = sl_price - impulse_low
        
        tp1_price = round(sl_price - (wave_size * 0.236), price_precision)
        tp2_price = round(sl_price - (wave_size * 0.382), price_precision)
        tp3_price = round(sl_price - (wave_size * 0.500), price_precision)

    elif direction == "LONG":
        local_low = float(context_df["low"].min())
        
        # 1. ТРИГГЕР: SFP (Цена пробила прошлое дно, но закрылась выше него)
        is_sfp = (candle_low < local_low) and (candle_close > local_low)
        
        # 2. АЛЬТ. ТРИГГЕР: Молот (Нижняя тень в 1.5 раза больше тела)
        lower_shadow = min(candle_close, candle_open) - candle_low
        is_hammer = lower_shadow > (body * 1.5)
        
        if is_sfp:
            trigger_fired = True
            trigger_type = "SFP (Ложный пробой минимума)"
            # 🔥 Мягкий фильтр: ложный пробой дна
            rsi_filter_passed = current_rsi <= 35.0  
        elif is_hammer:
            trigger_fired = True
            trigger_type = "Агрессивный Молот"
            # 🧊 Жесткий фильтр: обычный молот
            rsi_filter_passed = current_rsi <= 28.0  
            
        # РАСЧЕТ ЦЕЛЕЙ
        sl_price = candle_low if trigger_fired else local_low
        impulse_high = float(df.tail(90)["high"].max())
        wave_size = impulse_high - sl_price
        
        tp1_price = round(sl_price + (wave_size * 0.236), price_precision)
        tp2_price = round(sl_price + (wave_size * 0.382), price_precision)
        tp3_price = round(sl_price + (wave_size * 0.500), price_precision)

    elif direction == "LONG":
        local_low = float(context_df["low"].min())
        
        # 1. ТРИГГЕР: SFP (Цена пробила прошлое дно, но закрылась выше него)
        is_sfp = (candle_low < local_low) and (candle_close > local_low)
        
        # 2. АЛЬТ. ТРИГГЕР: Молот (Нижняя тень в 1.5 раза больше тела)
        lower_shadow = min(candle_close, candle_open) - candle_low
        is_hammer = lower_shadow > (body * 1.5)
        
        if is_sfp:
            trigger_fired = True
            trigger_type = "SFP (Ложный пробой минимума)"
        elif is_hammer:
            trigger_fired = True
            trigger_type = "Агрессивный Молот"
            
        # 3. ЖЕСТКИЙ ФИЛЬТР RSI
        rsi_filter_passed = current_rsi <= 28.0
        
        # РАСЧЕТ ЦЕЛЕЙ
        sl_price = candle_low if trigger_fired else local_low
        impulse_high = float(df.tail(90)["high"].max())
        wave_size = impulse_high - sl_price
        
        tp1_price = round(sl_price + (wave_size * 0.236), price_precision)
        tp2_price = round(sl_price + (wave_size * 0.382), price_precision)
        tp3_price = round(sl_price + (wave_size * 0.500), price_precision)

    return {
        "trigger_fired": trigger_fired,
        "rsi_filter_passed": rsi_filter_passed,
        "volume_climax": volume_climax,
        "trigger_type": trigger_type,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "tp3_price": tp3_price,
        "rsi_value": round(current_rsi, 1),
        "vol_ratio": round(vol_ratio, 1)
    }