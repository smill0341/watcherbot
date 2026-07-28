# =========================================================================
# МЕТОД 4: PANIC TRAP (Гибрид: Ретест для 1-2 ног + V-Bottom на Аномалиях)
# =========================================================================
import os
from .watcher_methods import _calc_tp_and_rr

class PanicTrapWatcher:
    CONFIG = {
        'CLIMAX_VOL_MULT': 2.0,   
        'MIN_BOUNCE_PCT': 3.2,    
        'OBSERVE_BARS': 10,       
        'ANOMALY_VOL_MULT': 2.0,  # Во сколько раз 3+ нога должна быть больше предыдущих
        'MIN_GREEN_VOL_PCT': 15.0,# Минимальный объем зеленой свечи (% от красной аномалии)
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
        
        self.state = "WAIT_CLIMAX"
        self.entry_price: float | None = None
        self.sl_price: float | None = None
        self.climax_extreme: float | None = None
        self.trap_extreme: float | None = None   
        self.climax_body = None
        self.climax_top = None       
        
        self.hump_formed = False   
        
        self.bars_since_climax = 0
        self.global_legs_count = 0  
        self.legs_count = 0        
        
        self.max_prev_leg_vol = 0.0   # Максимальный объем среди предыдущих ног
        self.current_leg_vol = 0.0    # Объем красного дна на текущей ноге

        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None

    def _tp(self):
        return f"{self._last_time} " if self._last_time is not None else ""

    def _dbg(self, msg):
        self.last_event_time = self._last_time
        self.last_event_msg = msg
        with open("panic_trap_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{self._tp()}[{self.max:.4f}] {msg}\n")

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, trend='UNKNOWN', **kwargs):
        self._last_time = kwargs.get('candle_time')
        
        if self.state in ["DEAD", "TRIGGERED"]:
            return None

        # ГЛОБАЛЬНЫЙ СБРОС
        if c_high >= self.max and self.state not in ["TRAP_SET", "WAIT_CONFIRMATION"]:
            self.global_legs_count = 0
            self.max_prev_leg_vol = 0.0
            self.current_leg_vol = 0.0

        if self.trade_type == 'LONG':
            
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
                    self.climax_extreme = c_low 
                    self.climax_body = min(c_open, c_close) 
                    self.climax_top = max(c_open, c_close) 
                    self.current_leg_vol = c_vol
                    
                    self.global_legs_count += 1
                    self.legs_count = self.global_legs_count
                    
                    self.bars_since_climax = 0
                    self.hump_formed = False
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🔴 ШАГ 1: ДНО {self.legs_count} (Тень={c_low:.5f}, Верх={self.climax_top:.5f}, Объем={self.current_leg_vol:.0f})")
                return None

            # =================================================================
            # ШАГ 2: НАБЛЮДЕНИЕ (Окно в 10 свечей)
            # =================================================================
            elif self.state == "OBSERVE_BOUNCE":
                climax_val = self.climax_extreme
                body_val = self.climax_body
                if climax_val is None or body_val is None:
                    self.state = "WAIT_CLIMAX"
                    return None

                self.bars_since_climax += 1

                # Синхронизация тени и тела при пробое ниже внутри одной ноги
                if c_low < climax_val:
                    climax_val = c_low
                    self.climax_extreme = climax_val 
                    
                    self.climax_body = min(c_open, c_close)
                    self.climax_top = max(c_open, c_close)
                    body_val = self.climax_body 
                    self.current_leg_vol = c_vol 
                    
                current_body = min(c_open, c_close)
                if current_body < body_val:
                    body_val = current_body
                    self.climax_body = body_val
                    self.climax_top = max(c_open, c_close) 
                    self.current_leg_vol = c_vol 

                bounce_pct = ((c_close - climax_val) / climax_val) * 100

                if bounce_pct >= self.CONFIG['MIN_BOUNCE_PCT'] and c_close > c_open:
                    
                    if self.legs_count <= 2:
                        # Только Капкан для 1-й и 2-й ноги
                        self.state = "TRAP_SET"
                        self.entry_price = body_val * 1.001
                        self.bars_since_climax = 0 
                        self.hump_formed = False
                        if self.CONFIG.get('DEBUG'):
                            self._dbg(f"🟡 РАЗВИЛКА 1: КАПКАН взведен на {self.entry_price:.5f}. Ждем касания уровня {self.max:.5f}.")
                    else:
                        # V-Bottom для 3-й ноги и далее - Поиск Аномалии и Поглощения
                        is_anomaly = (self.max_prev_leg_vol > 0) and (self.current_leg_vol >= self.max_prev_leg_vol * self.CONFIG['ANOMALY_VOL_MULT'])
                        is_body_absorbed = (self.climax_top is not None) and (c_close > self.climax_top)
                        green_vol_pct = (c_vol / self.current_leg_vol * 100) if self.current_leg_vol > 0 else 0
                        has_min_vol = green_vol_pct >= self.CONFIG['MIN_GREEN_VOL_PCT']

                        if self.CONFIG.get('DEBUG'):
                            self._dbg(f"🔎 V-ТЕСТ (ног:{self.legs_count}): Аномалия={is_anomaly} (Красн={self.current_leg_vol:.0f}, Пред.Макс={self.max_prev_leg_vol:.0f}), Поглощение={is_body_absorbed}, ОбъемЗел={green_vol_pct:.1f}%")

                        if is_anomaly and is_body_absorbed and has_min_vol:
                            self.state = "TRIGGERED"
                            self.sl_price = climax_val * 0.998
                            
                            if self.CONFIG.get('DEBUG'):
                                self._dbg(f"🟢 РАЗВИЛКА 2: ВХОД V-BOTTOM! Идеальная аномалия и выкуп.")
                                
                            risk_data, err = _calc_tp_and_rr(c_close, self.sl_price, self.trade_type, all_opposite_levels, self.CONFIG)
                            if err or not risk_data: return {'error': err or "Risk data is None"}
                            return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'],
                                    "reason": f"V-Bottom Аномалия (ног:{self.legs_count})", "legs_count": self.legs_count}
                        else:
                            if self.CONFIG.get('DEBUG'):
                                self._dbg(f"🟡 V-ДНО ИГНОР. Ждем дальше.")
                            return None

                    return None

                if c_close < climax_val:
                    # Слом ноги: сохраняем максимальный объем из предыдущих ног перед переходом к новой
                    self.max_prev_leg_vol = max(self.max_prev_leg_vol, self.current_leg_vol)
                    
                    self.global_legs_count += 1
                    self.legs_count = self.global_legs_count
                    self.bars_since_climax = 0 
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🔴 ЦЕНА ПОШЛА ВНИЗ: Закрытие ниже ямы (c_close={c_close:.5f}, ног:{self.legs_count}). Ищем новое дно.")
                    return None

                if self.bars_since_climax >= self.CONFIG['OBSERVE_BARS']:
                    self.state = "WAIT_CLIMAX"
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"❌ ТАЙМАУТ: Прошло {self.CONFIG['OBSERVE_BARS']} св., роста нет. Сброс поиска. (Глобальные ноги сохранены: {self.global_legs_count})")
                return None

            # =================================================================
            # ШАГ 3: РЕТЕСТ КАПКАНА (Только для 1-й и 2-й ямы)
            # =================================================================
            elif self.state == "TRAP_SET":
                if self.climax_extreme is None or self.entry_price is None:
                    self.state = "WAIT_CLIMAX"
                    return None

                if c_low < self.climax_extreme:
                    self.state = "OBSERVE_BOUNCE"
                    self.climax_extreme = c_low
                    self.max_prev_leg_vol = max(self.max_prev_leg_vol, self.current_leg_vol)
                    self.global_legs_count += 1 
                    self.legs_count = self.global_legs_count
                    self.bars_since_climax = 0
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"❌ СЛОМ РЕТЕСТА: Пробили капкан вниз. Переход к поиску новой ямы (ног:{self.legs_count}).")
                    return None

                if not self.hump_formed:
                    if c_high >= self.max:
                        self.hump_formed = True
                        if self.CONFIG.get('DEBUG'):
                            self._dbg(f"⛰️ ГОРБ СФОРМИРУЕТСЯ: Цена коснулась уровня {self.max:.5f}. Ретест АКТИВИРОВАН.")
                    return None

                if self.hump_formed and c_low <= self.entry_price:
                    self.trap_extreme = c_low
                    self.state = "WAIT_CONFIRMATION"
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🔎 ШАГ 3: Истинный ретест капкана. Оцениваем закрытие...")
                return None

            # =================================================================
            # ШАГ 4: ПОДТВЕРЖДЕНИЕ ВХОДА
            # =================================================================
            elif self.state == "WAIT_CONFIRMATION":
                if self.climax_extreme is None or self.entry_price is None:
                    self.state = "WAIT_CLIMAX"
                    return None

                if self.trap_extreme is None: self.trap_extreme = c_low
                else: self.trap_extreme = min(self.trap_extreme, c_low)
                
                if c_low < self.climax_extreme:
                    self.state = "OBSERVE_BOUNCE"
                    self.climax_extreme = c_low
                    self.max_prev_leg_vol = max(self.max_prev_leg_vol, self.current_leg_vol)
                    self.global_legs_count += 1
                    self.legs_count = self.global_legs_count
                    self.bars_since_climax = 0
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"❌ ПРОБОЙ ПРИ РЕТЕСТЕ: Переход к поиску новой ямы (ног:{self.legs_count}).")
                    return None
                
                if c_close > self.entry_price:
                    self.state = "TRIGGERED"
                    safe_trap = float(self.trap_extreme) if self.trap_extreme is not None else c_low
                    self.sl_price = safe_trap * 0.998
                    
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🟢 ШАГ 4: ВХОД (Ретест подтвержден)! Дно ямы устояло.")
                        
                    risk_data, err = _calc_tp_and_rr(c_close, self.sl_price, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": "Капкан (Двойное касание)", "legs_count": self.legs_count}

                return None

        return None