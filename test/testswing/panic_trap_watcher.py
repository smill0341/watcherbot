# =========================================================================
# МЕТОД 4: PANIC TRAP (Чистая V-Bottom Архитектура: Трекер + Снайпер)
# =========================================================================
from .watcher_methods import _calc_tp_and_rr

class PanicTrapWatcher:
    CONFIG = {
        'CLIMAX_VOL_MULT': 2.0,   
        'MIN_BOUNCE_PCT': 3.2,    
        'OBSERVE_BARS': 10,       
        'ANOMALY_VOL_MULT': 1.7,  
        'MIN_GREEN_VOL_PCT': 15.0,
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
        
        # --- Блок Трекера ---
        self.state = "WAIT_CLIMAX"
        self.climax_extreme: float | None = None
        self.climax_body = None
        self.climax_top = None       
        self.current_leg_vol = 0.0    
        
        self.bars_since_climax = 0
        self.global_legs_count = 0  
        self.legs_count = 0        
        self.max_prev_leg_vol = 0.0   

        # --- Служебные ---
        self.sl_price: float | None = None
        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None

    def _tp(self):
        return f"{self._last_time} " if self._last_time is not None else ""

    def _dbg(self, msg):
        self.last_event_time = self._last_time
        self.last_event_msg = msg
        if self.CONFIG.get('DEBUG'):
            with open("panic_trap_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{self._tp()}[{self.max:.4f}] {msg}\n")


    def _advance_to_next_leg(self):
        self.max_prev_leg_vol = max(self.max_prev_leg_vol, self.current_leg_vol)
        self.global_legs_count += 1
        self.legs_count = self.global_legs_count
        self.bars_since_climax = 0

    def _is_v_bottom_triggered(self, c_close, c_vol):
        is_anomaly = (self.max_prev_leg_vol > 0) and (self.current_leg_vol >= self.max_prev_leg_vol * self.CONFIG['ANOMALY_VOL_MULT'])
        
        safe_climax_top = float(self.climax_top) if self.climax_top is not None else 0.0
        is_body_absorbed = (safe_climax_top > 0) and (c_close > safe_climax_top)
        
        green_vol_pct = (c_vol / self.current_leg_vol * 100) if self.current_leg_vol > 0 else 0
        has_min_vol = green_vol_pct >= self.CONFIG['MIN_GREEN_VOL_PCT']

        self._dbg(f"🔎 V-ТЕСТ (ног:{self.legs_count}): Аномалия={is_anomaly} (Красн={self.current_leg_vol:.0f}, Пред.Макс={self.max_prev_leg_vol:.0f}), Поглощение={is_body_absorbed}, ОбъемЗел={green_vol_pct:.1f}%")
        return is_anomaly and is_body_absorbed and has_min_vol

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, **kwargs):
        self._last_time = kwargs.get('candle_time')
        
        if self.state in ["DEAD", "TRIGGERED"]:
            return None

        # Защищаем буфер ожидания
        if c_high >= self.max and self.state != "WAIT_NEXT_LEG":
            self.global_legs_count = 0
            self.max_prev_leg_vol = 0.0
            self.current_leg_vol = 0.0

        if self.trade_type != 'LONG':
            return None

        # =================================================================
        # ШАГ 1: ПОИСК ПАНИКИ
        # =================================================================
        if self.state == "WAIT_CLIMAX":
            if c_low > self.max: 
                self.global_legs_count = 0 
                self.max_prev_leg_vol = 0.0
                self.current_leg_vol = 0.0
                return None 
            
            if baseline_vol is None or c_vol is None: return None
            
            if c_close < c_open and c_vol >= baseline_vol * self.CONFIG['CLIMAX_VOL_MULT']:
                self.state = "OBSERVE_BOUNCE"
                self.climax_extreme = float(c_low)
                self.climax_body = float(min(c_open, c_close))
                self.climax_top = float(max(c_open, c_close))
                self.current_leg_vol = float(c_vol)
                
                self.global_legs_count += 1
                self.legs_count = self.global_legs_count
                self.bars_since_climax = 0
                self._dbg(f"🔴 ШАГ 1: ДНО {self.legs_count} (Тень={self.climax_extreme:.5f}, Верх={self.climax_top:.5f}, Объем={self.current_leg_vol:.0f})")
            return None

        # =================================================================
        # ШАГ 2: РАДАР (Наблюдение за ямой)
        # =================================================================
        elif self.state == "OBSERVE_BOUNCE":
            if self.climax_extreme is None or self.climax_body is None:
                self.state = "WAIT_CLIMAX"
                return None

            safe_climax_extreme = float(self.climax_extreme)
            safe_climax_body = float(self.climax_body)
            self.bars_since_climax += 1

            if c_low < safe_climax_extreme:
                self.climax_extreme = float(c_low)
                self.climax_body = float(min(c_open, c_close))
                self.climax_top = float(max(c_open, c_close))
                self.current_leg_vol = float(c_vol)
                safe_climax_extreme = float(c_low)
                
            current_body = float(min(c_open, c_close))
            if current_body < safe_climax_body:
                self.climax_body = current_body
                self.climax_top = float(max(c_open, c_close))
                self.current_leg_vol = float(c_vol)

            bounce_pct = ((c_close - safe_climax_extreme) / safe_climax_extreme) * 100

            if bounce_pct >= self.CONFIG['MIN_BOUNCE_PCT'] and c_close > c_open:
                if self.legs_count <= 2:
                    self.state = "WAIT_NEXT_LEG"
                    self.bars_since_climax = 0 
                    self._dbg(f"🟡 ОЖИДАНИЕ: Отскок есть, ждем пробой для следующей ноги (ног:{self.legs_count}).")
                    return None
                else:
                    if self._is_v_bottom_triggered(c_close, c_vol):
                        self.state = "TRIGGERED"
                        self.sl_price = safe_climax_extreme * 0.998
                        
                        self._dbg(f"🟢 ВХОД V-BOTTOM! Идеальная аномалия и выкуп.")
                        risk_data, err = _calc_tp_and_rr(c_close, self.sl_price, self.trade_type, all_opposite_levels, self.CONFIG)
                        if err or not risk_data: return {'error': err or "Risk data is None"}
                        
                        return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'],
                                "reason": f"V-Bottom Аномалия (ног:{self.legs_count})", "legs_count": self.legs_count}
                    else:
                        self._dbg(f"🟡 V-ДНО ИГНОР. Условия не выполнены, ждем дальше.")
                        return None

            if c_close < safe_climax_extreme:
                self._advance_to_next_leg()
                self._dbg(f"🔴 ЦЕНА ПОШЛА ВНИЗ: Закрытие ниже ямы (c_close={c_close:.5f}, ног:{self.legs_count}). Ищем новое дно.")
                return None

            if self.bars_since_climax >= self.CONFIG['OBSERVE_BARS']:
                self.state = "WAIT_CLIMAX"
                self._dbg(f"❌ ТАЙМАУТ: Прошло {self.CONFIG['OBSERVE_BARS']} св., роста нет. Сброс поиска. (Глобальные ноги сохранены: {self.global_legs_count})")
            return None

        # =================================================================
        # ШАГ 3: БУФЕР ПАМЯТИ
        # =================================================================
        elif self.state == "WAIT_NEXT_LEG":
            if self.climax_extreme is None:
                self.state = "WAIT_CLIMAX"
                return None

            safe_climax_extreme = float(self.climax_extreme)
            if c_low < safe_climax_extreme:
                self.state = "OBSERVE_BOUNCE"
                self.climax_extreme = float(c_low)
                self.max_prev_leg_vol = max(self.max_prev_leg_vol, self.current_leg_vol)
                
                self.climax_body = float(min(c_open, c_close))
                self.climax_top = float(max(c_open, c_close))
                self.current_leg_vol = float(c_vol)
                
                self.global_legs_count += 1
                self.legs_count = self.global_legs_count
                self.bars_since_climax = 0
                
                if self.CONFIG.get('DEBUG'):
                    self._dbg(f"❌ СЛОМ ВНИЗ: Переход к поиску новой ямы (ног:{self.legs_count}).")
                return None

        return None