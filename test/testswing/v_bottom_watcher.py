# -*- coding: utf-8 -*-
from .watcher_methods import _calc_tp_and_rr

class VBottomWatcher:
    CONFIG = {
        'ELEVATED_VOL_MULT': 2.0,
        'PEAK_TOLERANCE_PCT': 85.0,   
        'MAX_REBREACHES': 5.0,          
        'BREATH_BUFFER_PCT': 3.0,     
        'VOL_MATCH_PCT': 101.0,
        'TP_MODE': 'fixed_pct',
        'FIXED_TP_PCT': 10.0,
        'TAKE_PROFIT': 10.0,
        'TP_BUFFER_PCT': 0.0,
        'SL_BUFFER': 0.5,
        'MIN_RR': 1.0,
        'USE_RR_FILTER': False,
        'DEBUG': True,
    }

    def __init__(self, level_min: float, level_max: float, trade_type: str):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "SEARCHING"

        self.tracker_vol = 0.0
        self.start_vol = 0.0
        self.cand_low = 0.0
        self.cand_vol = 0.0
        self.breach_count = 0     
        self.sl_price: float | None = None
        self.entry_price: float | None = None

        self.history_log = ""
        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None

    def _tp(self):
        return f"{self._last_time} " if self._last_time is not None else ""

    def _dbg(self, msg):
        self.last_event_time = self._last_time
        self.last_event_msg = msg
        with open("v_bottom_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{self._tp()}[{self.max:.4f}] {msg}\n")

    @staticmethod
    def _fmt(v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000: return f"{v/1_000:.1f}k"
        return str(int(v))

    def _reset_chain(self):
        self.state = "SEARCHING"
        self.tracker_vol = 0.0
        self.start_vol = 0.0
        self.cand_low = 0.0
        self.cand_vol = 0.0
        self.history_log = ""

    def on_breach_start(self):
        if self.state in ("DEAD", "TRIGGERED"):
            return
        self.breach_count += 1  
        if self.breach_count > self.CONFIG['MAX_REBREACHES']:
            self._dbg(f"💀 [ЛИМИТ] повторное пробитие #{self.breach_count} сверх лимита -> DEAD")
            self.state = "DEAD"
            return
        if self.CONFIG.get('DEBUG'):
            self._dbg(f"🔄 [ПОВТОРНОЕ ПРОБИТИЕ #{self.breach_count}] математика с нуля")
        self._reset_chain()

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, **kwargs):
        self._last_time = kwargs.get('candle_time')
        
        if self.state in ("DEAD", "TRIGGERED"):
            return None

        if not baseline_vol or baseline_vol <= 0:
            return None

        if self.trade_type != 'LONG':
            return None

        # --- 1. ПРОВЕРКА ОТМЕНЫ (СБРОС) ---
        buffer_top = self.max * (1 + self.CONFIG['BREATH_BUFFER_PCT'] / 100.0)
        if c_low > buffer_top or c_close > buffer_top:
            if self.state != "SEARCHING":
                if self.history_log:
                    self._dbg(f"🛑 [СРЫВ] {self.history_log} -> [ОТМЕНА: цена ушла выше буфера ({self.CONFIG['BREATH_BUFFER_PCT']}%)]")
                self._reset_chain()
            return None
            
        if c_low > self.max:
            return None

        is_red = c_close < c_open

        if self.state == "SEARCHING":
            if is_red and self.CONFIG.get('DEBUG'):
                self._dbg(f"[ищем Ориентир] red vol={c_vol:.0f} need>={baseline_vol * self.CONFIG['ELEVATED_VOL_MULT']:.0f} (фон={baseline_vol:.0f})")
            if is_red and c_vol >= (baseline_vol * self.CONFIG['ELEVATED_VOL_MULT']):
                self.tracker_vol = float(c_vol)
                self.state = "WAIT_START"
                self.history_log = f"Фон:{self._fmt(baseline_vol)} -> Ориентир:{self._fmt(c_vol)}"
                if self.CONFIG.get('DEBUG'):
                    self._dbg(self.history_log)
            return None

        elif self.state == "WAIT_START":
            if is_red and self.CONFIG.get('DEBUG'):
                self._dbg(f"[ищем Старт] red vol={c_vol:.0f} need>{self.tracker_vol:.0f}")
            if is_red and c_vol > self.tracker_vol:
                self.start_vol = float(c_vol)
                self.state = "WAIT_PEAK"
                self.history_log += f" -> Старт:{self._fmt(c_vol)}"
                if self.CONFIG.get('DEBUG'):
                    self._dbg(self.history_log)
            return None

        elif self.state == "WAIT_PEAK":
            need_peak = self.start_vol * (self.CONFIG['PEAK_TOLERANCE_PCT'] / 100.0)
            
            if is_red and self.CONFIG.get('DEBUG'):
                self._dbg(f"[ищем Пик1] red vol={c_vol:.0f} need>={need_peak:.0f} (старт={self.start_vol:.0f})")
                
            if is_red and c_vol >= need_peak:
                self.cand_low = float(c_low)
                self.cand_vol = float(c_vol) if c_vol > self.start_vol else float(self.start_vol)
                self.state = "WAIT_NEW_PEAK"
                self.history_log += f" -> Пик:{self._fmt(c_vol)}"
                if self.CONFIG.get('DEBUG'):
                    self._dbg(self.history_log)
            return None

        elif self.state == "WAIT_GREEN":
            if c_close > c_open:
                need_green = self.cand_vol * (self.CONFIG['VOL_MATCH_PCT'] / 100.0)
                
                if self.CONFIG.get('DEBUG'):
                    self._dbg(f"[ТЕСТ ВЫКУПА] Зел. vol={c_vol:.0f}, надо>={need_green:.0f} (эталон={self.cand_vol:.0f})")

                if c_vol >= need_green:
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🟢 ОБЪЕМ ПРОЙДЕН! Вызываем _enter()")
                    return self._enter(c_close, c_vol, all_opposite_levels)
                
                if self.CONFIG.get('DEBUG'):
                    self._dbg(f"🔴 Не хватило объема. Сброс в WAIT_NEW_PEAK.")
                self.state = "WAIT_NEW_PEAK"
            elif c_vol >= (self.cand_vol * (self.CONFIG['PEAK_TOLERANCE_PCT'] / 100.0)):
                self.cand_low = min(self.cand_low, float(c_low))
                if c_vol > self.cand_vol:
                    self.cand_vol = float(c_vol)
                self.history_log += f" -> Пик+:{self._fmt(c_vol)}"
                if self.CONFIG.get('DEBUG'):
                    self._dbg(self.history_log)
            else:
                self.state = "WAIT_NEW_PEAK"
            return None

        elif self.state == "WAIT_NEW_PEAK":
            if is_red and c_vol >= (self.cand_vol * (self.CONFIG['PEAK_TOLERANCE_PCT'] / 100.0)):
                self.cand_low = min(self.cand_low, float(c_low))
                if c_vol > self.cand_vol:
                    self.cand_vol = float(c_vol)
                self.state = "WAIT_GREEN"
                self.history_log += f" -> Пик+:{self._fmt(c_vol)}"
                if self.CONFIG.get('DEBUG'):
                    self._dbg(f"{self.history_log} (новый пик)")
            return None

        return None

    def _enter(self, c_close, c_vol, all_opposite_levels):
        self.state = "TRIGGERED"
        actual_entry = float(c_close)
        
        safe_cand_low = float(self.cand_low) if self.cand_low else actual_entry
        actual_sl = safe_cand_low * 0.998

        if self.CONFIG.get('DEBUG'):
            self._dbg(f"🚪 Попытка входа! Entry: {actual_entry:.4f}, SL: {actual_sl:.4f}")

        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            if self.CONFIG.get('DEBUG'):
                self._dbg(f"❌ Калькулятор УБИЛ сделку: {err}")
            return {'error': err or "Risk data is None"}

        self.entry_price = actual_entry
        self.sl_price = risk_data['sl']

        self.history_log += f" -> Зел:{self._fmt(c_vol)}(ВХОД!)"
        reason_str = f"V-Дно [{self.history_log}]"

        if self.CONFIG.get('DEBUG'):
            self._dbg(f"🚀 СДЕЛКА УСПЕШНО СФОРМИРОВАНА: {reason_str}")

        return {"action": "BUY", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}