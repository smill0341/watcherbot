# -*- coding: utf-8 -*-
from .watcher_methods import _calc_tp_and_rr

class VRedTopWatcher:
    _log_cleared = False 

    CONFIG = {
        # ==========================================
        # [0] МАКРО-ФИЛЬТР (Старт)
        # ==========================================
        'MIN_PUMP_HEIGHT_PCT': 8.0,  # Памп от уровня: рост минимум на 12% для старта работы
        'MIN_EMA_DIST_PCT': 8.0,     # Цена должна быть минимум на 10% выше EMA
        'EMA_MUST_BE_ABOVE_LEVEL': False, # Искать шорт ТОЛЬКО если EMA физически выше уровня
        'MAX_TRADES_PER_LEVEL': 4,    # Лимит сделок на уровень (0 = без ограничений)
        
        'PAUSE_PUMP_PCT': 5.0,  # Пауза, если цена упала ниже 5% от уровня (от EMA не зависим!)
        
        # ==========================================
        # [0.1] МАКРО-СТРУКТУРА (ТРИ ИНДЕЙЦА)
        # ==========================================
        'MIN_PEAKS_TO_ARM': 3,              # Мин. количество структурных пиков (1 = старая логика, 3 = Три индейца)
        'MIN_PULLBACK_ATR': 2.0,            # Гибкий порог отката от пика (в ATR)
        'MIN_PULLBACK_PCT_FLOOR': 2.0,      # Жесткий пол отката (в %)
        'MIN_BREAKOUT_ATR': 1.0,            # Гибкий порог перехая (в ATR)
        'MIN_BREAKOUT_PCT_FLOOR': 1.5,      # Жесткий пол перехая (в %)
        'MAX_BARS_FROM_PEAK': 15,           # Макс. свечей от пика для поиска точки входа
        
        # ==========================================
        # [1] СВЕЧА 1 (ЗЕЛЕНЫЙ ЯКОРЬ) - БАЗА
        # ==========================================
        'C1_MIN_RANGE_PCT': 0.5,       # Мин. размах C1: свеча от Low до High не меньше 1%
        'C1_MIN_VOL_MULT': 1.0,        # Базовый зацеп C1: объем >= 1.5х от среднего за 20 баров
        
        # ==========================================
        # [2] МАРШРУТ RED 3 (ПОГЛОЩЕНИЕ ТЕЛОМ)
        # ==========================================
        'RED3_C1_MIN_VOL_MULT': 1.25,          # Мин. объем C1 для RED 3 (1.5x от фона)
        'RED3_C1_MIN_BODY_PCT': 40.0,          # Якорь RED 3: тело C1 от 40%
        'RED3_C1_MAX_TOP_SHADOW_PCT': 30.0,    # Якорь RED 3: верхняя тень C1 макс 30%
        
        'RED3_MIN_OVERLAP_PCT': 120.0,         # С2 Поглощение: красная перекрывает зеленую на 120%
        'RED3_MAX_TOP_SHADOW_PCT': 20.0,       # С2 Защита: верхняя тень макс 20%
        'RED3_MIN_BODY_PCT': 50.0,             # С2 Плотность: тело красной от 50%
        
        'RED3_C3_MIN_BODY_PCT': 40.0,          # С3 Подтверждение: тело красной от 10%
        'RED3_C3_MAX_BOTTOM_SHADOW_PCT': 40.0, # С3 Защита: нижняя тень макс 60%
        
        # ==========================================
        # [3] МАРШРУТ RED 2 (ТЕНЬ + ОБЪЕМ)
        # ==========================================
        'RED2_C1_MIN_RANGE_PCT': 1.0,          # ИНДИВИДУАЛЬНЫЙ РАЗМАХ: минимум 1.0% для якоря RED 2
        'RED2_C1_MIN_VOL_MULT': 1.0,           # Мин. объем C1 для RED 2 (1.5x от фона)
        'RED2_C1_MIN_BODY_PCT': 60.0,          # Якорь RED 2: тело C1 от 60%
        'RED2_C1_MAX_TOP_SHADOW_PCT': 40.0,    # Якорь RED 2: верхняя тень C1 макс 40%
        
        'RED2_MIN_TOP_SHADOW_PCT': 7.0,       # С2 Климакс: верхняя тень от 30%
        'RED2_VOL_OVERRIDE_MULT': 1.0,        # С2 Объем: объем красной > 130% от зеленой C1
        
        'RED2_C3_MIN_BODY_PCT': 55.0,          # С3 Подтверждение: тело красной от 60%
        'RED2_C3_MAX_BOTTOM_SHADOW_PCT': 40.0, # С3 Защита: нижняя тень макс 40%
        
        # ==========================================
        # [4] МАРШРУТ RED 1 (КЛАССИКА: ПИН-БАР -> РЕАКЦИЯ)
        # ==========================================
        'RED1_C1_MIN_VOL_MULT': 1.00,          # Мин. объем C1 для RED 1 (1.5x от фона)
        'RED1_C1_MIN_BODY_PCT': 43.0,          # Якорь RED 1: тело C1 от 50%
        'RED1_C1_MAX_TOP_SHADOW_PCT': 50.0,    # Якорь RED 1: верхняя тень C1 макс 50%
        
        'RED1_C2_MIN_TOP_SHADOW_MULT': 1.7,    # С2 Пин-бар: тень сверху минимум в 2 раза больше тела
        'RED1_C2_MAX_BODY_PCT': 40.0,          # С2 Пин-бар: тело макс 30%
        
        'RED1_C3_MIN_BODY_PCT': 40.0,          # С3 Реакция: тело красной от 50%
        'RED1_C3_MAX_BOTTOM_SHADOW_PCT': 30.0, # С3 Защита: нижняя тень макс 30%
        'RED1_C3_VOL_VS_C1_PCT': 75.0,         # С3 Объем: объем не меньше 90% от зеленой C1

        # ==========================================
        # [5] МАРШРУТ RED 4 (КАСКАД С ОТКАТОМ)
        # ==========================================
        'RED4_C1_MIN_VOL_MULT': 1.0,           # Якорь RED 4: объем C1 минимум 1x от фона
        'RED4_C1_MIN_BODY_PCT': 40.0,          # Якорь RED 4: тело C1 от 40%
        'RED4_C1_MAX_TOP_SHADOW_PCT': 40.0,    # Якорь RED 4: верхняя тень макс 40%
        
        'RED4_MIN_REDS': 4,                    # Минимум красных свечей подряд в лесенке
        'RED4_PULLBACK_MAX_PCT': 55.0,         # Максимальная высота отката (в % от высоты всего каскада)
        'RED4_PULLBACK_MIN_BODY_RATIO': 60.0,  # Тело зеленой свечи отката минимум 40% от всей ее высоты
        'RED4_PULLBACK_MIN_RANGE_PCT': 0.4,    # Сам откат не меньше 0.4% по высоте (отсекаем микро-дodжи)
        
        'RED4_PULLBACK_MIN_PCT': 30.0,         # МИНИМАЛЬНЫЙ отскок (отсекаем доджи и шум на дне)
        'RED4_PULLBACK_MAX_PCT': 75.0,         # МАКСИМАЛЬНЫЙ отскок (чтобы не сломать тренд)

        # ==========================================
        # [6] НАСТРОЙКИ РИСКА И ВЫХОДА
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
        
        # Структурные переменные
        self.peaks_count = 0             
        self.current_peak_high = 0.0      
        self.locked_peak_high = 0.0          
        self.lowest_since_high = float('inf')    
        self.pullback_confirmed = False 
        self.bars_since_peak = 0
        
        self.c1 = None
        self.c2 = None
        self.route = "NONE"
        self.active_routes = []
        
        self.pump_threshold = 0.0
        
        self.sl_price = None
        self.entry_price = None
        self.history_log = ""
        
        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None
        self.last_event_type = None
        
        self.trades_count = 0
        
        
        # --- Переменные для RED 4 (Каскад) ---
        self.casc_state = "IDLE"
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
            with open("v_red_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{self._tp()}[{self.min:.4f}] {msg}\n")

    def on_breach_start(self):
        if self.state not in ("DEAD", "TRIGGERED"):
            self.state = "WAIT_PUMP"
            self.c1 = None
            self.c2 = None
            self.trades_count = 0
            self.casc_state = "IDLE"
            
            self.peaks_count = 0             
            self.current_peak_high = 0.0      
            self.locked_peak_high = 0.0          
            self.lowest_since_high = float('inf')    
            self.pullback_confirmed = False 
            self.bars_since_peak = 0
            
            self.active_routes = []

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
                    # ФАЗА 1: Летим вверх, тянем макушку
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
                            
                            # ЛОГ: Только 3 четкие строки
                            self._dbg(f"📉 ПИК №{self.peaks_count}! {self.locked_peak_high:.4f}")
                else:
                    # ФАЗА 2: Откат состоялся. Если пиков не хватает - ждем пробой.
                    if self.peaks_count < target_peaks:
                        self.lowest_since_high = min(self.lowest_since_high, float(c_low))
                        req_breakout = max(safe_atr * self.CONFIG.get('MIN_BREAKOUT_ATR', 1.0), 
                                           self.locked_peak_high * (self.CONFIG.get('MIN_BREAKOUT_PCT_FLOOR', 1.5) / 100.0))
                        
                        if high_val >= self.locked_peak_high + req_breakout:
                            # ИСТИННЫЙ ПРОБОЙ В НОВУЮ ВОЛНУ
                            self.peaks_count += 1
                            self.current_peak_high = high_val
                            self.locked_peak_high = high_val
                            self.lowest_since_high = high_val
                            self.pullback_confirmed = False
                            self.last_event_type = "NEW_PEAK"
                        elif high_val > self.current_peak_high:
                            # Шум (закол)
                            self.current_peak_high = high_val
                            self.lowest_since_high = high_val
                    else:
                        # Если 3 пика уже набраны, мы просто обновляем макушку (счетчик больше не растет)
                        if high_val > self.current_peak_high:
                            self.current_peak_high = high_val
                            self.locked_peak_high = high_val

            # ШЛЮЗ СКАНИРОВАНИЯ: Набрали 3 пика И 3-й пик полностью сформирован (был откат)
            is_peaks_ok = (self.peaks_count >= target_peaks) and self.pullback_confirmed
            
            if is_peaks_ok and is_above_pump and is_above_ema:
                self.state = "WAIT_C1"
                self.pump_threshold = target_pump
                self.peak_high = self.current_peak_high
                self.last_event_type = "SCAN"
            else:
                return None
                
        else:
            # МЫ В БОЕВОМ РЕЖИМЕ (Скан 4-х стратегий)
            if high_val > self.peak_high:
                self.peak_high = high_val

            # Отдельный порог для паузы (привязан ТОЛЬКО к уровню)
            pause_pump_pct = self.CONFIG.get('PAUSE_PUMP_PCT', 5.0)
            pause_pump_threshold = self.min * (1 + pause_pump_pct / 100.0)
            is_above_pause_pump = float(c_close) >= pause_pump_threshold

            # ЗОНИРОВАНИЕ: Макро-сброс или тактическая пауза
            if not is_above_pause_pump:
                # 1. Полная отмена (цена ушла под уровень <= 0%)
                if float(c_close) <= self.min:
                    self._dbg(f"🛑 [МАКРО-СБРОС] Цена ушла под уровень. Уровень отвязан (IDLE). {m_log}")
                    self.state = "IDLE"       # <--- Мягкий сброс. Симулятор отпустит уровень, а бот сможет ожить.
                    self.trades_count = 0
                    self.c1, self.c2, self.route = None, None, "NONE"
                    self.active_routes = []
                    # Обнуление структуры
                    self.peaks_count = 0             
                    self.current_peak_high = 0.0      
                    self.locked_peak_high = 0.0          
                    self.lowest_since_high = float('inf')    
                    self.pullback_confirmed = False
                    self.casc_state = "IDLE"
                    return None
                # 2. Зона паузы (от 0% до PAUSE_PUMP_PCT)
                else:
                    if self.state != "WAIT_C1" or self.c1 is not None or self.casc_state != "IDLE":
                        self._dbg(f"⏸️ [ПАУЗА] Цена ниже {pause_pump_pct}% от уровня. Сделки не ищем, пики сохраняем. {m_log}")
                        self.state = "WAIT_C1"
                        self.c1, self.c2, self.route = None, None, "NONE"
                        self.active_routes = []
                        self.casc_state = "IDLE"
                    return None
            
        # =======================================================
        # --- ПАРАЛЛЕЛЬНЫЙ МАРШРУТ: RED 4 (КАСКАД С ОТКАТОМ) ---
        # =======================================================
        is_red = c_close < c_open
        is_green = c_close > c_open

        if self.state != "WAIT_PUMP":
            # 1. Если цена делает перехай выше нашего якоря С1 — каскад ломается
            if high_val > self.casc_start_high and self.casc_state != "IDLE":
                self.casc_state = "BROKEN"

            if self.casc_state == "COUNTING":
                # Считаем свечу "нормальной", если у нее есть тело (отсекаем крестики-доджи)
                hl = float(c_high - c_low)
                body = float(c_open - c_close)
                body_pct = (body / hl * 100.0) if hl > 0 else 0.0
                
                # СТРОГОЕ УСЛОВИЕ: Красная, нормальное тело (>= 20%), закрылась НИЖЕ прошлого закрытия
                if is_red and body_pct >= 20.0 and float(c_close) < self.casc_last_close:
                    self.casc_count += 1
                    self.casc_last_close = float(c_close)
                    # Фиксируем истинное дно волны
                    self.casc_low = min(self.casc_low, float(c_low))
                else:
                    # Как только пошел боковик (доджи) или зеленая свеча - спуск окончен
                    cascade_height_pct = ((self.casc_start_high - self.casc_low) / self.min) * 100.0
                    
                    # Проверяем: набрали ли мы 4 свечи И общая высота падения хотя бы > 0.5% (отсекаем шум)
                    if self.casc_count >= self.CONFIG.get('RED4_MIN_REDS', 4) and cascade_height_pct >= 0.5:
                        self.casc_state = "WAIT_PULLBACK"
                        self.casc_pullback_high = float(c_high)
                        self.last_event_type = "SCAN"
                    else:
                        self.casc_state = "BROKEN"
                        
            elif self.casc_state == "WAIT_PULLBACK":
                # Тянем макушку отката за любой свечой
                if float(c_high) > self.casc_pullback_high:
                    self.casc_pullback_high = float(c_high)

                # Триггер: первая же красная свеча завершает откат, измеряем волну
                if is_red:
                    cascade_height = self.casc_start_high - self.casc_low
                    pullback_height = self.casc_pullback_high - self.casc_low
                    
                    pullback_pct = (pullback_height / cascade_height * 100.0) if cascade_height > 0 else 100.0
                    
                    min_pb = self.CONFIG.get('RED4_PULLBACK_MIN_PCT', 40.0)
                    max_pb = self.CONFIG.get('RED4_PULLBACK_MAX_PCT', 75.0)
                    
                    # Жесткий фильтр: отскок обязан быть в рамках 40% - 75%
                    if min_pb <= pullback_pct <= max_pb:
                        
                        red4_ema_min = self.CONFIG.get('RED4_MIN_EMA_DIST_PCT', 0.0)
                        if ema_dist_pct < red4_ema_min:
                            self._dbg(f"❌ Отмена [RED 4]: Отрыв от EMA всего {ema_dist_pct:.1f}% (нужно >= {red4_ema_min}%). {m_log}")
                            self.casc_state = "BROKEN"
                            return None
                        
                        self.route = "RED 4 CASCADE"
                        self.last_event_type = "GOOD_RED"
                        
                        c1_range_equiv = (cascade_height / self.min * 100.0)
                        self.c1 = {'range_pct': c1_range_equiv} 
                        
                        self.history_log = f"Спуск: {self.casc_count} свечей ({c1_range_equiv:.2f}%), Откат: {pullback_pct:.0f}% (мин {min_pb}%)"
                        self._dbg(f"✅ ВХОД [RED 4 CASCADE]: {self.history_log}. {m_log}")
                        
                        return self._enter(c_high, c_close, all_opposite_levels, c_rsi=rsi_val)
                    else:
                        self._dbg(f"❌ Отмена [RED 4]: Откат {pullback_pct:.0f}% не вошел в рамки {min_pb}% - {max_pb}%. {m_log}")
                        self.casc_state = "BROKEN"
                        
        # --- ШАГ 3: ОБЩЕЕ ПОДТВЕРЖДЕНИЕ ДЛЯ ВСЕХ МЕТОДОВ (С3) ---
        if self.state == "WAIT_C3":
            if self.c1 is None or self.c2 is None:
                self.state = "WAIT_C1"
                return None
                
            hl = float(c_high - c_low)
            body = float(c_open - c_close)
            bottom_shadow = float(c_close - c_low)
            is_red = c_open > c_close
            # Переопределяем порог для подтверждения входов на 5% (чтобы не отменять сделки в зоне 5-8%)
            pause_pump_threshold = self.min * (1 + self.CONFIG.get('PAUSE_PUMP_PCT', 5.0) / 100.0)
            is_above_pump = float(c_close) >= pause_pump_threshold
            
            body_pct = (body / hl * 100.0) if hl > 0 else 0.0
            bottom_shadow_pct = (bottom_shadow / hl * 100.0) if hl > 0 else 0.0
            
            if is_red:
                # 1. Финал RED 3
                if "RED 3" in self.active_routes:
                    if body_pct >= self.CONFIG['RED3_C3_MIN_BODY_PCT'] and bottom_shadow_pct <= self.CONFIG['RED3_C3_MAX_BOTTOM_SHADOW_PCT']:
                        self.route = "RED 3"
                        self.last_event_type = "GOOD_RED"
                        self.history_log = f"С3 подтвердила: Тело={body_pct:.0f}%({self.CONFIG['RED3_C3_MIN_BODY_PCT']:.0f}%), Н.Тень={bottom_shadow_pct:.0f}%(макс {self.CONFIG['RED3_C3_MAX_BOTTOM_SHADOW_PCT']:.0f}%)"
                        self._dbg(f"✅ ВХОД [RED 3]: {self.history_log}. {m_log}")
                        return self._enter(c_high, c_close, all_opposite_levels, c_rsi=rsi_val)
                        
                # 2. Финал RED 2
                if "RED 2" in self.active_routes:
                    if body_pct >= self.CONFIG['RED2_C3_MIN_BODY_PCT'] and bottom_shadow_pct <= self.CONFIG['RED2_C3_MAX_BOTTOM_SHADOW_PCT']:
                        self.route = "RED 2"
                        self.last_event_type = "GOOD_RED"
                        self.history_log = f"С3 подтвердила: Тело={body_pct:.0f}%({self.CONFIG['RED2_C3_MIN_BODY_PCT']:.0f}%), Н.Тень={bottom_shadow_pct:.0f}%(макс {self.CONFIG['RED2_C3_MAX_BOTTOM_SHADOW_PCT']:.0f}%)"
                        self._dbg(f"✅ ВХОД [RED 2]: {self.history_log}. {m_log}")
                        return self._enter(c_high, c_close, all_opposite_levels, c_rsi=rsi_val)
                        
                # 3. Финал RED 1
                if "RED 1" in self.active_routes:
                    is_engulfing = c_close <= self.c1['o']
                    req_vol = self.c1['v'] * (self.CONFIG['RED1_C3_VOL_VS_C1_PCT'] / 100.0)
                    if is_engulfing and body_pct >= self.CONFIG['RED1_C3_MIN_BODY_PCT'] and bottom_shadow_pct <= self.CONFIG['RED1_C3_MAX_BOTTOM_SHADOW_PCT'] and float(c_vol) >= req_vol and is_above_pump:
                        self.route = "RED 1"
                        self.last_event_type = "GOOD_RED"
                        shadow_ratio = (self.c2['top_shadow'] / self.c2['abs_body']) if self.c2['abs_body'] > 0 else 999.0
                        self.history_log = f"С3 подтвердила: Тень х{shadow_ratio:.1f}, Тело={body_pct:.0f}%({self.CONFIG['RED1_C3_MIN_BODY_PCT']:.0f}%)"
                        self._dbg(f"✅ ВХОД [RED 1]: {self.history_log}. {m_log}")
                        return self._enter(c_high, c_close, all_opposite_levels, c_rsi=rsi_val)
                        
            # Если никто не совпал - сброс
            c3_abs_body = abs(float(c_open - c_close))
            c3_body_pct = (c3_abs_body / hl * 100.0) if hl > 0 else 0.0
            
            c3_bot_shadow = float(c_close - c_low) if is_red else float(c_open - c_low)
            c3_bot_shadow_pct = (c3_bot_shadow / hl * 100.0) if hl > 0 else 0.0
            
            c3_top_shadow = float(c_high - c_open) if is_red else float(c_high - c_close)
            c3_top_shadow_pct = (c3_top_shadow / hl * 100.0) if hl > 0 else 0.0
            
            c3_vol_vs_c1 = (float(c_vol) / self.c1['v'] * 100.0) if self.c1 and self.c1.get('v') else 0.0
            
            color_str = "RED" if is_red else "GREEN"
            fail_details = f"Цвет: {color_str} | Тело: {c3_body_pct:.0f}% | Н.Тень: {c3_bot_shadow_pct:.0f}% | В.Тень: {c3_top_shadow_pct:.0f}% | V от С1: {c3_vol_vs_c1:.0f}%"

            self._dbg(f"❌ Отмена [С3]: {fail_details}. {m_log}")
            self.state = "WAIT_C1"
            self.c1, self.c2, self.active_routes = None, None, []

        # --- ШАГ 2: ОЦЕНКА РЕАКЦИИ (С2) ---
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
            
            survivors = []
            self.c2 = {'o': float(c_open), 'h': float(c_high), 'l': float(c_low), 'c': float(c_close), 'v': float(c_vol), 'top_shadow': c2_top_shadow, 'abs_body': abs_body}
            
            # RED 3 - СТРОГО КРАСНАЯ (поглощение)
            if "RED 3" in self.active_routes:
                overlap_target = self.c1['c'] - (c1_body * (self.CONFIG['RED3_MIN_OVERLAP_PCT'] / 100.0))
                if is_red and float(c_close) <= overlap_target and c2_body_pct >= self.CONFIG['RED3_MIN_BODY_PCT'] and c2_top_shadow_pct <= self.CONFIG['RED3_MAX_TOP_SHADOW_PCT']:
                    survivors.append("RED 3")
            
            # RED 2 - СТРОГО КРАСНАЯ (объем + тень)
            if "RED 2" in self.active_routes:
                req_override_vol = self.c1['v'] * self.CONFIG['RED2_VOL_OVERRIDE_MULT']
                if is_red and c2_top_shadow_pct >= self.CONFIG['RED2_MIN_TOP_SHADOW_PCT'] and float(c_vol) >= req_override_vol:
                    survivors.append("RED 2")
                    
            # RED 1 - ЦВЕТ НЕ ВАЖЕН (пин-бар), важна только тень и тело
            if "RED 1" in self.active_routes:
                shadow_ratio = (c2_top_shadow / abs_body) if abs_body > 0 else 999.0
                if shadow_ratio >= self.CONFIG['RED1_C2_MIN_TOP_SHADOW_MULT'] and c2_body_pct <= self.CONFIG['RED1_C2_MAX_BODY_PCT']:
                    survivors.append("RED 1")
                    
            if survivors:
                self.active_routes = survivors
                self.state = "WAIT_C3"
                surv_str = ", ".join(survivors)
                self._dbg(f"⏳ [С2] Выжили: [{surv_str}]. Ждем С3. {m_log}")
                return None
                
            # Если никто не выжил - формируем детальный лог отмены (Факт/Требование) для активных маршрутов
            fail_logs = []
            for route in self.active_routes:
                if route == "RED 3":
                    req_overlap = self.c1['c'] - (c1_body * (self.CONFIG['RED3_MIN_OVERLAP_PCT'] / 100.0))
                    overlap_str = "ДА" if is_red and float(c_close) <= req_overlap else "НЕТ"
                    fail_logs.append(f"RED 3(Погл:{overlap_str}, Тело:{c2_body_pct:.0f}%/≥{self.CONFIG['RED3_MIN_BODY_PCT']}%, В.Тень:{c2_top_shadow_pct:.0f}%/≤{self.CONFIG['RED3_MAX_TOP_SHADOW_PCT']}%)")
                elif route == "RED 2":
                    req_vol = self.c1['v'] * self.CONFIG['RED2_VOL_OVERRIDE_MULT']
                    vol_ok = "ДА" if is_red and float(c_vol) >= req_vol else "НЕТ"
                    fail_logs.append(f"RED 2(Красная:{'ДА' if is_red else 'НЕТ'}, Объем:{vol_ok}, В.Тень:{c2_top_shadow_pct:.0f}%/≥{self.CONFIG['RED2_MIN_TOP_SHADOW_PCT']}%)")
                elif route == "RED 1":
                    shadow_ratio = (c2_top_shadow / abs_body) if abs_body > 0 else 999.0
                    fail_logs.append(f"RED 1(Тень:{shadow_ratio:.1f}x/≥{self.CONFIG['RED1_C2_MIN_TOP_SHADOW_MULT']}x, Тело:{c2_body_pct:.0f}%/≤{self.CONFIG['RED1_C2_MAX_BODY_PCT']}%)")
            
            # Если по какой-то причине маршрутов не было, выводим базу
            route_fails_str = " | ".join(fail_logs) if fail_logs else f"Цвет: {'RED' if is_red else 'GREEN'} | Тело: {c2_body_pct:.0f}% | В.Тень: {c2_top_shadow_pct:.0f}%"

            self._dbg(f"❌ Отмена [С2]: {route_fails_str}. {m_log}")
            self.state = "WAIT_C1"
            self.c1, self.c2, self.active_routes = None, None, []
            return None
        # --- ШАГ 1: ПОИСК СВЕЧИ 1 (ЯКОРЬ) ---
        if self.state == "WAIT_C1":
            # Тихая блокировка по EMA удалена. Теперь сканируем всё, что выше зоны паузы (5% от уровня).
                
            is_green = c_close > c_open
            hl = float(c_high - c_low)
            body = float(c_close - c_open)
            top_shadow = float(c_high - c_close)
            
            body_pct = (body / hl * 100.0) if hl > 0 else 0.0
            top_shadow_pct = (top_shadow / hl * 100.0) if hl > 0 else 0.0
            
            candle_range_pct = (hl / float(c_close)) * 100.0
            
            req_c1_vol = safe_baseline * self.CONFIG['C1_MIN_VOL_MULT']
            is_vol_ok = float(c_vol) >= req_c1_vol

            if is_green and is_vol_ok and candle_range_pct >= self.CONFIG['C1_MIN_RANGE_PCT']:
                vol_mult = float(c_vol) / safe_baseline
                
                self.active_routes = []
                
                # Примеряем RED 1
                if vol_mult >= self.CONFIG['RED1_C1_MIN_VOL_MULT'] and body_pct >= self.CONFIG['RED1_C1_MIN_BODY_PCT'] and top_shadow_pct <= self.CONFIG['RED1_C1_MAX_TOP_SHADOW_PCT']:
                    self.active_routes.append("RED 1")
                # Примеряем RED 2
                red2_req_range = self.CONFIG.get('RED2_C1_MIN_RANGE_PCT', self.CONFIG['C1_MIN_RANGE_PCT'])
                if vol_mult >= self.CONFIG['RED2_C1_MIN_VOL_MULT'] and body_pct >= self.CONFIG['RED2_C1_MIN_BODY_PCT'] and top_shadow_pct <= self.CONFIG['RED2_C1_MAX_TOP_SHADOW_PCT'] and candle_range_pct >= red2_req_range:
                    self.active_routes.append("RED 2")
                # Примеряем RED 3
                if vol_mult >= self.CONFIG['RED3_C1_MIN_VOL_MULT'] and body_pct >= self.CONFIG['RED3_C1_MIN_BODY_PCT'] and top_shadow_pct <= self.CONFIG['RED3_C1_MAX_TOP_SHADOW_PCT']:
                    self.active_routes.append("RED 3")
                    
                # Примеряем RED 4 (Запуск каскада строго от якоря С1)
                if vol_mult >= self.CONFIG.get('RED4_C1_MIN_VOL_MULT', 1.0) and body_pct >= self.CONFIG.get('RED4_C1_MIN_BODY_PCT', 40.0) and top_shadow_pct <= self.CONFIG.get('RED4_C1_MAX_TOP_SHADOW_PCT', 40.0):
                    self.active_routes.append("RED 4")
                    self.casc_state = "COUNTING"
                    self.casc_count = 0
                    self.casc_last_close = float('inf')
                    self.casc_start_high = float(c_high)
                    self.casc_pullback_high = 0.0
                    self.casc_low = float('inf')  # Жесткий сброс дна для новой волны

                if self.active_routes:
                    self.last_event_type = "SCAN"
                    self.c1 = {
                        'o': float(c_open), 'h': float(c_high), 'l': float(c_low), 'c': float(c_close), 'v': float(c_vol),
                        'vol_mult': vol_mult,
                        'body_pct': body_pct,
                        'top_shadow_pct': top_shadow_pct,
                        'range_pct': candle_range_pct,
                        'abs_body': body
                    }
                    self.state = "WAIT_REACTION"
                    
                    routes_str = ", ".join(self.active_routes)
                    req_vol = self.CONFIG['C1_MIN_VOL_MULT']
                    req_rng = self.CONFIG['C1_MIN_RANGE_PCT']
                    
                    c1_vol_fmt = self._fmt(float(c_vol))
                    base_vol_fmt = self._fmt(safe_baseline)
                    self._dbg(f"🎯 [С1] Якорь обьем: {c1_vol_fmt} (фон {base_vol_fmt}) - x{vol_mult:.1f} ({req_vol}) | R: {candle_range_pct:.2f}% ({req_rng}%) | тело {body_pct:.0f}%. Допущены: [{routes_str}]. {m_log}")
                else:
                    self._dbg(f"⚠️ [С1 ПРОПУСК] Зеленая не подошла: V x{vol_mult:.1f} (нужно {req_c1_vol/safe_baseline:.1f}) | Тело: {body_pct:.0f}% | В.Тень: {top_shadow_pct:.0f}%. {m_log}")
            elif is_green and not is_vol_ok:
                vol_mult = float(c_vol) / safe_baseline
               #self._dbg(f"⚠️ [С1 ПРОПУСК ПО ОБЪЕМУ] V: {self._fmt(float(c_vol))} (фон {self._fmt(safe_baseline)}) - x{vol_mult:.1f} < {self.CONFIG['C1_MIN_VOL_MULT']}. {m_log}")
            
        return None

    def _enter(self, c_high, c_close, all_opposite_levels, c_rsi=0.0):
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
        
        # Формируем расширенный лог в зависимости от маршрута
        if self.route == "RED 4 CASCADE":
            c1_range = self.c1['range_pct'] if self.c1 and 'range_pct' in self.c1 else 0.0
            base_reason = f"{self.route} | ур. ниже {dist_from_level:.1f}% | Спуск: {c1_range:.2f}%"
        else:
            c1_dict = self.c1 if isinstance(self.c1, dict) else {}
            c1_range = c1_dict.get('range_pct', 0.0)
            req_range = self.CONFIG.get('C1_MIN_RANGE_PCT', 0.0)
            c1_vol = c1_dict.get('vol_mult', 0.0) * 100
            c1_body = c1_dict.get('body_pct', 0.0)
            
            if self.route == "RED 1": req_body = self.CONFIG.get('RED1_C1_MIN_BODY_PCT', 0.0)
            elif self.route == "RED 2": req_body = self.CONFIG.get('RED2_C1_MIN_BODY_PCT', 0.0)
            elif self.route == "RED 3": req_body = self.CONFIG.get('RED3_C1_MIN_BODY_PCT', 0.0)
            else: req_body = 0.0
            
            base_reason = f"{self.route} | ур. ниже {dist_from_level:.1f}% | С1: V {c1_vol:.0f}% | R: {c1_range:.2f}% ({req_range}%) | тело {c1_body:.0f}%/{req_body:.0f}%"

        # Добавляем RSI и номер сделки в конец строки
        reason_str = f"{base_reason} | RSI:{c_rsi:.1f} | Сделка #{self.trades_count + 1}"

        self.trades_count += 1
        max_trades = self.CONFIG.get('MAX_TRADES_PER_LEVEL', 0)

        if max_trades > 0 and self.trades_count >= max_trades:
            self.state = "TRIGGERED"
            self._dbg(f"🛑 Лимит сделок на памп исчерпан ({self.trades_count}/{max_trades}). Вотчер остановлен.")
        else:
            self.state = "WAIT_C1"
            self.c1, self.c2 = None, None
            self.casc_state = "IDLE"
            self._dbg(f"🔄 Сделка #{self.trades_count} открыта. Возврат в поиск новых пиков (WAIT_C1).")

        return {"action": "SELL", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}