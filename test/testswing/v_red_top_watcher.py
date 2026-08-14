# -*- coding: utf-8 -*-
from .watcher_methods import _calc_tp_and_rr

class VRedTopWatcher:
    _log_cleared = False 

    CONFIG = {
        # ==========================================
        # [0] МАКРО-ФИЛЬТР (Старт)
        # ==========================================
        'MIN_PUMP_HEIGHT_PCT': 15.0,  # Памп от уровня: рост минимум на 15% для старта работы
        'MAX_TRADES_PER_LEVEL': 0,    # Лимит сделок на уровень (0 = без ограничений)
        'VOL_HISTORY_BARS': 100,      # Окно Эвереста: ищет макс. объем за последние 100 свечей
        
        # ==========================================
        # [1] СВЕЧА 1 (ЗЕЛЕНЫЙ ЯКОРЬ) - БАЗА
        # ==========================================
        'C1_MIN_RANGE_PCT': 1.0,       # Мин. размах C1: свеча от Low до High не меньше 2%
        'C1_MIN_VOL_PCT_OF_MAX': 40.0, # Базовый зацеп C1: объем не меньше 40% от Эвереста
        
        # ==========================================
        # [2] МАРШРУТ RED 3 (ПОГЛОЩЕНИЕ ТЕЛОМ)
        # ==========================================
        'RED3_C1_MIN_VOL_PCT_OF_MAX': 40.0,    # Мин. объем C1 для RED 3 от Эвереста
        'RED3_C1_MIN_BODY_PCT': 40.0,          # Якорь RED 3: тело C1 от 40%
        'RED3_C1_MAX_TOP_SHADOW_PCT': 30.0,    # Якорь RED 3: верхняя тень C1 макс 30%
        
        'RED3_MIN_OVERLAP_PCT': 120.0,         # С2 Поглощение: красная перекрывает зеленую на 120%
        'RED3_MAX_TOP_SHADOW_PCT': 20.0,       # С2 Защита: верхняя тень макс 20%
        'RED3_MIN_BODY_PCT': 50.0,             # С2 Плотность: тело красной от 50%
        
        'RED3_C3_MIN_BODY_PCT': 10.0,          # С3 Подтверждение: тело красной от 10%
        'RED3_C3_MAX_BOTTOM_SHADOW_PCT': 60.0, # С3 Защита: нижняя тень макс 60%
        
        # ==========================================
        # [3] МАРШРУТ RED 2 (ТЕНЬ + ОБЪЕМ)
        # ==========================================
        'RED2_C1_MIN_VOL_PCT_OF_MAX': 40.0,    # Мин. объем C1 для RED 2 от Эвереста
        'RED2_C1_MIN_BODY_PCT': 30.0,          # Якорь RED 2: тело C1 от 30%
        'RED2_C1_MAX_TOP_SHADOW_PCT': 40.0,    # Якорь RED 2: верхняя тень C1 макс 40%
        
        'RED2_MIN_TOP_SHADOW_PCT': 30.0,       # С2 Климакс: верхняя тень от 30%
        'RED2_VOL_OVERRIDE_MULT': 1.30,        # С2 Объем: объем красной > 130% от зеленой C1
        
        'RED2_C3_MIN_BODY_PCT': 20.0,          # С3 Подтверждение: тело красной от 20%
        'RED2_C3_MAX_BOTTOM_SHADOW_PCT': 40.0, # С3 Защита: нижняя тень макс 40%
        
        # ==========================================
        # [4] МАРШРУТ RED 1 (КЛАССИКА: ПИН-БАР -> РЕАКЦИЯ)
        # ==========================================
        'RED1_C1_MIN_VOL_PCT_OF_MAX': 50.0,    # Мин. объем C1 для RED 1 от Эвереста
        'RED1_C1_MIN_BODY_PCT':50.0,          # Якорь RED 1: тело C1 от 50%
        'RED1_C1_MAX_TOP_SHADOW_PCT': 50.0,    # Якорь RED 1: верхняя тень C1 макс 50%
        
        'RED1_C2_MIN_TOP_SHADOW_MULT': 2.0,    # С2 Пин-бар: тень сверху минимум в 2 раза больше тела
        'RED1_C2_MAX_BODY_PCT': 30.0,          # С2 Пин-бар: тело макс 30%
        
        'RED1_C3_MIN_BODY_PCT': 50.0,          # С3 Реакция: тело красной от 50%
        'RED1_C3_MAX_BOTTOM_SHADOW_PCT': 30.0, # С3 Защита: нижняя тень макс 30%
        'RED1_C3_VOL_VS_C1_PCT': 90.0,         # С3 Объем: объем не меньше 90% от зеленой C1

        # ==========================================
        # [5] НАСТРОЙКИ РИСКА И ВЫХОДА
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
        
        if self.CONFIG.get('DEBUG') and not VRedTopWatcher._log_cleared:
            with open("v_red_debug.log", "w", encoding="utf-8") as f:
                f.write("=== НОВЫЙ ТЕСТ ЗАПУЩЕН ===\n")
            VRedTopWatcher._log_cleared = True
        
        self.state = "WAIT_PUMP"
        self.peak_high = 0.0
        
        self.c1 = None
        self.c2 = None
        self.route = "NONE"
        
        self.vol_history = []
        self.pump_threshold = 0.0
        
        self.sl_price = None
        self.entry_price = None
        self.history_log = ""
        
        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None
        self.last_event_type = None
        
        self.trades_count = 0          

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
            self.c1 = None
            self.c2 = None
            self.trades_count = 0

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
        
        self.vol_history.append(float(c_vol))
        vol_bars = self.CONFIG.get('VOL_HISTORY_BARS', 100)
        if len(self.vol_history) > vol_bars:
            self.vol_history.pop(0)
            
        current_max_vol = max(self.vol_history) if self.vol_history else 0.0001

        if self.state != "WAIT_PUMP":
            if high_val > self.peak_high:
                self.peak_high = high_val
                
            if float(c_close) < self.pump_threshold:
                self.state = "WAIT_PUMP"
                self.trades_count = 0
                self.c1, self.c2, self.route = None, None, "NONE"
                return None

        # --- ШАГ 0: ЖДЕМ ПАМПА ---
        if self.state == "WAIT_PUMP":
            target_pump = self.min * (1 + self.CONFIG['MIN_PUMP_HEIGHT_PCT'] / 100.0)
            if high_val >= target_pump:
                self.state = "WAIT_C1"
                self.peak_high = high_val
                self.pump_threshold = target_pump
                self.last_event_type = "SCAN"
            return None

        # --- ШАГ 3.5: ЖДЕМ ПОДТВЕРЖДЕНИЯ ДЛЯ RED 3 ---
        if self.state == "WAIT_C3_RED3":
            if self.c1 is None or self.c2 is None:
                self.state = "WAIT_C1"
                return None
                
            hl = float(c_high - c_low)
            body = float(c_open - c_close)
            bottom_shadow = float(c_close - c_low)
            is_red = c_open > c_close
            
            body_pct = (body / hl * 100.0) if hl > 0 else 0.0
            bottom_shadow_pct = (bottom_shadow / hl * 100.0) if hl > 0 else 0.0
            
            is_solid = body_pct >= self.CONFIG['RED3_C3_MIN_BODY_PCT']
            is_no_tail = bottom_shadow_pct <= self.CONFIG['RED3_C3_MAX_BOTTOM_SHADOW_PCT']
            
            if is_red and is_solid and is_no_tail:
                self.last_event_type = "GOOD_RED"
                self.history_log = f"С3 подтвердила: Тело={body_pct:.0f}%({self.CONFIG['RED3_C3_MIN_BODY_PCT']:.0f}%), Н.Тень={bottom_shadow_pct:.0f}%(макс {self.CONFIG['RED3_C3_MAX_BOTTOM_SHADOW_PCT']:.0f}%)"
                self._dbg(f"✅ ВХОД [RED 3]: {self.history_log}. {m_log}")
                return self._enter(c_high, c_close, all_opposite_levels)
            else:
                self._dbg(f"❌ Отмена [RED 3] С3. {m_log}")
                self.state = "WAIT_C1"
                self.c1, self.c2 = None, None

        # --- ШАГ 3.6: ЖДЕМ ПОДТВЕРЖДЕНИЯ ДЛЯ RED 2 ---
        if self.state == "WAIT_C3_RED2":
            if self.c1 is None or self.c2 is None:
                self.state = "WAIT_C1"
                return None
                
            hl = float(c_high - c_low)
            body = float(c_open - c_close)
            bottom_shadow = float(c_close - c_low)
            is_red = c_open > c_close
            
            body_pct = (body / hl * 100.0) if hl > 0 else 0.0
            bottom_shadow_pct = (bottom_shadow / hl * 100.0) if hl > 0 else 0.0
            
            is_solid = body_pct >= self.CONFIG['RED2_C3_MIN_BODY_PCT']
            is_no_tail = bottom_shadow_pct <= self.CONFIG['RED2_C3_MAX_BOTTOM_SHADOW_PCT']
            
            if is_red and is_solid and is_no_tail:
                self.last_event_type = "GOOD_RED"
                self.history_log = f"С3 подтвердила: Тело={body_pct:.0f}%({self.CONFIG['RED2_C3_MIN_BODY_PCT']:.0f}%), Н.Тень={bottom_shadow_pct:.0f}%(макс {self.CONFIG['RED2_C3_MAX_BOTTOM_SHADOW_PCT']:.0f}%)"
                self._dbg(f"✅ ВХОД [RED 2]: {self.history_log}. {m_log}")
                return self._enter(c_high, c_close, all_opposite_levels)
            else:
                self._dbg(f"❌ Отмена [RED 2] С3. {m_log}")
                self.state = "WAIT_C1"
                self.c1, self.c2 = None, None

        # --- ШАГ 3: ЖДЕМ ПОГЛОЩЕНИЯ ПОСЛЕ ПИН-БАРА (МАРШРУТ RED 1) ---
        if self.state == "WAIT_C3_RED1":
            if self.c1 is None or self.c2 is None:
                self.state = "WAIT_C1"
                return None
                
            hl = float(c_high - c_low)
            body = float(c_open - c_close)
            bottom_shadow = float(c_close - c_low)
            is_red = c_open > c_close
            is_engulfing = c_close <= self.c1['o']
            
            body_pct = (body / hl * 100.0) if hl > 0 else 0.0
            bottom_shadow_pct = (bottom_shadow / hl * 100.0) if hl > 0 else 0.0
            
            is_solid = body_pct >= self.CONFIG['RED1_C3_MIN_BODY_PCT']
            is_no_tail = bottom_shadow_pct <= self.CONFIG['RED1_C3_MAX_BOTTOM_SHADOW_PCT']
            
            req_vol = self.c1['v'] * (self.CONFIG['RED1_C3_VOL_VS_C1_PCT'] / 100.0)
            is_vol_ok = float(c_vol) >= req_vol
            is_above_pump = float(c_close) >= self.pump_threshold

            if is_red and is_engulfing and is_solid and is_no_tail and is_vol_ok and is_above_pump:
                self.route = "RED 1"
                self.last_event_type = "GOOD_RED"
                c3_vol_pct = (float(c_vol) / self.c1['v']) * 100.0
                self.history_log = f"С3 подтвердила: Объем={c3_vol_pct:.0f}%({self.CONFIG['RED1_C3_VOL_VS_C1_PCT']:.0f}%), Тело={body_pct:.0f}%({self.CONFIG['RED1_C3_MIN_BODY_PCT']:.0f}%)"
                self._dbg(f"✅ ВХОД [RED 1]: {self.history_log}. {m_log}")
                return self._enter(c_high, c_close, all_opposite_levels)
            else:
                self._dbg(f"❌ Отмена [RED 1] С3. {m_log}")
                self.state = "WAIT_C1"
                self.c1, self.c2 = None, None
                
        # --- ШАГ 2: ОЦЕНКА РЕАКЦИИ И ИНДИВИДУАЛЬНЫХ ЯКОРЕЙ ---
        if self.state == "WAIT_REACTION":
            if self.c1 is None:
                self.state = "WAIT_C1"
                return None
                
            hl = float(c_high - c_low)
            abs_body = abs(float(c_close - c_open))
            is_red = c_open > c_close
            
            c2_top_shadow = float(c_high - c_open) if is_red else float(c_high - c_close)
            c2_top_shadow_pct = (c2_top_shadow / hl * 100.0) if hl > 0 else 0.0
            c2_body_pct = (abs_body / hl * 100.0) if hl > 0 else 0.0
            
            c1_body = self.c1['c'] - self.c1['o']
            c1_body_pct = self.c1['body_pct']
            c1_top_pct = self.c1['top_shadow_pct']
            
            # 1. Проверка RED 3
            is_r3_vol_ok = self.c1['vol_pct'] >= self.CONFIG['RED3_C1_MIN_VOL_PCT_OF_MAX']
            is_r3_c1_ok = (is_r3_vol_ok and c1_body_pct >= self.CONFIG['RED3_C1_MIN_BODY_PCT'] and c1_top_pct <= self.CONFIG['RED3_C1_MAX_TOP_SHADOW_PCT'])
            overlap_target_red3 = self.c1['c'] - (c1_body * (self.CONFIG['RED3_MIN_OVERLAP_PCT'] / 100.0))
            is_red3_overlap = c_close <= overlap_target_red3
            is_red3_solid = c2_body_pct >= self.CONFIG['RED3_MIN_BODY_PCT']
            is_red3_no_heli = c2_top_shadow_pct <= self.CONFIG['RED3_MAX_TOP_SHADOW_PCT']
            
            if is_red and is_r3_c1_ok and is_red3_overlap and is_red3_solid and is_red3_no_heli:
                self.route = "RED 3"
                self.c2 = {'o': float(c_open), 'h': float(c_high), 'l': float(c_low), 'c': float(c_close), 'v': float(c_vol)}
                self.state = "WAIT_C3_RED3"
                self._dbg(f"⏳ С2 подтверждена [RED 3]: Тело={c2_body_pct:.0f}%({self.CONFIG['RED3_MIN_BODY_PCT']:.0f}%), Верх.Тень={c2_top_shadow_pct:.0f}%(макс {self.CONFIG['RED3_MAX_TOP_SHADOW_PCT']:.0f}%). Жду С3. {m_log}")
                return None
                
            # 2. Проверка RED 2
            is_r2_vol_ok = self.c1['vol_pct'] >= self.CONFIG['RED2_C1_MIN_VOL_PCT_OF_MAX']
            is_r2_c1_ok = (is_r2_vol_ok and c1_body_pct >= self.CONFIG['RED2_C1_MIN_BODY_PCT'] and c1_top_pct <= self.CONFIG['RED2_C1_MAX_TOP_SHADOW_PCT'])
            req_override_vol = self.c1['v'] * self.CONFIG['RED2_VOL_OVERRIDE_MULT']
            is_red2_shadow = c2_top_shadow_pct >= self.CONFIG['RED2_MIN_TOP_SHADOW_PCT']
            is_red2_vol_override = float(c_vol) >= req_override_vol
            
            if is_red and is_r2_c1_ok and is_red2_shadow and is_red2_vol_override:
                self.route = "RED 2"
                self.c2 = {'o': float(c_open), 'h': float(c_high), 'l': float(c_low), 'c': float(c_close), 'v': float(c_vol)}
                self.state = "WAIT_C3_RED2"
                c2_v_pct = (float(c_vol) / self.c1['v']) * 100.0
                req_v_pct = self.CONFIG['RED2_VOL_OVERRIDE_MULT'] * 100.0
                self._dbg(f"⏳ С2 подтверждена [RED 2]: Верх.Тень={c2_top_shadow_pct:.0f}%({self.CONFIG['RED2_MIN_TOP_SHADOW_PCT']:.0f}%), Объем={c2_v_pct:.0f}%({req_v_pct:.0f}%). Жду С3. {m_log}")
                return None
                
            # 3. Проверка RED 1
            is_r1_vol_ok = self.c1['vol_pct'] >= self.CONFIG['RED1_C1_MIN_VOL_PCT_OF_MAX']
            is_r1_c1_ok = (is_r1_vol_ok and c1_body_pct >= self.CONFIG['RED1_C1_MIN_BODY_PCT'] and c1_top_pct <= self.CONFIG['RED1_C1_MAX_TOP_SHADOW_PCT'])
            shadow_ratio = (c2_top_shadow / abs_body) if abs_body > 0 else 999.0
            is_pinbar = shadow_ratio >= self.CONFIG['RED1_C2_MIN_TOP_SHADOW_MULT']
            is_small_body = c2_body_pct <= self.CONFIG['RED1_C2_MAX_BODY_PCT']
            
            if is_pinbar and is_small_body and is_r1_c1_ok:
                self.c2 = {'o': float(c_open), 'h': float(c_high), 'l': float(c_low), 'c': float(c_close), 'v': float(c_vol)}
                self.state = "WAIT_C3_RED1"
                self._dbg(f"⏳ С2 подтверждена [RED 1]: Тень_x={shadow_ratio:.1f}({self.CONFIG['RED1_C2_MIN_TOP_SHADOW_MULT']:.1f}), Тело={c2_body_pct:.0f}%(макс {self.CONFIG['RED1_C2_MAX_BODY_PCT']:.0f}%). Жду С3. {m_log}")
                return None
                
            self.state = "WAIT_C1"
            self.c1 = None

        # --- ШАГ 1: ПОИСК СВЕЧИ 1 (ЯКОРЬ) ---
        if self.state == "WAIT_C1":
            is_green = c_close > c_open
            hl = float(c_high - c_low)
            body = float(c_close - c_open)
            top_shadow = float(c_high - c_close)
            
            body_pct = (body / hl * 100.0) if hl > 0 else 0.0
            top_shadow_pct = (top_shadow / hl * 100.0) if hl > 0 else 0.0
            
            candle_range_pct = (hl / float(c_close)) * 100.0
            
            req_c1_vol = current_max_vol * (self.CONFIG['C1_MIN_VOL_PCT_OF_MAX'] / 100.0)
            is_vol_ok = float(c_vol) >= req_c1_vol

            if is_green and is_vol_ok and candle_range_pct >= self.CONFIG['C1_MIN_RANGE_PCT']:
                self.last_event_type = "SCAN"
                vol_pct_of_max = (float(c_vol) / current_max_vol * 100.0) if current_max_vol > 0 else 0.0
                
                self.c1 = {
                    'o': float(c_open), 'h': float(c_high), 'l': float(c_low), 'c': float(c_close), 'v': float(c_vol),
                    'vol_pct': vol_pct_of_max,
                    'body_pct': body_pct,
                    'top_shadow_pct': top_shadow_pct
                }
                self.state = "WAIT_REACTION"
                self._dbg(f"🎯 ЗАХВАТ ЦЕЛИ (С1): Объем={vol_pct_of_max:.0f}% ({self._fmt(float(c_vol))}) от пика ({self._fmt(current_max_vol)}). Жду реакцию (RED1/2/3). {m_log}")
            
        return None

    def _enter(self, c_high, c_close, all_opposite_levels):
        actual_entry = float(c_close)
        actual_sl = self.peak_high * 1.005 

        self._dbg(f"🚪 ОРДЕР УШЕЛ! Entry: {actual_entry:.4f}, SL: {actual_sl:.4f}")

        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            self._dbg(f"❌ Калькулятор УБИЛ сделку: {err}")
            return {'error': err}

        self.entry_price = actual_entry
        self.sl_price = risk_data['sl']
        
        dist_from_level = ((actual_entry - self.min) / self.min) * 100.0 if self.min > 0 else 0.0
        reason_str = f"{self.route} | ур. ниже {dist_from_level:.1f}%"

        self.trades_count += 1
        max_trades = self.CONFIG.get('MAX_TRADES_PER_LEVEL', 0)

        if max_trades > 0 and self.trades_count >= max_trades:
            self.state = "TRIGGERED"
            self._dbg(f"🛑 Лимит сделок на памп исчерпан ({self.trades_count}/{max_trades}). Вотчер остановлен.")
        else:
            self.state = "WAIT_C1"
            self.c1, self.c2 = None, None
            self._dbg(f"🔄 Сделка #{self.trades_count} открыта. Возврат в поиск новых пиков (WAIT_C1).")

        return {"action": "SELL", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}