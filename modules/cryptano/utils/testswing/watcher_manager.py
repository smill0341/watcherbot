"""
watcher_manager.py
==================
Оркестратор для watcher-ов. 
Он не знает, КАК работают стратегии. Он просто берет нужную функцию 
из watcher_methods.py, вызывает её и возвращает симулятору готовый ответ.
"""

from .watcher_methods import SweepReclaimWatcher, check_volume_reversal, check_pit_climax

STRATEGIES = ["SWEEP_RECLAIM", "VOLUME_REVERSAL", "PIT_CLIMAX"]

class WatcherManager:
    def __init__(self, strategy, config=None):
        if strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy: {strategy}. Use one of {STRATEGIES}")
        self.strategy = strategy
        self.config = config or {}
        self.burned_levels = set()
        self._watchers = {}

    def _level_id(self, level, trade_type):
        return f"{trade_type}_{level['min']}_{level['max']}"

    def _deny(self, reason):
        # Возвращаем полную структуру, чтобы избежать ошибок при распаковке
        return {'allow': "False", 'reason': reason, 'sl': 0.0, 'tp': 0.0, 'level_id': None, 'extreme_price': "0.0",
                'is_real_sweep': "False", 'overshoot_pct': 0.0, 'candles_in_sweep': 0}


    def clear_dead_watchers(self, active_level_ids):
        """Удаляет инстансы стейт-машин для уровней, которые ушли с графика."""
        dead_keys = [k for k in self._watchers.keys() if k not in active_level_ids]
        for k in dead_keys:
            del self._watchers[k]

    # -------------------------------------------------------------------------
    # 1. SWEEP RECLAIM
    # -------------------------------------------------------------------------
    def evaluate_sweep_reclaim(self, level, c_open, c_high, c_low, c_close, all_opposite_levels, trade_type):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = SweepReclaimWatcher(level['min'], level['max'], trade_type)

        watcher = self._watchers[level_id]
        signal = watcher.update(c_open, c_high, c_low, c_close, all_opposite_levels)

        if not signal:
            return self._deny(f"No signal (state: {watcher.state})")
            
        # Защита от ошибки калькулятора
        if 'error' in signal:
            return self._deny(signal['error'])

        signal['allow'] = "True"
        signal['level_id'] = level_id
        signal['extreme_price'] = str(watcher.extreme_price) if watcher.extreme_price is not None else "0.0"
        return signal

    # -------------------------------------------------------------------------
    # 2. VOLUME REVERSAL (SMC)
    # -------------------------------------------------------------------------
    def evaluate_volume_reversal(self, level, df, trade_type, all_opposite_levels):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        # Вызываем метод (настройки он берет сам внутри себя)
        signal = check_volume_reversal(df, level, trade_type, all_opposite_levels)

        if not signal:
            return self._deny("No volume reversal pattern in window")
            
        # Защита от ошибки калькулятора
        if 'error' in signal:
            return self._deny(signal['error'])

        signal['allow'] = "True"
        signal['level_id'] = level_id
        signal['extreme_price'] = str(signal.get('sl', '0.0'))
        signal['is_real_sweep'] = "True"
        return signal

    # -------------------------------------------------------------------------
    # 3. PIT CLIMAX (Wyckoff)
    # -------------------------------------------------------------------------
    def evaluate_pit_climax(self, level, df, trade_type, all_opposite_levels):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        # Вызываем метод (настройки он берет сам внутри себя)
        signal = check_pit_climax(df, level, trade_type, all_opposite_levels)

        if not signal:
            return self._deny("No pit climax pattern in window")
            
        # Защита от ошибки калькулятора
        if 'error' in signal:
            return self._deny(signal['error'])

        signal['allow'] = "True"
        signal['level_id'] = level_id
        signal['extreme_price'] = str(signal.get('sl', '0.0'))
        signal['is_real_sweep'] = "True"
        return signal