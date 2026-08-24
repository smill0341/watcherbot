# -*- coding: utf-8 -*-
from .watcher_methods import _calc_tp_and_rr

class BounceWatcher:
    _log_cleared = False 

    CONFIG = {
        'MIN_SCORE': 3.0,               # Фильтр: только сильные уровни
        'MIN_VOL_MULT': 1.5,            # Всплеск объема х1.5 от фона
        'PINBAR_SHADOW_RATIO': 2.0,     # Тень минимум в 2 раза больше тела
        'MAX_BODY_PCT': 30.0,           # Плотность тела не более 30%
        
        'TP_MODE': 'fixed_pct',         # Или 'structural', если хочешь тянуть до противоположного уровня
        'FIXED_TP_PCT': 10.0,            # Условный профит, если не структурный
        'TAKE_PROFIT': 5.0,
        'TP_BUFFER_PCT': 0.0,
        'SL_BUFFER': 0.1,               # Отступ стопа за кончик тени
        'RR_TARGET': 1.0,               # Целевой Risk/Reward (например, 1 к 5)
        'MIN_RR': 1.0,
        'USE_RR_FILTER': false,                # Фильтр по R:R (если True, то сделки с R:R < MIN_RR не открываются)
        'DEBUG': True,
    }

    def __init__(self, level_min: float, level_max: float, trade_type: str):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "SCANNING"
        self._last_time = None
        self.trades_count = 0
        
        if self.CONFIG.get('DEBUG') and not BounceWatcher._log_cleared:
            with open("bounce_debug.log", "w", encoding="utf-8") as f:
                f.write("=== НОВЫЙ ТЕСТ BOUNCE ЗАПУЩЕН ===\n")
            BounceWatcher._log_cleared = True

    def _dbg(self, msg):
        if self.CONFIG.get('DEBUG'):
            time_str = f"{self._last_time} " if self._last_time else ""
            with open("bounce_debug.log", "a", encoding="utf-8") as f:
                f.write(f"{time_str}[{self.trade_type} {self.min:.4f}-{self.max:.4f}] {msg}\n")

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, level_score=0, candle_time=None):
        self._last_time = candle_time
        
        if self.state in ("TRIGGERED", "DEAD"):
            return None
            
        if level_score < self.CONFIG['MIN_SCORE']:
            return None

        c_open, c_high, c_low, c_close, c_vol = map(float, (c_open, c_high, c_low, c_close, c_vol))
        hl = c_high - c_low
        if hl <= 0: return None

        body = abs(c_close - c_open)
        body_pct = (body / hl) * 100.0
        vol_mult = (c_vol / baseline_vol) if baseline_vol and baseline_vol > 0 else 0.0

        if self.trade_type == 'LONG':
            # Лонг от поддержки (верхняя граница уровня - max)
            if c_low <= self.max and c_close > self.max:
                lower_shadow = min(c_open, c_close) - c_low
                shadow_ratio = (lower_shadow / body) if body > 0 else 999.0
                
                if shadow_ratio >= self.CONFIG['PINBAR_SHADOW_RATIO'] and body_pct <= self.CONFIG['MAX_BODY_PCT']:
                    if vol_mult >= self.CONFIG['MIN_VOL_MULT']:
                        sl = c_low * (1.0 - self.CONFIG['SL_BUFFER'] / 100.0)
                        return self._enter(c_close, sl, vol_mult, shadow_ratio, body_pct, all_opposite_levels)

        elif self.trade_type == 'SHORT':
            # Шорт от сопротивления (нижняя граница уровня - min)
            if c_high >= self.min and c_close < self.min:
                upper_shadow = c_high - max(c_open, c_close)
                shadow_ratio = (upper_shadow / body) if body > 0 else 999.0
                
                if shadow_ratio >= self.CONFIG['PINBAR_SHADOW_RATIO'] and body_pct <= self.CONFIG['MAX_BODY_PCT']:
                    if vol_mult >= self.CONFIG['MIN_VOL_MULT']:
                        sl = c_high * (1.0 + self.CONFIG['SL_BUFFER'] / 100.0)
                        return self._enter(c_close, sl, vol_mult, shadow_ratio, body_pct, all_opposite_levels)

        return None

    def _enter(self, actual_entry, actual_sl, vol_mult, shadow_ratio, body_pct, all_opposite_levels):
        self.state = "TRIGGERED"
        
        # Запрашиваем расчет профита у штатного калькулятора
        risk_data, err = _calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            self._dbg(f"❌ Калькулятор отклонил сделку: {err}")
            return {'error': err}

        # Если используем жесткий R:R вместо расчетного TP:
        if self.CONFIG['TP_MODE'] == 'fixed_rr':
            risk = abs(actual_entry - actual_sl)
            tp = actual_entry + (risk * self.CONFIG['RR_TARGET']) if self.trade_type == 'LONG' else actual_entry - (risk * self.CONFIG['RR_TARGET'])
        else:
            tp = risk_data['tp']

        reason_str = f"BOUNCE | V x{vol_mult:.1f} | Тень x{shadow_ratio:.1f} | Тело {body_pct:.1f}%"
        self._dbg(f"✅ ВХОД! Entry: {actual_entry:.4f}, SL: {actual_sl:.4f}, TP: {tp:.4f}")

        return {
            "allow": True,
            "level_id": f"{self.min}_{self.max}",
            "action": "BUY" if self.trade_type == 'LONG' else "SELL",
            "entry_price": actual_entry,
            "sl": actual_sl,
            "tp": tp,
            "reason": reason_str,
            "is_real_sweep": False,
            "candles_in_sweep": 0
        }