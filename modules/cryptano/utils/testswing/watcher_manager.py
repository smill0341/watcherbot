"""
watcher_manager.py
===================
ТУПОЙ РОУТЕР (Переключатель).
Не принимает решений. Не считает TP/SL. Не фильтрует по R/R.
Только вызывает нужную стратегию из watcher_methods.py и передает ответ дальше.
"""

from modules.cryptano.utils.testswing.watcher_methods import SweepReclaimWatcher, check_volume_reversal, check_pit_climax

STRATEGIES = ("SWEEP_RECLAIM", "VOLUME_REVERSAL", "PIT_CLIMAX")

class WatcherManager:

    def __init__(self, strategy, config=None):
        if strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy: {strategy}. Use one of {STRATEGIES}")
        self.strategy = strategy
        self.burned_levels = set()
        self._watchers = {}

    @staticmethod
    def _level_id(level, trade_type):
        return f"{trade_type}_{level['min']}_{level['max']}"

    def mark_level_burned(self, level, trade_type):
        self.burned_levels.add(self._level_id(level, trade_type))

    def reset_burned_levels(self):
        self.burned_levels.clear()

    # --- Управление объектами для SWEEP_RECLAIM ---
    def get_or_create_watcher(self, level, trade_type):
        level_id = self._level_id(level, trade_type)
        if level_id not in self._watchers:
            self._watchers[level_id] = SweepReclaimWatcher(level['min'], level['max'], trade_type)
        return self._watchers[level_id]

    def clear_dead_watchers(self, current_level_ids):
        to_drop = []
        for level_id, w in self._watchers.items():
            if w.state == "TRIGGERED":
                to_drop.append(level_id)
            elif w.state == "FRESH" and level_id not in current_level_ids:
                to_drop.append(level_id)
        for level_id in to_drop:
            del self._watchers[level_id]

    @staticmethod
    def _deny(reason):
        return {'allow': False, 'reason': reason, 'sl': None, 'tp': None, 'level_id': None, 'extreme_price': None,
                'is_real_sweep': "False", 'overshoot_pct': 0.0, 'candles_in_sweep': 0}

    # =====================================================================
    # ЧИСТЫЙ РОУТИНГ (БЕЗ МАТЕМАТИКИ И ФИЛЬТРОВ)
    # =====================================================================

    def evaluate_sweep_reclaim(self, level, c_open, c_high, c_low, c_close, all_opposite_levels, trade_type):
        # Проверка сжигания уровня (единственный глобальный фильтр, который мы оставили)
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        watcher = self.get_or_create_watcher(level, trade_type)
        
        # Вся математика (TP, SL, R/R) теперь происходит ВНУТРИ watcher.update
        signal = watcher.update(c_open, c_high, c_low, c_close, all_opposite_levels)

        if not signal:
            return self._deny(f"No signal (watcher state: {watcher.state})")

        signal['allow'] = "True"
        signal['level_id'] = level_id
        signal['extreme_price'] = watcher.extreme_price
        return signal

    def evaluate_volume_reversal(self, level, df, trade_type, all_opposite_levels):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        # Вся математика (TP, SL, R/R) теперь происходит ВНУТРИ check_volume_reversal
        signal = check_volume_reversal(df, level, trade_type, all_opposite_levels)

        if not signal:
            return self._deny("No volume reversal pattern in window")

        signal['allow'] = "True"
        signal['level_id'] = level_id
        signal['extreme_price'] = signal['sl']
        signal['is_real_sweep'] = "True"
        return signal

    def evaluate_pit_climax(self, level, df, trade_type, all_opposite_levels):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        # Вся математика (TP, SL, R/R) теперь происходит ВНУТРИ check_pit_climax
        signal = check_pit_climax(df, level, trade_type, all_opposite_levels)

        if not signal:
            return self._deny("No pit climax pattern in window")

        signal['allow'] = "True"
        signal['level_id'] = level_id
        signal['extreme_price'] = signal['sl']
        signal['is_real_sweep'] = "True"
        return signal