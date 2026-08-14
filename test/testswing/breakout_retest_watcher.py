# -*- coding: utf-8 -*-
from .watcher_methods import _calc_tp_and_rr

class BreakoutRetestWatcher:
    CONFIG = {
        'BASE_CANDLES': 15,                 # Сколько свечей собираем базу для объема
        'VOL_EXCESS_PCT': 20.0,             # На сколько % объем должен превысить базу (20%)
        
        # --- НАСТРОЙКИ РИСКА ---
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
        
        self.state = "WAIT_BREAKOUT"
        
        self.bars_counted = 0
        self.max_vol_base = 0.0

        # Служебные
        self.sl_price: float | None = None
        self.entry_price: float | None = None
        self.history_log = ""
        
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
            with open("breakout_retest_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{self._tp()}[{self.max:.4f}] {msg}\n")

    def _reset_chain(self):
        self.state = "DEAD"
        self._dbg("❌ Капкан жестко убит командой из симулятора.")
        
    def on_breach_start(self):
        if self.state not in ("DEAD", "TRIGGERED"):
            self.state = "WAIT_BREAKOUT"

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, **kwargs):
        self._last_time = kwargs.get('candle_time')
        self.last_event_time = self._last_time  
        self.last_event_type = None
        
        if self.state in ("DEAD", "TRIGGERED"): return None
        if not baseline_vol or baseline_vol <= 0: return None
        if self.trade_type != 'LONG': return None

        is_red = c_close < c_open

        # --- ШАГ 1: ФИКСАЦИЯ НАД УРОВНЕМ И СТАРТ СЧЕТЧИКА ---
        if self.state == "WAIT_BREAKOUT":
            self.last_event_type = "SCAN"
            
            if c_close > self.max:
                self.state = "WAIT_PULLBACK" # Используем этот статус, чтобы симулятор не убил уровень
                self.bars_counted = 1
                self.max_vol_base = float(c_vol)
                self.last_event_type = "BREAKOUT"
                self._dbg(f"🚀 СТАРТ. Свеча 1/10. Объем: {c_vol}")
            return None

        # --- ШАГ 2: НАКОПЛЕНИЕ БАЗЫ (10 СВЕЧЕЙ) ---
        elif self.state == "WAIT_PULLBACK":
            self.bars_counted += 1
            self.max_vol_base = max(self.max_vol_base, float(c_vol))
            self.last_event_type = "PULLBACK"
            
            self._dbg(f"⏳ Сбор базы. Свеча {self.bars_counted}/{self.CONFIG['BASE_CANDLES']}. Макс. объем: {self.max_vol_base}")
            
            if self.bars_counted >= self.CONFIG['BASE_CANDLES']:
                self.state = "WAIT_TRIGGER"
                self._dbg(f"🎯 БАЗА СОБРАНА. Целевой максимум: {self.max_vol_base}. Переход в режим охоты.")
            return None

        # --- ШАГ 3: ОХОТА (ВХОД НА ЗЕЛЕНОЙ СВЕЧЕ +20%) ---
        elif self.state == "WAIT_TRIGGER":
            self.last_event_type = "SCAN"
            
            if not is_red:
                target_vol = self.max_vol_base * (1 + self.CONFIG['VOL_EXCESS_PCT'] / 100.0)
                
                if c_vol >= target_vol:
                    self.last_event_type = "GOOD_GREEN"
                    self._dbg(f"✅ ВЫСТРЕЛ! Зеленая свеча. Объем {c_vol} >= {target_vol:.1f} (+20%)")
                    self.history_log = f"Volume Spike +20% (Base: {self.max_vol_base})"
                    return self._enter(c_low, c_close, all_opposite_levels)
                else:
                    self._dbg(f"🟡 Зеленая свеча мимо. Объем {c_vol} не дотянул до {target_vol:.1f}")
            else:
                self._dbg(f"🔴 Игнор красной свечи.")
            
            return None

        return None

    def _enter(self, c_low, c_close, all_opposite_levels):
        self.state = "TRIGGERED"
        actual_entry = float(c_close)
        
        # Стоп-лосс пока ставим просто за лой текущей пробойной свечи, так как зон больше нет
        actual_sl = float(c_low) * 0.998 

        self._dbg(f"🚪 ВХОД (Лонг Объем). Entry: {actual_entry:.4f}, SL: {actual_sl:.4f}")

        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            self._dbg(f"❌ Калькулятор УБИЛ сделку: {err}")
            return {'error': err or "Risk data is None"}

        self.entry_price = actual_entry
        self.sl_price = risk_data['sl']
        reason_str = f"Volume Breakout [{self.history_log}]"

        self._dbg(f"🚀 СДЕЛКА СФОРМИРОВАНА: {reason_str}")
        return {"action": "BUY", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}