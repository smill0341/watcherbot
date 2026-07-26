# =========================================================================
# МЕТОД 5 (ТЕСТ): V-BOTTOM (Вход на первой яме без ожидания ретеста)
# =========================================================================
import os
from .watcher_methods import _calc_tp_and_rr

class VBottomTestWatcher:
    CONFIG = {
        'CLIMAX_VOL_MULT': 2.0,   
        'TP_MODE': 'fixed_pct',
        'FIXED_TP_PCT': 10.0,     
        'TAKE_PROFIT': 10.0,
        'TP_BUFFER_PCT': 0.0,
        'SL_BUFFER': 0.5,         
        'MIN_RR': 1.0,
        'USE_RR_FILTER': False,   
        'DEBUG': True,
    }

    def __init__(self, level_min, level_max, trade_type):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        
        self.state = "WAIT_CLIMAX"
        self.sl_price = None
        self.climax_extreme = None
        self.legs_count = 0        
        self.prev_red_body_mid = None # Для проверки силы отскока

        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None

    def _tp(self):
        return f"{self._last_time} " if self._last_time is not None else ""

    def _dbg(self, msg):
        self.last_event_time = self._last_time
        self.last_event_msg = msg
        with open("vbottom_test_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{self._tp()}[{self.max:.4f}] {msg}\n")

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, trend='UNKNOWN', **kwargs):
        self._last_time = kwargs.get('candle_time')
        
        if self.state in ["DEAD", "TRIGGERED"]:
            return None

        if self.trade_type == 'LONG':
            
            # =================================================================
            # ШАГ 1: ПОИСК ПАНИКИ (Удар)
            # =================================================================
            if self.state == "WAIT_CLIMAX":
                if c_low > self.max: return None 
                if c_close < c_open and c_vol >= baseline_vol * self.CONFIG['CLIMAX_VOL_MULT']:
                    self.state = "WAIT_REVERSAL"
                    self.climax_extreme = c_low 
                    self.legs_count = 1
                    # Запоминаем середину этой красной свечи, чтобы потом оценить силу отскока
                    self.prev_red_body_mid = c_close + ((c_open - c_close) / 2) 
                    
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🔴 ШАГ 1: УДАР/ЯМА (c_low={c_low:.5f})")
                return None

            # =================================================================
            # ШАГ 2: V-РАЗВОРОТ (Мгновенный вход на нормальном отскоке)
            # =================================================================
            elif self.state == "WAIT_REVERSAL":
                
                # Если дамп продолжается (свеча красная) — тянем дно ниже
                if c_close < c_open:
                    if c_low < self.climax_extreme:
                        self.climax_extreme = c_low
                        self.legs_count += 1
                        self.prev_red_body_mid = c_close + ((c_open - c_close) / 2)
                        
                        if self.CONFIG.get('DEBUG'):
                            self._dbg(f"🔴 ШАГ 1 (Продолжение): ЯМА ГЛУБЖЕ (c_low={c_low:.5f}, ног:{self.legs_count})")
                    return None
                    
                # Если появилась зеленая свеча — проверяем силу отскока
                if c_close > c_open:
                    # Решение твоего пункта 1: Проверка на "нормальный рост"
                    # Если закрытие зеленой свечи ниже середины предыдущей красной - это слабый отскок
                    if c_close < self.prev_red_body_mid:
                        if self.CONFIG.get('DEBUG'):
                            self._dbg(f"⚠️ ИГНОР: Слабый отскок (c_close={c_close:.5f} < mid={self.prev_red_body_mid:.5f}). Ждем дальше.")
                        return None
                            
                    # Если отскок сильный — ВХОДИМ СРАЗУ, никакого ожидания второго касания
                    self.state = "TRIGGERED"
                    self.sl_price = self.climax_extreme * 0.998 # Стоп под самое дно V-ямы
                    
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🟢 ШАГ 2: ВХОД (V-Bottom)! Сильный отскок зафиксирован. (Вход по {c_close:.5f})")
                        
                    risk_data, err = _calc_tp_and_rr(c_close, self.sl_price, self.trade_type, all_opposite_levels, self.CONFIG)
                    if err or not risk_data: return {'error': err or "Risk data is None"}
                    
                    return {"action": "BUY", "sl": risk_data['sl'], "tp": risk_data['tp'],
                            "reason": f"V-Bottom (Вход на первой яме, ног:{self.legs_count})",
                            "legs_count": self.legs_count, "trend_at_entry": trend}

                return None

        return None