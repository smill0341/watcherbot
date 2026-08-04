# -*- coding: utf-8 -*-
from .risk_calc import calc_tp_and_rr

class VGreenBottomWatcher:
    CONFIG = {
        'RED_TRIGGER_MULT': 2.0,      # Во сколько раз объем первой красной свечи должен превысить средний (avg_vol), чтобы капкан активировался и засчитал старт Ямы №1.
        'MIN_BODY_PCT': 60.0,         # Минимальная плотность тела зеленой свечи. Тело (от Open до Close) должно занимать не менее 60% от всей длины свечи (от Low до High). Отсекает доджи и свечи с огромными тенями сверху.
        'BREATH_BUFFER_PCT': 1.5,     # Буфер отмены (зона дыхания). Если цена до начала падения улетит вверх на 1.5% выше верхней границы твоего уровня — капкан сбрасывается (убивается).
        'MIN_DUMP_VOL_PCT': 65.0,     # Требование к объему зеленой свечи. Она должна набрать минимум 85% от максимального объема падающей красной свечи в текущей яме (trigger_dump_vol).
        'MIN_ATR_MULT': 1.5, 
        'MIN_PREV_RED_TOP_WICK_PCT': 5.0, # Верхняя тень красной свечи минимум 5% от ее длины 
        'MIN_CLOSE_MARGIN_PCT': 0.2,      # Мин. зазор закрытия (0.2% выше открытия красной)
        
        # --- НАСТРОЙКИ КУЛЬМИНАЦИИ (АНОМАЛЬНЫЙ ОБЪЕМ) ---
        'CLIMAX_VOL_MULT': 50.0,      # Во сколько раз объем должен превысить базовый для старта Кульминации
        'CLIMAX_MAX_BARS': 5,         # Сколько свечей даем на перекрытие тела# Минимальный физический размер зеленой свечи (High - Low). Она должна быть больше среднего ATR минимум в 1.5 раза. Отсекает рыночный микро-шум.
        'MIN_CLIMAX_VOL_USD': 1000000.0, # <-- МИНИМУМ 1 МЛН ДОЛЛАРОВ
        
        # --- НАСТРОЙКИ СТРУКТУРЫ ---
        'MIN_BREAKDOWN_PCT': 1.8,     # На сколько процентов цена должна пробить старое дно, чтобы начать новую яму
        'MIN_PITS_TO_ARM': 3,         # Начиная с какой по счету ямы бот включает радар и начинает сканировать каждую зеленую свечу на дне.
        'MIN_PULLBACK_PCT': 3.0,      # Фильтр структуры. На сколько процентов цена должна физически отскочить от локального дна вверх, чтобы бот признал отскок состоявшимся и позволил начать следующую яму при пробое.
        'MAX_BARS_IN_PIT': 10,        # Максимальное количество свечей в яме.
        'MAX_PREV_RED_BODY_PCT': 52.0, #  Максимальный размер тела предыдущей красной свечи (в %)   
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
        
        self.state = "WAIT_FIRST_DUMP"
        
        # --- Чистая макро-структура (ChoCh) ---
        self.pits_count = 0             
        self.current_pit_low = 0.0      
        self.pit_ceiling = 0.0          
        self.highest_since_low = 0.0    
        self.pullback_confirmed = False 
        self.trigger_dump_vol = 0.0
        self.bars_since_low = 0         #  Таймер свечей на дне
        self.locked_pit_low = 0.0   
        
        # --- Память для Кульминации ---
        self.in_climax_mode = False
        self.climax_low = 0.0
        self.climax_body_top = 0.0
        self.climax_timer = 0

        # --- Память для предыдущей свечи ---
        self.prev_is_red = False
        self.prev_red_vol = 0.0
        self.prev_low = 0.0
        self.prev_body_pct = 0.0
        self.prev_top_shadow_pct = 0.0
        self.prev_open = 0.0

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
        self.prev_open = 0.0
        self.history_log = ""
        self.last_event_type = None
        
        self.prev_body_pct = 0.0
        self.prev_top_shadow_pct = 0.0
        
        self.locked_pit_low = 0.0  # 
        self.bars_since_low = 0    # 

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
                self.locked_pit_low = float(c_low)    # <-- ФИКСИРУЕМ СТРУКТУРНОЕ ДНО
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

            # --- НОВЫЙ БЛОК: РЕЖИМ КУЛЬМИНАЦИИ (CLIMAX MODE) ---
            climax_mult = self.CONFIG.get('CLIMAX_VOL_MULT', 50.0)
            min_usd_vol = self.CONFIG.get('MIN_CLIMAX_VOL_USD', 1000000.0)

            if baseline_vol:
                # c_vol - это УЖЕ доллары. Никаких умножений на цену!
                is_climax_vol = (c_vol >= baseline_vol * climax_mult) and (c_vol >= min_usd_vol)
            else:
                is_climax_vol = False

            # 1. Если видим аномальную красную свечу (и это минимум 2-я яма)
            if is_red and is_climax_vol and self.pits_count >= 2:
                self.in_climax_mode = True
                self.climax_low = float(c_low)
                self.climax_body_top = float(c_open) # У красной свечи Открытие (Open) всегда сверху тела
                self.climax_timer = 0
                self._dbg(f"🔥 КУЛЬМИНАЦИЯ! Яма:{self.pits_count} | Vol x{c_vol/baseline_vol:.1f} | Объем: ${self._fmt(c_vol)}. Пол:{self.climax_low:.4f}")
            
            # 2. Если мы УЖЕ в режиме ожидания перекрытия
            elif self.in_climax_mode:
                if float(c_low) < self.climax_low:
                    self._dbg(f"❌ Кульминация сломана: пробили тень ({c_low:.4f} < {self.climax_low:.4f})")
                    self.in_climax_mode = False
                elif self.climax_timer >= self.CONFIG.get('CLIMAX_MAX_BARS', 5):
                    self._dbg(f"⏳ Кульминация отменена: вышло время ({self.CONFIG['CLIMAX_MAX_BARS']} св.)")
                    self.in_climax_mode = False
                else:
                    self.climax_timer += 1
                    # Если зеленая закрылась ВЫШЕ открытия (тела) красной кульминационной
                    if not is_red and float(c_close) > self.climax_body_top:
                        self.last_event_type = "GOOD_GREEN"
                        self.history_log += f" -> Вход(Climax x{climax_mult})"
                        self._dbg(f"🚀 [CLIMAX ВХОД] Зеленая перекрыла тело красной ({c_close:.4f} > {self.climax_body_top:.4f})!")
                        return self._enter(c_low, c_close, all_opposite_levels)
            # --- КОНЕЦ БЛОКА КУЛЬМИНАЦИИ ---

            # 1. ЕСЛИ ЦЕНА ОБНОВЛЯЕТ ЛОКАЛЬНОЕ ДНО (Летим ниже)
            if float(c_low) < self.current_pit_low:
                self.bars_since_low = 0  # <-- НОВОЕ ДНО: СБРАСЫВАЕМ ТАЙМЕР ВХОДА
                
                if self.pullback_confirmed:
                    # Считаем границу НАСТОЯЩЕГО пробоя (-2% от зафиксированного дна ямы)
                    breakdown_target = self.locked_pit_low * (1 - self.CONFIG.get('MIN_BREAKDOWN_PCT', 2.0) / 100.0)
                    
                    if float(c_low) <= breakdown_target:
                        # НАСТОЯЩИЙ ПРОБОЙ
                        self.pits_count += 1
                        bounce_pct = (self.highest_since_low - self.locked_pit_low) / self.locked_pit_low * 100.0 if self.locked_pit_low > 0 else 0.0
                        self.trigger_dump_vol = float(c_vol) if is_red else 0.0
                        self.last_event_type = "PIT" 
                        self._dbg(f"📉 ЯМА №{self.pits_count} (Пробой > {self.CONFIG.get('MIN_BREAKDOWN_PCT')}%). Отскок был {bounce_pct:.1f}%")
                        
                        # Фиксируем новое структурное дно и сбрасываем отскок
                        self.locked_pit_low = float(c_low)
                        self.pullback_confirmed = False 
                    else:
                        # ЗАКОЛ: Дно пробили, но недостаточно глубоко. Счечик ям НЕ трогаем.
                        if is_red:
                            self.trigger_dump_vol = max(self.trigger_dump_vol, float(c_vol))
                        self._dbg(f"⚠️ ЗАКОЛ: Лой {c_low:.4f}, ждем пробоя ниже {breakdown_target:.4f}")
                else:
                    # Мы всё еще падаем в рамках текущей ямы, отскока еще не было.
                    # Тянем структурное дно за ценой вниз.
                    self.locked_pit_low = float(c_low)
                    if is_red:
                        self.trigger_dump_vol = max(self.trigger_dump_vol, float(c_vol))
                
                # В ЛЮБОМ СЛУЧАЕ (и при пробое, и при заколе) сдвигаем локальное дно под новый уровень
                # чтобы будущий 3% отскок считался от самой нижней точки
                self.current_pit_low = float(c_low)
                self.highest_since_low = float(c_high)
            
            
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
                    
                    # ПРОВЕРКА ПРЕДЫДУЩЕЙ СВЕЧИ: Должна быть красной, не слишком плотной И иметь тень сверху
                    max_red_body = self.CONFIG.get('MAX_PREV_RED_BODY_PCT', 50.0)
                    min_top_wick = self.CONFIG.get('MIN_PREV_RED_TOP_WICK_PCT', 5.0)
                    
                    is_prev_red_ok = self.prev_is_red and (self.prev_body_pct <= max_red_body) and (self.prev_top_shadow_pct >= min_top_wick)

                    # <-- УСЛОВИЕ ОБЪЕМА: Объем зеленой >= 90% от предыдущей красной
                    is_local_vol_ok = c_vol >= (self.prev_red_vol * 0.9)

                    # <-- НОВОЕ УСЛОВИЕ ЗАЗОРА: Закрытие строго на X% выше открытия красной
                    min_margin_pct = self.CONFIG.get('MIN_CLOSE_MARGIN_PCT', 0.2)
                    target_close = self.prev_open * (1 + (min_margin_pct / 100.0)) if self.prev_open > 0 else 0.0
                    is_margin_ok = float(c_close) >= target_close

                    self._dbg(f"🔍 [ТЕСТ ВХОДА] Яма:{self.pits_count} | Vol:{self._fmt(c_vol)} | Зазор(>={min_margin_pct}%): {is_margin_ok} | Плотн:{body_pct:.1f}% | Пред.Красн(Тело {self.prev_body_pct:.1f}%) -> {is_prev_red_ok}")

                    # Вход только если сошлись ВСЕ 6 фильтров (добавили is_margin_ok)
                    if is_vol_ok and is_body_ok and is_atr_ok and is_prev_red_ok and is_local_vol_ok and is_margin_ok:
                        self.last_event_type = "GOOD_GREEN" 
                        self.history_log += f" -> Вход(Яма {self.pits_count}):{self._fmt(c_vol)}"
                        return self._enter(c_low, c_close, all_opposite_levels)
                    else:
                        # Логируем причину отмены
                        if not is_vol_ok:
                            self._dbg(f"❌ [ОТМЕНА ВХОДА] Не хватило глобального объема ({self._fmt(c_vol)} < {self._fmt(need_vol)})")
                        elif not is_local_vol_ok:
                            self._dbg(f"❌ [ОТМЕНА ВХОДА] Зеленая слабее пред. красной ({self._fmt(c_vol)} < {self._fmt(self.prev_red_vol * 0.9)})")
                        elif not is_margin_ok:
                            self._dbg(f"❌ [ОТМЕНА ВХОДА] Нет зазора пробоя (Закрытие {float(c_close):.4f} < Цели {target_close:.4f})")
                        elif not is_body_ok:
                            self._dbg(f"❌ [ОТМЕНА ВХОДА] Рыхлое тело зел. ({body_pct:.1f}% < {self.CONFIG['MIN_BODY_PCT']}%)")
                        elif not is_atr_ok:
                            self._dbg(f"❌ [ОТМЕНА ВХОДА] Зел. свеча слишком мелкая (< {self.CONFIG['MIN_ATR_MULT']} ATR)")
                        elif not is_prev_red_ok:
                            if not self.prev_is_red:
                                self._dbg(f"❌ [ОТМЕНА ВХОДА] Перед зеленой была НЕ красная свеча")
                            elif self.prev_body_pct > max_red_body:
                                self._dbg(f"❌ [ОТМЕНА ВХОДА] Пред. красная слишком плотная (Тело {self.prev_body_pct:.1f}% > {max_red_body}%)")
                            else:
                                self._dbg(f"❌ [ОТМЕНА ВХОДА] У пред. красной нет верхней тени (Тень {self.prev_top_shadow_pct:.1f}% < {min_top_wick}%)")
                else:
                    # Если прошло больше 10 свечей, просто молча скипаем и ждем новую панику или яму
                    self._dbg(f"⏳ [БЛОК ВХОДА] Прошло {self.bars_since_low} св. Окно входа для этой ямы закрыто.")
            
            self.prev_is_red = is_red
            self.prev_low = float(c_low)
            self.prev_open = float(c_open) #  Запоминаем цену открытия красной свечи
            self.prev_red_vol = float(c_vol) if is_red else 0.0
            
            # Вычисляем и запоминаем процент тела и верхней тени текущей свечи
            hl = float(c_high - c_low)
            bdy = abs(float(c_close - c_open))
            self.prev_body_pct = (bdy / hl * 100.0) if hl > 0 else 0.0
            
            # Если свеча красная, верхняя тень это расстояние от High до Open
            if is_red and hl > 0:
                self.prev_top_shadow_pct = (float(c_high - c_open) / hl * 100.0)
            else:
                self.prev_top_shadow_pct = 0.0
            
            return None

        return None
       

    def _enter(self, c_low, c_close, all_opposite_levels):
        self.state = "TRIGGERED"
        actual_entry = float(c_close)
        
        safe_low = min(float(c_low), self.prev_low) if self.prev_low > 0 else float(c_low)
        actual_sl = safe_low * 0.998

        self._dbg(f"🚪 Попытка входа! Entry: {actual_entry:.4f}, SL: {actual_sl:.4f}")

        risk_data, err = calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            self._dbg(f"❌ Калькулятор УБИЛ сделку: {err}")
            return {'error': err or "Risk data is None"}

        self.entry_price = actual_entry
        self.sl_price = risk_data['sl']
        reason_str = f"Green-Bottom [{self.history_log}]"

        self._dbg(f"🚀 СДЕЛКА СФОРМИРОВАНА: {reason_str}")
        return {"action": "BUY", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}