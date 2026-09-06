class BounceParent:
    """Минимальный parent для BounceManager — свой отдельный реестр,
    не пересекается с VBottomManager (иначе уровни BOUNCE и V_BOTTOM
    столкнутся в одном словаре)."""

    def __init__(self):
        self._watchers = {}
        self.burned_levels = set()

    def _level_id(self, level, trade_type):
        return f"BC_{trade_type}_{level['min']}_{level['max']}"

    def _deny(self, reason):
        return {'allow': False, 'reason': reason, 'sl': 0.0, 'tp': 0.0,
                'level_id': None, 'entry_price': None, 'history_log': ''}

    def clear_dead_watchers(self, active_level_ids):
        dead_keys = [k for k, w in self._watchers.items()
                     if k not in active_level_ids
                     and getattr(w, 'state', None) in ("TRIGGERED", "DEAD")]
        return {k: self._watchers.pop(k) for k in dead_keys}