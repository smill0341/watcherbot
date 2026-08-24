"""
watcher_methods.py
==================
Изолированные методы определения точки входа.
Каждая стратегия — независимая капсула со своими личными настройками (CONFIG).
В самом низу файла находится общий калькулятор _calc_tp_and_rr, 
который по команде стратегии считает Тейк-Профит и проверяет Risk/Reward.

Доступные методы:
    - ChochRetestWatcher / check_choch_zone: слом структуры + объем (SMC),
      ДВУХФАЗНЫЙ вход - CHoCH фиксирует зону происхождения импульса, вход только
      на возврате цены в эту зону (не на самой свече пробоя)
    - SweepReclaimWatcher: стейт-машина, ищет ложный пробой (вынос стопов) + возврат
    - check_pit_climax: ищет двойное дно/вершину с капитуляцией (Wyckoff)
"""

import pandas as pd
import numpy as np
from smartmoneyconcepts import smc
from typing import Optional

# =========================================================================
# МЕТОД 1: VOLUME REVERSAL (SMC) - CHoCH + Аномальный объём, ДВУХФАЗНЫЙ ВХОД
# =========================================================================
class ChochRetestWatcher:
    # Добавили CONFIG сюда, чтобы калькулятору было откуда брать настройки
    CONFIG = {
        'TP_MODE': 'structural',
        'FIXED_TP_PCT': 8.0,
        'TAKE_PROFIT': 8.0,
        'TP_BUFFER_PCT': 0.3,
        'SL_BUFFER': 0.2,
        'MIN_RR': 1.5,
        'USE_RR_FILTER': True,
    }

    def __init__(self, level_min, level_max, trade_type, retest_tolerance_pct=70.0,
                 max_retest_candles=40):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "WAIT_CHOCH"
        self.origin_low: Optional[float] = None
        self.origin_high: Optional[float] = None
        self.choch_close: Optional[float] = None
        self.candles_in_retest = 0
        self.retest_tolerance_pct = retest_tolerance_pct
        self.max_retest_candles = max_retest_candles

    def on_choch_detected(self, origin_low, origin_high, choch_close):
        if self.state != "WAIT_CHOCH":
            return
        self.origin_low = origin_low
        self.origin_high = origin_high
        self.choch_close = choch_close
        self.state = "WAIT_RETEST"
        self.candles_in_retest = 0

    # Обрати внимание: добавился аргумент all_opposite_levels для калькулятора
    def update(self, c_open, c_high, c_low, c_close, all_opposite_levels):
        if self.state in ("TRIGGERED", "DEAD", "WAIT_CHOCH"):
            return None

        if self.state == "WAIT_RETEST":
            if self.origin_low is None or self.origin_high is None or self.choch_close is None:
                self.state = "DEAD"
                return None

            self.candles_in_retest += 1
            if self.candles_in_retest > self.max_retest_candles:
                self.state = "DEAD"
                return None

            if self.trade_type == 'LONG':
                if c_close < self.min:
                    self.state = "DEAD"
                    return None
                impulse = self.choch_close - self.origin_low
                retrace_level = self.choch_close - impulse * (self.retest_tolerance_pct / 100)
                if c_low <= retrace_level and c_low >= self.origin_low and c_close > c_open:
                    self.state = "TRIGGERED"
                    # Считаем нормальный TP и SL через твой калькулятор
                    risk_data, err = _calc_tp_and_rr(c_close, self.origin_low, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    
                    return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": f"CHoCH+Retest {self.retest_tolerance_pct:.0f}% (через {self.candles_in_retest}св)"}
                            
            else:  # SHORT
                if c_close > self.max:
                    self.state = "DEAD"
                    return None
                impulse = self.origin_high - self.choch_close
                retrace_level = self.choch_close + impulse * (self.retest_tolerance_pct / 100)
                if c_high >= retrace_level and c_high <= self.origin_high and c_close < c_open:
                    self.state = "TRIGGERED"
                    
                    risk_data, err = _calc_tp_and_rr(c_close, self.origin_high, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    
                    return {"action": "SELL", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": f"CHoCH+Retest (через {self.candles_in_retest}св)"}

        return None

def check_choch_zone(df, level, direction):
    CONFIG = {
        'SWING_LENGTH': 10,       # Снизили до 10, чтобы бот не был слепым
        'BASELINE_BARS': 200,
        'VOLUME_MULTIPLIER': 2.0, # Снизили до 2.0 (классика)
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

    # БЫСТРАЯ ПРОВЕРКА: сразу рубим, если объема нет (чтобы тест летал)
    vol_at_break = float(df['volume'].iloc[now_idx])
    if vol_at_break < (baseline_vol * vol_mult):
        return None

    # Тяжелая математика только если объем прошел
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
        if len(bullish) == 0:
            return None

        # МЫ УБРАЛИ c_close_now > level['max']
        c_close_now = float(df['close'].iloc[now_idx])
        origin_low = float(bullish['Level'].iloc[0])
        origin_high = float(df['high'].iloc[now_idx])
        return {"origin_low": origin_low, "origin_high": origin_high, "choch_close": c_close_now}

    elif direction == 'SHORT':
        bearish = broken_now[broken_now['CHOCH'] == -1]
        if len(bearish) == 0:
            return None

        # МЫ УБРАЛИ c_close_now < level['min']
        c_close_now = float(df['close'].iloc[now_idx])
        origin_high = float(bearish['Level'].iloc[0])
        origin_low = float(df['low'].iloc[now_idx])
        return {"origin_low": origin_low, "origin_high": origin_high, "choch_close": c_close_now}

    return None

# =========================================================================
# МЕТОД 3: PIT_CLIMAX (Wyckoff Selling Climax/Spring + Test)
# =========================================================================
def check_pit_climax(df, level, direction, all_opposite_levels):
    # ---------------------------------------------------------
    # ЛИЧНЫЕ НАСТРОЙКИ СТРАТЕГИИ PIT_CLIMAX
    # ---------------------------------------------------------
    CONFIG = {
        'CLIMAX_VOL_MULT': 3.5,   # Множитель паники
        'TEST_VOL_RATIO': 0.4,    # Объем второго удара (50% от первого)
        'MIN_GAP': 6,             # Важно: минимум 5 свечей между ударами, чтобы ждать каскад
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


# =========================================================================
# МЕТОД 4: PANIC TRAP (Вторичный тест дна/вершины)
# =========================================================================
class PanicTrapWatcher:
    CONFIG = {
        'CLIMAX_VOL_MULT': 2.0,   
        'MIN_GAP': 8,             # ВОТ ОНО! Минимальное кол-во свечей между красным и желтым кругом
        'RETEST_VOL_DECAY_MAX': 0.5,   # объём подтверждающей свечи должен быть < 50% от объёма последней climax-ноги
        'MIN_LEGS_FOR_DECAY': 2,       # фильтр включается либо в DOWN(LONG)/UP(SHORT), либо если каскад уже >= N ног (локальный обвал, даже если макро-тренд считается RANGE)
        'MAX_WAIT_FOR_DECAY': 24,      # после стольки свечей ожидания тихого ретеста — берём сделку по факту, а не теряем её насовсем
        'TP_MODE': 'fixed_pct',
        'FIXED_TP_PCT': 10.0,     
        'TAKE_PROFIT': 10.0,
        'TP_BUFFER_PCT': 0.0,
        'SL_BUFFER': 0.5,         
        'MIN_RR': 1.0,
        'USE_RR_FILTER': False,   
    }

    def __init__(self, level_min, level_max, trade_type):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "WAIT_CLIMAX"
        self.entry_price = None
        self.sl_price = None
        self.climax_extreme = None
        self.bars_since_climax = 0
        self.climax_vol = None     # объём последней (самой глубокой) climax-ноги каскада
        self.legs_count = 0        # сколько раз climax_extreme обновлялся ниже/выше — глубина каскада
        self.total_wait_bars = 0   # сколько свечей ждём тихий ретест с самого начала каскада (не сбрасывается)

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, trend='UNKNOWN'):
        if self.state in ["DEAD", "TRIGGERED"]:
            return None

        if self.trade_type == 'LONG':
            if self.state == "WAIT_CLIMAX":
                if c_low > self.max: return None 
                if c_close < c_open and c_vol >= baseline_vol * self.CONFIG['CLIMAX_VOL_MULT']:
                    self.state = "WAIT_GREEN"
                    self.climax_extreme = c_low 
                    self.climax_vol = c_vol
                    self.legs_count = 1
                    self.bars_since_climax = 0
                return None

            elif self.state == "WAIT_GREEN":
                self.bars_since_climax += 1
                self.total_wait_bars += 1
                if c_close < c_open:
                    prev_extreme = self.climax_extreme if self.climax_extreme is not None else c_low
                    new_extreme = min(prev_extreme, c_low)
                    if new_extreme < prev_extreme:
                        # цена пробила предыдущее дно каскада — это новая, более глубокая climax-нога.
                        # Именно её объём становится новой базой для сравнения на "тихий" ретест.
                        self.climax_vol = c_vol
                        self.legs_count += 1
                    self.climax_extreme = new_extreme
                    self.bars_since_climax = 0 # Обновляем красное дно - счетчик сбрасывается
                    return None
                if c_close > c_open:
                    # Доп. подтверждение только для DOWN-тренда: продавцы должны реально выдохнуться.
                    # Если зелёная свеча идёт всё ещё на заметном объёме — это не тихий ретест,
                    # а просто отскок внутри каскада. Не считаем это подтверждением, ждём дальше.
                    need_decay_check = (trend == 'DOWN' or self.legs_count >= self.CONFIG['MIN_LEGS_FOR_DECAY']) and self.climax_vol
                    timed_out = self.total_wait_bars >= self.CONFIG['MAX_WAIT_FOR_DECAY']
                    if need_decay_check and not timed_out:
                        decay_ratio = c_vol / self.climax_vol
                        if decay_ratio > self.CONFIG['RETEST_VOL_DECAY_MAX']:
                            return None
                    # Если timed_out — ловим сделку по факту (не идеальный тихий ретест,
                    # но лучше поймать её, чем потерять насовсем), помечаем это в reason ниже.
                    self.state = "TRAP_SET"
                    safe_extreme = self.climax_extreme if self.climax_extreme is not None else c_low
                    self.entry_price = safe_extreme * 1.001
                    self.sl_price = safe_extreme * 0.998
                return None

            elif self.state == "TRAP_SET":
                self.bars_since_climax += 1
                
                # Если цена перебила дно ДО ретеста - это не ретест, отменяем и ждем новый отскок
                if c_low < self.climax_extreme:
                    self.state = "WAIT_GREEN"
                    self.climax_extreme = c_low
                    self.climax_vol = c_vol
                    self.legs_count += 1
                    self.bars_since_climax = 0
                    return None

                # Блокировка: ждем чтобы сформировался разрыв до желтого круга!
                if self.bars_since_climax < self.CONFIG['MIN_GAP']:
                    return None

                if c_low <= self.entry_price:
                    self.state = "TRIGGERED"
                    risk_data, err = _calc_tp_and_rr(self.entry_price, self.sl_price, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    fallback_tag = " [fallback:timeout]" if self.total_wait_bars >= self.CONFIG['MAX_WAIT_FOR_DECAY'] else ""
                    return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": f"Капкан (Пауза: {self.bars_since_climax}св, ног:{self.legs_count}){fallback_tag}",
                            "legs_count": self.legs_count, "trend_at_entry": trend}
            return None

        elif self.trade_type == 'SHORT':
            if self.state == "WAIT_CLIMAX":
                if c_high < self.min: return None 
                if c_close > c_open and c_vol >= baseline_vol * self.CONFIG['CLIMAX_VOL_MULT']:
                    self.state = "WAIT_RED"
                    self.climax_extreme = c_high
                    self.climax_vol = c_vol
                    self.legs_count = 1
                    self.bars_since_climax = 0
                return None

            elif self.state == "WAIT_RED":
                self.bars_since_climax += 1
                self.total_wait_bars += 1
                if c_close > c_open:
                    prev_extreme = self.climax_extreme if self.climax_extreme is not None else c_high
                    new_extreme = max(prev_extreme, c_high)
                    if new_extreme > prev_extreme:
                        # цена пробила предыдущий пик каскада — новая, более высокая climax-нога.
                        self.climax_vol = c_vol
                        self.legs_count += 1
                    self.climax_extreme = new_extreme
                    self.bars_since_climax = 0
                    return None
                if c_close < c_open:
                    # Зеркальный фильтр для SHORT: актуален в UP-тренде (растущий нож).
                    need_decay_check = (trend == 'UP' or self.legs_count >= self.CONFIG['MIN_LEGS_FOR_DECAY']) and self.climax_vol
                    timed_out = self.total_wait_bars >= self.CONFIG['MAX_WAIT_FOR_DECAY']
                    if need_decay_check and not timed_out:
                        decay_ratio = c_vol / self.climax_vol
                        if decay_ratio > self.CONFIG['RETEST_VOL_DECAY_MAX']:
                            return None
                    self.state = "TRAP_SET"
                    safe_extreme = self.climax_extreme if self.climax_extreme is not None else c_high
                    self.entry_price = safe_extreme * 0.999
                    self.sl_price = safe_extreme * 1.002
                return None

            elif self.state == "TRAP_SET":
                self.bars_since_climax += 1
                
                if c_high > self.climax_extreme:
                    self.state = "WAIT_RED"
                    self.climax_extreme = c_high
                    self.climax_vol = c_vol
                    self.legs_count += 1
                    self.bars_since_climax = 0
                    return None

                if self.bars_since_climax < self.CONFIG['MIN_GAP']:
                    return None

                if c_high >= self.entry_price:
                    self.state = "TRIGGERED"
                    risk_data, err = _calc_tp_and_rr(self.entry_price, self.sl_price, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    fallback_tag = " [fallback:timeout]" if self.total_wait_bars >= self.CONFIG['MAX_WAIT_FOR_DECAY'] else ""
                    return {"action": "SELL", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": f"Капкан (Пауза: {self.bars_since_climax}св, ног:{self.legs_count}){fallback_tag}",
                            "legs_count": self.legs_count, "trend_at_entry": trend}
            return None

        return None