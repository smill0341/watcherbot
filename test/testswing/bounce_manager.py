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
    CONFIG = {
        # Если середины двух уровней (одной стороны, LONG/LONG или SHORT/SHORT)
        # отличаются меньше чем на этот % — считаем их одним и тем же уровнем
        # (дрожание пересчёта базы раз в 12ч), не заводим второй вотчер.
        'LEVEL_DEDUP_TOLERANCE_PCT': 2.0,
        # "Кладбище": на сколько % цена должна уйти от мёртвой (DEAD/TRIGGERED)
        # зоны, чтобы система перестала считать новый уровень на этом месте клоном.
        'GRAVEYARD_ESCAPE_PCT': 5.0,
    }

    def __init__(self, parent):
        """parent — это WatcherManager. Общая инфраструктура (_watchers,
        burned_levels, _level_id, _deny) пока остаётся там, чтобы её
        продолжали видеть и остальные стратегии — здесь только читаем её
        через parent, не дублируем."""
        self.parent = parent
        # Счётчик реальных пробоев — источник для "конверсии" (пробитые уровни -> сделки).
        # Считается по событию SWEEP_BOTTOM, не по старому tracked_support (его для
        # BOUNCE больше нет — см. шаг 3 изоляции).
        self.pierced_count = 0
        # "Кладбище" мёртвых зон: [{'trade_type','min','max','escaped'}, ...].
        # Живёт, пока цена не уйдёт от зоны дальше GRAVEYARD_ESCAPE_PCT — до этого
        # момента новый уровень, пересчитанный на этом же месте (дрожание базы),
        # считается клоном, а не честным новым сетапом. См. _find_graveyard_match.
        self.graveyard = []
        self._graveyard_recorded = set()  # level_id, уже занесённые в кладбище

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

    def _update_graveyard(self, c_close):
        """Вызывается раз в свечу. Отпускает мёртвую зону, как только цена
        реально ушла от неё дальше GRAVEYARD_ESCAPE_PCT — с этого момента
        уровень на этом месте больше не считается автоматически клоном."""
        tolerance = self.CONFIG['GRAVEYARD_ESCAPE_PCT'] / 100.0
        for entry in self.graveyard:
            if entry['escaped']:
                continue
            escape_low = entry['min'] * (1 - tolerance)
            escape_high = entry['max'] * (1 + tolerance)
            if c_close < escape_low or c_close > escape_high:
                entry['escaped'] = True

    def _record_death(self, watcher, trade_type, level_id):
        """Заносит вотчер в кладбище один раз, в момент его смерти (DEAD/TRIGGERED)."""
        if level_id in self._graveyard_recorded:
            return
        self._graveyard_recorded.add(level_id)
        self.graveyard.append({
            'trade_type': trade_type,
            'min': watcher.min,
            'max': watcher.max,
            'escaped': False,
        })

    def _find_graveyard_match(self, level, trade_type):
        """'clone'  — совпал с ещё не отпущенной мёртвой зоной, блокировать.
        'reborn' — совпал с уже отпущенной (цена сбегала и вернулась), разрешить.
        None     — совпадений в кладбище нет."""
        tolerance = self.CONFIG['LEVEL_DEDUP_TOLERANCE_PCT'] / 100.0
        lvl_mid = (level['min'] + level['max']) / 2
        matched_escaped = False
        for entry in self.graveyard:
            if entry['trade_type'] != trade_type:
                continue
            entry_mid = (entry['min'] + entry['max']) / 2
            if entry_mid > 0 and abs(lvl_mid - entry_mid) / entry_mid <= tolerance:
                if entry['escaped']:
                    matched_escaped = True
                else:
                    return 'clone'
        return 'reborn' if matched_escaped else None

    def evaluate_bounce_side(self, trade_type, touched_levels, evaluator):
        """
        Перебирает ВСЕ уровни BOUNCE этой стороны (LONG/SHORT), которые нужно
        проверить на этой свече:
          1. уже активные вотчеры (защита от того, что 12-часовое обновление
             CURRENT_SUPPORTS/RESISTANCES выкинет уровень, пока по нему ещё
             идёт сканирование)
          2. плюс новые уровни, которых свеча только что коснулась (touched_levels) —
             НО только если это правда новый уровень, а не дрожание уже
             отслеживаемого (см. LEVEL_DEDUP_TOLERANCE_PCT). Если новый кандидат
             похож на уже живой вотчер — координаты живого вотчера остаются
             замороженными, второй вотчер не заводим.

        evaluator(level_id, level) -> decision dict — вызывающий код сам считает
        контекст (тренд, ATR и т.д.) и зовёт self.evaluate_bounce(...); менеджер
        этого не делает, у него нет доступа к индикаторам симулятора.

        Возвращает список (level_id, level, decision) для ВСЕХ проверенных
        уровней — вызывающий код сам решает, что делать с решениями (войти,
        нарисовать событие на графике).
        """
        levels_to_eval = {}
        active_mids = []  # середины уже живых вотчеров этой стороны — для сверки на дубли
        for level_id, w in list(self._watchers.items()):
            if w.trade_type == trade_type and w.state not in ("DEAD", "TRIGGERED"):
                levels_to_eval[level_id] = {'min': w.min, 'max': w.max,
                                             'score': getattr(w, 'level_score', 0),
                                             'type': getattr(w, 'level_type', 'UNKNOWN')}
                active_mids.append((w.min + w.max) / 2)

        tolerance = self.CONFIG['LEVEL_DEDUP_TOLERANCE_PCT'] / 100.0

        for lvl in touched_levels:
            level_id = self._level_id(lvl, trade_type)
            if level_id in levels_to_eval:
                continue

            lvl_mid = (lvl['min'] + lvl['max']) / 2
            is_duplicate = any(
                mid > 0 and abs(lvl_mid - mid) / mid <= tolerance
                for mid in active_mids
            )
            if is_duplicate:
                # Это дрожание уже отслеживаемого уровня — не заводим второй вотчер,
                # координаты живого остаются замороженными как есть.
                continue

            grave_status = self._find_graveyard_match(lvl, trade_type)
            if grave_status == 'clone':
                # Дрожание пересчёта на месте мёртвой зоны, цена никуда не уходила —
                # не даём вотчеру родиться заново с нуля.
                continue
            if grave_status == 'reborn':
                # Цена реально уходила и вернулась — честный новый сетап на старом
                # месте. Помечаем, чтобы вотчер и лог это знали (тег OD).
                lvl = dict(lvl)
                lvl['_reborn'] = True

            levels_to_eval[level_id] = lvl

        results = []
        for level_id, lvl in levels_to_eval.items():
            decision = evaluator(level_id, lvl)
            results.append((level_id, lvl, decision))
        return results

    def on_levels_refreshed(self, current_supports, current_resistances):
        """Вызывается тестером раз в 12 часов, когда обновилась база уровней
        (CURRENT_SUPPORTS/CURRENT_RESISTANCES). Строит набор ID уровней, которые
        сейчас актуальны, и просит менеджер убрать вотчеры для тех, кого в этом
        наборе больше нет (кроме защищённых состоянием — см. clear_dead_watchers)."""
        current_level_ids = set()
        for s in current_supports:
            current_level_ids.add(self._level_id(s, 'LONG'))
        for r in current_resistances:
            current_level_ids.add(self._level_id(r, 'SHORT'))
        self.parent.clear_dead_watchers(current_level_ids)

    def get_zone_drawing(self, c_close, allow_long=True, allow_short=True):
        """Возвращает (sup_min, sup_max, res_min, res_max) — границы зоны,
        которая СЕЙЧАС в фокусе (последний реально пробитый живой уровень —
        тот же принцип, что определяет право на вход, см. _get_focus_level_id),
        или None там, где живых вотчеров нет / сторона выключена.

        Раньше рисовалась просто ближайшая к цене живая зона — из-за этого
        линия скакала (зигзаг) каждый раз, когда две зоны оказывались на
        похожем расстоянии от цены. Теперь линия висит на одном уровне,
        пока он в фокусе, и переезжает только когда фокус реально сменился
        (новый пробой перехватил приоритет) — так же, как переключаются входы.

        Тестер сам решает, писать ли NaN вместо None — менеджер про numpy/
        pandas ничего не знает."""
        sup_min = sup_max = res_min = res_max = None

        if allow_long:
            focus_id = self._get_focus_level_id('LONG')
            if focus_id is not None:
                w = self._watchers.get(focus_id)
                if w is not None:
                    sup_min, sup_max = w.min, w.max
            else:
                # Ещё никто не пробит — фокуса нет, откатываемся на ближайшую
                # живую зону, просто чтобы было что показать до первого пробоя.
                active_longs = [w for w in self._watchers.values()
                                 if getattr(w, 'trade_type', None) == 'LONG'
                                 and getattr(w, 'state', None) not in ("DEAD", "TRIGGERED")]
                if active_longs:
                    near = min(active_longs, key=lambda w: abs(((w.min + w.max) / 2) - c_close))
                    sup_min, sup_max = near.min, near.max

        if allow_short:
            focus_id = self._get_focus_level_id('SHORT')
            if focus_id is not None:
                w = self._watchers.get(focus_id)
                if w is not None:
                    res_min, res_max = w.min, w.max
            else:
                active_shorts = [w for w in self._watchers.values()
                                  if getattr(w, 'trade_type', None) == 'SHORT'
                                  and getattr(w, 'state', None) not in ("DEAD", "TRIGGERED")]
                if active_shorts:
                    near = min(active_shorts, key=lambda w: abs(((w.min + w.max) / 2) - c_close))
                    res_min, res_max = near.min, near.max

        return sup_min, sup_max, res_min, res_max

    def _get_focus_level_id(self, trade_type):
        """Среди уже пробитых и ещё живых (SCANNING) вотчеров этой стороны —
        какой пробит последним по времени. Только он имеет право на вход в
        этот момент; остальные наблюдаются (сканируются, событие пишется),
        но сделку не открывают, пока сами не станут последним пробитым, или
        пока текущий фокус не умрёт (TRIGGERED/DEAD/RUNAWAY навсегда).

        Возвращает None, если ещё никто из живых вотчеров этой стороны не был
        пробит — тогда ограничение снимается (первый пробой всегда свободен)."""
        candidates = [
            (lid, w) for lid, w in self._watchers.items()
            if getattr(w, 'trade_type', None) == trade_type
            and getattr(w, 'state', None) == "SCANNING"
            and getattr(w, 'pierced_bottom', False)
            and getattr(w, 'last_pierce_time', None) is not None
        ]
        if not candidates:
            return None
        if trade_type == 'LONG':
            # При равном времени пробоя — предпочитаем более глубокий (низкий) уровень
            return max(candidates, key=lambda item: (item[1].last_pierce_time, -item[1].min))[0]
        else:
            return max(candidates, key=lambda item: (item[1].last_pierce_time, item[1].max))[0]

    def process_candle(self, c_low, c_high, c_close, current_supports, current_resistances,
                        df_slice, allow_long=True, allow_short=True):
        """
        Единая точка входа на одну свечу для BOUNCE. Заменяет собой то, что
        раньше было двумя отдельными блоками в test_simulator.py (LONG и SHORT):
        сбор уровней (активные + только что тронутые), вызов evaluate_bounce
        для каждого, сборка решений на вход и событий для отрисовки.

        Тестеру НЕ нужно знать про touched/evaluate_bounce_side/события вотчера —
        он просто зовёт это один раз и слепо исполняет то, что вернулось.

        Возвращает (orders, draw_events):
          orders      — список dict {'trade_type', 'level', 'decision'} с
                        allow=True, готовых к self._try_enter(...) в тестере.
          draw_events — список (имя_колонки, значение) для слепой записи в
                        self.original_df — менеджер сам решил, что произошло
                        и в какую колонку это рисуется, тестер просто копирует.
        """
        self._update_graveyard(c_close)

        orders = []
        draw_events = []

        def _collect_event(level_id, trade_type):
            w = self._watchers.get(level_id)
            if w is None:
                return
            et = getattr(w, 'last_event_type', None)
            if et == "SWEEP_BOTTOM":
                self.pierced_count += 1
                draw_events.append(('bounce_sweep', c_low if trade_type == 'LONG' else c_high))
            elif et == "SCAN":
                draw_events.append(('bounce_scan', c_close))
            elif et in ("GOOD_GREEN", "GOOD_RED"):
                draw_events.append(('bounce_good', c_close))
            elif et == "RUNAWAY":
                draw_events.append(('bounce_release', c_close))

        if allow_long:
            focus_long_id = self._get_focus_level_id('LONG')
            touched_long = [s for s in current_supports if c_low <= s['max']]
            for level_id, lvl, decision in self.evaluate_bounce_side(
                    'LONG', touched_long,
                    lambda lid, l: self.evaluate_bounce(
                        l, df_slice, 'LONG', current_resistances,
                        is_focus=(focus_long_id is None or lid == focus_long_id))):
                _collect_event(level_id, 'LONG')
                if decision.get('allow'):
                    actual_lvl = next((s for s in current_supports if s['min'] == lvl['min'] and s['max'] == lvl['max']), lvl)
                    orders.append({'trade_type': 'LONG', 'level': actual_lvl, 'decision': decision})

        if allow_short:
            focus_short_id = self._get_focus_level_id('SHORT')
            touched_short = [r for r in current_resistances if c_high >= r['min']]
            for level_id, lvl, decision in self.evaluate_bounce_side(
                    'SHORT', touched_short,
                    lambda lid, l: self.evaluate_bounce(
                        l, df_slice, 'SHORT', current_supports,
                        is_focus=(focus_short_id is None or lid == focus_short_id))):
                _collect_event(level_id, 'SHORT')
                if decision.get('allow'):
                    actual_lvl = next((r for r in current_resistances if r['min'] == lvl['min'] and r['max'] == lvl['max']), lvl)
                    orders.append({'trade_type': 'SHORT', 'level': actual_lvl, 'decision': decision})

        return orders, draw_events

    # -------------------------------------------------------------------------
    # BOUNCE (Отбой от макро-уровня)
    # -------------------------------------------------------------------------
    def evaluate_bounce(self, level, df, trade_type, all_opposite_levels, is_focus=True):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = BounceWatcher(level['min'], level['max'], trade_type)
            self._watchers[level_id].level_type = level.get('type', 'UNKNOWN')
            self._watchers[level_id].level_score = level.get('score', 0)
            if level.get('_reborn'):
                self._watchers[level_id].reborn = True
                self._watchers[level_id]._dbg(
                    "🪦 ВОСКРЕС | Эта зона уже была здесь и умерла раньше, "
                    "но цена уходила и вернулась — считаем новым сетапом"
                )
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
            all_opposite_levels, level_score=level.get('score', 0), candle_time=df.index[-1],
            is_focus=is_focus
        )

        if watcher.state in ("DEAD", "TRIGGERED"):
            self._record_death(watcher, trade_type, level_id)

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