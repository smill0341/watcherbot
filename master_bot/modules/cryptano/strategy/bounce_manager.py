"""
bounce_manager.py
==================
BOUNCE-специфичная часть менеджера, вынесенная в отдельный класс.
"""

import os
import json
import datetime
import pandas as pd

from .bounce_watcher import BounceWatcher

# Два независимых "режима" SHORT, работающих ОДНОВРЕМЕННО на одну и ту же
# resistance-зону: CLIMAX (SHORT_CLIMAX_MODE=True, 3 закрытия подряд перед
# входом) и MIRROR (зеркально LONG, вход от первого касания). Каждый вотчер
# получает свою СОБСТВЕННУЮ копию CONFIG (см. BounceWatcher.__init__) —
# поэтому оба семейства не мешают друг другу, даже запущенные в одном
# процессе. LONG эту раздвоенность не затрагивает вообще (mode=None).
SHORT_MODES = ('CLIMAX', 'MIRROR')


class BounceManager:
    CONFIG = {
        # Если середины двух уровней (одной стороны, LONG/LONG или SHORT/SHORT)
        # отличаются меньше чем на этот % — считаем их одним и тем же уровнем
        # (дрожание пересчёта базы раз в 12ч), не заводим второй вотчер.
        'LEVEL_DEDUP_TOLERANCE_PCT': 4.0,
        # "Кладбище": на сколько % цена должна уйти от мёртвой (DEAD/TRIGGERED)
        # зоны, чтобы система перестала считать новый уровень на этом месте клоном.
        'GRAVEYARD_ESCAPE_PCT': 5.0,
    }

    def __init__(self, parent, log_dir_override=None):
        """parent — это WatcherManager. Общая инфраструктура (_watchers,
        burned_levels, _level_id, _deny) пока остаётся там, чтобы её
        продолжали видеть и остальные стратегии — здесь только читаем её
        через parent, не дублируем.

        log_dir_override — своя папка логов для ВСЕХ вотчеров, созданных
        через этот менеджер (см. BounceWatcher.log_dir_override). None у
        боевого bounce_mgr (пишет в стандартный bounce_logs/), задаётся
        только симулятором (modules/cryptano/simulator/) — свой изолированный
        BounceManager(BounceParent(), log_dir_override=...) на каждый прогон."""
        self.parent = parent
        self.log_dir_override = log_dir_override
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
        # Сделки по уровню — ВНЕ вотчера: level_id -> [unix-время свечи входа, ...].
        # Лимит MAX_TRADES_PER_LEVEL (BounceWatcher.CONFIG) проверяется ПО ЭТОМУ
        # списку в evaluate_bounce. Раньше счётчик жил внутри вотчера и
        # обнулялся: рескан (reset_for_rescan на каждой новой "жизни"),
        # удаление TRIGGERED-вотчера с последующим созданием нового на той же
        # зоне. Итог — "1 сделка на уровень" на деле была "1 сделка на жизнь
        # вотчера", и один уровень торговался снова и снова. Персистится в
        # bounce_state.json. Ключ — тот же level_id, что у вотчера.
        self.level_trades = {}
        # Время (unix-секунды) последней СВЕЧИ, которую BOUNCE реально обработал,
        # отдельно по каждой монете. Персистится (см. to_state_dict/load_state) —
        # нужно watcher_plan.py::check_bounce(), чтобы после рестарта бота
        # "догнать" (реплеить) все свечи, пропущенные за время простоя, а не
        # видеть только текущую и терять/сдвигать события во времени.
        self.last_processed_time = {}

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

    def _update_graveyard(self, c_close, coin="UNKNOWN"):
        """Вызывается раз в свечу. Отпускает мёртвую зону, как только цена
        реально ушла от неё дальше GRAVEYARD_ESCAPE_PCT — с этого момента
        уровень на этом месте больше не считается автоматически клоном.
        Фильтр по coin обязателен — без него c_close монеты A сравнивался
        бы с мёртвыми зонами вообще всех монет (кладбище общее на весь бот),
        давая случайные ложные escaped на чужих ценах."""
        tolerance = self.CONFIG['GRAVEYARD_ESCAPE_PCT'] / 100.0
        for entry in self.graveyard:
            if entry.get('coin') != coin:
                continue
            if entry['escaped']:
                continue
            escape_low = entry['min'] * (1 - tolerance)
            escape_high = entry['max'] * (1 + tolerance)
            if c_close < escape_low or c_close > escape_high:
                entry['escaped'] = True

    def _record_death(self, watcher, trade_type, level_id):
        """Заносит смерть вотчера (DEAD/TRIGGERED) в кладбище — ОДИН раз на
        каждую смерть.

        РАНЬШЕ: запись делалась один раз на level_id за всё время
        (_graveyard_recorded) — если на той же зоне вотчер умирал ВТОРОЙ раз
        (после "воскрешения"), кладбище об этом не узнавало, старая запись
        оставалась "цена уже ушла", и вотчер мог тут же родиться снова.
        ТЕПЕРЬ: "уже записано" — флаг на самом вотчере (_death_recorded),
        сбрасывается в reset_for_rescan (новая жизнь того же объекта) и
        отсутствует у нового объекта. Повторная смерть на той же зоне
        ОБНОВЛЯЕТ существующую запись: escaped=False (защита снова работает),
        died_at — время этой смерти."""
        if getattr(watcher, '_death_recorded', False):
            return
        watcher._death_recorded = True
        self._graveyard_recorded.add(level_id)

        t = getattr(watcher, '_last_time', None)
        try:
            if t is not None and getattr(t, 'tzinfo', None) is not None:
                died_at = t.tz_convert('UTC').tz_localize(None).isoformat()
            elif t is not None:
                died_at = t.isoformat()
            else:
                died_at = None
        except Exception:
            died_at = None
        if died_at is None:
            died_at = datetime.datetime.utcnow().isoformat()

        coin = getattr(watcher, 'coin', None)
        mode = getattr(watcher, 'mode', None)
        for entry in self.graveyard:
            if (entry.get('coin') == coin and entry.get('trade_type') == trade_type
                    and entry.get('mode') == mode
                    and abs(float(entry['min']) - float(watcher.min)) < 1e-12
                    and abs(float(entry['max']) - float(watcher.max)) < 1e-12):
                entry['escaped'] = False
                entry['died_at'] = died_at
                entry['level_id'] = level_id
                return
        self.graveyard.append({
            'coin': coin,
            'trade_type': trade_type,
            'mode': mode,  # CLIMAX/MIRROR/None — не путать семьи между собой
            'min': watcher.min,
            'max': watcher.max,
            'escaped': False,
            'died_at': died_at,
            'level_id': level_id,
        })

    def prune_graveyard(self, coin, current_zones, max_age_days=14):
        """Кладбище само по себе никогда не забывалось — росло бесконечно.
        Теперь запись монеты удаляется, если:
          1. её зоны больше НЕТ среди текущих уровней монеты (macro + custom)
             — уровня нет, помнить о нём незачем;
          2. цена от неё уже ушла (escaped) и смерть была дольше
             max_age_days назад (столько же, сколько смотрит рескан).
        Записи, которые ещё блокируют (escaped=False) и чья зона жива, не
        трогаются. Вызывать раз на монету за скан (см. watcher_plan.py::
        check_bounce)."""
        tolerance = self.CONFIG['LEVEL_DEDUP_TOLERANCE_PCT'] / 100.0
        mids = [(z['min'] + z['max']) / 2 for z in (current_zones or []) if 'min' in z and 'max' in z]
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)
        kept, removed_ids = [], set()
        for entry in self.graveyard:
            if entry.get('coin') != coin:
                kept.append(entry)
                continue
            e_mid = (entry['min'] + entry['max']) / 2
            zone_alive = any(m > 0 and abs(e_mid - m) / m <= tolerance for m in mids)
            too_old = False
            if entry.get('escaped'):
                try:
                    too_old = datetime.datetime.fromisoformat(str(entry.get('died_at'))) < cutoff
                except Exception:
                    too_old = True  # старые записи без даты смерти — считаем старыми
            if zone_alive and not too_old:
                kept.append(entry)
            elif entry.get('level_id'):
                removed_ids.add(entry['level_id'])
        self.graveyard = kept
        self._graveyard_recorded -= removed_ids
        return len(removed_ids)

    def _find_graveyard_match(self, level, trade_type, mode=None, coin="UNKNOWN"):
        """'clone'  — совпал с ещё не отпущенной мёртвой зоной, блокировать.
        'reborn' — совпал с уже отпущенной (цена сбегала и вернулась), разрешить.
        None     — совпадений в кладбище нет.
        Фильтр по coin обязателен — без него новый уровень монеты A мог
        ложно блокироваться как "клон" мёртвой зоны совсем другой монеты B,
        если их цены случайно оказались в одном диапазоне (частое дело для
        мелких альткоинов)."""
        tolerance = self.CONFIG['LEVEL_DEDUP_TOLERANCE_PCT'] / 100.0
        lvl_mid = (level['min'] + level['max']) / 2
        matched_escaped = False
        for entry in self.graveyard:
            if entry.get('coin') != coin:
                continue
            if entry['trade_type'] != trade_type or entry.get('mode') != mode:
                continue
            entry_mid = (entry['min'] + entry['max']) / 2
            if entry_mid > 0 and abs(lvl_mid - entry_mid) / entry_mid <= tolerance:
                if entry['escaped']:
                    matched_escaped = True
                else:
                    return 'clone'
        return 'reborn' if matched_escaped else None

    def evaluate_bounce_side(self, trade_type, touched_levels, evaluator, mode=None, coin="UNKNOWN"):
        """
        Перебирает ВСЕ уровни BOUNCE этой стороны (LONG/SHORT) И ЭТОГО mode
        (CLIMAX/MIRROR/None) ЭТОЙ КОНКРЕТНОЙ МОНЕТЫ, которые нужно проверить
        на этой свече:
          1. уже активные вотчеры (защита от того, что 12-часовое обновление
             CURRENT_SUPPORTS/RESISTANCES выкинет уровень, пока по нему ещё
             идёт сканирование)
          2. плюс новые уровни, которых свеча только что коснулась (touched_levels) —
             НО только если это правда новый уровень, а не дрожание уже
             отслеживаемого (см. LEVEL_DEDUP_TOLERANCE_PCT). Если новый кандидат
             похож на уже живой вотчер ЭТОГО ЖЕ mode — координаты живого вотчера
             остаются замороженными, второй вотчер не заводим. Вотчеры ДРУГОГО
             mode на той же зоне это не видят и не блокируют — climax и mirror
             живут полностью независимо, каждый в своём собственном пространстве
             дублей (см. mode в level_id и в фильтре active_mids ниже).

        КРИТИЧНО (найденный баг): self._watchers — ОДИН общий реестр на ВЕСЬ
        бот, не один на монету, как было в testswing/симуляторе. Без фильтра
        по coin здесь watcher.update() монеты A вызывался бы с ценой свечи
        монеты B на каждом скане монеты B — event_log монеты A заполнялся
        чужими ценами (RUNAWAY/SWEEP_BOTTOM с ценами десятков других монет
        в одном вотчере). coin обязателен для корректной работы в бою.

        evaluator(level_id, level) -> decision dict — вызывающий код сам считает
        контекст (тренд, ATR и т.д.) и зовёт self.evaluate_bounce(...); менеджер
        этого не делает, у него нет доступа к индикаторам симулятора.

        Возвращает список (level_id, level, decision) для ВСЕХ проверенных
        уровней — вызывающий код сам решает, что делать с решениями (войти,
        нарисовать событие на графике).
        """
        levels_to_eval = {}
        active_mids = []  # середины уже живых вотчеров этой стороны+mode — для сверки на дубли
        for level_id, w in list(self._watchers.items()):
            if getattr(w, 'coin', None) != coin:
                continue
            if w.trade_type == trade_type and getattr(w, 'mode', None) == mode and w.state not in ("DEAD", "TRIGGERED"):
                levels_to_eval[level_id] = {'min': w.min, 'max': w.max,
                                             'score': getattr(w, 'level_score', 0),
                                             'type': getattr(w, 'level_type', 'UNKNOWN')}
                active_mids.append((w.min + w.max) / 2)

        tolerance = self.CONFIG['LEVEL_DEDUP_TOLERANCE_PCT'] / 100.0

        for lvl in touched_levels:
            level_id = self._level_id(lvl, trade_type)
            if mode:
                level_id = f"{level_id}__{mode}"
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

            grave_status = self._find_graveyard_match(lvl, trade_type, mode=mode, coin=coin)
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
        наборе больше нет (кроме защищённых состоянием — см. clear_dead_watchers).
        Для SHORT добавляем ОБА варианта id (__CLIMAX и __MIRROR) — иначе оба
        семейства вотчеров выглядели бы для этой проверки как 'уже неактуальные'
        и удалялись бы, хотя зона по факту всё ещё в CURRENT_RESISTANCES."""
        current_level_ids = set()
        for s in current_supports:
            current_level_ids.add(self._level_id(s, 'LONG'))
        for r in current_resistances:
            base_id = self._level_id(r, 'SHORT')
            for m in SHORT_MODES:
                current_level_ids.add(f"{base_id}__{m}")
        self.parent.clear_dead_watchers(current_level_ids)

    def _get_focus_level_id(self, trade_type, mode=None, coin="UNKNOWN"):
        """Единый центр принятия решений по фокусу — отдельно для каждой
        комбинации trade_type+mode, И ОБЯЗАТЕЛЬНО в пределах одной монеты
        (см. предупреждение в evaluate_bounce_side выше — тот же класс
        бага: без фильтра по coin вотчер монеты A мог получить фокус или
        отказ в фокусе из-за вотчера совершенно другой монеты B). CLIMAX
        и MIRROR на одной и той же resistance-зоне никогда не конкурируют
        друг с другом за фокус — это два независимых потока сигналов."""
        # Новая логика для шортов в режиме Climax
        if trade_type == 'SHORT' and mode == 'CLIMAX':
            STAGE_RANK = {'WAIT_MAX': 1, 'WAIT_BUFFER': 2, 'SEARCHING': 3}
            candidates = [
                (lid, w) for lid, w in self._watchers.items()
                if getattr(w, 'coin', None) == coin
                and getattr(w, 'trade_type', None) == 'SHORT'
                and getattr(w, 'mode', None) == 'CLIMAX'
                and getattr(w, 'climax_stage', None) in STAGE_RANK
                and getattr(w, 'state', None) not in ("DEAD", "TRIGGERED")
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda item: (STAGE_RANK[item[1].climax_stage], item[1].max))[0]

        # Старая логика — для LONG (mode=None) и для SHORT MIRROR (mode='MIRROR')
        candidates = [
            (lid, w) for lid, w in self._watchers.items()
            if getattr(w, 'coin', None) == coin
            and getattr(w, 'trade_type', None) == trade_type
            and getattr(w, 'mode', None) == mode
            and getattr(w, 'state', None) == "SCANNING"
            and getattr(w, 'pierced_bottom', False)
            and getattr(w, 'last_pierce_time', None) is not None
        ]
        if not candidates:
            return None
        if trade_type == 'LONG':
            return max(candidates, key=lambda item: (item[1].last_pierce_time, -item[1].min))[0]
        else:
            return max(candidates, key=lambda item: (item[1].last_pierce_time, item[1].max))[0]

    def kill_short_watchers_behind_pierced_neighbor(self, c_high, coin="UNKNOWN"):
        """Хардовая смерть SHORT-вотчера, если цена уже пробила порог входа
        БЛИЖАЙШЕЙ живой SHORT-зоны выше него — той же формулой, что и обычный
        вход (край зоны для POC, середина для всех остальных). Раз порог
        входа соседа выше уже пробит — этот, нижний, уровень точно уже
        позади рынка, независимо от того, что дальше будет с соседом (жив
        он, сработал, тоже умрёт) — наша смерть от его судьбы не зависит.

        Раньше это была отдельная зона (paired_level), которую вотчер
        замораживал себе ОДИН раз при рождении и больше не обновлял — если
        уровни потом пересчитывались, сравнение шло со старой, неактуальной
        зоной, и смерть могла не сработать, хотя по факту цена уже давно
        пробила настоящую ближайшую зону сверху. Теперь сравниваем каждую
        свечу напрямую с текущим списком живых вотчеров — он и так всегда
        актуален (создаётся/удаляется по факту), запоминать и обновлять
        отдельно нечего.

        Не может быть так, что верхний уровень умер раньше нижнего по этой
        же причине: если верхний пробил свои 50% — то и порог нижнего (он
        ниже по цене) уже пройден ещё раньше, нижний уже мёртв к этому
        моменту. Поэтому достаточно сравнивать каждый вотчер только с
        БЛИЖАЙШИМ соседом сверху, дальние тут ни при чём."""
        shorts = [w for w in self._watchers.values()
                  if getattr(w, 'coin', None) == coin
                  and getattr(w, 'trade_type', None) == 'SHORT'
                  and getattr(w, 'state', None) not in ("DEAD", "TRIGGERED")]
        for w in shorts:
            candidates = [o for o in shorts if o is not w and o.min > w.max]
            if not candidates:
                continue
            nearest = min(candidates, key=lambda o: o.min - w.max)
            n_is_poc = '4h_poc_standalone' in getattr(nearest, 'level_type', '')
            n_trigger = nearest.min if n_is_poc else (nearest.min + nearest.max) / 2.0
            if c_high >= n_trigger:
                w.state = "DEAD"
                w.last_event_type = "RUNAWAY"
                w.history_log = (
                    f"Пробит вход соседней зоны выше ({c_high:.4f} >= {n_trigger:.4f}, "
                    "той же формулой, что и обычный вход) — этот уровень позади рынка, убит навсегда."
                )
                w._dbg(f"🔴 СМЕРТЬ (пробит вход соседа выше) | {w.history_log}")

    def process_candle(self, c_low, c_high, c_close, current_supports, current_resistances,
                        df_slice, allow_long=True, allow_short=True, c_atr=0.0, coin="UNKNOWN"):
        self._update_graveyard(c_close, coin=coin)
        self.kill_short_watchers_behind_pierced_neighbor(c_high, coin=coin)

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
            elif et in ("CLIMAX_NEAR_BREACH", "CLIMAX_FAR_BREACH"):
                draw_events.append(('climax_breach', c_high))

        # ================================================================
        # ПОДХОД С ПРАВИЛЬНОЙ СТОРОНЫ — единое правило рождения вотчера
        # (то же самое, что в watcher_plan.py::rescan_single_bounce_watcher):
        #   LONG  — только если ПРЕДЫДУЩАЯ свеча закрылась ВЫШЕ зоны (цена
        #           пришла сверху — зона сейчас действительно поддержка);
        #   SHORT — только если предыдущая свеча закрылась НИЖЕ зоны (пришла
        #           снизу — зона сейчас действительно сопротивление).
        # Раньше касанием считалось просто c_low <= max / c_high >= min — это
        # выполняется и когда цена ПОД поддержкой лезет в неё снизу, так и
        # рождался LONG от зоны, которая по факту сопротивление.
        # Касается ТОЛЬКО рождения новых вотчеров: уже живые вотчеры
        # evaluate_bounce_side кормит свечой в любом случае.
        # df_slice — окно свечей, последняя строка = текущая свеча.
        # ================================================================
        prev_close = float(df_slice['close'].iloc[-2]) if df_slice is not None and len(df_slice) >= 2 else None

        if allow_long:
            focus_long_id = self._get_focus_level_id('LONG', coin=coin)
            touched_long = [s for s in current_supports
                            if c_low <= s['max'] and prev_close is not None and prev_close > s['max']]
            for level_id, lvl, decision in self.evaluate_bounce_side(
                    'LONG', touched_long,
                    lambda lid, l: self.evaluate_bounce(
                        l, df_slice, 'LONG', current_resistances,
                        is_focus=(focus_long_id is None or lid == focus_long_id), c_atr=c_atr,
                        coin=coin),
                    coin=coin):
                _collect_event(level_id, 'LONG')
                if decision.get('allow'):
                    actual_lvl = next((s for s in current_supports if s['min'] == lvl['min'] and s['max'] == lvl['max']), lvl)
                    orders.append({'trade_type': 'LONG', 'level': actual_lvl, 'decision': decision})

        if allow_short:
            touched_short = [r for r in current_resistances
                             if c_high >= r['min'] and prev_close is not None and prev_close < r['min']]
            # SHORT_CLIMAX_MODE (см. BounceWatcher.CONFIG) — ЕДИНЫЙ переключатель:
            # False -> CLIMAX-вотчер вообще не создаётся и не ищет вход, работает
            # только MIRROR. True -> оба, как раньше. Раньше это значение читалось
            # только ПОСЛЕ создания вотчера (как override конкретному инстансу) —
            # сам список режимов для поиска был жёстко ('CLIMAX', 'MIRROR')
            # всегда, поэтому выключение в конфиге ничего не отключало.
            active_short_modes = SHORT_MODES if BounceWatcher.CONFIG.get('SHORT_CLIMAX_MODE') else ('MIRROR',)
            for mode in active_short_modes:
                focus_short_id = self._get_focus_level_id('SHORT', mode=mode, coin=coin)
                for level_id, lvl, decision in self.evaluate_bounce_side(
                        'SHORT', touched_short,
                        lambda lid, l, _mode=mode, _focus=focus_short_id: self.evaluate_bounce(
                            l, df_slice, 'SHORT', current_supports,
                            is_focus=(_focus is None or lid == _focus), c_atr=c_atr,
                            mode=_mode, coin=coin),
                        mode=mode, coin=coin):
                    _collect_event(level_id, 'SHORT')
                    if decision.get('allow'):
                        actual_lvl = next((r for r in current_resistances if r['min'] == lvl['min'] and r['max'] == lvl['max']), lvl)
                        orders.append({'trade_type': 'SHORT', 'level': actual_lvl, 'decision': decision})

            # === ЖЕСТКОЕ УБИЙСТВО НИЖНИХ УРОВНЕЙ (только внутри семьи CLIMAX,
            # только внутри ЭТОЙ монеты — это правило "побеждает самый верхний"
            # родом из климакс-логики, MIRROR-вотчеров оно не касается вообще,
            # у них своя, независимая линия сигналов). БЕЗ фильтра по coin тут
            # был тот же баг, что и в evaluate_bounce_side — climax монеты A
            # мог убить climax монеты B, если у него max оказался ниже. ===
            searching_shorts = [
                w for w in self._watchers.values()
                if getattr(w, 'coin', None) == coin
                and getattr(w, 'trade_type', None) == 'SHORT'
                and getattr(w, 'mode', None) == 'CLIMAX'
                and getattr(w, 'climax_stage', None) == 'SEARCHING'
                and getattr(w, 'state', None) not in ("DEAD", "TRIGGERED")
            ]
            if searching_shorts:
                highest_max = max(w.max for w in searching_shorts)
                for w in self._watchers.values():
                    if getattr(w, 'coin', None) == coin and getattr(w, 'trade_type', None) == 'SHORT' and getattr(w, 'mode', None) == 'CLIMAX' and getattr(w, 'state', None) not in ("DEAD", "TRIGGERED"):
                        if w.max < highest_max:
                            w.state = "DEAD"
                            w.last_event_type = "RUNAWAY"
                            w.history_log = "Приоритет отдан более высокому уровню. Нижний сетап убит навсегда."
                            w._dbg(f"🔴 ОТМЕНА | {w.history_log}")

        return orders, draw_events
    # -------------------------------------------------------------------------
    # BOUNCE (Отбой от макро-уровня)
    # -------------------------------------------------------------------------
    def evaluate_bounce(self, level, df, trade_type, all_opposite_levels, is_focus=True,
                        c_atr=0.0, mode=None, coin="UNKNOWN"):
        level_id = self._level_id(level, trade_type)
        if mode:
            level_id = f"{level_id}__{mode}"
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        # Лимит сделок на уровень — по self.level_trades (см. __init__), а не
        # по счётчику внутри вотчера. Исчерпан -> вотчер не создаём, а
        # живой (если остался) закрываем, чтобы не висел "в работе".
        max_total = BounceWatcher.CONFIG.get('MAX_TRADES_PER_LEVEL', 1)
        if max_total > 0 and len(self.level_trades.get(level_id, [])) >= max_total:
            w_existing = self._watchers.get(level_id)
            if w_existing is not None and w_existing.state not in ("DEAD", "TRIGGERED"):
                w_existing.state = "TRIGGERED"
                w_existing.history_log = (f"По этому уровню уже была сделка "
                                          f"({len(self.level_trades[level_id])}/{max_total}) — больше не торгуем.")
            return self._deny("Лимит сделок на уровень исчерпан")

        if level_id not in self._watchers:
            config_overrides = None
            if trade_type == 'SHORT' and mode:
                # CLIMAX -> SHORT_CLIMAX_MODE=True, MIRROR -> False. Фиксируется
                # НАВСЕГДА в момент рождения вотчера, независимо от того, что
                # сейчас стоит в BounceWatcher.CONFIG по умолчанию.
                config_overrides = {'SHORT_CLIMAX_MODE': (mode == 'CLIMAX')}
            self._watchers[level_id] = BounceWatcher(level['min'], level['max'], trade_type,
                                                       config_overrides=config_overrides, mode=mode,
                                                       coin=coin, log_dir_override=self.log_dir_override)
            self._watchers[level_id].level_type = level.get('type', 'UNKNOWN')
            self._watchers[level_id].level_date = level.get('date')
            self._watchers[level_id].level_score = level.get('score', 0)
            # Ручная зона (custom_levels.json) — не мержится с авто при чтении
            # (get_merged_levels_for_coin просто конкатенирует списки), поэтому
            # 'type' у неё ровно "MANUAL", без примесей. Вход для таких зон —
            # см. CONFIG['MANUAL_MIDPOINT_ENTRY_ENABLED'] в bounce_watcher.py.
            self._watchers[level_id].simple_entry = (level.get('type') == 'MANUAL')
            # ================================================================
            # ДВЕ РАЗНЫЕ ДАТЫ — НЕ ПУТАТЬ И НЕ ПОДМЕНЯТЬ ОДНУ ДРУГОЙ.
            #
            # activated_at — дата УРОВНЯ: момент, с которого уровень вообще
            #   можно брать в работу (подтвердился технически, см.
            #   levels_builder.py). Это НЕ касание и НЕ дата пика в прошлом.
            #   Копируется с уровня как есть. Правило: раньше activated_at
            #   уровень не работает, даже если цена его касалась.
            #
            # born_at — момент рождения ЭТОГО вотчера: время свечи (UTC,
            #   с часами и минутами), на которой цена коснулась уже активного
            #   уровня и вотчер был создан. Берётся из времени свечи, а не из
            #   часов компьютера — в реплее/догоне свеча может быть в прошлом.
            #   Рескан одного вотчера (watcher_plan.py::
            #   rescan_single_bounce_watcher) стартует от born_at, поэтому не
            #   доходит до старой сделки прошлого вотчера на этой же зоне.
            #
            # born_at никогда не раньше activated_at: вотчер рождается только
            # на уже активном уровне.
            # ================================================================
            self._watchers[level_id].activated_at = level.get('activated_at')
            born_ts = df.index[-1] if len(df) else None
            if born_ts is not None and hasattr(born_ts, 'tz_localize'):
                born_ts = born_ts.tz_convert('UTC').tz_localize(None) if born_ts.tzinfo is not None else born_ts
                self._watchers[level_id].born_at = born_ts.strftime('%Y-%m-%dT%H:%M:%S')
            else:
                self._watchers[level_id].born_at = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S')
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
            is_focus=is_focus, c_atr=c_atr
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
        try:
            _entry_ts = int(pd.Timestamp(df.index[-1]).timestamp())
        except Exception:
            _entry_ts = int(datetime.datetime.utcnow().timestamp())
        self.level_trades.setdefault(level_id, []).append(_entry_ts)
        signal['extreme_price'] = "0.0"
        signal['is_real_sweep'] = False
        signal['overshoot_pct'] = 0.0
        signal['candles_in_sweep'] = 0

        return signal

    def forget_level_trades_since(self, level_ids, since_unix):
        """Для рескана: рескан заново проигрывает историю зоны с since_unix,
        поэтому сделки по этим level_id с этого момента забываем — рескан
        найдёт их сам (и снова запишет через evaluate_bounce). Сделки
        РАНЬШЕ since_unix не трогаем — рескан их не видит, они остаются
        в силе и продолжают держать лимит."""
        for lid in level_ids:
            times = self.level_trades.get(lid)
            if not times:
                continue
            kept = [t for t in times if t < since_unix]
            if kept:
                self.level_trades[lid] = kept
            else:
                del self.level_trades[lid]

    # -------------------------------------------------------------------------
    # Хозяйство: количество вотчеров / уборка мёртвых (шаг 5)
    # -------------------------------------------------------------------------
    def watcher_count(self):
        return len(self._watchers)

    def clear_dead_watchers(self, active_level_ids):
        """Тонкая обёртка над parent.clear_dead_watchers — та же роль, что
        VBottomManager.clear_dead_watchers: убирает уже DEAD/TRIGGERED
        вотчеров, которых нет среди активных id этого скана. Не трогает
        живые (SCANNING/searching) вотчеры независимо от active_level_ids —
        см. BounceParent.clear_dead_watchers."""
        return self.parent.clear_dead_watchers(active_level_ids)

    def clear_graveyard_by_coin(self, coin):
        """Чистит кладбище (graveyard) для конкретной монеты ЦЕЛИКОМ.

        ⚠️ Больше НЕ используется ручным ресканом (см. clear_graveyard_for_ids
        ниже) — оставлена как есть на случай, если где-то ещё понадобится
        снести защиту по всей монете разом. Раньше именно эта функция
        вызывалась из run_web.py::_run_rescan и ломала кладбище чужих,
        не участвующих в рескане уровней той же монеты (см. комментарий
        в clear_graveyard_for_ids — тот самый баг с "вчера убитые
        пересканом вотчеры возвращаются сегодня")."""
        removed_ids = set()
        kept = []
        for entry in self.graveyard:
            if entry.get('coin') == coin:
                base_id = self._level_id({'min': entry['min'], 'max': entry['max']}, entry['trade_type'])
                level_id = f"{base_id}__{entry['mode']}" if entry.get('mode') else base_id
                removed_ids.add(level_id)
            else:
                kept.append(entry)
        self.graveyard = kept
        self._graveyard_recorded -= removed_ids

    def clear_graveyard_for_ids(self, level_ids):
        """То же самое, что clear_graveyard_by_coin, но точечно — только
        для переданных level_id (используется ручным ресканом МОНЕТЫ, см.
        run_web.py::_run_rescan, вместо clear_graveyard_by_coin).

        Почему это важно: clear_graveyard_by_coin сносил кладбище для
        ВСЕЙ монеты, хотя защита реально нужна только тем вотчерам, которых
        рескан прямо сейчас сбрасывает через reset_for_rescan() (иначе их
        же собственная свежая запись в кладбище мешала бы им "воскреснуть"
        тем же объектом). У монеты почти всегда есть и ДРУГИЕ, не
        участвующие в этом рескане уровни, которые уже умерли естественно
        раньше (вчера, позавчера — живым сканом или прошлым ресканом) и
        были честно занесены в кладбище — clear_graveyard_by_coin стирал
        и их защиту тоже. Итог: следующий же рескан этой монеты (или просто
        совпадение по времени с плановым сканом) снова касался цены той же
        зоны в реплей-окне и заводил "новый" вотчер на месте вчера
        убитого — визуально неотличимо от "удаление/смерть не сработали".

        Точечная версия убирает из кладбища только записи, чей level_id
        совпадает с переданным набором — уровни монеты, не участвующие в
        текущем рескане, остаются защищены как и были."""
        ids = set(level_ids)
        if not ids:
            return
        removed_ids = set()
        kept = []
        for entry in self.graveyard:
            base_id = self._level_id({'min': entry['min'], 'max': entry['max']}, entry['trade_type'])
            level_id = f"{base_id}__{entry['mode']}" if entry.get('mode') else base_id
            if level_id in ids:
                removed_ids.add(level_id)
            else:
                kept.append(entry)
        self.graveyard = kept
        self._graveyard_recorded -= removed_ids

    def remove_watchers_by_coin(self, coin):
        """Удаляет ВСЕХ вотчеров конкретной монеты (живых и мёртвых) —
        используется ручным ресканом (см. background_tasks.py, флаг
        rescan_{coin}.flag): раз рескан честно пересобирает путь монеты
        заново от выбранной даты, старых вотчеров нужно убрать полностью,
        иначе реплей будет накладывать прошлые свечи на вотчер, который уже
        успел уйти вперёд по своему реальному состоянию — получится каша, а
        не честный повтор. В отличие от VBottomManager.remove_watchers_by_coin
        (просто del, без возврата) — тут ВОЗВРАЩАЕМ удалённых, чтобы
        background_tasks.py успел заархивировать их в watcher_history.json
        (иначе их точки просто исчезли бы бесследно, ничем не отличаясь от
        обычного стирания памяти)."""
        removed = {}
        for level_id in [k for k, w in self._watchers.items() if getattr(w, 'coin', None) == coin]:
            removed[level_id] = self._watchers.pop(level_id)
        return removed

    # -------------------------------------------------------------------------
    # Персистентность между рестартами бота (шаги 2-3). По той же схеме, что
    # VBottomManager.to_state_dict/save_state/load_state — дамп __dict__
    # каждого вотчера целиком + отдельно состояние самого менеджера
    # (pierced_count/graveyard/_graveyard_recorded), которого у VBottomManager
    # нет, потому что у него нет ни кладбища, ни счётчика пробоев.
    # -------------------------------------------------------------------------

    # Поля BounceWatcher, которые могут содержать pandas.Timestamp
    # (json.dump их не переваривает) — переводим в ISO-строку туда-обратно.
    _TIMESTAMP_FIELDS = ('_last_time', 'last_event_time', 'last_pierce_time')

    def _watcher_to_state_entry(self, watcher):
        """Сериализует ОДНОГО вотчера в JSON-совместимый словарь — вынесено
        из to_state_dict() отдельной функцией, чтобы save_state() могло
        проверить каждого вотчера по отдельности (см. её докстринг)."""
        state = dict(watcher.__dict__)
        for ts_field in self._TIMESTAMP_FIELDS:
            val = state.get(ts_field)
            if val is not None and hasattr(val, 'isoformat'):
                state[ts_field] = val.isoformat()
        return {
            'min': watcher.min,
            'max': watcher.max,
            'trade_type': watcher.trade_type,
            'mode': getattr(watcher, 'mode', None),
            'coin': getattr(watcher, 'coin', 'UNKNOWN'),
            'state': state,
        }

    def to_state_dict(self):
        """Сериализует вотчеров BOUNCE + состояние самого менеджера
        в JSON-совместимый словарь. НЕ проверяет сериализуемость (это
        теперь делает save_state() по каждому вотчеру отдельно) — этот
        метод просто строит структуру."""
        watchers_out = {
            level_id: self._watcher_to_state_entry(watcher)
            for level_id, watcher in self._watchers.items()
        }
        return {
            'watchers': watchers_out,
            'pierced_count': self.pierced_count,
            'graveyard': self.graveyard,
            'graveyard_recorded': list(self._graveyard_recorded),
            'last_processed_time': self.last_processed_time,
            'level_trades': self.level_trades,
        }

    def save_state(self, path):
        """Атомарно сохраняет текущее состояние BOUNCE (вотчеры + кладбище +
        счётчики) в файл. Вызывается из live_scan.py::save_watcher_state()
        раз в скан-цикл, вместе с v_bottom_mgr.

        ВАЖНО (найдено при разборе бага "удалённые/мёртвые вотчеры
        возвращаются после рестарта бота сами по себе, без касания цены"):
        раньше вся пачка вотчеров сериализовалась ОДНИМ json.dump() —
        если хотя бы у ОДНОГО вотчера было несериализуемое в JSON поле
        (например numpy/pandas-тип, случайно попавший в __dict__ мимо
        _TIMESTAMP_FIELDS), json.dump() падал на середине, ничего не
        писалось на диск вообще, и файл оставался на диске в состоянии
        последнего УДАЧНОГО сохранения — возможно, многочасовой или
        многодневной давности. В памяти всё было верно (вотчер удалён/
        мёртв, active_watchers.json тоже верный — он сохраняется отдельно
        и этой болячке не подвержен), но именно ЭТОТ файл, из которого
        load_state() восстанавливает вотчеров при каждом старте бота,
        тихо не обновлялся. Следующий рестарт честно грузил этот старый
        снимок — и "воскрешал" из него всё, что успело измениться в
        реальности после последнего удачного сохранения, включая уже
        удалённые вотчеры. Ни одна out-of-band причина (кладбище/5%-уход/
        точечный рескан) тут была ни при чём — файл на диске просто не
        поспевал за памятью.

        Теперь каждый вотчер сериализуется и валидируется ОТДЕЛЬНО: если
        у конкретного вотчера есть проблемное поле — он логируется и
        пропускается ТОЛЬКО в этом цикле сохранения (не навсегда — если
        причина временная/поле потом станет валидным, следующий же
        успешный цикл сохранит его нормально), а все остальные вотчеры
        всё равно долетают до диска, как и должны."""
        watchers_out = {}
        for level_id, watcher in self._watchers.items():
            entry = self._watcher_to_state_entry(watcher)
            try:
                json.dumps(entry)  # пробный прогон — только чтобы поймать несериализуемое поле ДО общего dump
            except (TypeError, ValueError) as e:
                print(f"[BounceManager] ⚠️ Вотчер {level_id} ({getattr(watcher, 'coin', '?')}) "
                      f"не сохранён в состояние в этом цикле — несериализуемое поле: {e}")
                continue
            watchers_out[level_id] = entry

        data = {
            'watchers': watchers_out,
            'pierced_count': self.pierced_count,
            'graveyard': self.graveyard,
            'graveyard_recorded': list(self._graveyard_recorded),
            'last_processed_time': self.last_processed_time,
            'level_trades': self.level_trades,
        }
        try:
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception as e:
            # Тут падать может уже только на уровне менеджера (graveyard/
            # last_processed_time) — вотчеры к этому моменту уже все
            # проверены по отдельности выше.
            print(f"[BounceManager] ⚠️ Не удалось сохранить состояние BOUNCE: {e}")

    def load_state(self, path):
        """Восстанавливает вотчеров BOUNCE + graveyard + pierced_count из
        файла состояния при старте бота. Вызывается один раз при импорте
        live_scan.py, сразу после создания bounce_mgr."""
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[BounceManager] ⚠️ Не удалось прочитать состояние BOUNCE: {e}")
            return

        watchers_in = data.get('watchers', {})
        restored = 0
        for level_id, entry in watchers_in.items():
            saved_coin = entry.get('coin')
            if not saved_coin or saved_coin == 'UNKNOWN':
                print(f"[BounceManager] 🗑️ Игнорируем зомби-вотчер без монеты: {level_id}")
                continue
            try:
                mode = entry.get('mode')
                saved_state = dict(entry.get('state', {}))
                # config_overrides не хранится отдельно — он уже "запёкся"
                # внутри сохранённого self.CONFIG (см. __dict__ дамп), поэтому
                # конструктору передаём базовые аргументы, а CONFIG перезатрём
                # ниже вместе со всем остальным состоянием.
                watcher = BounceWatcher(entry['min'], entry['max'], entry['trade_type'],
                                          mode=mode, coin=saved_coin, log_dir_override=self.log_dir_override)
                for ts_field in self._TIMESTAMP_FIELDS:
                    val = saved_state.get(ts_field)
                    if val is not None:
                        try:
                            saved_state[ts_field] = pd.Timestamp(val)
                        except Exception:
                            saved_state[ts_field] = None
                watcher.__dict__.update(saved_state)
                self._watchers[level_id] = watcher
                restored += 1
            except Exception as e:
                print(f"[BounceManager] ⚠️ Пропущен BOUNCE-вотчер {level_id} при восстановлении: {e}")

        self.pierced_count = data.get('pierced_count', 0)
        self.graveyard = data.get('graveyard', [])
        self._graveyard_recorded = set(data.get('graveyard_recorded', []))
        self.last_processed_time = data.get('last_processed_time', {})
        self.level_trades = data.get('level_trades', {}) or {}

        if restored:
            print(f"[BounceManager] ✅ Восстановлено {restored} BOUNCE-вотчеров из {path}")