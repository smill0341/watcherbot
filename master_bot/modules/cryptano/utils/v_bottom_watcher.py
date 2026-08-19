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
"""

from .risk_calc import calc_tp_and_rr


class VBottomWatcher:
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

    def __init__(self, level_min, level_max, trade_type):
        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.state = "SEARCHING"

        self.tracker_vol = 0.0
        self.start_vol = 0.0
        self.cand_low = 0.0
        self.cand_vol = 0.0
        self.breach_count = 0     # сколько заходов под уровень уже было (первый = 0, считаются повторные)
        self.sl_price = None
        self.entry_price = None

        self.history_log = ""
        self._last_time = None
        self.last_event_time = None
        self.last_event_msg = None

    def _tp(self):
        """Строка с временем текущей свечи для debug-принтов, если время передано."""
        return f"{self._last_time} " if self._last_time is not None else ""

    def _dbg(self, msg):
        """Пишет строку в файл v_bottom_debug.log (дописывает), а не в консоль —
        терминал не тянет тысячи строк, файл открывается текстовым редактором
        целиком, ищется по Ctrl+F. Также запоминает событие для авто-маркера
        на графике симулятора (см. test_simulator.py)."""
        self.last_event_time = self._last_time
        self.last_event_msg = msg
        with open("v_bottom_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{self._tp()}[{self.max:.4f}] {msg}\n")

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
        self.history_log = ""

    def on_breach_start(self):
        """Вызывается симулятором в момент НОВОГО пробития уровня вниз
        (создание origin_level_long). На ПЕРВОМ пробитии вотчера ещё нет
        (создаётся свежим при первом evaluate), так что каждый вызов сюда —
        это ПОВТОРНЫЙ заход. Сверх MAX_REBREACHES — уровень мёртв."""
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
                    self._reset_chain()
                return None
            if c_low > self.max:
                # высунулись НАД уровнем, но внутри буфера — просто "дышим",
                # ничего не сбрасываем и не считаем эту свечу (пики/выкуп
                # ищем только строго под уровнем)
                return None

            is_red = c_close < c_open

            if self.state == "SEARCHING":
                # --- 2. ПОИСК ОРИЕНТИРА ---
                if is_red and self.CONFIG.get('DEBUG'):
                    self._dbg(f"[ищем Ориентир] red vol={c_vol:.0f} need>={baseline_vol * self.CONFIG['ELEVATED_VOL_MULT']:.0f} (фон={baseline_vol:.0f})")
                if is_red and c_vol >= (baseline_vol * self.CONFIG['ELEVATED_VOL_MULT']):
                    self.tracker_vol = c_vol
                    self.state = "WAIT_START"
                    self.history_log = f"Фон:{self._fmt(baseline_vol)} -> Ориентир:{self._fmt(c_vol)}"
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(self.history_log)
                return None

            elif self.state == "WAIT_START":
                # --- 3. ПОИСК СТАРТА (строго больше ориентира — эскалация) ---
                if is_red and self.CONFIG.get('DEBUG'):
                    self._dbg(f"[ищем Старт] red vol={c_vol:.0f} need>{self.tracker_vol:.0f}")
                if is_red and c_vol > self.tracker_vol:
                    self.start_vol = c_vol
                    self.state = "WAIT_PEAK"
                    self.history_log += f" -> Старт:{self._fmt(c_vol)}"
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(self.history_log)
                return None

            elif self.state == "WAIT_PEAK":
                # --- 4. ПОИСК ПЕРВОГО ПИКА (строго больше старта — эскалация) ---
                # Первый пик — только точка отсчёта (конец лестницы разгона),
                # выкуп на нём НЕ проверяется. Сразу идём искать следующий,
                # более сильный/сопоставимый пик (блок 6) — вот у НЕГО уже
                # будет проверка одной свечи на выкуп.
                need_peak = self.start_vol * (self.CONFIG['PEAK_TOLERANCE_PCT'] / 100.0)
                
                if is_red and self.CONFIG.get('DEBUG'):
                    self._dbg(f"[ищем Пик1] red vol={c_vol:.0f} need>={need_peak:.0f} (старт={self.start_vol:.0f})")
                    
                if is_red and c_vol >= need_peak:
                    self.cand_low = c_low
                    # Если объем меньше старта, эталоном для будущего выкупа все равно оставляем старт (чтобы не занижать планку для зеленой свечи)
                    self.cand_vol = c_vol if c_vol > self.start_vol else self.start_vol
                    self.state = "WAIT_NEW_PEAK"
                    self.history_log += f" -> Пик:{self._fmt(c_vol)}"
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(self.history_log)
                return None

            elif self.state == "WAIT_GREEN":
                # --- 5. ПРОВЕРКА ВЫКУПА (РОВНО ОДНА свеча сразу после пика) ---
                if c_close > c_open:
                    need_green = self.cand_vol * (self.CONFIG['VOL_MATCH_PCT'] / 100.0)
                    
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"[ТЕСТ ВЫКУПА] Зел. vol={c_vol:.0f}, надо>={need_green:.0f} (эталон={self.cand_vol:.0f})")

                    if c_vol >= need_green:
                        if self.CONFIG.get('DEBUG'):
                            self._dbg(f"🟢 ОБЪЕМ ПРОЙДЕН! Вызываем _enter()")
                        # Идеальный выкуп! ВХОД!
                        return self._enter(c_close, c_vol, all_opposite_levels)
                    
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"🔴 Не хватило объема. Сброс в WAIT_NEW_PEAK.")
                    # зелёная, но объёма не хватило — попытка мимо
                    self.state = "WAIT_NEW_PEAK"
                elif c_vol >= (self.cand_vol * (self.CONFIG['PEAK_TOLERANCE_PCT'] / 100.0)):
                    # Красная в допуске от пика (или сильнее) — тоже считается
                    # новым пиком, для неё снова проверяется ровно одна
                    # следующая свеча. Эталон поднимаем, только если она
                    # реально БОЛЬШЕ (вниз эталон не ходит).
                    # Дно (для стоп-лосса) всегда обновляем, если оно ниже старого!
                    self.cand_low = min(self.cand_low, c_low)
                    # А вот эталон объема поднимаем только если он реально вырос
                    if c_vol > self.cand_vol:
                        self.cand_vol = c_vol
                    self.history_log += f" -> Пик+:{self._fmt(c_vol)}"
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(self.history_log)
                else:
                    # Красная, но не сильнее пика — попытка мимо
                    self.state = "WAIT_NEW_PEAK"
                return None

            elif self.state == "WAIT_NEW_PEAK":
                # --- 6. ПОИСК НОВОГО ПИКА (после сорванной попытки) ---
                # Пик из памяти не теряется. Ищем красную СИЛЬНЕЕ него —
                # без ограничения по числу свечей на сам поиск. Как только
                # нашли — она становится новым пиком, и для неё снова
                # запускается проверка ровно одной следующей свечи (блок 5).
                if is_red and c_vol >= (self.cand_vol * (self.CONFIG['PEAK_TOLERANCE_PCT'] / 100.0)):
                    # Дно всегда обновляем, если упали ниже
                    self.cand_low = min(self.cand_low, c_low)
                    # Объем обновляем только на повышение
                    if c_vol > self.cand_vol:
                        self.cand_vol = c_vol
                    self.state = "WAIT_GREEN"
                    self.history_log += f" -> Пик+:{self._fmt(c_vol)}"
                    if self.CONFIG.get('DEBUG'):
                        self._dbg(f"{self.history_log} (новый пик)")
                return None

        elif self.trade_type == 'SHORT':
            return None

        return None

    def _enter(self, c_close, c_vol, all_opposite_levels):
        # --- ВХОД ---
        self.state = "TRIGGERED"
        actual_entry = c_close
        actual_sl = self.cand_low * 0.998

        if self.CONFIG.get('DEBUG'):
            self._dbg(f"🚪 Попытка входа! Entry: {actual_entry:.4f}, SL: {actual_sl:.4f}")

        risk_data, err = calc_tp_and_rr(actual_entry, actual_sl, self.trade_type, all_opposite_levels, self.CONFIG)
        if err or not risk_data:
            self.state = "DEAD"
            if self.CONFIG.get('DEBUG'):
                self._dbg(f"❌ Калькулятор УБИЛ сделку: {err}")
            return {'error': err or "Risk data is None"}

        self.entry_price = actual_entry
        self.sl_price = risk_data['sl']

        self.history_log += f" -> Зел:{self._fmt(c_vol)}(ВХОД!)"
        reason_str = f"V-Дно [{self.history_log}]"

        if self.CONFIG.get('DEBUG'):
            self._dbg(f"🚀 СДЕЛКА УСПЕШНО СФОРМИРОВАНА: {reason_str}")

        return {"action": "BUY", "entry_price": actual_entry, "sl": risk_data['sl'], "tp": risk_data['tp'], "reason": reason_str}