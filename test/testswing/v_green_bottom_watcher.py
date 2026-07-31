# -*- coding: utf-8 -*-
from .watcher_methods import _calc_tp_and_rr


class VGreenBottomWatcher:
    CONFIG = {
        'RED_TRIGGER_MULT': 2.0,      # Шаг 1: Триггер (красная свеча х2 фона)
        'GREEN_VOL_MULT': 3.0,        # Шаг 3: Выкупная зеленая (х3 фона)
        'MIN_BODY_PCT': 60.0,         # Шаг 3: Плотность зеленой (>= 60%)
        'MIN_PREV_RED_PCT': 40.0,     # ФИЛЬТР: Красная перед зеленой должна быть >= 30% от объема зеленой
        'BREATH_BUFFER_PCT': 1.5,  
        'MIN_ATR_MULT': 1.5,          # Отмена, если цена ушла высоко вверх (зазор 1.5%)
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
        self.temp_trigger_low = 0.0
        self.trigger_low = 0.0
        
        # Память для предыдущей свечи
        self.prev_is_red = False
        self.prev_red_vol = 0.0
        self.prev_low = 0.0

        self.sl_price: float | None = None
        self.entry_price: float | None = None
        self.history_log = ""
        
        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None

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
        self.temp_trigger_low = 0.0
        self.trigger_low = 0.0
        self.prev_is_red = False
        self.prev_red_vol = 0.0
        self.prev_low = 0.0
        self.history_log = ""

    def on_breach_start(self):
        if self.state in ("DEAD", "TRIGGERED"):
            return
        self._reset()

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, **kwargs):
        self._last_time = kwargs.get('candle_time')
        
        # 1. ДОСТАЕМ EMA ИЗ ПАРАМЕТРОВ БОТА/ТЕСТЕРА
        c_ema = kwargs.get('c_ema')
        
        if self.state in ("DEAD", "TRIGGERED"): return None
        if not baseline_vol or baseline_vol <= 0: return None
        if self.trade_type != 'LONG': return None

        # 2. ЖЕСТКАЯ ПРОВЕРКА ИЗНАЧАЛЬНО (только до старта)
        # Проверяем положение относительно EMA только пока мы в режиме "ожидания первого удара".
        if self.state == "WAIT_FIRST_DUMP" and c_ema is not None and c_ema == c_ema:
            # Если изначально уровень ИЛИ цена выше EMA — убиваем капкан сразу и навсегда
            if self.max > c_ema or c_close > c_ema:
                self._dbg(f"🛑 [ОТМЕНА ИЗНАЧАЛЬНО] Уровень или цена выше EMA ({c_ema:.2f}). Капкан убит до старта.")
                self.state = "DEAD"
                return None

        is_red = c_close < c_open
    
        # --- 0. ПРОВЕРКА ОТМЕНЫ (УХОД ЗА БУФЕР) ---
        buffer_top = self.max * (1 + self.CONFIG['BREATH_BUFFER_PCT'] / 100.0)
        
        # Если тело свечи закрылось выше уровня + N% зазор
        if c_close > buffer_top:
            if self.state != "DEAD":
                self._dbg(f"🛑 [ОТМЕНА СДЕЛКИ] Тело ушло выше уровня (+{self.CONFIG['BREATH_BUFFER_PCT']}% зазор). Уровень мертв.")
                self.state = "DEAD"  # Наглухо закрываем капкан
            return None

        # --- ШАГ 1: НАЧАЛО ПЕРВОГО ПРОЛИВА ---
        if self.state == "WAIT_FIRST_DUMP":
            if is_red and c_vol >= (baseline_vol * self.CONFIG['RED_TRIGGER_MULT']):
                self.state = "WAIT_FIRST_BOTTOM"
                self.temp_trigger_low = float(c_low)
                self.history_log = f"Фон:{self._fmt(baseline_vol)} -> Старт:{self._fmt(c_vol)}"
                self._dbg(f"🔴 [ШАГ 1] Удар продавца. {self.history_log}")
            
            # Сохраняем стейт свечи перед выходом
            self.prev_is_red = is_red
            self.prev_red_vol = float(c_vol) if is_red else 0.0
            self.prev_low = float(c_low)
            return None

        # --- ШАГ 1.5: ФИКСАЦИЯ ПЕРВОГО ДНА ---
        elif self.state == "WAIT_FIRST_BOTTOM":
            if is_red:
                # Цена продолжает падать, дна еще нет, просто тянем минимум вниз
                self.temp_trigger_low = min(self.temp_trigger_low, float(c_low))
            else:
                # Появилась зеленая свеча! Первое дно зафиксировано.
                self.state = "WAIT_SECOND_BOTTOM"
                self.trigger_low = self.temp_trigger_low
                self.history_log += f" -> Дно_1:{self.trigger_low:.2f}"
                self._dbg(f"✅ Первое дно зафиксировано: {self.trigger_low:.4f}. Ждем пробоя вниз.")
            
            self.prev_is_red = is_red
            self.prev_red_vol = float(c_vol) if is_red else 0.0
            self.prev_low = float(c_low)
            return None

        # --- ШАГ 2: ОЖИДАНИЕ ВТОРОГО ДНА ---
        elif self.state == "WAIT_SECOND_BOTTOM":
            if float(c_low) < self.trigger_low:
                self.state = "ARMED"
                self.history_log += f" -> Дно_2:{float(c_low):.2f}"
                self._dbg(f"📉 [ШАГ 2] Цена пробила первое дно (Low: {c_low:.4f}). Ищем снайперский выкуп!")
                # Проваливаемся в ШАГ 3

        # --- ШАГ 3: БОЕВОЙ РЕЖИМ (ПОИСК ВЫКУПА) ---
        if self.state == "ARMED":
            if not is_red: # ЗЕЛЕНАЯ СВЕЧА
                need_vol = baseline_vol * self.CONFIG['GREEN_VOL_MULT']
                is_vol_ok = c_vol >= need_vol

                # ПРОПУСКАЕМ ТЕСТ ТОЛЬКО ДЛЯ АНОМАЛЬНЫХ ЗЕЛЕНЫХ (спама больше не будет)
                if is_vol_ok:
                    high_low = float(c_high - c_low)
                    body = float(c_close - c_open)
                    body_pct = (body / high_low * 100.0) if high_low > 0 else 0.0
                    is_body_ok = body_pct >= self.CONFIG['MIN_BODY_PCT']

                    # НОВОЕ: Считаем требование по ATR (размер свечи)
                    min_req_size = c_atr * self.CONFIG.get('MIN_ATR_MULT', 1.5) if c_atr else 0.0
                    is_atr_ok = high_low >= min_req_size if min_req_size > 0 else True

                    # Обновленный лог со всеми данными
                    self._dbg(f"🔍 [ТЕСТ ЗЕЛЕНОЙ] Фон:{self._fmt(baseline_vol)} | Vol:{self._fmt(c_vol)} (надо>{self._fmt(need_vol)}), Плотность:{body_pct:.1f}% (надо>{self.CONFIG['MIN_BODY_PCT']}%), ATR-размер: {high_low:.4f} (надо>{min_req_size:.4f})")

                    if is_body_ok and is_atr_ok:
                        # Считаем минимальный требуемый объем для красной свечи
                        min_red_vol = c_vol * (self.CONFIG['MIN_PREV_RED_PCT'] / 100.0)

                        if self.prev_is_red and c_vol > self.prev_red_vol and self.prev_red_vol >= min_red_vol:
                            self.history_log += f" -> Выкуп:{self._fmt(c_vol)}"
                            return self._enter(c_low, c_close, all_opposite_levels)
                        else:
                            if not self.prev_is_red:
                                self._dbg("❌ [ОТМЕНА] Перед зеленой свечой не было красной.")
                            elif self.prev_red_vol < min_red_vol:
                                self._dbg(f"❌ [ОТМЕНА] Объем красной ({self._fmt(self.prev_red_vol)}) меньше {self.CONFIG['MIN_PREV_RED_PCT']}% от зеленой ({self._fmt(min_red_vol)}).")
                            else:
                                self._dbg(f"❌ [ОТМЕНА] Объем зеленой ({self._fmt(c_vol)}) МЕНЬШЕ предыдущей красной ({self._fmt(self.prev_red_vol)}).")
                    else:
                        if not is_body_ok:
                            self._dbg(f"❌ [ОТМЕНА] Тело свечи рыхлое (< {self.CONFIG['MIN_BODY_PCT']}%)")
                        if not is_atr_ok:
                            self._dbg(f"❌ [ОТМЕНА] Размер свечи ({high_low:.4f}) меньше {self.CONFIG.get('MIN_ATR_MULT', 1.5)} ATR ({min_req_size:.4f}). Это не импульс.")

            # Обновляем память в конце шага
            self.prev_is_red = is_red
            self.prev_red_vol = float(c_vol) if is_red else 0.0
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