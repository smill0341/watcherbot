# -*- coding: utf-8 -*-
"""
v_bottom_watcher.py
====================
Стратегия V_BOTTOM. Два независимых метода поиска "пика продавцов",
оба работают В МОМЕНТЕ ЗАКРЫТИЯ свечи (без задержки, без заглядывания
в будущее) и оба ведут в один и тот же механизм "после пика ждём
зелёную с выкупом объёма":

  Дверь А (резкие): красная свеча — пик, если её объём в ANOMALY_MULT
           раз больше максимума пиков каскада.

  Дверь Б (тяжёлые/ступенчатые): красная свеча — пик, если её объём
           не ниже DOOR_B_PEAK_TOLERANCE_PCT % от максимума пиков
           каскада ("в той же лиге", не обязательно рекордный).

Обе двери проверяются на каждой красной свече, независимо друг от
друга. Если на одной и той же свече сработали обе — Дверь А в
приоритете (сработала первой в очерёдности проверки), Дверь Б её не
перезаписывает.

Вход возможен уже с ПЕРВОГО кандидата, вооружившегося любой дверью
(без "второй пик", без anchor-логики).

После пика ждём зелёную свечу с выкупом объёма >= VOL_MATCH_PCT %
до MAX_GAP_CANDLES свечей. Если во время ожидания встречается более
сильная красная свеча (объём выше текущего кандидата) — кандидат
подменяется на неё, ожидание продлевается. Не дождались за отведённое
число свечей — пик уходит в panic_peaks, продолжаем искать по
каскаду. Вход — по цене закрытия зелёной свечи выкупа.
"""

from .watcher_methods import _calc_tp_and_rr


class VBottomWatcher:
    CONFIG = {
        'WAKE_UP_VOL_MULT': 2.0,       # порог начала каскада + порог "кандидат в пик"
        'ANOMALY_MULT': 1.7,           # Дверь А: во сколько раз пик больше максимума пиков каскада
        'DOOR_B_PEAK_TOLERANCE_PCT': 90.0,  # Дверь Б: пик не ниже этого % от максимума пиков каскада
        'VOL_MATCH_PCT': 110.0,        # зелёная должна выкупить объём пика (в %)
        'MAX_GAP_CANDLES': 3,          # сколько свечей ждём зелёную после вооружения
        'MIN_DEPTH_PCT': 2.0,          # минимальная глубина пролива под уровнем

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
        self.state = "SEARCHING"

        self.panic_peaks = []
        self.steps_count = 0
        self.last_tracked_low = float('inf')

        self.cand_low = 0.0
        self.cand_high = 0.0
        self.cand_vol = 0.0
        self.door_used = None
        self.bars_since = 0

        self.debug_baseline = 0.0
        self.sl_price = None
        self.entry_price = None

    @staticmethod
    def _fmt(v):
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v / 1_000:.1f}k"
        return str(int(v))

    def _reset_cascade(self):
        self.state = "SEARCHING"
        self.panic_peaks = []
        self.steps_count = 0
        self.last_tracked_low = float('inf')

    # ------------------------------------------------------------------ LONG
    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, trend='UNKNOWN', vol_std=None):
        if self.state in ("DEAD", "TRIGGERED"):
            return None
        if not baseline_vol or baseline_vol <= 0:
            return None
        if self.trade_type != 'LONG':
            return None

        if c_low > self.max:
            self._reset_cascade()
            return None

        current_depth_pct = ((self.max - c_low) / self.max) * 100
        is_red = c_close < c_open

        if self.state == "SEARCHING":
            if is_red and c_vol >= (baseline_vol * self.CONFIG['WAKE_UP_VOL_MULT']):
                self.panic_peaks = [c_vol]
                self.steps_count = 1
                self.last_tracked_low = c_low
                self.debug_baseline = baseline_vol
                self.state = "TRACKING_CASCADE"
            return None

        if self.state not in ("TRACKING_CASCADE", "WAIT_GREEN"):
            return None

        result = None

        if self.state == "TRACKING_CASCADE" and is_red:
            if c_low < self.last_tracked_low:
                self.steps_count += 1
                self.last_tracked_low = c_low

            armed = False
            highest_peak_all = max(self.panic_peaks) if self.panic_peaks else c_vol

            # --- Дверь А: резкий выброс, x1.7 от максимума пиков каскада ---
            if c_vol >= (highest_peak_all * self.CONFIG['ANOMALY_MULT']) and current_depth_pct >= self.CONFIG['MIN_DEPTH_PCT']:
                self.cand_low, self.cand_high, self.cand_vol = c_low, c_high, c_vol
                self.door_used = 'A'
                self.bars_since = 0
                self.state = "WAIT_GREEN"
                armed = True

            # --- Дверь Б: пик "в той же лиге" (>= 90% от максимума пиков) ---
            if not armed:
                if c_vol >= (highest_peak_all * (self.CONFIG['DOOR_B_PEAK_TOLERANCE_PCT'] / 100.0)) and current_depth_pct >= self.CONFIG['MIN_DEPTH_PCT']:
                    self.cand_low, self.cand_high, self.cand_vol = c_low, c_high, c_vol
                    self.door_used = 'B'
                    self.bars_since = 0
                    self.state = "WAIT_GREEN"
                    armed = True

            if not armed and c_vol >= (baseline_vol * self.CONFIG['WAKE_UP_VOL_MULT']):
                self.panic_peaks.append(c_vol)

        elif self.state == "WAIT_GREEN":
            self.bars_since += 1
            if not is_red:
                result = self._check_green_and_maybe_enter(c_open, c_close, c_vol, all_opposite_levels)
            elif c_vol >= self.cand_vol:
                self.cand_low, self.cand_high, self.cand_vol = c_low, c_high, c_vol
                self.bars_since = 0

            if not result and self.bars_since > self.CONFIG['MAX_GAP_CANDLES']:
                self.panic_peaks.append(self.cand_vol)
                self.state = "TRACKING_CASCADE"

        return result

    def _check_green_and_maybe_enter(self, c_open, c_close, c_vol, all_opposite_levels):
        if c_close <= c_open:
            return None
        if c_vol < (self.cand_vol * (self.CONFIG['VOL_MATCH_PCT'] / 100.0)):
            return None

        self.state = "TRIGGERED"
        actual_entry = c_close
        actual_sl = self.cand_low * 0.998
        self.entry_price = actual_entry
        self.sl_price = actual_sl
        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            return {'error': err or "Risk data is None"}

        reason_str = (f"V-Дно [Дверь_{self.door_used} | Фон:{self._fmt(self.debug_baseline)} "
                      f"Пик:{self._fmt(self.cand_vol)} Зел:{self._fmt(c_vol)}]")
        return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}