# -*- coding: utf-8 -*-
"""
v_bottom_watcher.py
====================
Стратегия V_BOTTOM. Строгая логика поиска пиков.
Ориентир -> Старт -> Пик 1 (точка отсчёта, БЕЗ проверки выкупа) -> Пик 2+ -> Проверка ОДНОЙ следующей свечи -> Вход/Мимо -> ...

Правила:
  * Лестница Ориентир -> Старт -> Пик требует СТРОГОЙ эскалации
    (каждая ступень больше предыдущей).
  * Пик 1 (сразу после Старта) — это только начало движения, точка
    отсчёта. Выкуп на нём НЕ проверяется вообще. Сразу ищем
    следующую красную свечу.
  * Начиная со ВТОРОГО пика: после каждого нового пика проверяется
    РОВНО ОДНА следующая свеча:
      - зелёная, объём >= VOL_MATCH_PCT % от Пика -> ВХОД;
      - красная, объём >= PEAK_TOLERANCE_PCT % от Пика (допуск +-10%)
        -> она сама становится новым Пиком (эталон поднимается,
        только если она реально БОЛЬШЕ), и для НЕЁ снова проверяется
        ровно одна следующая свеча;
      - иначе (не подошло ни туда, ни туда) -> эта попытка мимо,
        Пик остаётся как есть, продолжаем сканировать дальнейшие
        свечи в поиске новой красной в том же допуске от текущего
        Пика (без ограничения по числу свечей на сам поиск —
        ограничена только ПРОВЕРКА ВЫКУПА, ровно одной свечой сразу
        после каждого найденного/обновлённого пика, начиная со 2-го).
  * Повторное пробитие уровня: разрешено MAX_REBREACHES (по умолч. 1)
    повторных заходов под уровень. Каждый заход считается С НУЛЯ
    (вся математика заново, свежий фон). Сверх лимита — уровень для
    стратегии мёртв. Сброс математики при новом заходе гарантирует
    симулятор вызовом on_breach_start() в момент пробития.
  * Буфер BREATH_BUFFER_PCT над уровнем: мелкое выныривание в этой
    зоне не сбрасывает цепочку, только выход ЗА буфер — полный сброс.

📝 ЛОГИ (минимум, по вехам, не по каждой свече):
  пробитие уровня (первое — в __init__, повторное — в on_breach_start),
  каждый подтверждённый пик (Пик 1, Пик 2, Пик 3, ...), вход, срыв/отмена.
  НЕ логируем: попытки на каждой свече, которые ни к чему не привели
  (проверка условия "подходит ли эта свеча" сама по себе не событие,
  событие — только когда она РЕАЛЬНО стала ориентиром/стартом/пиком/входом).
"""

import os
import datetime
from collections import deque
from ..utils.risk_calc import calc_tp_and_rr


class VBottomWatcher:
    # Ключи (log_dir, log_name), для которых лог уже открыт "с нуля" в этом
    # процессе — общий на весь класс (переживает между вотчерами, как у
    # BounceWatcher._initialized_log_coins). Нужен, чтобы при первом же
    # событии в файле НЕ дописывать поверх лога прошлого прогона симулятора
    # (сервер симулятора не перезапускается между прогонами), а начать
    # заново — и при этом не тереть его на каждой новой свече.
    _initialized_log_files = set()
    _DEBUG_RING_SIZE = 400  # см. BounceWatcher._DEBUG_RING_SIZE — тот же смысл,
                             # буфер debug-строк В ПАМЯТИ на КАЖДЫЙ вотчер отдельно.

    CONFIG = {
        'ELEVATED_VOL_MULT': 2.0,
        'PEAK_TOLERANCE_PCT': 85.0,   # допуск: красная >= этого % от текущего пика тоже считается новым пиком (+-10%)
        'MAX_REBREACHES': 5.0,          # сколько ПОВТОРНЫХ заходов под уровень разрешено (вниз-вверх-вниз = 1 повтор)
        'BREATH_BUFFER_PCT': 3.0,     # буфер над уровнем (%): мелкое выныривание в этой зоне НЕ сбрасывает цепочку
        'VOL_MATCH_PCT': 101.0,
        'TP_MODE': 'fixed_pct',
        'FIXED_TP_PCT': 10.0,
        'TAKE_PROFIT': 10.0,
        'TP_BUFFER_PCT': 0.0,
        'SL_BUFFER': 0.5,
        'MIN_RR': 1.0,
        'USE_RR_FILTER': False,
        'DEBUG': True,
    }

    def __init__(self, level_min, level_max, trade_type, coin="UNKNOWN", level_date=None,
                 level_type=None, level_score=None, log_dir_override=None, candle_time=None):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.coin = coin
        # Если задан — логи идут туда вместо боевой logs/watchers/ (см.
        # _dbg ниже). Передаётся симулятором (simulate_engine.py), чтобы не
        # мешать тестовые прогоны с реальным ботом — та же идея, что у
        # BounceWatcher.log_dir_override.
        self.log_dir_override = log_dir_override
        # Дата ФОРМИРОВАНИЯ уровня (из macro_levels.json, lvl['date']) — не
        # путать с моментом, когда вотчер начал следить (это происходит
        # позже, при реальном касании). Замораживаем один раз при рождении,
        # той же логикой, что min/max — не сверяем заново с текущим
        # macro_levels.json (он мог пересчитаться и потерять точное
        # совпадение). Нужно, чтобы на графике линия уровня начиналась там,
        # где уровень реально появился, а не только с момента слежки.
        self.level_date = level_date
        # Тип/сила уровня (level['type']/level['score'] из macro_levels.json) —
        # раньше не сохранялись вообще (конструктор их не принимал), поэтому
        # дашборд (background_tasks.py::_build_active_watchers_export, читает
        # именно эти атрибуты через getattr) показывал дефолт "уровень"/"—".
        # Симулятор этой проблемы не имел — он берёт type/score прямо из
        # свежего level dict, а не с самого вотчера (см. simulate_engine.py).
        self.level_type = level_type
        self.level_score = level_score
        self.state = "SEARCHING"

        self.tracker_vol = 0.0
        self.start_vol = 0.0
        self.cand_low = 0.0
        self.cand_vol = 0.0
        self.breach_count = 0     # сколько заходов под уровень уже было (первый = 0, считаются повторные)
        self.peak_count = 0       # номер текущего подтверждённого пика (1, 2, 3, ...) — только для логов/нумерации
        self.sl_price = None
        self.entry_price = None

        self.history_log = ""
        # Время текущей свечи в момент рождения вотчера — иначе первая же
        # строка лога ("🔻 [ПРОБИТИЕ]" ниже) пишется ДО того, как update()
        # хоть раз проставит _last_time, и в окошке "лог вотчера" (см.
        # app.py::/api/watcher_debug_log) висит строка вообще без времени.
        # vbottom_manager.py передаёт df.index[-1] в момент создания.
        self._last_time = candle_time
        self.last_event_time = None
        self.last_event_msg = None
        self.last_event_type = None
        self.event_log = []  # [{time, type, price}, ...] — путь вотчера для дашборда
        self.debug_ring = deque(maxlen=VBottomWatcher._DEBUG_RING_SIZE)  # см. _dbg ниже

        # Вотчер создаётся ровно в момент ПЕРВОГО пробития уровня (см.
        # vbottom_manager.py::evaluate_v_bottom — is_new_watcher). Повторные
        # заходы под уровень идут через on_breach_start() ниже, у него свой
        # лог — здесь логируем именно первое пробитие, единственный раз.
        if self.CONFIG.get('DEBUG'):
            self._dbg(f"🔻 [ПРОБИТИЕ] уровень {self.min:.4f}-{self.max:.4f} пробит, начинаем слежение")

    def _tp(self):
        """Строка с временем текущей свечи для debug-принтов, если время передано."""
        return f"{self._last_time} " if self._last_time is not None else ""

    def _dbg(self, msg):
        """Пишет строку в лог этой монеты — не в консоль, терминал не тянет
        тысячи строк, файл открывается текстовым редактором целиком, ищется
        по Ctrl+F. Также запоминает событие для авто-маркера на графике
        симулятора (см. test_simulator.py).

        Боевой режим (log_dir_override не задан): logs/watchers/{coin}.log,
        дописывает вечно, никогда не чистится автоматически — как раньше.

        Режим симулятора (log_dir_override задан, см. simulate_engine.py):
        своя папка, файл называется {coin}_V.log (не {coin}.log — иначе
        пересёкся бы в той же папке с логами BOUNCE-симулятора). При первом
        событии в файле за время жизни процесса — начинает с нуля (сервер
        симулятора не перезапускается между прогонами, иначе каждый новый
        клик "Старт" молча дописывался бы поверх предыдущего прогона), дальше
        в рамках того же прогона — дописывает.

        Вызывается ТОЛЬКО на реальных вехах (пробитие/пик/вход/срыв), не на
        каждой свече — см. докстринг модуля."""
        self.last_event_time = self._last_time
        self.last_event_msg = msg

        # Кольцевой буфер В ПАМЯТИ на ЭТОТ вотчер (см. debug_ring/_DEBUG_RING_SIZE
        # выше) — для окошка "лог вотчера" на дашборде, отдельно от файла ниже.
        self.debug_ring.append(f"{self._tp()}[V_BOTTOM][{self.max:.4f}] {msg}")

        log_dir = self.log_dir_override or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "logs", "watchers")
        os.makedirs(log_dir, exist_ok=True)
        # Один файл {coin}.log и в бою, и в симуляторе; в симуляторе его
        # обнуляет раннер перед каждым прогоном (bounce_simulate._reset_sim_log).
        with open(os.path.join(log_dir, f"{self.coin}.log"), "a", encoding="utf-8") as f:
            f.write(f"{self._tp()}[V_BOTTOM][{self.max:.4f}] {msg}\n")

    def _record_event(self, event_type, price):
        """Копит точки пути вотчера (для отрисовки на графике дашборда).
        Ограничено 100 точками на вотчер — этого с большим запасом хватает
        на один цикл жизни (от Ориентира до входа/смерти).
        Время конвертируем в unix-секунды сразу тут: candle_time приходит
        как pandas.Timestamp, который json.dump не умеет сериализовать —
        если оставить как есть, экспорт в JSON будет тихо падать."""
        self.last_event_type = event_type
        t = self._last_time
        if t is not None and hasattr(t, "timestamp"):
            t = int(t.timestamp())
        self.event_log.append({"time": t, "type": event_type, "price": price})
        if len(self.event_log) > 100:
            self.event_log = self.event_log[-100:]

    @staticmethod
    def _fmt(v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000: return f"{v/1_000:.1f}k"
        return str(int(v))

    def _reset_chain(self):
        """Полный сброс математики цикла (лестница + пик + таймер)."""
        self.state = "SEARCHING"
        self.tracker_vol = 0.0
        self.start_vol = 0.0
        self.cand_low = 0.0
        self.cand_vol = 0.0
        self.peak_count = 0
        self.history_log = ""

    def on_breach_start(self):
        """Вызывается симулятором в момент НОВОГО пробития уровня вниз
        (создание origin_level_long). На ПЕРВОМ пробитии вотчера ещё нет
        (создаётся свежим при первом evaluate, лог — в __init__), так что
        каждый вызов сюда — это ПОВТОРНЫЙ заход. Сверх MAX_REBREACHES —
        уровень мёртв."""
        if self.state in ("DEAD", "TRIGGERED"):
            return
        self.breach_count += 1  # номер повторного захода (1 = первый повтор)
        if self.breach_count > self.CONFIG['MAX_REBREACHES']:
            self._dbg(f"💀 [ЛИМИТ] повторное пробитие #{self.breach_count} сверх лимита -> DEAD")
            self.state = "DEAD"
            return
        if self.CONFIG.get('DEBUG'):
            self._dbg(f"🔄 [ПОВТОРНОЕ ПРОБИТИЕ #{self.breach_count}] математика с нуля")
        self._reset_chain()

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, trend='UNKNOWN', vol_std=None, candle_time=None):
        self._last_time = candle_time
        if self.state in ("DEAD", "TRIGGERED"):
            return None

        if not baseline_vol or baseline_vol <= 0:
            return None

        if self.trade_type == 'LONG':

            # --- 1. ПРОВЕРКА ОТМЕНЫ (СБРОС) ---
            buffer_top = self.max * (1 + self.CONFIG['BREATH_BUFFER_PCT'] / 100.0)
            if c_low > buffer_top or c_close > buffer_top:
                # ушли ДАЛЕКО за буфер — настоящий выход из движения, полный сброс
                if self.state != "SEARCHING":
                    if self.history_log:
                        self._dbg(f"🛑 [СРЫВ] {self.history_log} -> [ОТМЕНА: цена ушла выше буфера ({self.CONFIG['BREATH_BUFFER_PCT']}%)]")
                    self._record_event("CANCEL", c_close)
                    self._reset_chain()
                return None
            if c_low > self.max:
                # высунулись НАД уровнем, но внутри буфера — просто "дышим",
                # ничего не сбрасываем и не считаем эту свечу (пики/выкуп
                # ищем только строго под уровнем)
                return None

            is_red = c_close < c_open

            if self.state == "SEARCHING":
                # --- 2. ПОИСК ОРИЕНТИРА (лог только когда реально нашли) ---
                need_orientir = baseline_vol * self.CONFIG['ELEVATED_VOL_MULT']
                if is_red and c_vol >= need_orientir:
                    self.tracker_vol = c_vol
                    self.state = "WAIT_START"
                    self.history_log = f"Фон:{self._fmt(baseline_vol)} -> Ориентир:{self._fmt(c_vol)}"
                    self._record_event("ORIENTIR", c_close)
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"Ориентир: объём {self._fmt(c_vol)} (порог >= {self._fmt(need_orientir)}, фон {self._fmt(baseline_vol)})")
                return None

            elif self.state == "WAIT_START":
                # --- 3. ПОИСК СТАРТА (строго больше ориентира — эскалация) ---
                need_start = self.tracker_vol
                if is_red and c_vol > need_start:
                    self.start_vol = c_vol
                    self.state = "WAIT_PEAK"
                    self.history_log += f" -> Старт:{self._fmt(c_vol)}"
                    self._record_event("START", c_close)
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"Старт: объём {self._fmt(c_vol)} (порог > {self._fmt(need_start)})")
                return None

            elif self.state == "WAIT_PEAK":
                # --- 4. ПОИСК ПЕРВОГО ПИКА (строго больше старта — эскалация) ---
                # Первый пик — только точка отсчёта (конец лестницы разгона),
                # выкуп на нём НЕ проверяется. Сразу идём искать следующий,
                # более сильный/сопоставимый пик (блок 6) — вот у НЕГО уже
                # будет проверка одной свечи на выкуп.
                need_peak = self.start_vol * (self.CONFIG['PEAK_TOLERANCE_PCT'] / 100.0)

                if is_red and c_vol >= need_peak:
                    self.cand_low = c_low
                    # Если объем меньше старта, эталоном для будущего выкупа все равно оставляем старт (чтобы не занижать планку для зеленой свечи)
                    self.cand_vol = c_vol if c_vol > self.start_vol else self.start_vol
                    self.state = "WAIT_NEW_PEAK"
                    self.peak_count = 1
                    self.history_log += f" -> Пик {self.peak_count}:{self._fmt(c_vol)}"
                    self._record_event("PEAK", c_close)
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"Пик {self.peak_count}: объём {self._fmt(c_vol)} (порог >= {self._fmt(need_peak)})")
                return None

            elif self.state == "WAIT_GREEN":
                # --- 5. ПРОВЕРКА ВЫКУПА (РОВНО ОДНА свеча сразу после пика) ---
                if c_close > c_open:
                    need_green = self.cand_vol * (self.CONFIG['VOL_MATCH_PCT'] / 100.0)
                    if c_vol >= need_green:
                        # Идеальный выкуп! ВХОД! (лог входа — в _enter())
                        return self._enter(c_close, c_vol, all_opposite_levels, need_green)
                    # зелёная, но объёма не хватило — попытка мимо, без лога
                    self.state = "WAIT_NEW_PEAK"
                else:
                    need_peak_next = self.cand_vol * (self.CONFIG['PEAK_TOLERANCE_PCT'] / 100.0)
                    if c_vol >= need_peak_next:
                        # Красная в допуске от пика (или сильнее) — тоже считается
                        # новым пиком, для неё снова проверяется ровно одна
                        # следующая свеча. Эталон поднимаем, только если она
                        # реально БОЛЬШЕ (вниз эталон не ходит).
                        # Дно (для стоп-лосса) всегда обновляем, если оно ниже старого!
                        self.cand_low = min(self.cand_low, c_low)
                        # А вот эталон объема поднимаем только если он реально вырос
                        if c_vol > self.cand_vol:
                            self.cand_vol = c_vol
                        self.peak_count += 1
                        self.history_log += f" -> Пик {self.peak_count}:{self._fmt(c_vol)}"
                        self._record_event("PEAK", c_close)
                        if self.CONFIG.get('DEBUG'):
                            self._dbg(f"Пик {self.peak_count}: объём {self._fmt(c_vol)} (допуск >= {self._fmt(need_peak_next)})")
                    else:
                        # Красная, но не сильнее пика — попытка мимо, без лога
                        self.state = "WAIT_NEW_PEAK"
                return None

            elif self.state == "WAIT_NEW_PEAK":
                # --- 6. ПОИСК НОВОГО ПИКА (после сорванной попытки) ---
                # Пик из памяти не теряется. Ищем красную СИЛЬНЕЕ него —
                # без ограничения по числу свечей на сам поиск. Как только
                # нашли — она становится новым пиком, и для неё снова
                # запускается проверка ровно одной следующей свечи (блок 5).
                need_peak_next = self.cand_vol * (self.CONFIG['PEAK_TOLERANCE_PCT'] / 100.0)
                if is_red and c_vol >= need_peak_next:
                    # Дно всегда обновляем, если упали ниже
                    self.cand_low = min(self.cand_low, c_low)
                    # Объем обновляем только на повышение
                    if c_vol > self.cand_vol:
                        self.cand_vol = c_vol
                    self.state = "WAIT_GREEN"
                    self.peak_count += 1
                    self.history_log += f" -> Пик {self.peak_count}:{self._fmt(c_vol)}"
                    self._record_event("PEAK", c_close)
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"Пик {self.peak_count}: объём {self._fmt(c_vol)} (допуск >= {self._fmt(need_peak_next)})")
                return None

        elif self.trade_type == 'SHORT':
            return None

        return None

    def _enter(self, c_close, c_vol, all_opposite_levels, need_vol=None):
        # --- ВХОД ---
        self.state = "TRIGGERED"
        actual_entry = c_close
        actual_sl = self.cand_low * 0.998

        risk_data, err = calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            self._record_event("DEAD", actual_entry)
            if self.CONFIG.get('DEBUG'):
                self._dbg(f"❌ [ВХОД ОТКЛОНЁН] entry={actual_entry:.4f} sl={actual_sl:.4f} -> {err}")
            return {'error': err or "Risk data is None"}

        self.entry_price = actual_entry
        self.sl_price = risk_data['sl']

        self._record_event("ENTRY", actual_entry)
        # reason_str — компактная цепочка для колонки "Метод" в таблице
        # сделок (фронт), не для лог-файла — там она печатается только
        # ОДИН раз на сделку, поэтому копить её в history_log весь путь
        # вотчера не мешает (в отличие от _dbg ниже, где так делать не надо).
        self.history_log += f" -> Зел:{self._fmt(c_vol)}(ВХОД!)"
        reason_str = f"V-Дно [{self.history_log}]"

        if self.CONFIG.get('DEBUG'):
            vol_part = f" (порог >= {self._fmt(need_vol)})" if need_vol is not None else ""
            self._dbg(f"🚀 [ВХОД] объём {self._fmt(c_vol)}{vol_part} entry={actual_entry:.4f} sl={risk_data['sl']:.4f} tp={risk_data['tp']:.4f}")

        return {"action": "BUY", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}