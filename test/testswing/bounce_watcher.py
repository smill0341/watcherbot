# -*- coding: utf-8 -*-

class BounceWatcher:
    """
    v1 — ПРИМИТИВ. Задача не "точная стратегия", а быстро увидеть на графике,
    где вообще срабатывает идея "объёмный вход у уровня", прежде чем
    ужесточать условия (тень/тело, RR-фильтр, ATR-стоп и т.д. — см. TODO ниже).

    Условие входа:
        - цена коснулась зоны уровня (не обязательно глубоко)
        - свеча правильного цвета (зелёная для LONG, красная для SHORT)
        - объём >= VOL_SPIKE_MULT (по умолчанию x3) от фонового (baseline_vol)

    Выход: фиксированный % (FIXED_TP_PCT) — работает по-настоящему в этой
    версии, в отличие от старой (там 'fixed_pct' был мёртвой веткой и TP
    реально брался от противоположных уровней через _calc_tp_and_rr).
    """

    _log_cleared = False

    CONFIG = {
        'MIN_SCORE': 1.0,          
        'VOL_SPIKE_MULT': 3.0,     
        'MIN_VOL_MULT_TO_LOG': 2.0,   # Фильтр мусора: не рисовать SCAN и не писать лог, если объем ниже х2
        'MIN_BODY_PCT': 20.0,         # Плотность свечи: тело должно занимать минимум 40% от всего размаха
        'MAX_WICKS_PCT': 60.0,        # Защита от отвержения: верхняя тень (для лонга) не больше 30%
        'FIXED_TP_PCT': 7.0,       
        'SL_PCT': 50.0,            
        'MAX_TRADES_PER_LEVEL': 2, 
        'DEBUG': True,

        # --- TODO для следующих итераций (сейчас не используется) ---
        # 'PINBAR_SHADOW_RATIO': 1.5,   # требовать тень/тело — вернуть, когда примитив обкатан
        # 'MAX_BODY_PCT': 40.0,         # ограничить жирность тела свечи входа
        # 'USE_RR_FILTER': True,        # включить проверку риск/прибыль перед входом
        # 'MIN_RR': 1.0,
    }

    def __init__(self, level_min: float, level_max: float, trade_type: str):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "SCANNING"
        self._last_time = None
        self.last_event_time = None
        self.last_event_type = None
        self.trades_count = 0
    

        if self.CONFIG.get('DEBUG') and not BounceWatcher._log_cleared:
            with open("bounce_debug.log", "w", encoding="utf-8") as f:
                f.write("=== НОВЫЙ ТЕСТ BOUNCE (v1, примитив) ЗАПУЩЕН ===\n")
            BounceWatcher._log_cleared = True

    def on_breach_start(self):
        if self.state not in ("DEAD", "TRIGGERED"):
            self.state = "SCANNING"

    def _dbg(self, msg):
        if self.CONFIG.get('DEBUG'):
            time_str = f"{self._last_time} " if self._last_time else ""
            with open("bounce_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{time_str}[{self.trade_type} {self.min:.4f}-{self.max:.4f}] {msg}\n")

    @staticmethod
    def _fmt(v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000: return f"{v/1_000:.1f}k"
        return str(int(v))

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, level_score=0, candle_time=None, vol_90=0.0):
        self._last_time = candle_time
        self.last_event_time = candle_time
        self.last_event_type = None

        if self.state in ("TRIGGERED", "DEAD"):
            return None

        if level_score < self.CONFIG['MIN_SCORE']:
            return None

        c_open, c_high, c_low, c_close, c_vol = map(float, (c_open, c_high, c_low, c_close, c_vol))

        # --- СБОР СТАТИСТИКИ: Прокол дна (Sweep) ---
        if not hasattr(self, 'pierced_bottom'):
            self.pierced_bottom = False
            self.currently_pierced = False
            
        if self.trade_type == 'LONG':
            if c_low <= self.min:
                self.pierced_bottom = True  # Глобальный флаг для отчета (навсегда)
                if not self.currently_pierced:
                    self.last_event_type = "SWEEP_BOTTOM"  # Рисуем точку только 1 раз на прокол
                    self.currently_pierced = True
            else:
                self.currently_pierced = False  # Цена полностью поднялась над линией (c_low > min)
                
        elif self.trade_type == 'SHORT':
            if c_high >= self.max:
                self.pierced_bottom = True
                if not self.currently_pierced:
                    self.last_event_type = "SWEEP_BOTTOM"
                    self.currently_pierced = True
            else:
                self.currently_pierced = False
        # -------------------------------------------
        # -------------------------------------------

        
        # Для математики входа используем 90-й перцентиль (vol_90)
        logic_vol = vol_90 if vol_90 > 0 else baseline_vol
        vol_mult = (c_vol / logic_vol) if logic_vol > 0 else 0.0
        
        is_green = c_close > c_open
        is_red = c_close < c_open

        v_str = self._fmt(c_vol)

        # Высчитываем анатомию свечи
        hl = float(c_high - c_low)
        body = abs(float(c_close - c_open))
        top_shadow = float(c_high - max(c_open, c_close))
        bottom_shadow = min(c_open, c_close) - float(c_low)
        
        body_pct = (body / hl * 100.0) if hl > 0 else 0.0
        top_shadow_pct = (top_shadow / hl * 100.0) if hl > 0 else 0.0
        bottom_shadow_pct = (bottom_shadow / hl * 100.0) if hl > 0 else 0.0

        if self.trade_type == 'LONG':
            touched = c_low <= self.max and c_high >= self.min
            if touched and is_green:
                # Фильтр мусора: игнорим всё, что ниже MIN_VOL_MULT_TO_LOG
                if vol_mult >= self.CONFIG['MIN_VOL_MULT_TO_LOG']:
                    self.last_event_type = "SCAN"
                    
                    is_vol_ok = vol_mult >= self.CONFIG['VOL_SPIKE_MULT']
                    is_body_ok = body_pct >= self.CONFIG['MIN_BODY_PCT']
                    is_shadow_ok = top_shadow_pct <= self.CONFIG['MAX_WICKS_PCT']
                    
                    if is_vol_ok and is_body_ok and is_shadow_ok:
                        self.last_event_type = "GOOD_GREEN"
                        return self._enter(c_close, vol_mult, c_vol, logic_vol, baseline_vol)
                    else:
                        # Собираем причины отказа в строку
                        fail_reasons = []
                        if not is_vol_ok: fail_reasons.append(f"V:x{vol_mult:.1f}(<{self.CONFIG['VOL_SPIKE_MULT']})")
                        if not is_body_ok: fail_reasons.append(f"Тело:{body_pct:.0f}%(<{self.CONFIG['MIN_BODY_PCT']}%)")
                        if not is_shadow_ok: fail_reasons.append(f"В.Тень:{top_shadow_pct:.0f}%(>{self.CONFIG['MAX_WICKS_PCT']}%)")
                        
                        self._dbg(f"🟡 ПРОПУСК | ЗЕЛЕНАЯ | {', '.join(fail_reasons)} | V:{v_str}")

        elif self.trade_type == 'SHORT':
                if vol_mult >= self.CONFIG['MIN_VOL_MULT_TO_LOG']:
                    self.last_event_type = "SCAN"
                    
                    is_vol_ok = vol_mult >= self.CONFIG['VOL_SPIKE_MULT']
                    is_body_ok = body_pct >= self.CONFIG['MIN_BODY_PCT']
                    is_shadow_ok = bottom_shadow_pct <= self.CONFIG['MAX_WICKS_PCT']
                    
                    if is_vol_ok and is_body_ok and is_shadow_ok:
                        self.last_event_type = "GOOD_RED"
                        return self._enter(c_close, vol_mult, c_vol, logic_vol, baseline_vol)
                    else:
                        fail_reasons = []
                        if not is_vol_ok: fail_reasons.append(f"V:x{vol_mult:.1f}(<{self.CONFIG['VOL_SPIKE_MULT']})")
                        if not is_body_ok: fail_reasons.append(f"Тело:{body_pct:.0f}%(<{self.CONFIG['MIN_BODY_PCT']}%)")
                        if not is_shadow_ok: fail_reasons.append(f"Н.Тень:{bottom_shadow_pct:.0f}%(>{self.CONFIG['MAX_WICKS_PCT']}%)")
                        
                        self._dbg(f"🟡 ПРОПУСК | КРАСНАЯ | {', '.join(fail_reasons)} | V:{v_str}")

        return None

    def _enter(self, actual_entry, vol_mult, c_vol, logic_vol, baseline_vol):
        tp_pct = self.CONFIG['FIXED_TP_PCT'] / 100.0
        sl_pct = self.CONFIG['SL_PCT'] / 100.0

        if self.trade_type == 'LONG':
            tp = actual_entry * (1 + tp_pct)
            sl = actual_entry * (1 - sl_pct)
        else:
            tp = actual_entry * (1 - tp_pct)
            sl = actual_entry * (1 + sl_pct)

        self.trades_count += 1
        max_trades = self.CONFIG.get('MAX_TRADES_PER_LEVEL', 1)

        if max_trades > 0 and self.trades_count >= max_trades:
            self.state = "TRIGGERED"
        else:
            self.state = "SCANNING"

        v_str = self._fmt(c_vol)
        
        # Короткий и четкий лог
        reason_str = f"BOUNCE ({self.trades_count}/{max_trades}) | V:{v_str} (x{vol_mult:.1f})"
        self._dbg(f"✅ ВХОД: {actual_entry:.4f} | {reason_str}")

        return {
            "allow": True,
            "level_id": f"{self.min}_{self.max}",
            "action": "BUY" if self.trade_type == 'LONG' else "SELL",
            "entry_price": actual_entry,
            "sl": sl,
            "tp": tp,
            "reason": reason_str,
            "is_real_sweep": False,
            "candles_in_sweep": 0,
            "pierced_bottom": getattr(self, 'pierced_bottom', False)
        }