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
from smartmoneyconcepts import smc

SWING_LENGTH = 5      # окно поиска пивотов для smc.swing_highs_lows (подобрано под 15m)
BASELINE_BARS = 200   # ~2 суток на 15m - база объема для check_volume_reversal


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

def check_volume_reversal(df, level, direction, vol_mult=2.0, window=10):
    """
    Финальная логика (структура + объемный фильтр):
      1. Локация: цена под/над уровнем ИСХОДА (level - зафиксированный origin_level
         из test_simulator.py, не "первый попавшийся" уровень).
      2. Структура: используем smc.bos_choch (smart-money-concepts) - находим
         подтвержденный слом структуры (CHoCH) именно НА ТЕКУЩЕЙ свече (BrokenIndex
         совпадает с текущим индексом - слом подтвердился прямо сейчас).
      3. Объемный фильтр: свеча, подтвердившая слом (BrokenIndex), должна иметь
         объем >= vol_mult от базы - это и есть капитуляция, не пустой перегиб.
         (проверено на реальных данных: CHoCH без объема ~46% WR / -0.12%,
         CHoCH с объемом x2+ ~50% WR / +0.20% на 5ч горизонте)
      4. SL - под уровень свинга, который был сломан (Level из bos_choch).

    vol_mult: множитель объёма для подтверждения (из WATCHER_CONFIG['VOLUME_MULTIPLIER']).
              Раньше игнорировался, использовался хардкод CHOCH_VOL_RATIO=2.0 - починено.

    Требует пакет smartmoneyconcepts (pip install smartmoneyconcepts).
    """
    
    min_len = BASELINE_BARS + SWING_LENGTH * 4 + 10
    if len(df) < min_len:
        return None

    now_idx = len(df) - 1
    baseline_vol = df['volume'].iloc[-(BASELINE_BARS + 2):-2].mean()
    if baseline_vol <= 0:
        return None

    # Очистка колонок от дубликатов симулятора (чтобы не было краша N, 2)
    df_smc = pd.DataFrame({
        'open': df['open'],
        'high': df['high'],
        'low': df['low'],
        'close': df['close'],
        'volume': df['volume']
    })

    # Передаем ВЕСЬ график, как хотел Клод, без обрезаний
    sh = pd.DataFrame(smc.swing_highs_lows(df_smc, swing_length=SWING_LENGTH))
    bc = pd.DataFrame(smc.bos_choch(df_smc, sh, close_break=True))

    # Ищем строку, где слом структуры подтвердился ИМЕННО на текущей свече
    # (BrokenIndex == now_idx) - это и значит "разворот подтвердился прямо сейчас"
    broken_now = bc[bc['BrokenIndex'] == now_idx]
    if len(broken_now) == 0:
        return None

    if direction == 'LONG':
        bullish = broken_now[broken_now['CHOCH'] == 1]
        if len(bullish) == 0:
            return None

        c_close_now = float(df['close'].iloc[now_idx])
        if c_close_now > level['max']:
            return None  # подтверждение случилось не в яме - не наш сигнал

        vol_at_break = float(df['volume'].iloc[now_idx])
        if vol_at_break < (baseline_vol * vol_mult):
            return None  # слом без капитуляции - статистически слабый сигнал

        sl_price = float(bullish['Level'].iloc[0])
        return {"action": "BUY", "sl": sl_price,
                "reason": f"CHoCH + Vol x{vol_mult} confirm"}

    elif direction == 'SHORT':
        bearish = broken_now[broken_now['CHOCH'] == -1]
        if len(bearish) == 0:
            return None

        c_close_now = float(df['close'].iloc[now_idx])
        if c_close_now < level['min']:
            return None

        vol_at_break = float(df['volume'].iloc[now_idx])
        if vol_at_break < (baseline_vol * vol_mult):
            return None

        sl_price = float(bearish['Level'].iloc[0])
        return {"action": "SELL", "sl": sl_price,
                "reason": f"CHoCH + Vol x{vol_mult} confirm"}

    return None


# =========================================================================
# МЕТОД 4: pit_climax (Wyckoff Selling Climax + Secondary Test)
# =========================================================================
# Не структурный CHoCH (метод 3) - классический паттерн Wyckoff, целиком
# завязан на ОБЪЁМ, не на количество свечей:
#   1. Локация - цена в яме под уровнем
#   2. Climax - ПЕРВАЯ красная свеча в яме, чей объём - реальный всплеск
#      (>= climax_vol_mult от средней базы объёма). Это и есть капитуляция -
#      может быть одна свеча, может две, не привязано к времени вообще.
#   3. Secondary Test - ПОСЛЕ climax должен быть ВТОРОЙ объёмный слив:
#      красная свеча, делающая новый/равный минимум относительно climax,
#      с объёмом тоже выше нормы, но слабее climax (как будто уровень
#      пробили второй раз, на меньшей панике)
#   4. Вход - зелёная свеча после secondary test, объём >= confirm_mult от
#      climax, тело перекрывает предыдущую свечу (защита от мелкого дёрга)
def check_pit_climax(df, level, direction, **kwargs):
    if len(df) < 30:
        return None

    confirm_mult = kwargs.get('vol_mult', kwargs.get('VOLUME_MULTIPLIER', 0.5))
    # Порог всплеска объёма, который вообще считается climax-капитуляцией
    climax_vol_mult = kwargs.get('climax_vol_mult', 2.0)
    # Secondary Test засчитывается только если его объём < этой доли от climax
    test_vol_ratio = kwargs.get('test_vol_ratio', 0.7)

    current = df.iloc[-1]
    prev = df.iloc[-2]

    c_close, c_open = float(current['close']), float(current['open'])
    c_vol = float(current['volume'])

    p_close, p_open = float(prev['close']), float(prev['open'])
    p_high, p_low = float(prev['high']), float(prev['low'])

    # База объёма для определения "что такое всплеск" - средний объём за
    # BASELINE_BARS свечей перед текущим моментом (не за саму яму - яма
    # может быть короче базы)
    baseline_window = min(BASELINE_BARS, max(20, len(df) - 2))
    baseline_vol = float(df['volume'].iloc[-(baseline_window + 2):-2].mean())
    if baseline_vol <= 0:
        return None

    if direction == 'LONG':
        if c_close > level['max']:
            return None
        if c_close <= c_open:
            return None  # только зелёная свеча

        # Защита от ранних входов - зелёная свеча обязана перекрыть тело
        # предыдущей свечи
        if p_close < p_open:
            if c_close <= p_open:
                return None
        else:
            if c_close <= p_high:
                return None

        # Границы ямы - используем ЗАПОМНЕННЫЙ момент пробоя (если он есть -
        # проставляется в test_simulator.py в момент фиксации origin), а не
        # ищем его задним числом по ограниченному окну df. Иначе если origin
        # держится дольше окна, яма обрезается неправильно - climax считается
        # по случайному кусочку, а не по настоящей яме.
        pit_start_time = level.get('_pit_start_time')
        if pit_start_time is not None and pit_start_time in df.index:
            pit_start = df.index.get_loc(pit_start_time)
        else:
            close_arr = df['close'].values
            above_idx = np.where(close_arr[:-1] > level['max'])[0]
            pit_start = int(above_idx[-1] + 1) if len(above_idx) > 0 else max(0, len(df) - 30)

        pit_df = df.iloc[pit_start:-1]
        if len(pit_df) < 1:
            return None  # яма ещё не успела образоваться (origin только что зафиксирован)

        red_candles = pit_df[pit_df['close'] < pit_df['open']]
        if len(red_candles) == 0:
            return None

        # Climax - ПЕРВАЯ красная свеча в яме с реальным всплеском объёма
        # (>= climax_vol_mult от базы). Не "самая громкая за всю яму" - именно
        # первая, хронологически, потому что капитуляция это конкретное
        # событие, не результат перебора истории.
        climax_candidates = red_candles[red_candles['volume'] >= baseline_vol * climax_vol_mult]
        if len(climax_candidates) == 0:
            return None  # ещё не было настоящего объёмного слива - не наш сетап
        climax_idx = climax_candidates.index[0]
        climax_vol = float(df.loc[climax_idx, 'volume'])
        climax_low = float(df.loc[climax_idx, 'low'])

        # Secondary Test - ВТОРОЙ объёмный слив ПОСЛЕ climax: красная свеча,
        # делающая новый/равный минимум относительно climax (как будто уровень
        # пробили второй раз), с объёмом тоже выше нормы, но слабее climax.
        # Не просто "касание на любом объёме" - должен быть реальный повторный
        # слив, просто менее панический, чем первый.
        after_climax = pit_df[pit_df.index > climax_idx]
        after_climax_red = after_climax[after_climax['close'] < after_climax['open']]
        test_candidates = after_climax_red[
            (after_climax_red['low'] <= climax_low * 1.005) &
            (after_climax_red['volume'] >= baseline_vol) &
            (after_climax_red['volume'] < climax_vol * test_vol_ratio)
        ]
        if len(test_candidates) == 0:
            return None  # второго слива ещё не было - продавцы не подтвердили дно
        secondary_idx = test_candidates.index[-1]
        secondary_low = float(df.loc[secondary_idx, 'low'])

        # Вход возможен только ПОСЛЕ secondary test
        if current.name <= secondary_idx:
            return None

        if c_vol >= (climax_vol * confirm_mult):
            sl_price = min(climax_low, secondary_low)
            return {"action": "BUY", "sl": sl_price,
                    "reason": f"PitClimax: Confirm >= {int(confirm_mult*100)}% of Climax + Secondary Test"}

    elif direction == 'SHORT':
        if c_close < level['min']:
            return None
        if c_close >= c_open:
            return None

        if p_close > p_open:
            if c_close >= p_open:
                return None
        else:
            if c_close >= p_low:
                return None

        pit_start_time = level.get('_pit_start_time')
        if pit_start_time is not None and pit_start_time in df.index:
            spike_start = df.index.get_loc(pit_start_time)
        else:
            close_arr = df['close'].values
            below_idx = np.where(close_arr[:-1] < level['min'])[0]
            spike_start = int(below_idx[-1] + 1) if len(below_idx) > 0 else max(0, len(df) - 30)

        spike_df = df.iloc[spike_start:-1]
        if len(spike_df) < 1:
            return None

        green_candles = spike_df[spike_df['close'] > spike_df['open']]
        if len(green_candles) == 0:
            return None

        # Climax - ПЕРВАЯ зелёная свеча (памп) с реальным всплеском объёма
        climax_candidates = green_candles[green_candles['volume'] >= baseline_vol * climax_vol_mult]
        if len(climax_candidates) == 0:
            return None
        climax_idx = climax_candidates.index[0]
        climax_vol = float(df.loc[climax_idx, 'volume'])
        climax_high = float(df.loc[climax_idx, 'high'])

        # Secondary Test - ВТОРОЙ объёмный памп ПОСЛЕ climax, делающий
        # новый/равный максимум, но слабее climax
        after_climax = spike_df[spike_df.index > climax_idx]
        after_climax_green = after_climax[after_climax['close'] > after_climax['open']]
        test_candidates = after_climax_green[
            (after_climax_green['high'] >= climax_high * 0.995) &
            (after_climax_green['volume'] >= baseline_vol) &
            (after_climax_green['volume'] < climax_vol * test_vol_ratio)
        ]
        if len(test_candidates) == 0:
            return None
        secondary_idx = test_candidates.index[-1]
        secondary_high = float(df.loc[secondary_idx, 'high'])

        if current.name <= secondary_idx:
            return None

        if c_vol >= (climax_vol * confirm_mult):
            sl_price = max(climax_high, secondary_high)
            return {"action": "SELL", "sl": sl_price,
                    "reason": f"PitClimax: Confirm >= {int(confirm_mult*100)}% of Climax + Secondary Test"}

    return None