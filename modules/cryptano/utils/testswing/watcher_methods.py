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
import numpy as np


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

CONFIRM_BARS = 2  # после климакс-свечи столько баров должны держать higher-low, не пробивая её low



# МЕТОД 3: volume_reversal

def check_volume_reversal(df, level, direction, **kwargs):
    if len(df) < 30: 
        return None

    # 1. ЖЕЛЕЗОБЕТОННЫЙ ПАРСИНГ АРГУМЕНТОВ
    # Теперь мы точно ловим твою цифру из конфига (например, 0.5)
    confirm_mult = kwargs.get('vol_mult', kwargs.get('VOLUME_MULTIPLIER', 0.5))

    current = df.iloc[-1]
    prev = df.iloc[-2]
    
    c_close, c_open = float(current['close']), float(current['open'])
    c_vol = float(current['volume'])
    
    p_close, p_open = float(prev['close']), float(prev['open'])
    p_high, p_low = float(prev['high']), float(prev['low'])

    if direction == 'LONG':
        # Локация: строго внутри зоны
        if c_close > level['max']: return None
        if c_close <= c_open: return None # Только зеленая свеча

        # 2. ПРАЙС-ЭКШЕН (Защита от ранних входов как на 3 фото)
        # Зеленая свеча ОБЯЗАНА перекрыть тело предыдущей свечи.
        # Если красная свеча была огромной, бот не сможет войти сразу, 
        # ему придется подождать мелких свечей проторговки на дне, чтобы поглотить их.
        if p_close < p_open:
            if c_close <= p_open: return None
        else:
            if c_close <= p_high: return None

        # Ищем границы текущей ямы
        close_arr = df['close'].values
        above_idx = np.where(close_arr[:-1] > level['max'])[0]
        pit_start = int(above_idx[-1] + 1) if len(above_idx) > 0 else max(0, len(df)-30)
        
        pit_df = df.iloc[pit_start:-1]
        if len(pit_df) < 1: 
            return None
            
        # 3. Ищем Climax (самую жирную красную свечу в яме)
        red_candles = pit_df[pit_df['close'] < pit_df['open']]
        if len(red_candles) == 0: 
            return None
            
        climax_vol = float(red_candles['volume'].max())
        
        # 4. ВХОД: Сравниваем напрямую (Зеленая >= 50% от Климакса)
        if c_vol >= (climax_vol * confirm_mult):
            sl_price = float(df['low'].iloc[pit_start:].min())
            return {"action": "BUY", "sl": sl_price, 
                    "reason": f"Confirm >= {int(confirm_mult*100)}% of Pit Climax"}

    elif direction == 'SHORT':
        if c_close < level['min']: return None
        if c_close >= c_open: return None

        if p_close > p_open:
            if c_close >= p_open: return None
        else:
            if c_close >= p_low: return None

        close_arr = df['close'].values
        below_idx = np.where(close_arr[:-1] < level['min'])[0]
        spike_start = int(below_idx[-1] + 1) if len(below_idx) > 0 else max(0, len(df)-30)
        
        spike_df = df.iloc[spike_start:-1]
        if len(spike_df) < 1: 
            return None
            
        green_candles = spike_df[spike_df['close'] > spike_df['open']]
        if len(green_candles) == 0: 
            return None
            
        climax_vol = float(green_candles['volume'].max())
        
        if c_vol >= (climax_vol * confirm_mult):
            sl_price = float(df['high'].iloc[spike_start:].max())
            return {"action": "SELL", "sl": sl_price, 
                    "reason": f"Confirm >= {int(confirm_mult*100)}% of Peak Climax"}

    return None
  