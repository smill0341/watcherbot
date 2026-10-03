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
        """Архивирует все TRIGGERED/DEAD вотчеры — безусловно, каждый скан.

        Раньше тут ещё стояло условие "k not in active_level_ids" (не архивить,
        пока зона ещё считается активной). Для BOUNCE это была ловушка: level_id
        строится от самой зоны (min/max), один и тот же для CLIMAX и MIRROR —
        active_level_ids на каждом скане кладёт ОБА id для любой зоны, которая
        всё ещё актуальна хоть для какого-то режима (см. background_tasks.py,
        bc_active_level_ids). Если, скажем, MIRROR уже TRIGGERED (сделка
        случилась, "уровень закрыт насовсем"), а CLIMAX на той же зоне ещё жив
        и продолжает её трогать — зона никогда не "выпадает" из active_level_ids,
        и TRIGGERED-вотчер MIRROR застревал в памяти навсегда, никогда не попадая
        в watcher_history.json (пропадал из "в работе", но и не появлялся в
        "ПОСЛЕДНИЙ СКАН" — просто исчезал бесследно).

        Вотчер в состоянии TRIGGERED/DEAD по определению больше никогда ничего
        не сделает — архивировать его можно и нужно сразу же, независимо от
        того, что там с зоной у его соседей."""
        dead_keys = [k for k, w in self._watchers.items()
                     if getattr(w, 'state', None) in ("TRIGGERED", "DEAD")]
        return {k: self._watchers.pop(k) for k in dead_keys}