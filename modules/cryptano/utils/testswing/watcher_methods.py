"""
watcher_methods.py
==================
Изолированные методы определения точки входа.
Каждый метод — отдельная, независимая стратегия. Не знают друг о друге,
не знают про фильтры качества или контекст — только про "случился ли сигнал".

Доступные методы:
    - SweepReclaimWatcher: стейт-машина, ищет ложный пробой (вынос стопов) + возврат
    - check_choch: стейтлесс функция, ищет слом структуры (CHoCH) на истории N свечей

Оба возвращают унифицированный формат сигнала:
    {"action": "BUY"/"SELL", "sl": float|None, "reason": str}
    или None, если сигнала нет.
"""

import pandas as pd


# =========================================================================
# МЕТОД 1: SWEEP / RECLAIM (вынос стопов + возврат)
# =========================================================================
class SweepReclaimWatcher:
    """
    Стейт-машина на один уровень. Хранит состояние между вызовами update().

    FRESH   -> цена ещё не касалась зоны
    BELOW   -> (LONG) цена закрылась под зоной, ждём Reclaim
    ABOVE   -> (SHORT) цена закрылась над зоной, ждём Reclaim
    TRIGGERED -> сигнал уже отдан, watcher отработал

    Логика:
      LONG:  Touch зоны -> если закрытие ниже min (Sweep) -> ждать закрытия выше min (Reclaim) -> BUY
             если сразу отскочили без закрытия ниже min -> мгновенный BUY
      SHORT: симметрично, относительно max зоны
    """

    def __init__(self, level_min, level_max, trade_type, allow_bounce=True, allow_sweep=True):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "FRESH"
        self.extreme_price = None
        self.candles_in_sweep = 0  # сколько свечей просидели в BELOW/ABOVE
        self.allow_bounce = allow_bounce  # разрешён ли сигнал "Касание+Отказ" (без sweep)
        self.allow_sweep = allow_sweep    # разрешён ли сигнал "Sweep+Reclaim" (настоящий вынос)

    def update(self, c_open, c_high, c_low, c_close):
        if self.state in ["DEAD", "TRIGGERED"]:
            return None

        if self.trade_type == 'LONG':
            if self.state == "FRESH":
                touched = c_low <= self.max
                if not touched:
                    return None

                if c_close < self.min:
                    if not self.allow_sweep:
                        return None  # Sweep-паттерн выключен - игнорируем, остаёмся FRESH
                    # Цена закрылась НИЖЕ зоны (Sweep) -> ждём Reclaim
                    self.state = "BELOW"
                    self.extreme_price = c_low
                    self.candles_in_sweep = 1
                elif c_close > c_open:
                    if not self.allow_bounce:
                        return None  # Bounce-паттерн выключен - игнорируем
                    # Реальный отказ: касание зоны + бычья свеча (close > open)
                    # close остаётся в зоне или выше -> мгновенный сигнал
                    self.state = "TRIGGERED"
                    return {"action": "BUY", "sl": c_low, "reason": "Касание + Отказ (Bounce)",
                            "is_real_sweep": False, "overshoot_pct": 0.0, "candles_in_sweep": 0}
                # Если касание было, но свеча красная и close в зоне - не сигнал,
                # ждём следующую свечу (state остаётся FRESH)

            elif self.state == "BELOW":
                self.candles_in_sweep += 1
                if self.extreme_price is None:
                    self.extreme_price = c_low
                else:
                    self.extreme_price = min(self.extreme_price, c_low)
                # Reclaim = цена вернулась выше min (закрылась в зоне или выше)
                if c_close > self.min:
                    self.state = "TRIGGERED"
                    overshoot_pct = ((self.min - self.extreme_price) / self.min) * 100
                    return {"action": "BUY", "sl": self.extreme_price, "reason": "Возврат (Reclaim выноса)",
                            "is_real_sweep": True, "overshoot_pct": overshoot_pct,
                            "candles_in_sweep": self.candles_in_sweep}

        elif self.trade_type == 'SHORT':
            if self.state == "FRESH":
                touched = c_high >= self.min
                if not touched:
                    return None

                if c_close > self.max:
                    if not self.allow_sweep:
                        return None
                    # Цена закрылась ВЫШЕ зоны (Sweep) -> ждём Reclaim
                    self.state = "ABOVE"
                    self.extreme_price = c_high
                    self.candles_in_sweep = 1
                elif c_close < c_open:
                    if not self.allow_bounce:
                        return None
                    # Реальный отказ: касание зоны + медвежья свеча (close < open)
                    self.state = "TRIGGERED"
                    return {"action": "SELL", "sl": c_high, "reason": "Касание + Отказ (Bounce)",
                            "is_real_sweep": False, "overshoot_pct": 0.0, "candles_in_sweep": 0}
                # Если касание было, но свеча зелёная и close в зоне - не сигнал

            elif self.state == "ABOVE":
                self.candles_in_sweep += 1
                if self.extreme_price is None:
                    self.extreme_price = c_high
                else:
                    self.extreme_price = max(self.extreme_price, c_high)
                # Reclaim = цена вернулась ниже max (закрылась в зоне или ниже)
                if c_close < self.max:
                    self.state = "TRIGGERED"
                    overshoot_pct = ((self.extreme_price - self.max) / self.max) * 100
                    return {"action": "SELL", "sl": self.extreme_price, "reason": "Возврат (Reclaim выноса)",
                            "is_real_sweep": True, "overshoot_pct": overshoot_pct,
                            "candles_in_sweep": self.candles_in_sweep}


        return None


# =========================================================================
# МЕТОД 2: CHoCH (слом структуры на истории N свечей)
# =========================================================================
def check_choch(df, level, direction, lookback=15, sl_buffer_pct=0.5,
                 anti_knife_atr_mult=0.8):
    """
    Стейтлесс-функция. На каждый вызов пересчитывает весь паттерн с нуля
    по последним `lookback` свечам df. Не хранит состояние между вызовами —
    если нужна память между свечами, вызывающий код должен передавать
    срез df, расширяющийся на одну свечу каждый раз.

    df: DataFrame с колонками open, high, low, close, volume, atr
        (atr должен быть посчитан заранее и присутствовать как колонка)
    level: {'min': float, 'max': float, ...}
    direction: 'LONG' или 'SHORT'

    Логика:
      1. Анти-нож: пропускает свечи, где идёт аномально крупное движение
         в противоположную сторону с подтверждением объёмом.
      2. Касание зоны: если цена коснулась уровня и не пришёл анти-нож,
         запоминает high/low предыдущей+текущей свечи как choch_level.
      3. Ждёт пробоя choch_level. Если пробой произошёл на ПОСЛЕДНЕЙ свече
         переданного df — это и есть сигнал прямо сейчас.
      4. Если цена ушла за пределы уровня (на 1%) раньше, чем пробила
         choch_level — отмена, ищем заново.

    Returns:
        {"action": "BUY"/"SELL", "sl": None, "reason": str, "choch_level": float}
        или None
    """
    if len(df) < lookback + 1:
        return None

    wait_for_choch = False
    choch_level = None
    active_level = None

    start_idx = max(1, len(df) - lookback)

    for i in range(start_idx, len(df)):
        c_close, c_open = float(df['close'].iloc[i]), float(df['open'].iloc[i])
        c_high, c_low = float(df['high'].iloc[i]), float(df['low'].iloc[i])
        c_vol = float(df['volume'].iloc[i])

        p_close, p_open = float(df['close'].iloc[i - 1]), float(df['open'].iloc[i - 1])
        p_vol = float(df['volume'].iloc[i - 1])

        c_atr = float(df['atr'].iloc[i]) if not pd.isna(df['atr'].iloc[i]) else (c_high - c_low)

        is_falling_knife = (c_close < c_open and p_close < p_open and
                            (c_open - c_close) > (c_atr * anti_knife_atr_mult) and c_vol >= p_vol)
        is_flying_rocket = (c_close > c_open and p_close > p_open and
                            (c_close - c_open) > (c_atr * anti_knife_atr_mult) and c_vol >= p_vol)

        if wait_for_choch and active_level is not None:
            if direction == "LONG" and c_low < active_level['min'] * 0.99:
                wait_for_choch = False
                active_level = None
            elif direction == "SHORT" and c_high > active_level['max'] * 1.01:
                wait_for_choch = False
                active_level = None

            if wait_for_choch and choch_level is not None:
                if direction == "LONG" and c_close > choch_level:
                    if i == len(df) - 1:
                        return {
                            "action": "BUY", "sl": None,
                            "reason": "CHoCH Пробой (Подтвержден)",
                            "choch_level": choch_level,
                        }
                    else:
                        wait_for_choch = False
                        active_level = None
                elif direction == "SHORT" and c_close < choch_level:
                    if i == len(df) - 1:
                        return {
                            "action": "SELL", "sl": None,
                            "reason": "CHoCH Пробой (Подтвержден)",
                            "choch_level": choch_level,
                        }
                    else:
                        wait_for_choch = False
                        active_level = None

        if not wait_for_choch:
            if direction == "LONG":
                if c_low <= level['max'] and c_close > level['min']:
                    if not is_falling_knife:
                        wait_for_choch = True
                        choch_level = max(float(df['high'].iloc[i]), float(df['high'].iloc[i - 1]))
                        active_level = level
            elif direction == "SHORT":
                if c_high >= level['min'] and c_close < level['max']:
                    if not is_flying_rocket:
                        wait_for_choch = True
                        choch_level = min(float(df['low'].iloc[i]), float(df['low'].iloc[i - 1]))
                        active_level = level

    return None

# =========================================================================
# МЕТОД 3: VOLUME REVERSAL (Нырок под EMA в зоне + Аномальный объем)
# =========================================================================
def check_volume_reversal(df, level, direction, vol_mult=3.0, window=10):
    if len(df) < window:
        return None

    # 1. Создаем срез окна
    w_df = df.iloc[-window:]

    # 2. Находим индекс свечи с самым гигантским объемом в окне
    max_vol_idx = w_df['volume'].idxmax()
    climax_candle = w_df.loc[max_vol_idx]

    # 3. Проверяем, что аномалия реально была (x3 от среднего)
    if climax_candle['volume'] < (climax_candle['avg_vol'] * vol_mult):
        return None

    # Текущая (последняя закрытая) свеча
    c_close = float(w_df['close'].iloc[-1])
    c_open = float(w_df['open'].iloc[-1])

    if direction == 'LONG':
        # ИМЕННО объемная свеча должна нырнуть в зону и под EMA
        climax_in_zone = climax_candle['low'] <= level['max']
        climax_under_ema = climax_candle['low'] < climax_candle['ema']
        
        # Если объем прошел в воздухе, а не на уровне — отмена
        if not (climax_in_zone and climax_under_ema):
            return None

        # Триггер: текущая свеча зеленая и закрылась внутри или чуть выше зоны (защита от улета)
        if c_close > c_open and c_close >= level['min'] and c_close <= (level['max'] * 1.015):
            sl_price = float(w_df.loc[max_vol_idx:]['low'].min()) # Стоп под самое дно
            return {"action": "BUY", "sl": sl_price, "reason": f"Vol Climax {vol_mult}x + EMA Dip"}

    elif direction == 'SHORT':
        # То же самое зеркально для шорта
        climax_in_zone = climax_candle['high'] >= level['min']
        climax_above_ema = climax_candle['high'] > climax_candle['ema']

        # Если объем прошел в воздухе, а не на уровне — отмена
        if not (climax_in_zone and climax_above_ema):
            return None

        min_allowed_entry = level['min'] * 0.985
        
        # Триггер: текущая свеча красная и закрылась внутри или чуть ниже зоны
        if c_close < c_open and c_close <= level['max'] and c_close >= min_allowed_entry:
            sl_price = float(w_df.loc[max_vol_idx:]['high'].max()) # Стоп за самый хай
            return {"action": "SELL", "sl": sl_price, "reason": f"Vol Climax {vol_mult}x + EMA Surge"}

    return None