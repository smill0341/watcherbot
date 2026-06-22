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

    def __init__(self, level_min, level_max, trade_type):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "FRESH"
        self.extreme_price = None

    def update(self, c_open, c_high, c_low, c_close):
        if self.state in ["DEAD", "TRIGGERED"]:
            return None

        if self.trade_type == 'LONG':
            if self.state == "FRESH":
                if c_low <= self.max:
                    if c_close < self.min:
                        self.state = "BELOW"
                        self.extreme_price = c_low
                    else:
                        self.state = "TRIGGERED"
                        return {"action": "BUY", "sl": None, "reason": "Первое касание (Отскок)"}

            elif self.state == "BELOW":
                self.extreme_price = min(self.extreme_price, c_low) if self.extreme_price is not None else c_low
                if c_close > self.min:
                    self.state = "TRIGGERED"
                    return {"action": "BUY", "sl": self.extreme_price, "reason": "Возврат (Reclaim выноса)"}

        elif self.trade_type == 'SHORT':
            if self.state == "FRESH":
                if c_high >= self.min:
                    if c_close > self.max:
                        self.state = "ABOVE"
                        self.extreme_price = c_high
                    else:
                        self.state = "TRIGGERED"
                        return {"action": "SELL", "sl": None, "reason": "Первое касание (Отскок)"}

            elif self.state == "ABOVE":
                self.extreme_price = max(self.extreme_price, c_high) if self.extreme_price is not None else c_high
                if c_close < self.max:
                    self.state = "TRIGGERED"
                    return {"action": "SELL", "sl": self.extreme_price, "reason": "Возврат (Reclaim выноса)"}

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

            if wait_for_choch:
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
