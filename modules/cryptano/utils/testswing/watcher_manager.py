"""
watcher_manager.py
===================
Единственная точка правды для решения "входить или нет".

Берёт сигнал от одного из методов в watcher_methods.py, применяет фильтры
качества уровня (score, zone gap, level burn), считает SL/TP.
Не знает про биржу, Telegram, бэктест-движок — только про логику входа.

Использование:
    manager = WatcherManager(strategy="SWEEP_RECLAIM", config={...})

    # для SWEEP_RECLAIM (стейт-машина, нужен persistent watcher на уровень):
    watcher_obj = manager.get_or_create_watcher(level, 'LONG')
    decision = manager.evaluate_sweep_reclaim(level, watcher_obj, c_open, c_high, c_low, c_close,
                                               all_resistances, 'LONG')

    # для CHOCH (стейтлесс, нужен df-срез):
    decision = manager.evaluate_choch(level, df_slice, 'LONG', all_resistances)

    if decision['allow']:
        sl, tp = decision['sl'], decision['tp']
"""

from modules.cryptano.utils.testswing.watcher_methods import SweepReclaimWatcher, check_choch, check_volume_reversal

STRATEGIES = ("SWEEP_RECLAIM", "CHOCH", "VOLUME_REVERSAL")

class WatcherManager:

    def __init__(self, strategy, config):
        """
        STRATEGIES = ('SWEEP_RECLAIM', 'CHOCH', 'VOLUME_REVERSAL')
        config: словарь настроек, например:
            {
                'MIN_SCORE': 5,
                'USE_ZONE_GAP': True,
                'MIN_ZONE_GAP_PCT': 2.0,
                'USE_LEVEL_BURN': True,
                'TAKE_PROFIT': 8.0,
                'SL_BUFFER': 1.0,
                'CHOCH_LOOKBACK': 15,
                'CHOCH_ANTI_KNIFE_ATR_MULT': 0.8,
                'RR_RATIO': 3.0,          # только для CHOCH
                'USE_RR_FILTER': True,    # только для CHOCH
            }
        """
        if strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy: {strategy}. Use one of {STRATEGIES}")
        self.strategy = strategy
        self.config = config
        self.burned_levels = set()
        self._watchers = {}  # только для SWEEP_RECLAIM: level_id -> SweepReclaimWatcher

    # =====================================================================
    # ОБЩИЕ ФИЛЬТРЫ КАЧЕСТВА (одинаковы для обеих стратегий)
    # =====================================================================
    def _passes_quality_filters(self, level, trade_type, all_opposite_levels):
        """
        Проверяет score, zone gap, level burn. Не знает про сигнал watcher —
        это быстрая отсеялка ДО того, как тратить время на анализ паттерна.

        all_opposite_levels: для LONG -> список resistances, для SHORT -> supports
        """
        if level.get('score', 0) < self.config.get('MIN_SCORE', 0):
            return False, f"Score {level.get('score')} < MIN_SCORE"

        level_id = self._level_id(level, trade_type)
        if self.config.get('USE_LEVEL_BURN', True) and level_id in self.burned_levels:
            return False, "Level already burned"

        if self.config.get('USE_ZONE_GAP', True):
            if trade_type == 'LONG':
                closest = min(
                    [r['min'] for r in all_opposite_levels if r['min'] > level['max']],
                    default=None
                )
                if closest:
                    gap_pct = ((closest - level['max']) / level['max']) * 100
                    if gap_pct < self.config.get('MIN_ZONE_GAP_PCT', 2.0):
                        return False, f"Gap {gap_pct:.2f}% too small"
            else:
                closest = max(
                    [s['max'] for s in all_opposite_levels if s['max'] < level['min']],
                    default=None
                )
                if closest:
                    gap_pct = ((level['min'] - closest) / closest) * 100
                    if gap_pct < self.config.get('MIN_ZONE_GAP_PCT', 2.0):
                        return False, f"Gap {gap_pct:.2f}% too small"

        return True, None

    @staticmethod
    def _level_id(level, trade_type):
        return f"{trade_type}_{level['min']}_{level['max']}"

    def mark_level_burned(self, level, trade_type):
        self.burned_levels.add(self._level_id(level, trade_type))

    def reset_burned_levels(self):
        self.burned_levels.clear()

    # =====================================================================
    # SWEEP_RECLAIM: нужен persistent watcher-объект на каждый уровень
    # =====================================================================
    def get_or_create_watcher(self, level, trade_type):
        level_id = self._level_id(level, trade_type)
        if level_id not in self._watchers:
            self._watchers[level_id] = SweepReclaimWatcher(
                level['min'], level['max'], trade_type,
                allow_bounce=self.config.get('ALLOW_BOUNCE', True),
                allow_sweep=self.config.get('ALLOW_SWEEP', True),
            )
        return self._watchers[level_id]

    def drop_watcher(self, level, trade_type):
        level_id = self._level_id(level, trade_type)
        self._watchers.pop(level_id, None)

    def clear_dead_watchers(self, current_level_ids):
        """
        Чистит watcher'ы, которые либо TRIGGERED (уже отработали),
        либо относятся к уровню, которого больше нет в текущем списке
        И при этом сами в состоянии FRESH (ничего не отслеживают).
        Watcher'ы в процессе (BELOW/ABOVE) сохраняются независимо от
        текущего списка уровней — им нужно дожить свой цикл.
        """
        to_drop = []
        for level_id, w in self._watchers.items():
            if w.state == "TRIGGERED":
                to_drop.append(level_id)
            elif w.state == "FRESH" and level_id not in current_level_ids:
                to_drop.append(level_id)
        for level_id in to_drop:
            del self._watchers[level_id]

    def _calc_structural_target(self, entry_price, sl, trade_type, all_opposite_levels):
        """
        Считает TP в одном из двух режимов (config['TP_MODE']):
          - 'structural' (по умолчанию): TP = следующий противоположный уровень,
            минус буфер. Если уровня нет - fallback на fixed %.
          - 'fixed_pct': TP = entry +/- фиксированный % (config['FIXED_TP_PCT']),
            без привязки к уровням вообще - твой исходный вариант для сравнения.

        В обоих режимах применяется фильтр MIN_RR (риск/прибыль).

        Returns:
            (tp, allow, reason)
        """
        min_rr = self.config.get('MIN_RR', 1.5)
        tp_mode = self.config.get('TP_MODE', 'structural')

        risk = abs(entry_price - sl)
        if risk <= 0:
            return None, False, "Invalid risk (SL == entry)"

        if tp_mode == 'fixed_pct':
            fixed_pct = self.config.get('FIXED_TP_PCT', self.config.get('TAKE_PROFIT', 8.0))
            if trade_type == 'LONG':
                tp = entry_price * (1 + fixed_pct / 100)
                reward = tp - entry_price
            else:
                tp = entry_price * (1 - fixed_pct / 100)
                reward = entry_price - tp
            rr = reward / risk if risk > 0 else 0
            if rr < min_rr:
                return None, False, f"R/R (fixed {fixed_pct}%) {rr:.2f} < {min_rr}"
            return tp, True, f"Fixed TP {fixed_pct}%, R/R={rr:.2f}"

        # --- structural (по умолчанию) ---
        tp_buffer_pct = self.config.get('TP_BUFFER_PCT', 0.3)  # не долетаем до самого уровня
        fallback_tp_pct = self.config.get('TAKE_PROFIT', 8.0)

        if trade_type == 'LONG':
            candidates = [lvl['min'] for lvl in all_opposite_levels if lvl['min'] > entry_price]
            structural_level = min(candidates) if candidates else None
            if structural_level is not None:
                tp = structural_level * (1 - tp_buffer_pct / 100)
            else:
                tp = entry_price * (1 + fallback_tp_pct / 100)
            reward = tp - entry_price
        else:
            candidates = [lvl['max'] for lvl in all_opposite_levels if lvl['max'] < entry_price]
            structural_level = max(candidates) if candidates else None
            if structural_level is not None:
                tp = structural_level * (1 + tp_buffer_pct / 100)
            else:
                tp = entry_price * (1 - fallback_tp_pct / 100)
            reward = entry_price - tp

        rr = reward / risk if risk > 0 else 0
        if rr < min_rr:
            return None, False, f"R/R to structure {rr:.2f} < {min_rr} (target too close)"

        return tp, True, f"Structural TP, R/R={rr:.2f}"

    def evaluate_sweep_reclaim(self, level, c_open, c_high, c_low, c_close,
                                all_opposite_levels, trade_type):
        """
        Прогоняет один уровень через SweepReclaimWatcher + фильтры качества.

        Returns:
            {'allow': bool, 'reason': str, 'sl': float|None, 'tp': float|None,
             'level_id': str, 'extreme_price': float|None}
        """
        ok, reason = self._passes_quality_filters(level, trade_type, all_opposite_levels)
        if not ok:
            return self._deny(reason)

        watcher = self.get_or_create_watcher(level, trade_type)
        signal = watcher.update(c_open, c_high, c_low, c_close)

        if signal is None:
            return self._deny(f"No signal (watcher state: {watcher.state})")

        current_price = c_close
        sl_buffer_mult = 1 - (self.config.get('SL_BUFFER', 1.0) / 100)
        
        if trade_type == 'LONG':
            # SL берётся от watcher, но применяется буфер (отодвигаем вниз)
            sl = signal['sl'] * sl_buffer_mult
            sl = sl * (1 - 0.002)  # проскальзывание
        else:
            sl_buffer_mult = 1 + (self.config.get('SL_BUFFER', 1.0) / 100)
            sl = signal['sl'] * sl_buffer_mult
            sl = sl * (1 + 0.002)

        tp, tp_ok, tp_reason = self._calc_structural_target(current_price, sl, trade_type, all_opposite_levels)
        if not tp_ok:
            return self._deny(tp_reason)

        return {
            'allow': True,
            'reason': f"{signal['reason']} | {tp_reason}",
            'sl': sl,
            'tp': tp,
            'level_id': self._level_id(level, trade_type),
            'extreme_price': watcher.extreme_price,
            'is_real_sweep': signal.get('is_real_sweep', False),
            'overshoot_pct': signal.get('overshoot_pct', 0.0),
            'candles_in_sweep': signal.get('candles_in_sweep', 0),
        }

    # =====================================================================
    # CHOCH: стейтлесс, нужен df-срез (растущее окно истории)
    # =====================================================================
    def evaluate_choch(self, level, df, trade_type, all_opposite_levels):
        """
        Прогоняет один уровень через check_choch + фильтры качества + R/R.

        df: DataFrame с колонками open/high/low/close/volume/atr,
            срез ДО текущей свечи включительно (последняя строка = текущая свеча)

        Returns: тот же формат, что evaluate_sweep_reclaim
        """
        ok, reason = self._passes_quality_filters(level, trade_type, all_opposite_levels)
        if not ok:
            return self._deny(reason)

        signal = check_choch(
            df, level, trade_type,
            lookback=self.config.get('CHOCH_LOOKBACK', 15),
            anti_knife_atr_mult=self.config.get('CHOCH_ANTI_KNIFE_ATR_MULT', 0.8),
        )

        if signal is None:
            return self._deny("No CHoCH signal")

        current_price = float(df['close'].iloc[-1])
        sl_buffer = self.config.get('SL_BUFFER', 0.5)

        if trade_type == 'LONG':
            sl = level['min'] * (1 - sl_buffer / 100)
        else:
            sl = level['max'] * (1 + sl_buffer / 100)

        tp, tp_ok, tp_reason = self._calc_structural_target(current_price, sl, trade_type, all_opposite_levels)
        if not tp_ok:
            return self._deny(tp_reason)

        return {
            'allow': True,
            'reason': f"{signal['reason']} | {tp_reason}",
            'sl': sl,
            'tp': tp,
            'level_id': self._level_id(level, trade_type),
            'extreme_price': None,
        }
        
    def evaluate_volume_reversal(self, level, df, trade_type, all_opposite_levels):
        ok, reason = self._passes_quality_filters(level, trade_type, all_opposite_levels)
        if not ok:
            return self._deny(reason)

        vol_mult = self.config.get('VOLUME_MULTIPLIER', 2.0)
        signal = check_volume_reversal(df, level, trade_type, vol_mult=vol_mult, window=10)

        if signal is None:
            return self._deny("No volume reversal pattern in window")

        current_price = float(df['close'].iloc[-1])
        tp_pct = self.config.get('TAKE_PROFIT', 10.0)

        sl = signal['sl']
        if trade_type == 'LONG':
            tp = current_price * (1 + tp_pct / 100)
            sl = sl * 0.998 # Буфер 0.2% под снайперский минимум
        else:
            tp = current_price * (1 - tp_pct / 100)
            sl = sl * 1.002

        return {
            'allow': True,
            'reason': signal['reason'],
            'sl': sl,
            'tp': tp,
            'level_id': self._level_id(level, trade_type),
            'extreme_price': sl,
        }    

    @staticmethod
    def _deny(reason):
        return {'allow': False, 'reason': reason, 'sl': None, 'tp': None, 'level_id': None, 'extreme_price': None,
                'is_real_sweep': False, 'overshoot_pct': 0.0, 'candles_in_sweep': 0}
