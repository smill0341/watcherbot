# -*- coding: utf-8 -*-
from .watcher_methods import _calc_tp_and_rr

class SFPWatcher:
    CONFIG = {
        # ==========================================
        # МАКРО-ФИЛЬТР (Старт)
        # ==========================================
        'MIN_PUMP_HEIGHT_PCT': 1.5,      # Снижено! Ждем прокола на 1.5% над уровнем
        'MAX_DROP_BEFORE_PUMP_PCT': 2.0, # Защита от зависания: если упали на 2% ниже уровня - отмена
        
        # ==========================================
        # CHoCH (СЛОМ СТРУКТУРЫ)
        # ==========================================
        'CHOCH_MIN_VOL_MULT': 1.0,       # Объем пробойной свечи должен быть просто выше медианы

        # ==========================================
        # НАСТРОЙКИ РИСКА И ВЫХОДА
        # ==========================================
        'TP_MODE': 'fixed_pct',
        'FIXED_TP_PCT': 7.0,
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
        
        self.state = "WAIT_PUMP"
        self.peak_high = 0.0
        self.active_swing_low = None # Опорный структурный минимум
        
        self.vol_history = []
        
        self.sl_price = None
        self.entry_price = None
        self.history_log = ""
        
        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None
        self.last_event_type = None

    def _tp(self): return f"{self._last_time} " if self._last_time else ""

    def _fmt(self, v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000: return f"{v/1_000:.1f}k"
        return str(int(v))

    def _dbg(self, msg):
        self.last_event_time = self._last_time
        self.last_event_msg = msg
        if self.CONFIG.get('DEBUG'):
            with open("v_red_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{self._tp()}[{self.min:.4f}] {msg}\n")

    def on_breach_start(self):
        if self.state not in ("DEAD", "TRIGGERED"):
            self.state = "WAIT_PUMP"
            self.active_swing_low = None

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, **kwargs):
        self._last_time = kwargs.get('candle_time')
        self.last_event_time = self._last_time  
        self.last_event_type = None
        
        if self.state in ("DEAD", "TRIGGERED"): return None
        if self.trade_type != 'SHORT': return None

        high_val = float(c_high)
        close_val = float(c_close)
        
        # Забираем актуальный Swing Low, рассчитанный глобально
        current_swing_low = kwargs.get('swing_low')
        
        # --- ИЗОЛИРОВАННЫЙ УМНЫЙ ОБЪЕМ (МЕДИАНА) ---
        self.vol_history.append(float(c_vol))
        if len(self.vol_history) > 20:
            self.vol_history.pop(0)
            
        if len(self.vol_history) == 20:
            sorted_vols = sorted(self.vol_history)
            safe_baseline = (sorted_vols[9] + sorted_vols[10]) / 2.0
        else:
            safe_baseline = float(baseline_vol) if baseline_vol else 0.0001

        # Постоянный трекинг максимума пампа
        if self.state != "WAIT_PUMP":
            if high_val > self.peak_high:
                self.peak_high = high_val
                # Если сделали перехай, обновляем и опорный откат (Swing Low)
                if current_swing_low and current_swing_low > 0:
                    self.active_swing_low = float(current_swing_low)
                    self.last_event_type = "PEAK"

        # === ЗАЩИТА ОТ ЗАВИСАНИЯ ===
        # Если бот коснулся уровня, не смог запампить на нужную высоту и просто упал вниз
        if self.state == "WAIT_PUMP" and close_val < self.min * (1 - self.CONFIG['MAX_DROP_BEFORE_PUMP_PCT'] / 100.0):
            self.state = "DEAD"
            self._dbg(f"💀 Отмена пампа: цена упала ниже {self.CONFIG['MAX_DROP_BEFORE_PUMP_PCT']}% от уровня.")
            return None

        # --- ШАГ 0: ЖДЕМ ПАМПА ---
        if self.state == "WAIT_PUMP":
            target_pump = self.min * (1 + self.CONFIG['MIN_PUMP_HEIGHT_PCT'] / 100.0)
            if high_val >= target_pump:
                self.state = "WAIT_CHOCH"
                self.peak_high = high_val
                if current_swing_low and current_swing_low > 0:
                    self.active_swing_low = float(current_swing_low)
                
                self.last_event_type = "SCAN"
                self._dbg(f"🚀 ПРОБОЙ ПАМПА ({high_val:.4f}). Цель Swing Low: {self.active_swing_low}. Переход в ожидание CHoCH.")
            return None

        # --- ШАГ 1: ЖДЕМ СЛОМА СТРУКТУРЫ (CHoCH) ---
        if self.state == "WAIT_CHOCH":
            if not self.active_swing_low:
                return None 

            # Триггер: цена закрылась ниже опорного Swing Low
            if close_val < self.active_swing_low:
                is_vol_ok = float(c_vol) >= (safe_baseline * self.CONFIG['CHOCH_MIN_VOL_MULT'])

                if is_vol_ok:
                    self.last_event_type = "GOOD_RED"
                    self.history_log = f"Vol VS Baseline: {(float(c_vol)/safe_baseline):.1f}x"
                    self._dbg(f"✅ ВХОД (ШОРТ CHoCH): Пробой Swing Low ({self.active_swing_low:.4f}). {self.history_log}")
                    return self._enter(c_high, c_close, all_opposite_levels)
                else:
                    self._dbg(f"🔕 Пробой CHoCH без объема. Объем={self._fmt(c_vol)}, нужно >={self._fmt(safe_baseline * self.CONFIG['CHOCH_MIN_VOL_MULT'])}")
                    
        return None

    def _enter(self, c_high, c_close, all_opposite_levels):
        self.state = "TRIGGERED"
        actual_entry = float(c_close)
        actual_sl = self.peak_high * (1 + (self.CONFIG['SL_BUFFER'] / 100.0)) 

        self._dbg(f"🚪 ОРДЕР УШЕЛ! Entry: {actual_entry:.4f}, SL: {actual_sl:.4f}")

        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            self._dbg(f"❌ Калькулятор УБИЛ сделку: {err}")
            return {'error': err}

        self.entry_price = actual_entry
        self.sl_price = risk_data['sl']
        reason_str = f"CHoCH Short [{self.history_log}]"

        return {"action": "SELL", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}