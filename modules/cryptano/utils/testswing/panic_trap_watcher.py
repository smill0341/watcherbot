# =========================================================================
# МЕТОД 4: PANIC TRAP (Гибрид: Проверка отскока + Ретест или V-Bottom)
# =========================================================================
import os
from .watcher_methods import _calc_tp_and_rr

class PanicTrapWatcher:
    CONFIG = {
        'CLIMAX_VOL_MULT': 2.0,   
        'MIN_BOUNCE_PCT': 3.2,    # Процент роста для подтверждения ямы
        'OBSERVE_BARS': 10,       # Сколько свечей ждем этот 2% рост
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
        
        
        
        self.bars_since_climax = 0
        self.legs_count = 0        

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

        if self.trade_type == 'LONG':
            
            # =================================================================
            # ШАГ 1: ПОИСК ПАНИКИ
            # =================================================================
            if self.state == "WAIT_CLIMAX":
                if c_low > self.max: return None 
                if baseline_vol is None or c_vol is None: return None
                
                if c_close < c_open and c_vol >= baseline_vol * self.CONFIG['CLIMAX_VOL_MULT']:
                    self.state = "OBSERVE_BOUNCE"
                    self.climax_extreme = c_low 
                    self.climax_body = min(c_open, c_close) # <--- Запоминаем низ тела
                    self.legs_count = 1
                    self.bars_since_climax = 0
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🔴 ШАГ 1: ДНО {self.legs_count} (Тень={c_low:.5f}, Тело={self.climax_body:.5f})")
                return None

            # =================================================================
            # ШАГ 2: НАБЛЮДЕНИЕ (Окно в 10 свечей)
            # =================================================================
            elif self.state == "OBSERVE_BOUNCE":
                # 1. Жестко фиксируем локальные переменные для тайп-чекера
                climax_val = self.climax_extreme
                body_val = self.climax_body
                if climax_val is None or body_val is None:
                    self.state = "WAIT_CLIMAX"
                    return None

                self.bars_since_climax += 1

                # 2. Обновляем абсолютное дно (по теням)
                if c_low < climax_val:
                    climax_val = c_low
                    self.climax_extreme = climax_val 
                    
                # 3. Обновляем дно по телам (выбираем самую низкую плотность)
                current_body = min(c_open, c_close)
                if current_body < body_val:
                    body_val = current_body
                    self.climax_body = body_val

                # 4. Считаем рост от самой глубокой тени
                bounce_pct = ((c_close - climax_val) / climax_val) * 100

                # 5. Если рост подтвержден:
                if bounce_pct >= self.CONFIG['MIN_BOUNCE_PCT']:
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🔥 ОТСКОК ПОДТВЕРЖДЕН: Рост {bounce_pct:.2f}% (ног: {self.legs_count})")

                    if self.legs_count == 1:
                        # ТАКТИКА А: Капкан ставится по ТЕЛУ свечи (body_val), а не по тени!
                        self.state = "TRAP_SET"
                        self.entry_price = body_val * 1.001
                        self.bars_since_climax = 0 
                        if self.CONFIG.get('DEBUG'):
                            self._dbg(f"🟡 РАЗВИЛКА 1: КАПКАН взведен по телам на {self.entry_price:.5f}. Стоп спрятан за тень {climax_val:.5f}.")
                    else:
                        # ТАКТИКА Б: Вход V-Bottom
                        self.state = "TRIGGERED"
                        self.sl_price = climax_val * 0.998
                        
                        if self.CONFIG.get('DEBUG'):
                            self._dbg(f"🟢 РАЗВИЛКА 2: ВХОД V-BOTTOM! (ног:{self.legs_count})")
                            
                        risk_data, err = _calc_tp_and_rr(c_close, self.sl_price, self.trade_type, all_opposite_levels, self.CONFIG)
                        if err or not risk_data: return {'error': err or "Risk data is None"}
                        return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'],
                                "reason": f"V-Bottom (ног:{self.legs_count})", "legs_count": self.legs_count}
                    return None

                # 6. Если цена закрылась телом ниже самой глубокой тени - каскад продолжается
                if c_close < climax_val:
                    self.legs_count += 1
                    self.bars_since_climax = 0 
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🔴 ЦЕНА ПОШЛА ВНИЗ: Закрытие ниже ямы (c_close={c_close:.5f}, ног:{self.legs_count}). Ищем новое дно.")
                    return None

                # 7. Таймаут
                if self.bars_since_climax >= self.CONFIG['OBSERVE_BARS']:
                    self.state = "WAIT_CLIMAX"
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"❌ ТАЙМАУТ: Прошло {self.CONFIG['OBSERVE_BARS']} св., роста нет. Сброс поиска.")
                return None

            # =================================================================
            # ШАГ 3: РЕТЕСТ КАПКАНА (Только если актуальна 1-я яма)
            # =================================================================
            elif self.state == "TRAP_SET":
                if self.climax_extreme is None or self.entry_price is None:
                    self.state = "WAIT_CLIMAX"
                    return None

                # Если во время ожидания ретеста цена провалила дно - это каскад
                if c_low < self.climax_extreme:
                    self.state = "OBSERVE_BOUNCE"
                    self.climax_extreme = c_low
                    self.legs_count += 1 
                    self.bars_since_climax = 0
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"❌ СЛОМ РЕТЕСТА: Пробили капкан вниз. Переход к поиску новой ямы (ног:{self.legs_count}).")
                    return None

                # Ждем, когда цена вернется к капкану
                if c_low <= self.entry_price:
                    self.trap_extreme = c_low
                    self.state = "WAIT_CONFIRMATION"
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🔎 ШАГ 3: Ретест капкана. Оцениваем закрытие...")
                return None

            # =================================================================
            # ШАГ 4: ПОДТВЕРЖДЕНИЕ ВХОДА (Свеча закрылась выше)
            # =================================================================
            elif self.state == "WAIT_CONFIRMATION":
                if self.climax_extreme is None or self.entry_price is None:
                    self.state = "WAIT_CLIMAX"
                    return None

                if self.trap_extreme is None: self.trap_extreme = c_low
                else: self.trap_extreme = min(self.trap_extreme, c_low)
                
                # Защита от пробоя
                if c_low < self.climax_extreme:
                    self.state = "OBSERVE_BOUNCE"
                    self.climax_extreme = c_low
                    self.legs_count += 1
                    self.bars_since_climax = 0
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"❌ ПРОБОЙ ПРИ РЕТЕСТЕ: Переход к поиску новой ямы (ног:{self.legs_count}).")
                    return None
                
                # ТРИГГЕР: Свеча закрылась над капканом
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