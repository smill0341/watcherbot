# -*- coding: utf-8 -*-
from .watcher_methods import _calc_tp_and_rr

class VGreenBottomWatcher:
    CONFIG = {
        'RED_TRIGGER_MULT': 2.0,      # Во сколько раз объем первой красной свечи должен превысить средний (avg_vol), чтобы капкан активировался и засчитал старт Ямы №1.
        'MIN_BODY_PCT': 60.0,         # Минимальная плотность тела зеленой свечи. Тело (от Open до Close) должно занимать не менее 60% от всей длины свечи (от Low до High). Отсекает доджи и свечи с огромными тенями сверху.
        'BREATH_BUFFER_PCT': 1.5,     # Буфер отмены (зона дыхания). Если цена до начала падения улетит вверх на 1.5% выше верхней границы твоего уровня — капкан сбрасывается (убивается).
        'MIN_DUMP_VOL_PCT': 85.0,     # Требование к объему зеленой свечи. Она должна набрать минимум 85% от максимального объема падающей красной свечи в текущей яме (trigger_dump_vol).
        'MIN_ATR_MULT': 1.5,  
        # Минимальный физический размер зеленой свечи (High - Low). Она должна быть больше среднего ATR минимум в 1.5 раза. Отсекает рыночный микро-шум.
        
        # --- НАСТРОЙКИ СТРУКТУРЫ ---
        'MIN_PITS_TO_ARM': 3,         # Начиная с какой по счету ямы бот включает радар и начинает сканировать каждую зеленую свечу на дне.
        'MIN_PULLBACK_PCT': 3.0,      # Фильтр структуры. На сколько процентов цена должна физически отскочить от локального дна вверх, чтобы бот признал отскок состоявшимся и позволил начать следующую яму при пробое.
        'MAX_BARS_IN_PIT': 10,        # Максимальное количество свечей в яме.   
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
        
        self.state = "WAIT_FIRST_DUMP"
        
        # --- Чистая макро-структура (ChoCh) ---
        self.pits_count = 0             
        self.current_pit_low = 0.0      
        self.pit_ceiling = 0.0          
        self.highest_since_low = 0.0    
        self.pullback_confirmed = False 
        self.trigger_dump_vol = 0.0
        self.bars_since_low = 0         # <-- ДОБАВИЛИ: Таймер свечей на дне

        # --- Память для предыдущей свечи ---
        self.prev_is_red = False
        self.prev_red_vol = 0.0
        self.prev_low = 0.0

        # --- Служебные переменные ---
        self.sl_price: float | None = None
        self.entry_price: float | None = None
        self.history_log = ""
        
        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None
        self.last_event_type = None  # Для раскраски свечей на графике (red, yellow, blue)

    def _tp(self):
        return f"{self._last_time} " if self._last_time is not None else ""

    def _dbg(self, msg):
        self.last_event_time = self._last_time
        self.last_event_msg = msg
        if self.CONFIG.get('DEBUG'):
            with open("v_green_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{self._tp()}[{self.max:.4f}] {msg}\n")

    @staticmethod
    def _fmt(v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000: return f"{v/1_000:.1f}k"
        return str(int(v))

    def _reset(self):
        self.state = "WAIT_FIRST_DUMP"
        self.pits_count = 0       
        self.current_pit_low = 0.0
        self.highest_since_low = 0.0
        self.trigger_dump_vol = 0.0
        
        self.prev_is_red = False
        self.prev_red_vol = 0.0
        self.prev_low = 0.0
        self.history_log = ""
        self.last_event_type = None

    def on_breach_start(self):
        if self.state in ("DEAD", "TRIGGERED"):
            return
        self._reset()

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, **kwargs):
        self._last_time = kwargs.get('candle_time')
        # ИСПРАВЛЕНИЕ: принудительно обновляем время на каждом тике, чтобы тестер мог рисовать КАЖДУЮ свечу
        self.last_event_time = self._last_time  
        self.last_event_type = None
        
        if self.state in ("DEAD", "TRIGGERED"): return None
        if not baseline_vol or baseline_vol <= 0: return None
        if self.trade_type != 'LONG': return None

        safe_atr = float(c_atr) if (c_atr is not None and c_atr == c_atr) else 0.0001
        is_red = c_close < c_open

        buffer_top = self.max * (1 + self.CONFIG['BREATH_BUFFER_PCT'] / 100.0)
        if c_close > buffer_top and self.state == "WAIT_FIRST_DUMP":
            self.state = "DEAD" 
            return None

        # --- ШАГ 1: ПЕРВЫЙ УДАР В УРОВЕНЬ ---
        if self.state == "WAIT_FIRST_DUMP":
            if is_red and c_vol >= (baseline_vol * self.CONFIG['RED_TRIGGER_MULT']):
                self.state = "TRACKING_PIT"
                self.pits_count = 1
                self.current_pit_low = float(c_low)     
                self.pit_ceiling = float(c_high)
                self.highest_since_low = float(c_high)
                self.pullback_confirmed = False
                self.trigger_dump_vol = float(c_vol)
                self.last_event_type = "PIT"
                self._dbg(f"🔴 ЯМА №1 СТАРТ. Лой: {self.current_pit_low:.4f}")
            
            self.prev_is_red = is_red
            self.prev_low = float(c_low)
            return None

        # --- ШАГ 2: ВЕДЕНИЕ МАКРО-СТРУКТУРЫ ---
        elif self.state == "TRACKING_PIT":
            
            # РИСУЕМ КРАСНЫМ: красим все красные свечи, пока летим на дно
            if is_red and not self.pullback_confirmed:
                self.last_event_type = "PIT"

            # 1. ЕСЛИ ЦЕНА ОБНОВЛЯЕТ ДНО (Летим ниже)
            if float(c_low) < self.current_pit_low:
                self.bars_since_low = 0  # <-- НОВОЕ ДНО: СБРАСЫВАЕМ ТАЙМЕР
                
                # Новая яма засчитывается ТОЛЬКО если до этого цена отскочила на нужный процент
                if self.pullback_confirmed:
                    self.pits_count += 1
                    bounce_pct = (self.highest_since_low - self.current_pit_low) / self.current_pit_low * 100.0 if self.current_pit_low > 0 else 0.0
                    self.trigger_dump_vol = float(c_vol) if is_red else 0.0
                    self.last_event_type = "PIT" 
                    self._dbg(f"📉 ЯМА №{self.pits_count}. Отскок {bounce_pct:.1f}%")
                else:
                    if is_red:
                        self.trigger_dump_vol = max(self.trigger_dump_vol, float(c_vol))
                
                # Обновляем дно и сбрасываем трекеры
                self.current_pit_low = float(c_low)
                self.highest_since_low = float(c_high)
                self.pullback_confirmed = False 
            
            # 2. ЕСЛИ ЦЕНА НЕ ПРОБИВАЕТ ДНО (Флэт или рост)
            else:
                self.bars_since_low += 1 # <-- ДНА НЕТ: ТАЙМЕР ТИКАЕТ
                
                self.highest_since_low = max(self.highest_since_low, float(c_high))
                
                if is_red:
                    self.trigger_dump_vol = max(self.trigger_dump_vol, float(c_vol))
                else:
                    # ЗЕЛЕНАЯ СВЕЧА. Замеряем процент отскока
                    current_bounce_pct = (self.highest_since_low - self.current_pit_low) / self.current_pit_low * 100.0 if self.current_pit_low > 0 else 0.0
                    if current_bounce_pct >= self.CONFIG.get('MIN_PULLBACK_PCT', 3.0):
                        self.pullback_confirmed = True

            # 3. ПОИСК ЗЕЛЕНОЙ СВЕЧИ (ВНУТРИ ЯМЫ)
            if self.pits_count >= self.CONFIG.get('MIN_PITS_TO_ARM', 3) and not is_red:
                
                # ПРОВЕРКА НА ТАЙМАУТ СВЕЧЕЙ: Ищем вход, только если мы свежие на дне
                if self.bars_since_low <= self.CONFIG.get('MAX_BARS_IN_PIT', 10):
                    self.last_event_type = "SCAN" 
                    
                    need_vol = self.trigger_dump_vol * (self.CONFIG.get('MIN_DUMP_VOL_PCT', 100.0) / 100.0)
                    is_vol_ok = c_vol >= need_vol

                    high_low = float(c_high - c_low)
                    body = float(c_close - c_open)
                    body_pct = (body / high_low * 100.0) if high_low > 0 else 0.0
                    is_body_ok = body_pct >= self.CONFIG['MIN_BODY_PCT']

                    min_req_size = safe_atr * self.CONFIG.get('MIN_ATR_MULT', 1.5)
                    is_atr_ok = high_low >= min_req_size

                    self._dbg(f"🔍 [ТЕСТ ВХОДА] Яма:{self.pits_count} | Vol:{self._fmt(c_vol)} (надо>{self._fmt(need_vol)}) | Плотн:{body_pct:.1f}%")

                    if is_vol_ok and is_body_ok and is_atr_ok:
                        self.last_event_type = "GOOD_GREEN" 
                        self.history_log += f" -> Вход(Яма {self.pits_count}):{self._fmt(c_vol)}"
                        return self._enter(c_low, c_close, all_opposite_levels)
                    else:
                        if not is_vol_ok:
                            self._dbg(f"❌ [ОТМЕНА ВХОДА] Не хватило объема ({self._fmt(c_vol)} < {self._fmt(need_vol)})")
                        elif not is_body_ok:
                            self._dbg(f"❌ [ОТМЕНА ВХОДА] Рыхлое тело ({body_pct:.1f}% < {self.CONFIG['MIN_BODY_PCT']}%)")
                        elif not is_atr_ok:
                            self._dbg(f"❌ [ОТМЕНА ВХОДА] Свеча слишком мелкая (< {self.CONFIG['MIN_ATR_MULT']} ATR)")
                else:
                    # Если прошло больше 10 свечей, просто молча скипаем и ждем новую панику или яму
                    self._dbg(f"⏳ [БЛОК ВХОДА] Прошло {self.bars_since_low} св. Окно входа для этой ямы закрыто.")
            
            self.prev_is_red = is_red
            self.prev_low = float(c_low)
            return None

        return None
       

    def _enter(self, c_low, c_close, all_opposite_levels):
        self.state = "TRIGGERED"
        actual_entry = float(c_close)
        
        safe_low = min(float(c_low), self.prev_low) if self.prev_low > 0 else float(c_low)
        actual_sl = safe_low * 0.998

        self._dbg(f"🚪 Попытка входа! Entry: {actual_entry:.4f}, SL: {actual_sl:.4f}")

        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            self._dbg(f"❌ Калькулятор УБИЛ сделку: {err}")
            return {'error': err or "Risk data is None"}

        self.entry_price = actual_entry
        self.sl_price = risk_data['sl']
        reason_str = f"Green-Bottom [{self.history_log}]"

        self._dbg(f"🚀 СДЕЛКА СФОРМИРОВАНА: {reason_str}")
        return {"action": "BUY", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}