# -*- coding: utf-8 -*-
"""
v_bottom_watcher.py
====================
Стратегия V_BOTTOM (Разделение на Повышенный и Аномальный объемы).
Добавлена детальная отладка цифр прямо в лог сделки.
"""

from .watcher_methods import _calc_tp_and_rr

class VBottomWatcher:
    CONFIG = {
        'ELEVATED_VOL_MULT': 2.0,    
        'CLIMAX_VS_ELEVATED': 2.5,   # Твой множитель разрыва (Кульминации)
        'CLIMAX_VOL_MULT': 4.0,      
        'VOL_MATCH_PCT': 95.0,       
        'MAX_GAP_CANDLES': 2,        
        'TP_MODE': 'fixed_pct',
        'FIXED_TP_PCT': 10.0,
        'TAKE_PROFIT': 10.0,
        'TP_BUFFER_PCT': 0.0,
        'SL_BUFFER': 0.5,
        'MIN_RR': 1.0,
        'USE_RR_FILTER': False,
    }

    def __init__(self, level_min, level_max, trade_type):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "SEARCHING"
        
        self.tracker_vol = 0.0  
        self.debug_baseline = 0.0  # Запоминаем мертвый фон для отчета
        self.cand_low = 0.0
        self.cand_vol = 0.0     
        self.bars_since = 0
        self.sl_price = None
        self.entry_price = None

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, trend='UNKNOWN', vol_std=None):
        if self.state in ("DEAD", "TRIGGERED"): 
            return None
        
        if not baseline_vol or baseline_vol <= 0:
            return None

        # =================================================================
        # ЛОГИКА ДЛЯ ЛОНГА 
        # =================================================================
        if self.trade_type == 'LONG':
            if c_low > self.max: 
                return None 
                
            if self.state == "SEARCHING":
                # ШАГ 1: Ловим первый всплеск и запоминаем фон
                if c_close < c_open and c_vol >= (baseline_vol * self.CONFIG['ELEVATED_VOL_MULT']):
                    self.tracker_vol = c_vol
                    self.debug_baseline = baseline_vol # Фиксируем для лога
                    self.state = "TRAP_SET"
                return None

            elif self.state == "TRAP_SET":
                if c_close < c_open:
                    # ШАГ 2: Ищем Аномальный удар
                    is_anomalous_to_base = c_vol >= (baseline_vol * self.CONFIG['CLIMAX_VOL_MULT'])
                    is_culmination_spike = c_vol >= (self.tracker_vol * self.CONFIG['CLIMAX_VS_ELEVATED'])

                    if is_anomalous_to_base and is_culmination_spike:
                        self.cand_low = c_low
                        self.cand_vol = c_vol
                        self.bars_since = 0
                        self.state = "WAIT_GREEN"
                return None

            elif self.state == "WAIT_GREEN":
                self.bars_since += 1

                if self.bars_since > self.CONFIG['MAX_GAP_CANDLES']:
                    self.state = "SEARCHING"
                    return None

                if c_close < c_open: 
                    if c_vol > self.cand_vol:
                        self.cand_low = c_low
                        self.cand_vol = c_vol
                        self.bars_since = 0
                    return None

                elif c_close > c_open: 
                    # ШАГ 3: Зеленая свеча выкупа
                    is_vol_match = c_vol >= (self.cand_vol * (self.CONFIG['VOL_MATCH_PCT'] / 100.0))

                    if is_vol_match:
                        self.state = "TRIGGERED"
                        actual_entry = c_close
                        actual_sl = self.cand_low * 0.998 
                        
                        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
                        if err or not risk_data: 
                            self.state = "DEAD"
                            return {'error': err or "Risk data is None"}
                        
                        self.entry_price = actual_entry
                        self.sl_price = risk_data['sl']
                        
                        # ВОТ ТУТ ФОРМИРУЕТСЯ ПОДРОБНЫЙ ОТЧЕТ С ЦИФРАМИ:
                        def fmt(v):
                            if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
                            if v >= 1_000: return f"{v/1_000:.1f}k"
                            return str(int(v))
                            
                        target_barrier = self.tracker_vol * self.CONFIG['CLIMAX_VS_ELEVATED']
                        reason_str = f"V-Дно [Фон:{fmt(self.debug_baseline)} -> Старт:{fmt(self.tracker_vol)} -> Цель:{fmt(target_barrier)} | Удар:{fmt(self.cand_vol)} Зел:{fmt(c_vol)}]"
                        
                        return {"action": "BUY", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], 
                                "reason": reason_str}
                    else:
                        self.state = "SEARCHING"
                        return None

            return None

        # =================================================================
        # ЛОГИКА ДЛЯ ШОРТА (Отключена)
        # =================================================================
        elif self.trade_type == 'SHORT':
            return None

        return None