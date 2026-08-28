"""
bounce_manager.py
==================
BOUNCE-специфичная часть менеджера, вынесенная в отдельный класс.

Шаг 1 переезда: код перенесён 1-в-1 из watcher_manager.py, ни одна строка
логики не изменена — только физическое место, где она живёт. WatcherManager
по-прежнему предоставляет наружу те же самые методы (evaluate_bounce,
evaluate_bounce_side, has_active_bounce_watchers) как тонкие обёртки —
test_simulator.py ничего не должен заметить.
"""

from .bounce_watcher import BounceWatcher


class BounceManager:
    def __init__(self, parent):
        """parent — это WatcherManager. Общая инфраструктура (_watchers,
        burned_levels, _level_id, _deny) пока остаётся там, чтобы её
        продолжали видеть и остальные стратегии — здесь только читаем её
        через parent, не дублируем."""
        self.parent = parent

    @property
    def _watchers(self):
        return self.parent._watchers

    @property
    def burned_levels(self):
        return self.parent.burned_levels

    def _level_id(self, level, trade_type):
        return self.parent._level_id(level, trade_type)

    def _deny(self, reason):
        return self.parent._deny(reason)

    def has_active_bounce_watchers(self, trade_type=None):
        """True, если есть хотя бы один живой (не DEAD/TRIGGERED) BOUNCE-вотчер.
        Нужно тестеру, чтобы не пропускать свечу, пока по уровню ещё идёт сканирование,
        даже если CURRENT_SUPPORTS/RESISTANCES на этот момент пусты."""
        for w in self._watchers.values():
            if trade_type is not None and getattr(w, 'trade_type', None) != trade_type:
                continue
            if getattr(w, 'state', None) not in ("DEAD", "TRIGGERED"):
                return True
        return False

    def evaluate_bounce_side(self, trade_type, touched_levels, evaluator):
        """
        Перебирает ВСЕ уровни BOUNCE этой стороны (LONG/SHORT), которые нужно
        проверить на этой свече:
          1. уже активные вотчеры (защита от того, что 12-часовое обновление
             CURRENT_SUPPORTS/RESISTANCES выкинет уровень, пока по нему ещё
             идёт сканирование)
          2. плюс новые уровни, которых свеча только что коснулась (touched_levels)

        evaluator(level_id, level) -> decision dict — вызывающий код сам считает
        контекст (тренд, ATR и т.д.) и зовёт self.evaluate_bounce(...); менеджер
        этого не делает, у него нет доступа к индикаторам симулятора.

        Возвращает список (level_id, level, decision) для ВСЕХ проверенных
        уровней — вызывающий код сам решает, что делать с решениями (войти,
        нарисовать событие на графике).
        """
        levels_to_eval = {}
        for level_id, w in list(self._watchers.items()):
            if w.trade_type == trade_type and w.state not in ("DEAD", "TRIGGERED"):
                levels_to_eval[level_id] = {'min': w.min, 'max': w.max, 'score': 5.0, 'type': 'BOUNCE_ACTIVE'}

        for lvl in touched_levels:
            level_id = self._level_id(lvl, trade_type)
            if level_id not in levels_to_eval:
                levels_to_eval[level_id] = lvl

        results = []
        for level_id, lvl in levels_to_eval.items():
            decision = evaluator(level_id, lvl)
            results.append((level_id, lvl, decision))
        return results

    # -------------------------------------------------------------------------
    # BOUNCE (Отбой от макро-уровня)
    # -------------------------------------------------------------------------
    def evaluate_bounce(self, level, df, trade_type, all_opposite_levels):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = BounceWatcher(level['min'], level['max'], trade_type)
            self._watchers[level_id].level_type = level.get('type', 'UNKNOWN')
        watcher = self._watchers[level_id]

        if len(df) < 52:
            return self._deny("Not enough data")

        # Считаем 90-й перцентиль (отсекаем мусор и ночной флэт)
        baseline_vol = float(df['volume'].iloc[-52:-2].quantile(0.9))

        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        signal = watcher.update(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol,
            all_opposite_levels, level_score=level.get('score', 0), candle_time=df.index[-1]
        )

        if not signal:
            return self._deny(f"No signal (state: {watcher.state})")

        if 'error' in signal:
            return self._deny(signal['error'])

        # Уровень НЕ сжигаем! Менеджер больше не блочит уровень.
        # Вотчер сам перейдет в DEAD, когда исчерпает лимит сделок в своем конфиге.
        # self.burned_levels.add(level_id)

        signal['allow'] = True
        signal['level_id'] = level_id
        signal['extreme_price'] = "0.0"
        signal['is_real_sweep'] = False
        signal['overshoot_pct'] = 0.0
        signal['candles_in_sweep'] = 0

        return signal