"""
modules/cryptano/levels_history.py
===================================
История уровней — снимки того, что было в macro_levels.json на каждый
момент пересчёта (~раз в 12 часов), отдельно от самого macro_levels.json
(тот остаётся "как есть прямо сейчас", для живого скана — этот модуль его
не трогает и не заменяет).

Нужно для реплея/рескана (см. watcher_plan.py::check_bounce): без этого
прошлые свечи проверялись против СЕГОДНЯШНИХ уровней — а уровень мог за
две недели уехать/пропасть, и сделка, которую реплей "находит" в прошлом,
на самом деле в тот момент могла быть невозможна (уровня ещё/уже не было).

Формат — тот же, что уже используется в отдельном тестере (precalc.py):
один файл на месяц, database/levels_timeline_YYYY_MM.json, словарь
{"YYYY-MM-DD HH:MM:SS": <тот же словарь, что уходит в macro_levels.json>}.
Уже посчитанные тестером файлы можно класть в database/ как есть — этот
модуль читает их как свои, без пересчёта.
"""
import os
import datetime
from typing import Any, Dict, Optional

from modules.cryptano.utils.paths import DATABASE_DIR
from modules.cryptano.utils.storage import load_json, save_json_atomic

TIMELINE_RETENTION_MONTHS = 6  # держим полгода снимков, старше — чистим (см. cleanup_old_timelines)

# Кэш уже распарсенных таймлайнов месяца — {month_label: (mtime, timeline_dict)}.
# Инвалидируется по mtime файла, не по времени/счётчику: если файл на диске не
# менялся, отдаём тот же объект без повторного чтения+json.loads. Для симулятора
# (bounce_simulate.py реплеит тысячи свечей за один прогон, файл всё это время
# статичен) это убирает повторное чтение одного и того же месяца с диска на
# каждый вызов. Для боевого рескана файл может обновиться между вызовами —
# mtime-проверка это ловит корректно, никакого протухшего кэша.
_timeline_file_cache: Dict[str, tuple] = {}


def _timeline_path(month_label: str) -> str:
    return os.path.join(DATABASE_DIR, f"levels_timeline_{month_label}.json")


def _load_timeline_cached(month_label: str) -> Dict[str, Any]:
    path = _timeline_path(month_label)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}  # файла ещё нет — как и раньше, load_json(default={}) вернул бы то же самое

    cached = _timeline_file_cache.get(month_label)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    timeline = load_json(path, default={})
    if not isinstance(timeline, dict):
        timeline = {}
    _timeline_file_cache[month_label] = (mtime, timeline)
    return timeline


def snapshot_bucket_start(when: datetime.datetime) -> datetime.datetime:
    """Начало 12-часового бакета (00:00 или 12:00), на который попадает `when`
    — ТА ЖЕ сетка, на которой save_levels_snapshot() кладёт ключи снимков.
    Вынесено в отдельную функцию и переиспользуется и на запись (ниже), и на
    чтение (bounce_simulate.py — чтобы решить, нужно ли вообще звать
    get_levels_snapshot() заново для соседней свечи), чтобы кэш на стороне
    читателя никогда не мог разъехаться с реальной гранулярностью данных."""
    hour_bucket = 0 if when.hour < 12 else 12
    return when.replace(hour=hour_bucket, minute=0, second=0, microsecond=0)


def save_levels_snapshot(macro_base: Dict[str, Any], when: Optional[datetime.datetime] = None) -> None:
    """Дописывает снимок уровней в таймлайн — вызывать сразу после каждого
    пересчёта swing_hunter, тем же словарём, что ушёл в macro_levels.json
    (см. swing_hunter.py::build_macro_levels).

    Время округляется ВНИЗ до ближайших 00:00/12:00 — та же сетка, на
    которой тестер (precalc.py) строит свои снимки (pd.date_range(...,
    freq='12h') от полуночи), чтобы файлы бота и подложенные вручную файлы
    тестера были на одной временной решётке. Хранится как наивный UTC (без
    tzinfo) — то же самое, что у меток свечей после ccxt/pandas, чтобы
    сравнение в get_levels_snapshot не спотыкалось об разницу поясов."""
    when = when or datetime.datetime.utcnow()
    snapshot_time = snapshot_bucket_start(when)
    time_str = snapshot_time.strftime("%Y-%m-%d %H:%M:%S")
    month_label = snapshot_time.strftime("%Y_%m")

    path = _timeline_path(month_label)
    os.makedirs(DATABASE_DIR, exist_ok=True)
    timeline = load_json(path, default={})
    if not isinstance(timeline, dict):
        timeline = {}
    if time_str in timeline:
        return  # снимок на эту 12-часовую точку в этом скан-цикле уже есть — не дублируем
    timeline[time_str] = macro_base
    save_json_atomic(path, timeline)


def _latest_in_timeline(timeline: Dict[str, Any], before_or_at: Optional[datetime.datetime] = None):
    """Самый свежий снимок в словаре таймлайна — либо вообще самый
    последний по времени (before_or_at=None), либо самый последний СРЕДИ
    тех, что не позже before_or_at."""
    best_time = None
    best_snapshot = None
    for time_str, snapshot in timeline.items():
        try:
            ts = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if before_or_at is not None and ts > before_or_at:
            continue
        if best_time is None or ts > best_time:
            best_time = ts
            best_snapshot = snapshot
    return best_snapshot


def get_levels_snapshot(when: datetime.datetime) -> Optional[Dict[str, Any]]:
    """Ищет снимок уровней, актуальный НА МОМЕНТ `when` — ближайший по
    времени НАЗАД (никогда не заглядывает вперёд — иначе это снова была бы
    подгонка под будущий результат, только чуть более честная на вид).

    Если в файле месяца `when` снимков ещё нет настолько рано (например,
    when — первые часы месяца, а первый снимок появился только к полудню)
    — заглядывает в файл ПРЕДЫДУЩЕГО месяца и берёт его последний снимок
    (уровни не обнуляются 1-го числа, они просто продолжаются).

    Возвращает None, если снимка вообще нигде нет — вызывающий код должен
    честно не реплеить/не заводить новых вотчеров на этом участке, а не
    подставлять текущие уровни как раньше."""
    month_label = when.strftime("%Y_%m")
    timeline = _load_timeline_cached(month_label)
    if isinstance(timeline, dict) and timeline:
        snapshot = _latest_in_timeline(timeline, before_or_at=when)
        if snapshot is not None:
            return snapshot

    prev_month_dt = when.replace(day=1) - datetime.timedelta(days=1)
    prev_timeline = _load_timeline_cached(prev_month_dt.strftime("%Y_%m"))
    if isinstance(prev_timeline, dict) and prev_timeline:
        return _latest_in_timeline(prev_timeline)  # последний снимок предыдущего месяца целиком

    return None


def cleanup_old_timelines(keep_months: int = TIMELINE_RETENTION_MONTHS) -> None:
    """Удаляет файлы таймлайна старше keep_months — та же идея, что
    RETENTION_DAYS у candle_store, только тут по месяцам целиком, а не
    построчно (один файл = один месяц). Вызывать периодически, не на
    каждом скане (например, раз в сутки из background_tasks.py)."""
    if not os.path.isdir(DATABASE_DIR):
        return
    cutoff = datetime.datetime.now() - datetime.timedelta(days=keep_months * 31)
    for fname in os.listdir(DATABASE_DIR):
        if not (fname.startswith("levels_timeline_") and fname.endswith(".json")):
            continue
        label = fname[len("levels_timeline_"):-len(".json")]
        try:
            file_dt = datetime.datetime.strptime(label, "%Y_%m")
        except ValueError:
            continue
        if file_dt < cutoff:
            try:
                os.remove(os.path.join(DATABASE_DIR, fname))
                print(f"🧹 [LEVELS HISTORY] Удалён старый снимок: {fname}")
            except Exception as e:
                print(f"⚠️ [LEVELS HISTORY] Не смог удалить {fname}: {e}")