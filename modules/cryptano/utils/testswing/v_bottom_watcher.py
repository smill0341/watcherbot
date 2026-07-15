# -*- coding: utf-8 -*-
"""
v_bottom_watcher.py
====================
Стратегия V_BOTTOM. Строгая логика поиска пиков.
Ориентир -> Старт -> Пик 1 -> Выкуп 110% (1 свеча) ИЛИ Поиск Пика 2
"""

from watcher_methods import _calc_tp_and_rr

class VBottomWatcher:
    CONFIG = {
        'ELEVATED_VOL_MULT': 2.0,
        'CLIMAX_VS_ELEVATED': 1.7,   
        'CLIMAX_VOL_MULT': 4.0,
        'VOL_MATCH_PCT': 110.0,
        'MAX_GAP_CANDLES': 1, 
        'TP_MODE': 'fixed_pct',
        'FIXED_TP_PCT': 10.0,
        'TAKE_PROFIT': 10.0,
        'TP_BUFFER_PCT': 0.0,
        'SL_BUFFER': 0.5,
        'MIN_RR': 1.0,
        'USE_RR_FILTER': False,
        'DEBUG': False,   
    }

    def __init__(self, level_min, level_max, trade_type):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "SEARCHING"

        self.tracker_vol = 0.0  
        self.start_vol = 0.0    
        self.cand_low = 0.0
        self.cand_vol = 0.0     
        self.bars_since = 0     
        self.sl_price = None
        self.entry_price = None
        
        self.history_log = ""

    @staticmethod
    def _fmt(v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000: return f"{v/1_000:.1f}k"
        return str(int(v))

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, trend='UNKNOWN', vol_std=None):
        if self.state in ("DEAD", "TRIGGERED"):
            return None

        if not baseline_vol or baseline_vol <= 0:
            return None

        if self.trade_type == 'LONG':
            
            # --- 1. ПРОВЕРКА ОТМЕНЫ (СБРОС) ---
            if c_low > self.max:
                if self.state != "SEARCHING":
                    if self.history_log:
                        print(f"🛑 [СРЫВ] Уровень {self.max:.4f} | {self.history_log} -> [ОТМЕНА: Цена всплыла выше уровня]")
                    self.state = "SEARCHING"
                    self.history_log = ""
                return None

            is_red = c_close < c_open

            if self.state == "SEARCHING":
                # --- 2. ПОИСК ОРИЕНТИРА ---
                if is_red and c_vol >= (baseline_vol * self.CONFIG['ELEVATED_VOL_MULT']):
                    self.tracker_vol = c_vol
                    self.state = "WAIT_START"
                    self.history_log = f"Фон:{self._fmt(baseline_vol)} -> Ориентир:{self._fmt(c_vol)}"
                return None

            elif self.state == "WAIT_START":
                # --- 3. ПОИСК СТАРТА ---
                if is_red and c_vol > self.tracker_vol:
                    self.start_vol = c_vol
                    self.state = "WAIT_PEAK"
                    self.history_log += f" -> Старт:{self._fmt(c_vol)}"
                return None

            elif self.state == "WAIT_PEAK":
                # --- 4. ПОИСК ПЕРВОГО ПИКА ---
                if is_red and c_vol > self.start_vol:
                    self.cand_low = c_low
                    self.cand_vol = c_vol
                    self.state = "WAIT_GREEN"
                    self.history_log += f" -> Пик:{self._fmt(c_vol)}"
                return None

            elif self.state == "WAIT_GREEN":
                # --- 5. ПРОВЕРКА ВЫКУПА (СТРОГО ОДНА СВЕЧА) ---
                if c_close > c_open:
                    need_green = self.cand_vol * (self.CONFIG['VOL_MATCH_PCT'] / 100.0)
                    
                    if c_vol >= need_green:
                        # Идеальный выкуп! ВХОД!
                        self.state = "TRIGGERED"
                        actual_entry = c_close
                        actual_sl = self.cand_low * 0.998

                        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
                        if err or not risk_data:
                            self.state = "DEAD"
                            return {'error': err or "Risk data is None"}

                        self.entry_price = actual_entry
                        self.sl_price = risk_data['sl']
                        
                        self.history_log += f" -> Зел:{self._fmt(c_vol)}(ВХОД!)"
                        reason_str = f"V-Дно [{self.history_log}]"

                        return {"action": "BUY", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}
                    else:
                        # Слабая зеленая -> выкуп сорвался. Идем искать новый пик.
                        self.state = "WAIT_NEW_PEAK"
                else:
                    # Свеча красная. Проверяем, не обновила ли она пик сразу.
                    if c_vol >= self.cand_vol:
                        self.cand_low = c_low
                        self.cand_vol = c_vol
                        self.history_log += f" -> Пик+:{self._fmt(c_vol)}"
                        # Остаемся в WAIT_GREEN, ждем зеленую на следующей свече
                    else:
                        # Слабая красная -> выкуп сорвался. Идем искать новый пик.
                        self.state = "WAIT_NEW_PEAK"
                return None

            elif self.state == "WAIT_NEW_PEAK":
                # --- 6. ПОИСК НОВОГО ПИКА (ЕСЛИ ВЫКУП СОРВАЛСЯ) ---
                # Игнорируем зеленые и мелкие красные. Ждем красную >= прошлого пика.
                if is_red and c_vol >= self.cand_vol:
                    self.cand_low = c_low
                    self.cand_vol = c_vol
                    self.state = "WAIT_GREEN"
                    self.history_log += f" -> Пик+:{self._fmt(c_vol)}"
                return None

        elif self.trade_type == 'SHORT':
            return None

        return None