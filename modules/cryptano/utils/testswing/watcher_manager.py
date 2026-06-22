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

from modules.cryptano.utils.testswing.watcher_methods import SweepReclaimWatcher, check_choch


STRATEGIES = ("SWEEP_RECLAIM", "CHOCH")


class WatcherManager:

    def __init__(self, strategy, config):
        """
        strategy: "SWEEP_RECLAIM" или "CHOCH"
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
            self._watchers[level_id] = SweepReclaimWatcher(level['min'], level['max'], trade_type)
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
        if trade_type == 'LONG':
            sl = signal['sl'] if signal['sl'] is not None else level['min'] * (1 - self.config.get('SL_BUFFER', 1.0) / 100)
            sl = sl * (1 - 0.002) if signal['sl'] is not None else sl
            tp = current_price * (1 + self.config.get('TAKE_PROFIT', 8.0) / 100)
        else:
            sl = signal['sl'] if signal['sl'] is not None else level['max'] * (1 + self.config.get('SL_BUFFER', 1.0) / 100)
            sl = sl * (1 + 0.002) if signal['sl'] is not None else sl
            tp = current_price * (1 - self.config.get('TAKE_PROFIT', 8.0) / 100)

        return {
            'allow': True,
            'reason': signal['reason'],
            'sl': sl,
            'tp': tp,
            'level_id': self._level_id(level, trade_type),
            'extreme_price': watcher.extreme_price,
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
        tp_pct = self.config.get('TAKE_PROFIT', 10.0)

        if trade_type == 'LONG':
            sl = level['min'] * (1 - sl_buffer / 100)
            tp = current_price * (1 + tp_pct / 100)
            risk = current_price - sl
            reward = tp - current_price
        else:
            sl = level['max'] * (1 + sl_buffer / 100)
            tp = current_price * (1 - tp_pct / 100)
            risk = sl - current_price
            reward = current_price - tp

        if self.config.get('USE_RR_FILTER', True):
            rr_ratio = self.config.get('RR_RATIO', 3.0)
            if risk <= 0 or (reward / risk) < rr_ratio:
                rr_val = (reward / risk) if risk > 0 else 0
                return self._deny(f"R/R {rr_val:.2f} < {rr_ratio}")

        return {
            'allow': True,
            'reason': signal['reason'],
            'sl': sl,
            'tp': tp,
            'level_id': self._level_id(level, trade_type),
            'extreme_price': None,
        }

    @staticmethod
    def _deny(reason):
        return {'allow': False, 'reason': reason, 'sl': None, 'tp': None, 'level_id': None, 'extreme_price': None}
