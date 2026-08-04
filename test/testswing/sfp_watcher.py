# -*- coding: utf-8 -*-
from typing import Optional

class SFPWatcher:
    CONFIG = {
        # --- ФИЛЬТРЫ КАСАНИЯ ---
        'RSI_OVERSOLD': 35.0,      # RSI для лонга (перепроданность)
        'RSI_OVERBOUGHT': 65.0,    # RSI для шорта (перекупленность)
        'VOL_CLIMAX_MULT': 1.8,    # Во сколько раз объем свечи касания должен превысить фон
        'KNIFE_ATR_MULT': 0.8,     # Фильтр "падающего ножа" (отсекаем пробои на полном ходу)
        
        # --- НАСТРОЙКИ СДЕЛКИ ---
        'TAKE_PROFIT': 10.0,       # Фиксированный Тейк-Профит в %
        'SL_BUFFER': 0.5,          # Отступ стоп-лосса за границу зоны в %
        'RR_RATIO': 3.0,           # Минимальный Risk/Reward (если меньше - отмена)
        'CANCEL_BUFFER_PCT': 1.0,  # Запас отмены (если цена пробила зону на 1% - сетап мертв)
        
        'DEBUG': True,
    }

    def __init__(self, level_min: float, level_max: float, trade_type: str):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        
        self.state = "WAIT_TOUCH"
        self.choch_level = 0.0
        
        # Память для "падающего ножа" и CHoCH
        self.p_open: Optional[float] = None
        self.p_high: Optional[float] = None
        self.p_low: Optional[float] = None
        self.p_close: Optional[float] = None
        self.p_vol: Optional[float] = None
        
        self.touch_rsi_value = 0.0
        self.touch_vol_ratio = 0.0

        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None
        self.last_event_type = None

    def _tp(self):
        return f"{self._last_time} " if self._last_time is not None else ""

    def _dbg(self, msg):
        self.last_event_time = self._last_time
        self.last_event_msg = msg
        if self.CONFIG.get('DEBUG'):
            with open("sfp_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{self._tp()}[{self.max:.4f}] {msg}\n")

    def _reset(self):
        self.state = "WAIT_TOUCH"
        self.choch_level = 0.0

    def on_breach_start(self):
        if self.state in ("DEAD", "TRIGGERED"):
            return
        self._reset()

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, **kwargs):
        self._last_time = kwargs.get('candle_time')
        self.last_event_type = None
        
        # Извлекаем RSI (симулятор должен передавать его в kwargs!)
        c_rsi = kwargs.get('c_rsi', 50.0) 

        if self.state in ("DEAD", "TRIGGERED"): 
            return None
            
        if not baseline_vol or baseline_vol <= 0: 
            return None
            
        # Ждем хотя бы одну свечу для формирования памяти (чтобы проверять ножи и хаи)
        # Проверяем ВСЕ пять атрибутов в одном условии — так анализатор типов
        # сужает тип каждого из них до float отдельно (а не только p_open).
        if (self.p_open is None or self.p_high is None or self.p_low is None
                or self.p_close is None or self.p_vol is None):
            self._save_prev(c_open, c_high, c_low, c_close, c_vol)
            return None

        # После проверки выше все пять гарантированно float, не None.
        p_open = self.p_open
        p_high = self.p_high
        p_low = self.p_low
        p_close = self.p_close
        p_vol = self.p_vol

        safe_atr = float(c_atr) if (c_atr is not None and c_atr == c_atr) else 0.0001
        c_vol_ratio = float(c_vol) / float(baseline_vol)

        # 1. ПРОВЕРКА НА ЛЕТЯЩИЙ НОЖ / РАКЕТУ
        is_falling_knife = False
        is_flying_rocket = False

        if c_close < c_open and p_close < p_open:
            if (c_open - c_close) > (safe_atr * self.CONFIG['KNIFE_ATR_MULT']) and c_vol >= p_vol:
                is_falling_knife = True

        if c_close > c_open and p_close > p_open:
            if (c_close - c_open) > (safe_atr * self.CONFIG['KNIFE_ATR_MULT']) and c_vol >= p_vol:
                is_flying_rocket = True

        # =================================================================
        # СОСТОЯНИЕ 1: ЖДЕМ КАСАНИЯ ЗОНЫ
        # =================================================================
        if self.state == "WAIT_TOUCH":
            
            if self.trade_type == "LONG":
                # Касание зоны: лой зашел в зону, закрытие всё еще выше дна зоны
                if c_low <= self.max and c_close > self.min:
                    if is_falling_knife:
                        self._dbg("⚠️ Отмена: Падающий нож в зоне.")
                    elif c_rsi > self.CONFIG['RSI_OVERSOLD']:
                        self._dbg(f"❌ Пропуск: RSI {c_rsi:.1f} > {self.CONFIG['RSI_OVERSOLD']}")
                    elif c_vol_ratio < self.CONFIG['VOL_CLIMAX_MULT']:
                        self._dbg(f"❌ Пропуск: Объем {c_vol_ratio:.1f}x < {self.CONFIG['VOL_CLIMAX_MULT']}x")
                    else:
                        # ВСЕ ФИЛЬТРЫ ПРОЙДЕНЫ! Фиксируем CHoCH и ждем пробой.
                        self.state = "WAIT_CHOCH"
                        self.choch_level = max(float(c_high), p_high)
                        self.touch_rsi_value = c_rsi
                        self.touch_vol_ratio = c_vol_ratio
                        self.last_event_type = "TOUCH"
                        self._dbg(f"🎯 КАСАНИЕ (Лонг)! RSI={c_rsi:.1f}, Vol={c_vol_ratio:.1f}x. Ждем CHoCH пробой выше {self.choch_level:.4f}")
            
            # (Для шорта логика зеркальная, если понадобится)
            elif self.trade_type == "SHORT":
                if c_high >= self.min and c_close < self.max:
                    if is_flying_rocket:
                        self._dbg("⚠️ Отмена: Летящая ракета в зоне.")
                    elif c_rsi < self.CONFIG['RSI_OVERBOUGHT']:
                        self._dbg(f"❌ Пропуск: RSI {c_rsi:.1f} < {self.CONFIG['RSI_OVERBOUGHT']}")
                    elif c_vol_ratio < self.CONFIG['VOL_CLIMAX_MULT']:
                        self._dbg(f"❌ Пропуск: Объем {c_vol_ratio:.1f}x < {self.CONFIG['VOL_CLIMAX_MULT']}x")
                    else:
                        self.state = "WAIT_CHOCH"
                        self.choch_level = min(float(c_low), p_low) # Слом для шорта по лоям
                        self.touch_rsi_value = c_rsi
                        self.touch_vol_ratio = c_vol_ratio
                        self.last_event_type = "TOUCH"
                        self._dbg(f"🎯 КАСАНИЕ (Шорт)! RSI={c_rsi:.1f}, Vol={c_vol_ratio:.1f}x. Ждем CHoCH ниже {self.choch_level:.4f}")

        # =================================================================
        # СОСТОЯНИЕ 2: ЖДЕМ ПРОБОЯ СТРУКТУРЫ (CHoCH)
        # =================================================================
        elif self.state == "WAIT_CHOCH":
            
            if self.trade_type == "LONG":
                # Отмена 1: Цена провалилась ниже зоны с буфером
                if c_low < self.min * (1 - self.CONFIG['CANCEL_BUFFER_PCT'] / 100):
                    self.state = "DEAD"
                    self._dbg(f"💀 Сетап убит: цена провалилась ниже зоны.")
                    return None
                
                # ТРИГГЕР: Цена закрылась выше уровня CHoCH
                if float(c_close) > self.choch_level:
                    return self._enter(c_close)
            
            elif self.trade_type == "SHORT":
                if c_high > self.max * (1 + self.CONFIG['CANCEL_BUFFER_PCT'] / 100):
                    self.state = "DEAD"
                    return None
                if float(c_close) < self.choch_level:
                    return self._enter(c_close)

        # Обновляем память перед переходом к следующей свече
        self._save_prev(c_open, c_high, c_low, c_close, c_vol)
        return None

    def _save_prev(self, c_open, c_high, c_low, c_close, c_vol):
        self.p_open = float(c_open)
        self.p_high = float(c_high)
        self.p_low = float(c_low)
        self.p_close = float(c_close)
        self.p_vol = float(c_vol)

    def _enter(self, current_close):
        self.state = "TRIGGERED"
        
        # СТАРАЯ ЖЕСТКАЯ МАТЕМАТИКА (ФИКС 10%) ИЗ WATCHER_LOGIC.PY
        if self.trade_type == "LONG":
            actual_sl = self.min * (1 - self.CONFIG['SL_BUFFER'] / 100)
            actual_tp = current_close * (1 + self.CONFIG['TAKE_PROFIT'] / 100)
            risk = current_close - actual_sl
            reward = actual_tp - current_close
        else:
            actual_sl = self.max * (1 + self.CONFIG['SL_BUFFER'] / 100)
            actual_tp = current_close * (1 - self.CONFIG['TAKE_PROFIT'] / 100)
            risk = actual_sl - current_close
            reward = current_close - actual_tp

        # ФИЛЬТР R/R
        if risk <= 0 or (reward / risk) < self.CONFIG['RR_RATIO']:
            rr_val = (reward / risk) if risk > 0 else 0
            self.state = "DEAD"
            self._dbg(f"❌ ОТМЕНА ВХОДА: Плохой R/R. {rr_val:.2f} < {self.CONFIG['RR_RATIO']}")
            return {'error': "Плохой R/R"}

        self.last_event_type = "TRIGGER"
        reason_str = f"CHoCH Breakout | RSI={self.touch_rsi_value:.1f} | Vol={self.touch_vol_ratio:.1f}x"
        self._dbg(f"🚀 ВХОД! {reason_str}. Вход: {current_close:.4f}, SL: {actual_sl:.4f}")
        
        return {
            "action": "BUY" if self.trade_type == "LONG" else "SELL", 
            "entry_price": current_close, 
            "sl": actual_sl, 
            "tp": actual_tp, 
            "reason": reason_str
        }