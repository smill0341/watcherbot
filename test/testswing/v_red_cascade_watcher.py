# -*- coding: utf-8 -*-
from .watcher_methods import _calc_tp_and_rr

class VRedCascadeWatcher:
    _log_cleared = False 

    CONFIG = {
        # ==========================================
           # [0] МАКРО-ФИЛЬТР (Старт)
           # ==========================================
           'MIN_PUMP_HEIGHT_PCT': 8.0,  
           'MIN_EMA_DIST_PCT': 8.0,     
           'EMA_MUST_BE_ABOVE_LEVEL': False, 
           'MAX_TRADES_PER_LEVEL': 4,    
           
           'PAUSE_PUMP_PCT': 5.0,  
           
           # ==========================================
           # [0.1] МАКРО-СТРУКТУРА (ТРИ ИНДЕЙЦА)
           # ==========================================
           'MIN_PEAKS_TO_ARM': 3,              
           'MIN_PULLBACK_ATR': 2.0,            
           'MIN_PULLBACK_PCT_FLOOR': 2.0,      
           'MIN_BREAKOUT_ATR': 1.0,            
           'MIN_BREAKOUT_PCT_FLOOR': 1.5,      
           'MAX_BARS_FROM_PEAK': 15,           
           
           # ==========================================
           # [1] СВЕЧА 1 (ЗЕЛЕНЫЙ ЯКОРЬ) - БАЗА
           # ==========================================
           'C1_MIN_RANGE_PCT': 0.5,       
           
           # ==========================================
           # [2] МАРШРУТ RED 4 (КАСКАД С ОТКАТОМ) - ОСНОВНОЙ
           # ==========================================
           'RED4_C1_MIN_VOL_MULT': 1.0,           # Якорь RED 4: объем C1 минимум 1x от фона
           'RED4_C1_MIN_BODY_PCT': 40.0,          # Якорь RED 4: тело C1 от 40%
           'RED4_C1_MAX_TOP_SHADOW_PCT': 40.0,    # Якорь RED 4: верхняя тень макс 40%
           
           'RED4_MIN_REDS': 4,                    # Минимум красных свечей подряд в лесенке
           'RED4_PULLBACK_MIN_BODY_RATIO': 60.0,  # Тело зеленой свечи отката минимум 40% (задел на будущее)
           'RED4_PULLBACK_MIN_RANGE_PCT': 0.4,    # Сам откат не меньше 0.4% по высоте (отсекаем микро-доджи)
           
           'RED4_PULLBACK_MIN_PCT': 40.0,         # МИНИМАЛЬНЫЙ отскок в % от каскада
           'RED4_PULLBACK_MAX_PCT': 90.0,         # МАКСИМАЛЬНЫЙ отскок в % от каскада

        # ==========================================
        # [3] НАСТРОЙКИ РИСКА И ВЫХОДА
        # ==========================================
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
        
        if self.CONFIG.get('DEBUG') and not VRedCascadeWatcher._log_cleared:
            with open("v_red_cascade_debug.log", "w", encoding="utf-8") as f:
                f.write("=== НОВЫЙ ТЕСТ CASCADE ЗАПУЩЕН ===\n")
            VRedCascadeWatcher._log_cleared = True
        
        self.state = "WAIT_PUMP"
        self.peak_high = 0.0
        
        # Структурные переменные
        self.peaks_count = 0             
        self.current_peak_high = 0.0      
        self.locked_peak_high = 0.0          
        self.lowest_since_high = float('inf')    
        self.pullback_confirmed = False 
        self.bars_since_peak = 0
        
        self.c1 = None
        self.route = "NONE"
        
        self.pump_threshold = 0.0
        
        self.sl_price = None
        self.entry_price = None
        self.history_log = ""
        
        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None
        self.last_event_type = None
        
        self.trades_count = 0
        
        # --- Переменные Каскада ---
        self.casc_count = 0
        self.casc_last_close = 0.0
        self.casc_low = 0.0
        self.casc_start_high = 0.0
        self.casc_pullback_high = 0.0        

    def _tp(self): return f"{self._last_time} " if self._last_time else ""

    def _fmt(self, v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000: return f"{v/1_000:.1f}k"
        return str(int(v))

    def _dbg(self, msg):
        self.last_event_time = self._last_time
        self.last_event_msg = msg
        if self.CONFIG.get('DEBUG'):
            with open("v_red_cascade_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{self._tp()}[{self.min:.4f}] {msg}\n")

    def on_breach_start(self):
        if self.state not in ("DEAD", "TRIGGERED"):
            self.state = "WAIT_PUMP"
            self.c1 = None
            self.trades_count = 0
            
            self.peaks_count = 0             
            self.current_peak_high = 0.0      
            self.locked_peak_high = 0.0          
            self.lowest_since_high = float('inf')    
            self.pullback_confirmed = False 
            self.bars_since_peak = 0

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr=None, all_opposite_levels=None, c_atr_slow=None, c_ema=None, c_rsi=None, **kwargs):
        self._last_time = kwargs.get('candle_time')
        self.last_event_time = self._last_time  
        self.last_event_type = None
        
        dist_pct = ((float(c_close) - self.min) / self.min) * 100.0 if self.min > 0 else 0.0
        ema_dist_pct = ((float(c_close) - c_ema) / c_ema) * 100.0 if c_ema and c_ema > 0 else 0.0
        rsi_val = float(c_rsi) if c_rsi is not None else 0.0
        m_log = f"[RSI:{rsi_val:.1f} | ОтУровня:{dist_pct:.1f}% | ОтEMA:{ema_dist_pct:.1f}%]"
        
        if self.state in ("DEAD", "TRIGGERED"): return None
        if self.trade_type != 'SHORT': return None

        high_val = float(c_high)
        safe_baseline = float(baseline_vol) if baseline_vol and baseline_vol > 0 else 0.0001
        safe_atr = float(c_atr) if (c_atr is not None and c_atr == c_atr) else 0.0001

        # =======================================================
        # --- БЛОК СТРУКТУРЫ (СЧЕТЧИК ПИКОВ) ---
        # =======================================================
        target_peaks = self.CONFIG.get('MIN_PEAKS_TO_ARM', 3)
        target_pump = self.min * (1 + self.CONFIG['MIN_PUMP_HEIGHT_PCT'] / 100.0)
        min_ema_dist = self.CONFIG.get('MIN_EMA_DIST_PCT', 0.0)
        
        is_above_pump = float(c_close) >= target_pump
        is_above_ema = (c_ema is not None) and (ema_dist_pct >= min_ema_dist)

        if self.state == "WAIT_PUMP":
            if self.CONFIG.get('EMA_MUST_BE_ABOVE_LEVEL', False) and c_ema is not None and float(c_ema) < self.min:
                return None

            is_green_struct = c_close > c_open

            if self.peaks_count == 0 and high_val > self.min:
                self.peaks_count = 1
                self.current_peak_high = high_val
                self.locked_peak_high = high_val
                self.lowest_since_high = high_val
                self.pullback_confirmed = False
                self.last_event_type = "TRACK_START"
                
            elif self.peaks_count > 0:
                if is_green_struct and not self.pullback_confirmed:
                    self.last_event_type = "NEW_PEAK"

                if not self.pullback_confirmed:
                    if high_val > self.current_peak_high:
                        self.current_peak_high = high_val
                        self.locked_peak_high = high_val
                        self.lowest_since_high = high_val
                    else:
                        self.lowest_since_high = min(self.lowest_since_high, float(c_low))
                        req_drop = max(safe_atr * self.CONFIG.get('MIN_PULLBACK_ATR', 2.0), 
                                       self.current_peak_high * (self.CONFIG.get('MIN_PULLBACK_PCT_FLOOR', 2.0) / 100.0))
                        
                        if (self.current_peak_high - self.lowest_since_high) >= req_drop:
                            self.pullback_confirmed = True
                            self.locked_peak_high = self.current_peak_high
                            self._dbg(f"📉 ПИК №{self.peaks_count}! {self.locked_peak_high:.4f}")
                else:
                    if self.peaks_count < target_peaks:
                        self.lowest_since_high = min(self.lowest_since_high, float(c_low))
                        req_breakout = max(safe_atr * self.CONFIG.get('MIN_BREAKOUT_ATR', 1.0), 
                                           self.locked_peak_high * (self.CONFIG.get('MIN_BREAKOUT_PCT_FLOOR', 1.5) / 100.0))
                        
                        if high_val >= self.locked_peak_high + req_breakout:
                            self.peaks_count += 1
                            self.current_peak_high = high_val
                            self.locked_peak_high = high_val
                            self.lowest_since_high = high_val
                            self.pullback_confirmed = False
                            self.last_event_type = "NEW_PEAK"
                        elif high_val > self.current_peak_high:
                            self.current_peak_high = high_val
                            self.lowest_since_high = high_val
                    else:
                        if high_val > self.current_peak_high:
                            self.current_peak_high = high_val
                            self.locked_peak_high = high_val

            is_peaks_ok = (self.peaks_count >= target_peaks) and self.pullback_confirmed
            
            if is_peaks_ok and is_above_pump and is_above_ema:
                self.state = "WAIT_C1"
                self.pump_threshold = target_pump
                self.peak_high = self.current_peak_high
                self.last_event_type = "SCAN"
            else:
                return None
                
        else:
            # МЫ В БОЕВОМ РЕЖИМЕ
            if high_val > self.peak_high:
                self.peak_high = high_val

            pause_pump_pct = self.CONFIG.get('PAUSE_PUMP_PCT', 5.0)
            pause_pump_threshold = self.min * (1 + pause_pump_pct / 100.0)
            is_above_pause_pump = float(c_close) >= pause_pump_threshold

            # ЗОНИРОВАНИЕ: Макро-сброс или тактическая пауза
            if not is_above_pause_pump:
                if float(c_close) <= self.min:
                    self._dbg(f"🛑 [МАКРО-СБРОС] Цена ушла под уровень. Уровень отвязан (IDLE). {m_log}")
                    self.state = "IDLE"       
                    self.trades_count = 0
                    self.c1, self.route = None, "NONE"
                    self.peaks_count = 0             
                    self.current_peak_high = 0.0      
                    self.locked_peak_high = 0.0          
                    self.lowest_since_high = float('inf')    
                    self.pullback_confirmed = False
                    return None
                else:
                    if self.state != "WAIT_C1" or self.c1 is not None:
                        self._dbg(f"⏸️ [ПАУЗА] Цена ниже {pause_pump_pct}% от уровня. Сделки не ищем, пики сохраняем. {m_log}")
                        self.state = "WAIT_C1"
                        self.c1, self.route = None, "NONE"
                    return None
            
        # =======================================================
        # --- ОСНОВНАЯ ЛОГИКА: RED 4 (КАСКАД С ОТКАТОМ) ---
        # =======================================================
        is_red = c_close < c_open
        is_green = c_close > c_open

        if self.state == "WAIT_C1":
            hl = float(c_high - c_low)
            body = float(c_close - c_open)
            top_shadow = float(c_high - c_close)
            
            body_pct = (body / hl * 100.0) if hl > 0 else 0.0
            top_shadow_pct = (top_shadow / hl * 100.0) if hl > 0 else 0.0
            candle_range_pct = (hl / float(c_close)) * 100.0
            
            req_c1_vol = safe_baseline * self.CONFIG['RED4_C1_MIN_VOL_MULT']
            is_vol_ok = float(c_vol) >= req_c1_vol

            if is_green and is_vol_ok and candle_range_pct >= self.CONFIG['C1_MIN_RANGE_PCT']:
                vol_mult = float(c_vol) / safe_baseline
                
                if body_pct >= self.CONFIG['RED4_C1_MIN_BODY_PCT'] and top_shadow_pct <= self.CONFIG['RED4_C1_MAX_TOP_SHADOW_PCT']:
                    self.state = "COUNTING_CASCADE"
                    self.casc_count = 0
                    self.casc_last_close = float('inf')
                    self.casc_start_high = float(c_high)
                    self.casc_pullback_high = 0.0
                    self.casc_low = float('inf') 
                    
                    self.last_event_type = "SCAN"
                    self.c1 = {
                        'range_pct': candle_range_pct,
                        'vol_mult': vol_mult,
                        'body_pct': body_pct
                    }
                    
                    self._dbg(f"🎯 [С1 CASCADE] Якорь: V x{vol_mult:.1f} | R: {candle_range_pct:.2f}%. Ищем каскад. {m_log}")
        
        elif self.state in ("COUNTING_CASCADE", "WAIT_PULLBACK"):
            # 1. Если цена делает перехай выше нашего якоря С1 — каскад ломается
            if high_val > self.casc_start_high:
                self._dbg(f"❌ Отмена [RED 4]: Перехай якоря С1. Возврат в поиск. {m_log}")
                self.state = "WAIT_C1"
                self.c1 = None

            elif self.state == "COUNTING_CASCADE":
                hl = float(c_high - c_low)
                body = float(c_open - c_close)
                body_pct = (body / hl * 100.0) if hl > 0 else 0.0
                
                if is_red and body_pct >= 20.0 and float(c_close) < self.casc_last_close:
                    self.casc_count += 1
                    self.casc_last_close = float(c_close)
                    self.casc_low = min(self.casc_low, float(c_low))
                else:
                    cascade_height_pct = ((self.casc_start_high - self.casc_low) / self.min) * 100.0
                    if self.casc_count >= self.CONFIG['RED4_MIN_REDS'] and cascade_height_pct >= 0.5:
                        self.state = "WAIT_PULLBACK"
                        self.casc_pullback_high = float(c_high)
                        self.last_event_type = "SCAN"
                    else:
                        if self.casc_count > 0:
                            self._dbg(f"❌ Отмена [RED 4]: Каскад мал ({self.casc_count} св., {cascade_height_pct:.2f}%). {m_log}")
                        self.state = "WAIT_C1"
                        self.c1 = None
                        
            elif self.state == "WAIT_PULLBACK":
                if float(c_high) > self.casc_pullback_high:
                    self.casc_pullback_high = float(c_high)

                if is_red:
                    cascade_height = self.casc_start_high - self.casc_low
                    pullback_height = self.casc_pullback_high - self.casc_low
                    
                    pullback_pct = (pullback_height / cascade_height * 100.0) if cascade_height > 0 else 100.0
                    pullback_range_pct = (pullback_height / self.min) * 100.0
                    
                    min_pb = self.CONFIG['RED4_PULLBACK_MIN_PCT']
                    max_pb = self.CONFIG['RED4_PULLBACK_MAX_PCT']
                    min_pb_range = self.CONFIG['RED4_PULLBACK_MIN_RANGE_PCT']
                    
                    # Проверяем и проценты, и физический размах отката
                    if pullback_range_pct < min_pb_range:
                        self._dbg(f"❌ Отмена [RED 4]: Высота отката {pullback_range_pct:.2f}% меньше минимума {min_pb_range}%. {m_log}")
                        self.state = "WAIT_C1"
                        self.c1 = None
                    elif min_pb <= pullback_pct <= max_pb:
                        self.route = "RED 4 CASCADE"
                        self.last_event_type = "GOOD_RED"
                        
                        c1_range_equiv = (cascade_height / self.min * 100.0)
                        self.c1['range_pct'] = c1_range_equiv 
                        
                        self.history_log = f"Спуск: {self.casc_count} свечей ({c1_range_equiv:.2f}%), Откат: {pullback_pct:.0f}% (мин {min_pb}%)"
                        self._dbg(f"✅ ВХОД [RED 4 CASCADE]: {self.history_log}. {m_log}")
                        
                        return self._enter(c_high, c_close, all_opposite_levels, c_rsi=rsi_val)
                    else:
                        self._dbg(f"❌ Отмена [RED 4]: Откат {pullback_pct:.0f}% не вошел в рамки {min_pb}% - {max_pb}%. {m_log}")
                        self.state = "WAIT_C1"
                        self.c1 = None

        return None

    def _enter(self, c_high, c_close, all_opposite_levels, c_rsi=0.0):
        actual_entry = float(c_close)
        actual_sl = self.peak_high * 1.005 

        self._dbg(f"🚪 ОРДЕР УШЕЛ! Entry: {actual_entry:.4f}, SL: {actual_sl:.4f}")

        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "IDLE"
            self._dbg(f"❌ Калькулятор УБИЛ сделку: {err}")
            return {'error': err}

        self.entry_price = actual_entry
        self.sl_price = risk_data['sl']
        
        dist_from_level = ((actual_entry - self.min) / self.min) * 100.0 if self.min > 0 else 0.0
        
        c1_range = self.c1.get('range_pct', 0.0) if self.c1 else 0.0
        c1_vol = (self.c1.get('vol_mult', 0.0) * 100) if self.c1 else 0.0
        c1_body = self.c1.get('body_pct', 0.0) if self.c1 else 0.0
        
        req_vol = self.CONFIG.get('RED4_C1_MIN_VOL_MULT', 1.0) * 100
        req_body = self.CONFIG.get('RED4_C1_MIN_BODY_PCT', 40.0)
        
        base_reason = f"{self.route} | ур. ниже {dist_from_level:.1f}% | Спуск: {c1_range:.2f}% | V {c1_vol:.0f}%({req_vol:.0f}%) | тело {c1_body:.0f}%({req_body:.0f}%)"
        reason_str = f"{base_reason} | RSI:{c_rsi:.1f} | Сделка #{self.trades_count + 1}"

        self.trades_count += 1
        max_trades = self.CONFIG.get('MAX_TRADES_PER_LEVEL', 0)

        if max_trades > 0 and self.trades_count >= max_trades:
            self.state = "TRIGGERED"
            self._dbg(f"🛑 Лимит сделок на памп исчерпан ({self.trades_count}/{max_trades}). Вотчер остановлен.")
        else:
            self.state = "WAIT_C1"
            self.c1 = None
            self._dbg(f"🔄 Сделка #{self.trades_count} открыта. Возврат в поиск новых пиков (WAIT_C1).")

        return {"action": "SELL", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}