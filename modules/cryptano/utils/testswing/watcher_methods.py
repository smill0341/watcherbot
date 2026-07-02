"""
watcher_methods.py
==================
Изолированные методы определения точки входа.
Каждая стратегия — независимая капсула со своими личными настройками (CONFIG).
В самом низу файла находится общий калькулятор _calc_tp_and_rr, 
который по команде стратегии считает Тейк-Профит и проверяет Risk/Reward.

Доступные методы:
    - SweepReclaimWatcher: стейт-машина, ищет ложный пробой (вынос стопов) + возврат
    - check_volume_reversal: ищет слом структуры с аномальным объемом (SMC)
    - check_pit_climax: ищет двойное дно/вершину с капитуляцией (Wyckoff)
"""

import pandas as pd
import numpy as np
from smartmoneyconcepts import smc

# =========================================================================
# МЕТОД 1: SWEEP / RECLAIM (вынос стопов + возврат)
# =========================================================================
class SweepReclaimWatcher:
    """
    Стейт-машина на один уровень. Хранит состояние между вызовами update().
    """
    # ---------------------------------------------------------
    # ЛИЧНЫЕ НАСТРОЙКИ СТРАТЕГИИ SWEEP_RECLAIM
    # ---------------------------------------------------------
    CONFIG = {
        'ALLOW_BOUNCE': False,    # Касание+Отказ (без пробоя)
        'ALLOW_SWEEP': True,      # Настоящий вынос за уровень и возврат
        'TP_MODE': 'structural',  # 'structural' или 'fixed_pct'
        'FIXED_TP_PCT': 4.0,      # Если TP_MODE = 'fixed_pct'
        'TAKE_PROFIT': 4.0,       # Fallback, если структурного уровня нет
        'TP_BUFFER_PCT': 0.5,     # Не долетаем до структурного уровня на этот %
        'SL_BUFFER': 1.0,         # Отступ для стоп-лосса (в %)
        'MIN_RR': 1.0,            # Минимальный Risk/Reward
        'USE_RR_FILTER': True,    # Включить/выключить проверку R/R
    }

    def __init__(self, level_min, level_max, trade_type):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "FRESH"
        self.extreme_price = None
        self.candles_in_sweep = 0

    def update(self, c_open, c_high, c_low, c_close, all_opposite_levels):
        if self.state in ["DEAD", "TRIGGERED"]:
            return None

        if self.trade_type == 'LONG':
            if self.state == "FRESH":
                touched = c_low <= self.max
                if not touched:
                    return None

                if c_close < self.min:
                    if not self.CONFIG['ALLOW_SWEEP']: return None
                    self.state = "BELOW"
                    self.extreme_price = c_low
                    self.candles_in_sweep = 1
                elif c_close > c_open:
                    if not self.CONFIG['ALLOW_BOUNCE']: return None
                    self.state = "TRIGGERED"
                    risk_data, err = _calc_tp_and_rr(c_close, c_low, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": "Касание + Отказ (Bounce)", "is_real_sweep": False, "overshoot_pct": 0.0, "candles_in_sweep": 0}

            elif self.state == "BELOW":
                self.candles_in_sweep += 1
                self.extreme_price = min(self.extreme_price if self.extreme_price else c_low, c_low)
                    
                if c_close > self.min:
                    self.state = "TRIGGERED"
                    overshoot_pct = ((self.min - self.extreme_price) / self.min) * 100
                    risk_data, err = _calc_tp_and_rr(c_close, self.extreme_price, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": "Возврат (Reclaim выноса)", "is_real_sweep": True, "overshoot_pct": overshoot_pct, "candles_in_sweep": self.candles_in_sweep}

        elif self.trade_type == 'SHORT':
            if self.state == "FRESH":
                touched = c_high >= self.min
                if not touched:
                    return None

                if c_close > self.max:
                    if not self.CONFIG['ALLOW_SWEEP']: return None
                    self.state = "ABOVE"
                    self.extreme_price = c_high
                    self.candles_in_sweep = 1
                elif c_close < c_open:
                    if not self.CONFIG['ALLOW_BOUNCE']: return None
                    self.state = "TRIGGERED"
                    risk_data, err = _calc_tp_and_rr(c_close, c_high, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    return {"action": "SELL", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": "Касание + Отказ (Bounce)", "is_real_sweep": False, "overshoot_pct": 0.0, "candles_in_sweep": 0}

            elif self.state == "ABOVE":
                self.candles_in_sweep += 1
                self.extreme_price = max(self.extreme_price if self.extreme_price else c_high, c_high)
                    
                if c_close < self.max:
                    self.state = "TRIGGERED"
                    overshoot_pct = ((self.extreme_price - self.max) / self.max) * 100
                    risk_data, err = _calc_tp_and_rr(c_close, self.extreme_price, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    return {"action": "SELL", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": "Возврат (Reclaim выноса)", "is_real_sweep": True, "overshoot_pct": overshoot_pct, "candles_in_sweep": self.candles_in_sweep}

        return None


# =========================================================================
# МЕТОД 2: VOLUME_REVERSAL (CHoCH + Аномальный объем SMC)
# =========================================================================
def check_volume_reversal(df, level, direction, all_opposite_levels):
    # ---------------------------------------------------------
    # ЛИЧНЫЕ НАСТРОЙКИ СТРАТЕГИИ VOLUME_REVERSAL
    # ---------------------------------------------------------
    CONFIG = {
        'SWING_LENGTH': 15,       # Важно: 15, чтобы ослепнуть к микро-отскокам
        'BASELINE_BARS': 200,     # База объема
        'VOLUME_MULTIPLIER': 1.5, # Во сколько раз объем должен превышать базу
        'TP_MODE': 'structural',
        'FIXED_TP_PCT': 8.0,
        'TAKE_PROFIT': 8.0,
        'TP_BUFFER_PCT': 0.3,
        'SL_BUFFER': 0.2,         # Отступ стопа
        'MIN_RR': 2.0,            # Минимальный Risk/Reward
        'USE_RR_FILTER': True,
    }
    
    swing_len = CONFIG['SWING_LENGTH']
    base_bars = CONFIG['BASELINE_BARS']
    vol_mult = CONFIG['VOLUME_MULTIPLIER']

    min_len = base_bars + swing_len * 4 + 10
    if len(df) < min_len:
        return None

    now_idx = len(df) - 1
    baseline_vol = df['volume'].iloc[-(base_bars + 2):-2].mean()
    if baseline_vol <= 0:
        return None

    df_smc = pd.DataFrame({
        'open': df['open'], 'high': df['high'], 'low': df['low'],
        'close': df['close'], 'volume': df['volume']
    })

    sh = pd.DataFrame(smc.swing_highs_lows(df_smc, swing_length=swing_len))
    bc = pd.DataFrame(smc.bos_choch(df_smc, sh, close_break=True))

    broken_now = bc[bc['BrokenIndex'] == now_idx]
    if len(broken_now) == 0:
        return None

    if direction == 'LONG':
        bullish = broken_now[broken_now['CHOCH'] == 1]
        if len(bullish) == 0: return None

        c_close_now = float(df['close'].iloc[now_idx])
        if c_close_now > level['max']: return None 

        vol_at_break = float(df['volume'].iloc[now_idx])
        if vol_at_break < (baseline_vol * vol_mult): return None 

        raw_sl = float(bullish['Level'].iloc[0])
        risk_data, err = _calc_tp_and_rr(c_close_now, raw_sl, direction, all_opposite_levels, CONFIG)
        if err or not risk_data: return {'error': err or "Risk data is None"}
        return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": f"CHoCH + Vol x{vol_mult} confirm"}

    elif direction == 'SHORT':
        bearish = broken_now[broken_now['CHOCH'] == -1]
        if len(bearish) == 0: return None

        c_close_now = float(df['close'].iloc[now_idx])
        if c_close_now < level['min']: return None

        vol_at_break = float(df['volume'].iloc[now_idx])
        if vol_at_break < (baseline_vol * vol_mult): return None

        raw_sl = float(bearish['Level'].iloc[0])
        risk_data, err = _calc_tp_and_rr(c_close_now, raw_sl, direction, all_opposite_levels, CONFIG)
        if err or not risk_data: return {'error': err or "Risk data is None"}
        return {"action": "SELL", "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": f"CHoCH + Vol x{vol_mult} confirm"}

    return None


# =========================================================================
# МЕТОД 3: PIT_CLIMAX (Wyckoff Selling Climax/Spring + Test)
# =========================================================================
def check_pit_climax(df, level, direction, all_opposite_levels):
    # ---------------------------------------------------------
    # ЛИЧНЫЕ НАСТРОЙКИ СТРАТЕГИИ PIT_CLIMAX
    # ---------------------------------------------------------
    CONFIG = {
        'CLIMAX_VOL_MULT': 2.0,   # Множитель паники
        'TEST_VOL_RATIO': 0.5,    # Объем второго удара (50% от первого)
        'MIN_GAP': 5,             # Важно: минимум 5 свечей между ударами, чтобы ждать каскад
        'BASELINE_BARS': 52,
        'TP_MODE': 'fixed_pct',
        'FIXED_TP_PCT': 8.0,
        'TAKE_PROFIT': 8.0,
        'TP_BUFFER_PCT': 0.3,
        'SL_BUFFER': 0.2,
        'MIN_RR': 1.5,
        'USE_RR_FILTER': True,
    }
    
    panic_mult = CONFIG['CLIMAX_VOL_MULT']
    test_vol_ratio = CONFIG['TEST_VOL_RATIO']
    min_gap = CONFIG['MIN_GAP']
    base_bars = CONFIG['BASELINE_BARS']

    if len(df) < base_bars + 3:
        return None

    baseline_vol = float(df['volume'].iloc[-(base_bars):-2].mean())
    if baseline_vol <= 0:
        return None

    c = df.iloc[-1]
    c_vol = float(c['volume'])
    c_close, c_open = float(c['close']), float(c['open'])
    c_low, c_high = float(c['low']), float(c['high'])

    if direction == 'LONG':
        if c_close > level['max']: return None
        if c_close >= c_open: return None 
        if c_vol < (baseline_vol * 1.5): return None

        search_len = min(40, len(df) - 2)
        for i in range(1, search_len + 1):
            p = df.iloc[-1 - i]
            p_vol = float(p['volume'])
            p_close, p_open = float(p['close']), float(p['open'])
            p_low = float(p['low'])

            if p_close < p_open and p_vol >= (baseline_vol * panic_mult):
                if i >= min_gap:
                    middle_df = df.iloc[-i : -1]
                    if len(middle_df) > 0 and float(middle_df['low'].min()) < p_low:
                        break 
                    if c_vol >= (p_vol * test_vol_ratio):
                        if c_low <= p_low * 1.01:
                            raw_sl = min(p_low, c_low)
                            risk_data, err = _calc_tp_and_rr(c_close, raw_sl, direction, all_opposite_levels, CONFIG)
                            if err or not risk_data: return {'error': err or "Risk data is None"}
                            return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": f"Двойное красное дно (gap={i})"}
                break
        return None

    elif direction == 'SHORT':
        if c_close < level['min']: return None
        if c_close <= c_open: return None 
        if c_vol < (baseline_vol * 1.5): return None

        search_len = min(40, len(df) - 2)
        for i in range(1, search_len + 1):
            p = df.iloc[-1 - i]
            p_vol = float(p['volume'])
            p_close, p_open = float(p['close']), float(p['open'])
            p_high = float(p['high'])

            if p_close > p_open and p_vol >= (baseline_vol * panic_mult):
                if i >= min_gap:
                    middle_df = df.iloc[-i : -1]
                    if len(middle_df) > 0 and float(middle_df['high'].max()) > p_high:
                        break 
                    if c_vol >= (p_vol * test_vol_ratio):
                        if c_high >= p_high * 0.99:
                            raw_sl = max(p_high, c_high)
                            risk_data, err = _calc_tp_and_rr(c_close, raw_sl, direction, all_opposite_levels, CONFIG)
                            if err or not risk_data: return {'error': err or "Risk data is None"}
                            return {"action": "SELL", "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": f"Двойная зеленая вершина (gap={i})"}
                break
        return None

    return None


# =========================================================================
# УНИВЕРСАЛЬНЫЙ КАЛЬКУЛЯТОР РИСКОВ (Бухгалтер)
# =========================================================================
def _calc_tp_and_rr(entry_price, sl, trade_type, all_opposite_levels, config):
    """
    Тупой калькулятор. Берет личный конфиг стратегии, считает Тейк-Профит, 
    применяет буфер к Стоп-Лоссу и проверяет, проходит ли сделка по Risk/Reward.
    Возвращает: ( {'sl': float, 'tp': float}, 'Причина ошибки если не прошел' )
    """
    sl_buffer_pct = config.get('SL_BUFFER', 0.0)
    
    if trade_type == 'LONG':
        sl_adj = sl * (1 - sl_buffer_pct / 100)
        sl_adj = sl_adj * 0.998 # Микро-запас от проскальзывания
        risk = entry_price - sl_adj
    else:
        sl_adj = sl * (1 + sl_buffer_pct / 100)
        sl_adj = sl_adj * 1.002
        risk = sl_adj - entry_price

    if risk <= 0:
        return None, "Invalid risk (SL >= entry)"

    tp_mode = config.get('TP_MODE', 'structural')
    min_rr = config.get('MIN_RR', 1.5)

    if tp_mode == 'fixed_pct':
        fixed_pct = config.get('FIXED_TP_PCT', 8.0)
        if trade_type == 'LONG':
            tp = entry_price * (1 + fixed_pct / 100)
            reward = tp - entry_price
        else:
            tp = entry_price * (1 - fixed_pct / 100)
            reward = entry_price - tp
    else: # structural
        tp_buffer_pct = config.get('TP_BUFFER_PCT', 0.3)
        fallback_tp_pct = config.get('TAKE_PROFIT', 8.0)

        if trade_type == 'LONG':
            candidates = [lvl['min'] for lvl in all_opposite_levels if lvl['min'] > entry_price]
            structural_level = min(candidates) if candidates else None
            tp = structural_level * (1 - tp_buffer_pct / 100) if structural_level else entry_price * (1 + fallback_tp_pct / 100)
            reward = tp - entry_price
        else:
            candidates = [lvl['max'] for lvl in all_opposite_levels if lvl['max'] < entry_price]
            structural_level = max(candidates) if candidates else None
            tp = structural_level * (1 + tp_buffer_pct / 100) if structural_level else entry_price * (1 - fallback_tp_pct / 100)
            reward = entry_price - tp

    if config.get('USE_RR_FILTER', True):
        rr = reward / risk if risk > 0 else 0
        if rr < min_rr:
            return None, f"Poor R/R: {rr:.2f} < {min_rr}"

    return {"sl": sl_adj, "tp": tp}, None