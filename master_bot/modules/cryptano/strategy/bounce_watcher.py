# -*- coding: utf-8 -*-

from typing import Optional
import os


class BounceWatcher:

    # Набор (папка, монета), для которых в ЭТОМ запуске процесса уже была
    # первая запись в лог — используется только для маркера "процесс
    # перезапущен" и для ротации (см. _dbg), НЕ для стирания файла: бот
    # перезапускается много раз в день, лог должен переживать рестарт целиком.
    # Ключ включает папку (не просто монету), чтобы симулятор (своя папка,
    # см. log_dir_override) не путался с боевым логом той же монеты.
    _initialized_log_coins = set()
    _created_log_dirs = set()  # какие папки уже точно существуют на диске (os.makedirs один раз на папку)
    _LOG_DIR = "bounce_logs"  # относительно рабочей директории процесса (master_bot/)
    _MAX_LOG_LINES = 5000     # потолок на файл — при частых рестартах не даём ему расти бесконечно

    CONFIG = {
        'IGNORE_POC_LEVELS': False,  # True = полностью игнорировать POC-уровни
        'MIN_SCORE': 1.0,          
        'VOL_SPIKE_MULT': 3.0,     
        'MIN_VOL_MULT_TO_LOG': 1.5,   # Фильтр мусора: не рисовать SCAN и не писать лог, если объем ниже х1.5
        'MAX_RUNAWAY_PCT': 5.0,       # После пробоя: если цена ушла дальше этого % от уровня — отбой, вотчер умирает сам
        'MIN_BODY_PCT': 40.0,         # Плотность свечи: тело должно занимать минимум 40% от всего размаха
        'MAX_WICKS_PCT': 60.0,        # Защита от отвержения: верхняя тень (для лонга) не больше 30%
        'FIXED_TP_PCT': 7.0,
        'SL_PCT': 50.0,  # держим для отображения/справки — НЕ закрывает сделку (см. history.py::update_open_signals,
                          # на тестах реального стоп-лосса не было, закрытие только по TP или по MAX_HOLD_DAYS).
        'MAX_HOLD_DAYS': 14,  # закрыть по текущей цене, если TP так и не дошёл за это время (см. history.py::update_open_signals)
        'MAX_TRADES_PER_PIERCE': 1,   # сколько сделок разрешено на ОДИН пробой, пока не случится новый
        'MAX_TRADES_PER_LEVEL': 1,    # сколько сделок разрешено за всю жизнь уровня (0 = без лимита). Пробоев может
                                        # быть сколько угодно — сами по себе они бесплатны, считаются только сделки.
        'KILL_ON_TELEPORT_OPEN': False,  # хоронить вотчер, если он появился УЖЕ пробитым (цена открытия по ту
                                          # сторону уровня на первой же свече жизни вотчера). False — не хоронить
                                          # и не считать эту свечу пробоем вообще: просто пропустить её и следить
                                          # дальше как обычно, честный пробой засчитается позже своим чередом.
        'SHORT_CLIMAX_MODE': False,     # SHORT больше не зеркалит LONG: вместо входа на первом касании
                                          # resistance ждём, пока цена решительно (закрытием) пробьёт ЭТУ зону
                                          # (ближнюю), а следом и следующую resistance выше неё (дальнюю) — это
                                          # подтверждает сильный, возможно перегретый рывок. Вход ищем только
                                          # выше дальней зоны, когда цена ушла от неё ещё на CLIMAX_ATR_BUFFER
                                          # ATR (не просто высунулась). Если нет дальней зоны выше — не торгуем
                                          # вообще (ждём, пока появится). False — старое зеркальное поведение.
        'CLIMAX_ATR_BUFFER': 2.0,      # см. SHORT_CLIMAX_MODE — множитель ATR над дальней зоной, стартовое
                                          # значение, откалибровать по логам (см. историю обсуждения проекта)
        'DEBUG': True,
    }

    def __init__(self, level_min: float, level_max: float, trade_type: str,
                 config_overrides: Optional[dict] = None, mode: Optional[str] = None,
                 coin: str = "UNKNOWN", level_date=None, log_dir_override: Optional[str] = None):
        # Своя копия CONFIG на КАЖДЫЙ вотчер, а не общий класс-атрибут — иначе
        # два вотчера с разными режимами (climax/mirror) на одной и той же
        # монете в одном процессе читали бы ОДНО и то же значение
        # SHORT_CLIMAX_MODE и вели бы себя одинаково. mode — просто метка для
        # лога/фокуса ('CLIMAX'/'MIRROR'/None), сама логика идёт через CONFIG.
        self.CONFIG = {**BounceWatcher.CONFIG, **(config_overrides or {})}
        self.mode = mode
        # В общем реестре bounce_mgr (один инстанс на ВСЕ монеты, как и у
        # VBottomManager) вотчеры разных монет ничем, кроме min/max, не
        # разделены — coin нужен для рескана по монете, экспорта на дашборд
        # и восстановления после рестарта (см. BounceManager.load_state).
        self.coin = coin
        # Дата ФОРМИРОВАНИЯ уровня (из macro_levels.json, lvl['date']) — не
        # момент начала слежки (это позже, при реальном касании). Заморожена
        # один раз при рождении, той же логикой, что min/max — чтобы линия
        # уровня на графике начиналась там, где уровень реально появился.
        self.level_date = level_date
        # Своя папка логов — ТОЛЬКО для этого вотчера, не трогает
        # BounceWatcher._LOG_DIR (общий атрибут класса, используемый боевыми
        # вотчерами). Нужно симулятору (modules/cryptano/simulator/) — иначе
        # его прогоны писали бы в те же bounce_logs/*.log, что и живой бот,
        # смешивая боевые и тестовые записи в одном файле.
        self._log_dir_override = log_dir_override

        self.min = level_min
        self.max = level_max
        self.trade_type = trade_type
        self.zone_mid = (level_min + level_max) / 2.0
        self.state = "SCANNING"
        self._last_time = None
        self.last_event_time = None
        self.last_event_type = None
        # Путь вотчера для отрисовки на графике дашборда — та же идея, что
        # у V_BOTTOM/VGB/VRT (event_log + _record_event), раньше у BOUNCE
        # этого не было вообще, поэтому на графике не было ни одной точки.
        self.event_log = []
        # Причина смерти для панели "ПОСЛЕДНИЙ СКАН" (watcher_history.json) —
        # раньше у BounceWatcher такого поля не было вообще, поэтому карточка
        # DEAD всегда показывала "нет лога", какой бы ни была причина.
        # Записывается ровно в тех же местах, что и _dbg() при убийстве.
        self.history_log = ""
        self._started_logged = False
        self.trades_count = 0          # всего сделок за всю жизнь уровня (для лога/статистики)
        self.trades_since_pierce = 0   # сделок с момента ПОСЛЕДНЕГО пробоя — сбрасывается на новом пробое
        self.pierce_count = 0          # сколько раз этот уровень вообще был пробит
        self.last_pierce_time = None   # когда был последний пробой — для выбора "кто сейчас в фокусе"
        self._awaiting_recovery = False  # True = уровень появился уже пробитым (телепорт), ждём честного
                                          # реклейма (закрытие обратно за уровень), прежде чем считать пробои
        self.edge_touched = False  # LONG/MIRROR/SHORT-MIRROR: коснулись ближнего края полосы (не середины) —
                                     # первый, синий шаг; отдельно от currently_pierced (50%, оранжевый)
        self._edge_touch_logged = False  # точку/лог КРАЙ ЗОНЫ пишем только 1 раз за всю жизнь вотчера —
                                          # см. комментарий у места использования ниже

        # --- SHORT climax mode (см. CONFIG['SHORT_CLIMAX_MODE']) ---
        self.paired_level = None  # дальняя resistance-зона выше этой — задаётся менеджером при создании,
                                    # None = либо LONG (не участвует), либо для SHORT нет зоны выше = не торгуем
        self.climax_stage = None  # None -> 'WAIT_NEAR' -> 'WAIT_FAR' -> 'WAIT_BUFFER' -> 'SEARCHING'
        if self.trade_type == 'SHORT' and self.CONFIG.get('SHORT_CLIMAX_MODE'):
            self.climax_stage = 'WAIT_NEAR'
        self._step1_logged = False  # ШАГ 1 (climax_stage WAIT_NEAR->WAIT_MAX) — точку/лог пишем
                                     # только 1 раз за жизнь вотчера, см. место использования ниже
        self._step2_logged = False  # то же самое для ШАГ 2 (WAIT_MAX->WAIT_BUFFER)
        self._step3_logged = False  # то же самое для ШАГ 3 (WAIT_BUFFER->SEARCHING, "старт поиска")

    def reset_for_rescan(self):
        """Откатывает вотчера к состоянию 'только родился' — ТОТ ЖЕ объект,
        тот же level_id/coin/min/max/trade_type/mode (identity не трогаем),
        но весь накопленный за жизнь прогресс — с нуля.

        Раньше ручной рескан убивал вотчера и создавал нового — а level_id
        строится от точных float min/max, которые слегка дрожат между
        пересчётами macro_levels.json. Итог: два почти одинаковых уровня
        рядом, оба "живые", кружки на графике задваивались. Теперь рескан
        просто отматывает ПАМЯТЬ этого же объекта назад — дальше реплей
        (см. watcher_plan.py::check_bounce) кормит его старыми свечами как
        обычно, никакого нового id, никакого дубля зоны.

        Список полей — 1-в-1 то же самое, что выставляется в __init__ для
        всего, что накапливается за жизнь (не identity)."""
        self.state = "SCANNING"
        self.last_event_time = None
        self.last_event_type = None
        self.event_log = []
        self.history_log = ""
        self._started_logged = False
        self.trades_count = 0
        self.trades_since_pierce = 0
        self.pierce_count = 0
        self.last_pierce_time = None
        self._awaiting_recovery = False
        self.edge_touched = False
        self._edge_touch_logged = False
        self.climax_stage = None
        if self.trade_type == 'SHORT' and self.CONFIG.get('SHORT_CLIMAX_MODE'):
            self.climax_stage = 'WAIT_NEAR'
        self._step1_logged = False
        self._step2_logged = False
        self._step3_logged = False
        self._last_event_price = None
        self.pierced_bottom = False
        self.currently_pierced = False
        self.currently_runaway = False
        self._last_time = None

    def on_breach_start(self):
        if self.state not in ("DEAD", "TRIGGERED"):
            self.state = "SCANNING"

    def _dbg(self, msg):
        if self.CONFIG.get('DEBUG'):
            # Симулятор передаёт свою папку (см. __init__/log_dir_override) —
            # чтобы его прогоны не писали в те же bounce_logs/*.log, что и
            # живой бот. Ключ "первого касания"/ротации — по (папка, монета),
            # не просто по монете, иначе симулятор и бой в одном процессе
            # путали бы друг друга.
            log_dir = self._log_dir_override or BounceWatcher._LOG_DIR
            log_dir_key = (log_dir, self.coin)

            if log_dir not in BounceWatcher._created_log_dirs:
                os.makedirs(log_dir, exist_ok=True)
                BounceWatcher._created_log_dirs.add(log_dir)
            log_path = os.path.join(log_dir, f"{self.coin}.log")

            first_touch_this_run = log_dir_key not in BounceWatcher._initialized_log_coins
            if first_touch_this_run:
                BounceWatcher._initialized_log_coins.add(log_dir_key)
                # Ротация ВМЕСТО стирания: бот перезапускается много раз в день,
                # лог должен пережить рестарт целиком, но не расти бесконечно —
                # при первом обращении в новом запуске подрезаем файл до
                # последних _MAX_LOG_LINES строк, если он уже существует.
                if os.path.exists(log_path):
                    try:
                        with open(log_path, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                        if len(lines) > BounceWatcher._MAX_LOG_LINES:
                            with open(log_path, "w", encoding="utf-8") as f:
                                f.writelines(lines[-BounceWatcher._MAX_LOG_LINES:])
                    except Exception:
                        pass  # ротация не должна ронять сам лог

            # Переводим в киевское время (то же, что показывает график —
            # см. tickMarkFormatter в app.js) — раньше писали чистый UTC,
            # приходилось пересчитывать в уме, чтобы сверить лог с графиком.
            time_str = ""
            if self._last_time is not None:
                try:
                    time_str = f"{self._last_time.tz_convert('Europe/Kyiv').strftime('%Y-%m-%d %H:%M:%S')} "
                except Exception:
                    time_str = f"{self._last_time} "  # на случай неожиданного формата — не роняем лог
            mode_tag = f" {self.mode}" if getattr(self, 'mode', None) else ""
            tag = " OD" if getattr(self, 'reborn', False) else ""
            with open(log_path, "a", encoding="utf-8") as f:
                if first_touch_this_run:
                    f.write(f"=== BOUNCE лог для {self.coin} — процесс перезапущен, история сохранена ===\n")
                f.write(f"{time_str}[{self.trade_type} {self.min:.4f}-{self.max:.4f}{mode_tag}{tag}] {msg}\n")

    @staticmethod
    def _fmt(v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000: return f"{v/1_000:.1f}k"
        return str(int(v))

    def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, level_score=0, candle_time=None, vol_90=0.0, is_focus=True, c_atr=0.0):
        """
        Тонкая обёртка над _update_impl() — сама логика ниже НЕ менялась ни
        строкой, только вынесена в отдельный метод. Обёртка нужна, чтобы
        событие писалось РОВНО в одном месте, независимо от того, каким из
        множества internal return-путей (телепорт/реклейм/climax-ветка/
        обычный путь) закончился конкретный вызов — раньше пришлось бы
        вставлять _record_event() в каждый return по отдельности.
        """
        is_first_call = not self._started_logged
        result = self._update_impl(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels,
            level_score, candle_time, vol_90, is_focus, c_atr
        )
        if is_first_call:
            mode_label = self.mode if self.mode else "-"
            self._dbg(f"🆕 Новый вотчер | режим: {mode_label}")
            # START на график НЕ пишем (убрали по просьбе) — он не нёс
            # самостоятельного смысла (просто "тронули зону фитилём") и на
            # первой же свече жизни вотчера часто совпадал по времени с
            # реальным первым событием (CLIMAX_NEAR_BREACH/SWEEP_BOTTOM),
            # давая на графике задвоенную точку без дополнительного смысла.
            # Текстовый лог (_dbg выше) — отдельно, туда факт рождения
            # по-прежнему пишется, просто не превращается в точку на графике.
            self._started_logged = True
        if self.last_event_type:
            event_price = self._last_event_price if self._last_event_price is not None else c_close
            self._record_event(self.last_event_type, event_price)
        return result

    def _record_event(self, event_type, price):
        """Копит точки пути вотчера для отрисовки на графике дашборда —
        1-в-1 та же схема, что у VBottomWatcher._record_event (см.
        v_bottom_watcher.py): время сразу конвертируем в unix-секунды,
        иначе pandas.Timestamp тихо ломает json.dump при экспорте."""
        t = self._last_time
        if t is not None and hasattr(t, "timestamp"):
            t = int(t.timestamp())
        self.event_log.append({"time": t, "type": event_type, "price": price})
        if len(self.event_log) > 100:
            self.event_log = self.event_log[-100:]

    def _update_impl(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, level_score=0, candle_time=None, vol_90=0.0, is_focus=True, c_atr=0.0):
        is_first_candle = (self._last_time is None)
        
        self._last_time = candle_time
        self.last_event_time = candle_time
        self.last_event_type = None
        # Цена для точки на графике — по умолчанию None (обёртка update()
        # тогда возьмёт c_close). Явно переопределяем ниже только там, где
        # реальное условие завязано на фитиль (хай/лой), а не на закрытие —
        # иначе точка рисуется на цене закрытия, хотя сработало не оно.
        self._last_event_price = None

        if self.state in ("TRIGGERED", "DEAD"):
            return None

        # ПЕРЕКЛЮЧАТЕЛЬ: Полностью отключаем только 4h_poc_standalone, если тумблер включен
        if self.CONFIG.get('IGNORE_POC_LEVELS', True):
            if '4h_poc_standalone' in getattr(self, 'level_type', ''):
                return None
            
        if level_score < self.CONFIG['MIN_SCORE']:
            return None

        c_open, c_high, c_low, c_close, c_vol = map(float, (c_open, c_high, c_low, c_close, c_vol))

        # --- SHORT CLIMAX MODE: полностью отдельный путь, не зеркалит LONG ---
        # см. CONFIG['SHORT_CLIMAX_MODE'] и _update_climax_short(). LONG эту
        # ветку никогда не видит (climax_stage=None для LONG), весь код ниже
        # для LONG работает один в один как раньше.
        if self.trade_type == 'SHORT' and self.climax_stage is not None:
            return self._update_climax_short(
                c_open, c_high, c_low, c_close, c_vol, baseline_vol,
                candle_time, vol_90, is_focus, c_atr
            )

        # --- ЗАЩИТА ОТ ФАЛЬШИВОГО ПРОБОЯ (ТЕЛЕПОРТАЦИИ БАЗЫ) ---
        # Если уровень только появился в памяти бота, но свеча УЖЕ открылась
        # с пробитой стороны — по умолчанию (KILL_ON_TELEPORT_OPEN=True) хороним
        # его сразу, чтобы не словить сделку-мираж. Если выключено — НЕ хороним,
        # а ставим флаг ожидания честного реклейма (см. ниже): пока цена не
        # закроется обратно ЗА уровень хотя бы раз, вообще ничего не считаем
        # пробоем — просто наблюдаем молча.
        if is_first_candle:
            teleported = (self.trade_type == 'LONG' and c_open < self.min) or \
                         (self.trade_type == 'SHORT' and c_open > self.max)
            if teleported:
                if self.CONFIG.get('KILL_ON_TELEPORT_OPEN', True):
                    self.state = "DEAD"
                    self.last_event_type = "RUNAWAY"
                    if self.trade_type == 'LONG':
                        self.history_log = f"Уровень появился УЖЕ пробитым (Open {c_open:.4f} < Min {self.min:.4f})."
                        self._dbg(f"🔴 ОТМЕНА | {self.history_log}")
                    else:
                        self.history_log = f"Уровень появился УЖЕ пробитым (Open {c_open:.4f} > Max {self.max:.4f})."
                        self._dbg(f"🔴 ОТМЕНА | {self.history_log}")
                    return None
                else:
                    self._awaiting_recovery = True
                    self._dbg(f"⚪ ТЕЛЕПОРТ | Уровень появился уже пробитым (Open {c_open:.4f}) — ждём честного реклейма, пока не считаем пробоем")
                    return None

        # --- ОЖИДАНИЕ РЕКЛЕЙМА ПОСЛЕ ТЕЛЕПОРТА ---
        # Пока цена ни разу не закрылась обратно ЗА уровень — вообще ничего не
        # считаем (ни пробой, ни отбой), просто ждём. Как только реклейм
        # случился — снимаем флаг, и со СЛЕДУЮЩЕЙ свечи всё работает как обычно
        # (сам реклейм-close ещё не считается пробоем).
        if self._awaiting_recovery:
            if self.trade_type == 'LONG':
                if c_close > self.min:
                    self._awaiting_recovery = False
                    self._dbg(f"🟢 РЕКЛЕЙМ | Закрытие вернулось выше уровня ({c_close:.4f} > {self.min:.4f}) — с этого момента честные пробои считаются")
            else:
                if c_close < self.max:
                    self._awaiting_recovery = False
                    self._dbg(f"🔴 РЕКЛЕЙМ | Закрытие вернулось ниже уровня ({c_close:.4f} < {self.max:.4f}) — с этого момента честные пробои считаются")
            return None

        # --- СБОР СТАТИСТИКИ: Прокол дна (Sweep) ---
        if not hasattr(self, 'pierced_bottom'):
            self.pierced_bottom = False
            self.currently_pierced = False
            self.currently_runaway = False

        new_pierce_this_candle = False  # только что случился НОВЫЙ пробой именно на этой свече
        was_pierced_bottom_before = self.pierced_bottom  # до проверки ниже — уже искали вход по этому уровню?

        # ВИЛКА: POC торгуется от края, всё остальное — от середины зоны
        is_poc = '4h_poc_standalone' in getattr(self, 'level_type', '')
        trigger_long = self.min if is_poc else self.zone_mid
        trigger_short = self.max if is_poc else self.zone_mid

        # === ЖЁСТКАЯ СМЕРТЬ ПО СЛЕДУЮЩЕМУ УРОВНЮ (только SHORT — self.paired_level
        # считается только для SHORT, см. bounce_manager.py::evaluate_bounce) ===
        # Если цена дотянулась до СЛЕДУЮЩЕЙ resistance-зоны выше ровно ТЕМ ЖЕ
        # порогом, которым любой уровень вообще считается "пробитым" (край для
        # POC, середина для всех остальных — та же формула, что чуть выше для
        # trigger_short) — следующий уровень пробит настолько же честно, как и
        # любой другой уровень пробивается для входа. Значит этот, нижний,
        # уровень уже позади рынка — умирает НАВСЕГДА, не мягкий сброс, как
        # обычный ОТБОЙ ниже (тот прощает случайную тень, эта проверка — нет,
        # раз дотянулись до середины СЛЕДУЮЩЕЙ зоны, это не случайность).
        if self.trade_type == 'SHORT' and self.paired_level:
            pl = self.paired_level
            pl_is_poc = '4h_poc_standalone' in pl.get('type', '')
            pl_trigger = pl['min'] if pl_is_poc else (pl['min'] + pl['max']) / 2.0
            if c_high >= pl_trigger:
                self.state = "DEAD"
                self.last_event_type = "RUNAWAY"
                self.history_log = (
                    f"Цена пробила следующий уровень выше ({c_high:.4f} >= {pl_trigger:.4f}, "
                    "той же формулой, что и обычный вход) — этот уровень позади рынка, убит навсегда."
                )
                self._dbg(f"🔴 СМЕРТЬ (след. уровень пробит) | {self.history_log}")
                return None

        # --- НОВЫЙ ПЕРВЫЙ ШАГ: коснулись БЛИЖНЕГО КРАЯ полосы (не середины) ---
        # Для LONG цена падает сверху вниз в зону — ближний край self.max.
        # Для SHORT цена растёт снизу вверх в зону — ближний край self.min.
        # edge_touched сам по себе — мигающий флаг (сбрасывается, как только
        # цена выходит обратно за край), это нормально и нужно ниже для
        # других целей. А вот ТОЧКУ/ЛОГ пишем только на первое касание за
        # всю жизнь вотчера (_edge_touch_logged) — если цена долго топчется
        # прямо у края зоны (обычное дело для support/resistance), без этого
        # каждый повторный заход рисовал бы новую синюю точку и новую строку
        # в логе — та же болезнь, что чинили для оранжевого пробоя.
        if self.trade_type == 'LONG':
            edge_touched_now = c_low <= self.max
        else:
            edge_touched_now = c_high >= self.min
        if edge_touched_now:
            if not self.edge_touched:
                self.edge_touched = True
                if not self._edge_touch_logged:
                    self._edge_touch_logged = True
                    self.last_event_type = "ZONE_TOUCH"
                    # Условие тут по фитилю (c_low/c_high), не по закрытию —
                    # точка на графике должна стоять на реальной цене касания.
                    self._last_event_price = c_low if self.trade_type == 'LONG' else c_high
                    self._dbg(f"🔵 КРАЙ ЗОНЫ | Коснулись ближнего края полосы ({self.max if self.trade_type == 'LONG' else self.min:.4f})")
        else:
            self.edge_touched = False

        if self.trade_type == 'LONG':
            if c_low <= trigger_long:
                self.pierced_bottom = True  # Глобальный флаг для отчета (навсегда)
                if not self.currently_pierced:
                    self.currently_pierced = True
                    new_pierce_this_candle = True
            else:
                self.currently_pierced = False  # Цена полностью поднялась над линией (c_low > min)

        elif self.trade_type == 'SHORT':
            if c_high >= trigger_short:
                self.pierced_bottom = True
                if not self.currently_pierced:
                    self.currently_pierced = True
                    new_pierce_this_candle = True
            else:
                self.currently_pierced = False
        # -------------------------------------------

        # --- ОТБОЙ: цена ушла слишком далеко в другую сторону — эту конкретную
        # попытку не считаем сделкой, но и не хороним уровень навсегда: пробой,
        # не давший сделки, бесплатен, просто ждём следующий. Событие/лог — один
        # раз на начало ухода, не на каждой свече, пока он длится. ---
        runaway_this_candle = False
        runaway_direction = None  # 'up' | 'down' — куда именно ушла цена, для лога
        if self.pierced_bottom:
            # Раньше LONG проверял только уход ВВЕРХ (цена отскочила и убежала
            # мимо входа — упущенный бонус), а SHORT только уход ВНИЗ (то же
            # самое, зеркально). Уход в ПРОТИВОПОЛОЖНУЮ, плохую сторону — цена
            # проходит саму зону насквозь и улетает дальше (для LONG — вниз,
            # пробивая поддержку; для SHORT — вверх, пробивая сопротивление и
            # соседние уровни выше) — вообще не проверялся, пробой оставался
            # "живым" сколь угодно долго и на любом расстоянии от зоны.
            # Теперь проверяем оба предела независимо от trade_type.
            runaway_limit_up = self.max * (1 + self.CONFIG['MAX_RUNAWAY_PCT'] / 100.0)
            runaway_limit_down = self.min * (1 - self.CONFIG['MAX_RUNAWAY_PCT'] / 100.0)
            if c_close > runaway_limit_up:
                runaway_this_candle = True
                runaway_direction = 'up'
                runaway_limit = runaway_limit_up
            elif c_close < runaway_limit_down:
                runaway_this_candle = True
                runaway_direction = 'down'
                runaway_limit = runaway_limit_down

        if new_pierce_this_candle and runaway_this_candle:
            # Пробой и отбой случились на ОДНОЙ и той же свече (глубокий фитиль,
            # моментальный отскок за пределы лимита) — реального окна для входа
            # никогда не существовало. Не считаем это пробоем для статистики/
            # фокуса (pierce_count, last_pierce_time, событие SWEEP_BOTTOM), но
            # pierced_bottom остаётся True — честный будущий реклейм всё ещё разрешён.
            self.currently_pierced = False  # эта "попытка" пробоя отменена, ждём следующую как новую
        elif new_pierce_this_candle and not was_pierced_bottom_before:
            # not was_pierced_bottom_before — точку/лог/номер пишем только на
            # НАСТОЯЩИЙ первый пробой этого сетапа. Раньше это условие не
            # стояло — new_pierce_this_candle взводится на КАЖДОМ повторном
            # заходе цены при пиле вокруг середины зоны (currently_pierced
            # мигает), и каждый такой заход рисовал новую точку и писал новую
            # строку "ПРОКОЛ #N" — при живой пиле получалась куча точек
            # вплотную друг к другу и нечитаемый лог, хотя по факту это всё
            # ещё один и тот же, первый и единственный пробой.
            self.pierce_count += 1  # только для лога/нумерации, ничего не ограничивает
            self.trades_since_pierce = 0  # новый пробой — бюджет сделок на него свежий
            self.last_pierce_time = candle_time
            self.last_event_type = "SWEEP_BOTTOM"  # Рисуем точку только 1 раз на прокол
            self._last_event_price = c_low if self.trade_type == 'LONG' else c_high
            # Порог, который реально был пробит именно СЕЙЧАС: для POC-уровней
            # это край зоны (self.min/self.max), для всех остальных — всегда
            # середина зоны (zone_mid), край тут вообще не участвует. Раньше
            # лог всегда писал "ПРОКОЛ ДНА/ХАЯ" с приписком "(середина зоны,
            # если не POC)" — двусмысленно и читалось как "пробит край уровня".
            threshold_label = "край зоны (POC)" if is_poc else "50% зоны"
            if self.trade_type == 'LONG':
                self._dbg(f"🟠 ПРОКОЛ {threshold_label} (#{self.pierce_count}) | Лой свечи: {c_low:.4f} <= Порог: {trigger_long:.4f}")
            else:
                self._dbg(f"🟠 ПРОКОЛ {threshold_label} (#{self.pierce_count}) | Хай свечи: {c_high:.4f} >= Порог: {trigger_short:.4f}")

        if runaway_this_candle:
            if not self.currently_runaway:
                self.currently_runaway = True
                self.last_event_type = "RUNAWAY"
                # Этот пробой отменён — цена улетела слишком далеко. Раньше
                # pierced_bottom оставался True навсегда, и любая ПОЗЖЕ зелёная
                # свеча (хоть через несколько дней, хоть на совсем другом
                # падении) засчитывалась как вход по этому старому пробою.
                # Теперь честно требуем НОВЫЙ пробой (c_low <= self.min) прежде
                # чем снова разрешать вход.
                self.pierced_bottom = False
                self.currently_pierced = False
                if runaway_direction == 'up':
                    self._dbg(f"🟠 ОТБОЙ (пробой #{self.pierce_count} не реализован) | Цена ушла слишком высоко: {c_close:.4f} > лимит {runaway_limit:.4f} | Пробой аннулирован, жду новый")
                else:
                    self._dbg(f"🟠 ОТБОЙ (пробой #{self.pierce_count} не реализован) | Цена ушла слишком низко: {c_close:.4f} < лимит {runaway_limit:.4f} | Пробой аннулирован, жду новый")
            return None
        else:
            self.currently_runaway = False
        # -------------------------------------------

        # Свеча, которую нельзя засчитывать как вход, — ТОЛЬКО самая первая
        # свеча этого сетапа (та, что реально впервые пробила уровень).
        # Раньше тут использовался new_pierce_this_candle, который заново
        # взводится КАЖДЫЙ РАЗ, когда currently_pierced переключается
        # False -> True — а это происходит на любом повторном заходе цены
        # обратно за середину зоны при пиле, даже если pierced_bottom уже
        # был True и вход всё это время искался честно. Из-за этого при
        # пиле вокруг середины зоны каждая свеча-заход ошибочно считалась
        # "свечой пробоя" и целиком выпадала из поиска входа — вход не
        # находился НИКОГДА, сколько бы красных/зелёных свечей ни прошло.
        first_pierce_this_candle = new_pierce_this_candle and not was_pierced_bottom_before

        
        # Для математики входа используем 90-й перцентиль (vol_90)
        logic_vol = vol_90 if vol_90 > 0 else baseline_vol
        vol_mult = (c_vol / logic_vol) if logic_vol > 0 else 0.0
        
        is_green = c_close > c_open
        is_red = c_close < c_open

        v_str = self._fmt(c_vol)

        # Высчитываем анатомию свечи
        hl = float(c_high - c_low)
        body = abs(float(c_close - c_open))
        top_shadow = float(c_high - max(c_open, c_close))
        bottom_shadow = min(c_open, c_close) - float(c_low)
        
        body_pct = (body / hl * 100.0) if hl > 0 else 0.0
        top_shadow_pct = (top_shadow / hl * 100.0) if hl > 0 else 0.0
        bottom_shadow_pct = (bottom_shadow / hl * 100.0) if hl > 0 else 0.0

        if self.trade_type == 'LONG':
            max_per_pierce = self.CONFIG.get('MAX_TRADES_PER_PIERCE', 1)
            is_pierce_budget_ok = (max_per_pierce <= 0) or (self.trades_since_pierce < max_per_pierce)

            if self.pierced_bottom and is_green and not first_pierce_this_candle:
                # not first_pierce_this_candle — свеча САМОГО ПЕРВОГО пробоя не
                # может быть свечой входа: пробой только открывает поиск,
                # отбойная свеча ищется НАЧИНАЯ со следующей свечи после
                # пробоя. Любой повторный заход цены за середину зоны при
                # пиле (currently_pierced True->False->True) НЕ считается
                # новым пробоем для этой проверки — вход по-прежнему ищется.
                # Фильтр мусора: игнорим всё, что ниже MIN_VOL_MULT_TO_LOG
                if vol_mult >= self.CONFIG['MIN_VOL_MULT_TO_LOG']:
                    self.last_event_type = "SCAN"
                    
                    is_vol_ok = vol_mult >= self.CONFIG['VOL_SPIKE_MULT']
                    is_body_ok = body_pct >= self.CONFIG['MIN_BODY_PCT']
                    is_shadow_ok = top_shadow_pct <= self.CONFIG['MAX_WICKS_PCT']
                    
                    if is_vol_ok and is_body_ok and is_shadow_ok and is_pierce_budget_ok and is_focus:
                        self.last_event_type = "GOOD_GREEN"
                        return self._enter(c_close, vol_mult, c_vol, logic_vol, baseline_vol)
                    else:
                        # Собираем причины отказа в строку
                        fail_reasons = []
                        if not is_focus: fail_reasons.append("не в фокусе (пробит более свежий уровень)")
                        if not is_pierce_budget_ok: fail_reasons.append(f"Бюджет пробоя #{self.pierce_count} исчерпан, жду новый")
                        if not is_vol_ok: fail_reasons.append(f"V:x{vol_mult:.1f}(<{self.CONFIG['VOL_SPIKE_MULT']})")
                        if not is_body_ok: fail_reasons.append(f"Тело:{body_pct:.0f}%(<{self.CONFIG['MIN_BODY_PCT']}%)")
                        if not is_shadow_ok: fail_reasons.append(f"В.Тень:{top_shadow_pct:.0f}%(>{self.CONFIG['MAX_WICKS_PCT']}%)")
                        
                        self._dbg(f"🟡 ПРОПУСК | ЗЕЛЕНАЯ | {', '.join(fail_reasons)} | V:{v_str}")

        elif self.trade_type == 'SHORT':
            max_per_pierce = self.CONFIG.get('MAX_TRADES_PER_PIERCE', 1)
            is_pierce_budget_ok = (max_per_pierce <= 0) or (self.trades_since_pierce < max_per_pierce)

            if self.pierced_bottom and is_red and not first_pierce_this_candle:
                # not first_pierce_this_candle — свеча САМОГО ПЕРВОГО пробоя
                # (хай впервые коснулся середины/края зоны) не может быть
                # свечой входа: пробой только открывает поиск, отбойная свеча
                # ищется со следующей свечи. Повторные заходы при пиле вокруг
                # середины зоны (currently_pierced мигает) вход не блокируют.
                if vol_mult >= self.CONFIG['MIN_VOL_MULT_TO_LOG']:
                    self.last_event_type = "SCAN"
                    
                    is_vol_ok = vol_mult >= self.CONFIG['VOL_SPIKE_MULT']
                    is_body_ok = body_pct >= self.CONFIG['MIN_BODY_PCT']
                    is_shadow_ok = bottom_shadow_pct <= self.CONFIG['MAX_WICKS_PCT']
                    
                    if is_vol_ok and is_body_ok and is_shadow_ok and is_pierce_budget_ok and is_focus:
                        self.last_event_type = "GOOD_RED"
                        return self._enter(c_close, vol_mult, c_vol, logic_vol, baseline_vol)
                    else:
                        fail_reasons = []
                        if not is_focus: fail_reasons.append("не в фокусе (пробит более свежий уровень)")
                        if not is_pierce_budget_ok: fail_reasons.append(f"Бюджет пробоя #{self.pierce_count} исчерпан, жду новый")
                        if not is_vol_ok: fail_reasons.append(f"V:x{vol_mult:.1f}(<{self.CONFIG['VOL_SPIKE_MULT']})")
                        if not is_body_ok: fail_reasons.append(f"Тело:{body_pct:.0f}%(<{self.CONFIG['MIN_BODY_PCT']}%)")
                        if not is_shadow_ok: fail_reasons.append(f"Н.Тень:{bottom_shadow_pct:.0f}%(>{self.CONFIG['MAX_WICKS_PCT']}%)")
                        
                        self._dbg(f"🟡 ПРОПУСК | КРАСНАЯ | {', '.join(fail_reasons)} | V:{v_str}")

        return None

    def _update_climax_short(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol,
                                candle_time, vol_90, is_focus, c_atr):
            
            # 1. ЖЕСТКАЯ СМЕРТЬ (Глубокое падение на X ATR) — только если сетап
            # уже РЕАЛЬНО прошёл ВСЮ зону (обе границы, т.е. дошёл минимум до
            # WAIT_BUFFER). Пока вотчер ещё тестирует нижнюю/верхнюю границу
            # (WAIT_NEAR/WAIT_MAX) — откат вниз это НЕ провал сетапа, а
            # нормальная часть его формирования; тихий мягкий сброс (ниже)
            # это уже обрабатывает сам, без убийства. Убивать нужно только
            # тогда, когда уровень доказал себя (прошёл насквозь), сделки не
            # случилось, и цена всё равно ушла вниз — вот тогда сетап реально
            # не сработал.
            if self.climax_stage in ('WAIT_BUFFER', 'SEARCHING'):
                max_drop_limit = self.min - (c_atr * self.CONFIG.get('MAX_DROP_ATR_UNDER_LEVEL', 3.0))
                if c_close < max_drop_limit:
                    self.state = "DEAD"
                    self.history_log = "Уровень прошёл всю зону, сделки не было, цена ушла глубоко вниз (>3 ATR). Сетап убит."
                    if is_focus:
                        self.last_event_type = "RUNAWAY"
                        self._dbg(f"🔴 ОТМЕНА | {self.history_log}")
                    return None

            # 2. МЯГКИЙ СБРОС (Для 2-й сделки или отката)
            # Тихо обнуляем прогресс, без логов и точек.
            if self.climax_stage != 'WAIT_NEAR' and c_close < self.min:
                self.climax_stage = 'WAIT_NEAR'
                return None

            # 3. ШАГ 1: закрытие выше нижней границы зоны (не касание фитилем —
            # иначе эта точка выполнялась бы мгновенно, тем же самым условием,
            # что уже проверил внешний фильтр touched_short в process_candle).
            # Цена может много раз откатываться под self.min и возвращаться
            # (мягкий сброс выше — это нормально, часть формирования сетапа),
            # поэтому climax_stage честно переключается WAIT_NEAR->WAIT_MAX
            # каждый раз — а вот точку/лог пишем только на самый первый раз
            # за всю жизнь вотчера (_step1_logged), иначе цена, топчущаяся
            # у нижней границы много часов подряд, рисовала бы новую точку
            # на каждом возврате — та же болезнь, что чинили для ZONE_TOUCH/
            # SWEEP_BOTTOM.
            if self.climax_stage == 'WAIT_NEAR':
                if c_close >= self.min:
                    self.climax_stage = 'WAIT_MAX'
                    if not self._step1_logged:
                        self._step1_logged = True
                        self.last_event_type = "CLIMAX_NEAR_BREACH"
                        self._dbg(f"🔵 ШАГ 1 | Закрытие выше нижней границы ({self.min:.4f})")
                return None

            # 4. ШАГ 2: закрытие выше верхней границы зоны (тоже не фитиль —
            # иначе одна свеча с длинной тенью могла бы пробить сразу и это,
            # и буфер ATR ниже, без реального удержания цены наверху).
            # Тот же мягкий сброс выше может откатить climax_stage обратно в
            # WAIT_NEAR с ЛЮБОЙ более поздней стадии (в том числе с WAIT_MAX
            # и дальше) — значит эта проверка тоже может отработать не один
            # раз за жизнь вотчера. Точку/лог — только на первый раз.
            if self.climax_stage == 'WAIT_MAX':
                if c_close >= self.max:
                    self.climax_stage = 'WAIT_BUFFER'
                    if not self._step2_logged:
                        self._step2_logged = True
                        self.last_event_type = "CLIMAX_FAR_BREACH"
                        self._dbg(f"🔵 ШАГ 2 | Закрытие выше верхней границы ({self.max:.4f})")
                return None

            # 5. ШАГ 3 (Уход на ATR) — тоже закрытием, не фитилём.
            # Фиксация нового лидера. Счётчик пробоев/бюджет сделок обновляем
            # КАЖДЫЙ раз (это реальный повторный заход в поиск после отката —
            # справедливо считать его новой попыткой), а точку/лог — только
            # на первый раз за жизнь вотчера, по той же причине, что и выше.
            if self.climax_stage == 'WAIT_BUFFER':
                buffer_limit = self.max + (c_atr * self.CONFIG.get('CLIMAX_ATR_BUFFER', 2.0))
                if c_close >= buffer_limit:
                    self.climax_stage = 'SEARCHING'
                    self.pierce_count += 1
                    self.trades_since_pierce = 0
                    self.last_pierce_time = candle_time
                    if not self._step3_logged:
                        self._step3_logged = True
                        self.last_event_type = "SWEEP_BOTTOM"
                        self._dbg(f"🔵 ШАГ 3 | Закрытие выше буфера ({buffer_limit:.4f}). Старт поиска.")
                return None

            # 6. ПОИСК ВХОДА
            if self.climax_stage == 'SEARCHING':
                if c_close <= self.max: return None
                
                is_red = c_close < c_open
                logic_vol = vol_90 if vol_90 > 0 else baseline_vol
                vol_mult = (c_vol / logic_vol) if logic_vol > 0 else 0.0

                if not is_red or vol_mult < 2.0: return None
                
                # АНТИ-СПАМ: Желтые сканы пишет ТОЛЬКО лидер (is_focus)
                if not is_focus: return None

                hl = float(c_high - c_low)
                body = abs(float(c_close - c_open))
                bottom_shadow = min(c_open, c_close) - float(c_low)
                body_pct = (body / hl * 100.0) if hl > 0 else 0.0
                bottom_shadow_pct = (bottom_shadow / hl * 100.0) if hl > 0 else 0.0

                max_per_pierce = self.CONFIG.get('MAX_TRADES_PER_PIERCE', 1)
                is_pierce_budget_ok = (max_per_pierce <= 0) or (self.trades_since_pierce < max_per_pierce)

                self.last_event_type = "SCAN"
                is_vol_ok = vol_mult >= self.CONFIG['VOL_SPIKE_MULT']
                is_body_ok = body_pct >= self.CONFIG['MIN_BODY_PCT']
                is_shadow_ok = bottom_shadow_pct <= self.CONFIG['MAX_WICKS_PCT']

                if is_vol_ok and is_body_ok and is_shadow_ok and is_pierce_budget_ok:
                    self.last_event_type = "GOOD_RED"
                    decision = self._enter(c_close, vol_mult, c_vol, logic_vol, baseline_vol)
                    
                    # Блокировка: ждем физического падения под зону для 2-й сделки
                    if self.state != "TRIGGERED":
                        self.climax_stage = 'WAIT_RECHARGE'
                        
                    return decision
                else:
                    fail_reasons = []
                    if not is_pierce_budget_ok: fail_reasons.append("лимит исчерпан")
                    if not is_vol_ok: fail_reasons.append(f"V:x{vol_mult:.1f}(<{self.CONFIG['VOL_SPIKE_MULT']})")
                    if not is_body_ok: fail_reasons.append(f"Тело:{body_pct:.0f}%")
                    if not is_shadow_ok: fail_reasons.append(f"Н.Тень:{bottom_shadow_pct:.0f}%")
                    
                    v_str = self._fmt(c_vol)
                    self._dbg(f"🟡 Скан | {', '.join(fail_reasons)} | V:{v_str}")
                    return None

            return None
    def _enter(self, actual_entry, vol_mult, c_vol, logic_vol, baseline_vol):
        tp_pct = self.CONFIG['FIXED_TP_PCT'] / 100.0
        sl_pct = self.CONFIG['SL_PCT'] / 100.0

        if self.trade_type == 'LONG':
            tp = actual_entry * (1 + tp_pct)
            sl = actual_entry * (1 - sl_pct)
        else:
            tp = actual_entry * (1 - tp_pct)
            sl = actual_entry * (1 + sl_pct)

        self.trades_count += 1
        self.trades_since_pierce += 1
        # Реальный вход — самое важное событие для графика, перекрывает
        # GOOD_GREEN/GOOD_RED, выставленный чуть раньше на этой же свече
        # (тот был просто "нашли кандидата", это — "реально вошли").
        self.last_event_type = "ENTRY"
        self._last_event_price = actual_entry

        max_per_pierce = self.CONFIG.get('MAX_TRADES_PER_PIERCE', 1)
        max_trades_total = self.CONFIG.get('MAX_TRADES_PER_LEVEL', 1)

        # Насовсем умираем только когда исчерпан общий лимит сделок за всю жизнь
        # уровня. Пробоев может быть сколько угодно — они сами по себе бесплатны.
        if max_trades_total > 0 and self.trades_count >= max_trades_total:
            self.state = "TRIGGERED"
        else:
            self.state = "SCANNING"

        v_str = self._fmt(c_vol)
        
        # Короткий и чёткий лог: сделка X из общего лимита, в рамках пробоя #N
        reason_str = f"BOUNCE (пробой #{self.pierce_count}, сделка {self.trades_count}/{max_trades_total if max_trades_total > 0 else '∞'}) | V:{v_str} (x{vol_mult:.1f})"
        self._dbg(f"✅ ВХОД: {actual_entry:.4f} | {reason_str}")

        if self.state == "TRIGGERED":
            self._dbg(f"🛑 СТОП-СКАН | Общий лимит сделок исчерпан ({self.trades_count}/{max_trades_total}), уровень закрыт насовсем")

        return {
            "allow": True,
            "level_id": f"{self.min}_{self.max}",
            "action": "BUY" if self.trade_type == 'LONG' else "SELL",
            "entry_price": actual_entry,
            "sl": sl,
            "tp": tp,
            "reason": reason_str,
            "is_real_sweep": False,
            "candles_in_sweep": 0,
            "pierced_bottom": getattr(self, 'pierced_bottom', False),
            "reborn": getattr(self, 'reborn', False),
            "volume": c_vol,
            "volume_mult": vol_mult,
        }