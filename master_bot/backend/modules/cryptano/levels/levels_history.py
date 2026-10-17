"""
modules/cryptano/levels_history.py
===================================
История уровней — снимки того, что было в macro_levels.json на каждый
момент пересчёта (два раза в день: 03:05 и 15:05 по Киеву), отдельно от
самого macro_levels.json (тот остаётся "как есть прямо сейчас", для живого
скана — этот модуль его не трогает и не заменяет). Только ЧТЕНИЕ снимков:
пишет их precalc_for_bot.py, боевой бот сюда не обращается.

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
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from backend.modules.cryptano.utils.paths import DATABASE_DIR
from backend.modules.cryptano.utils.storage import load_json

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


# Время пересчёта уровней — то же, что у боевого бота (swing_hunter.py: TIME_ASIAN_CLOSE / TIME_US_OPEN,
# по киевскому времени). Снимки симулятора стоят ровно в эти моменты, ключи хранятся в UTC.
SCAN_TZ = ZoneInfo("Europe/Kyiv")
SCAN_TIMES = ("03:05", "15:05")


def scan_times_utc(day: datetime.date) -> List[datetime.datetime]:
    """Моменты пересчёта уровней за киевский календарный день `day` — наивный UTC."""
    out = []
    for hm in SCAN_TIMES:
        h, m = (int(x) for x in hm.split(":"))
        local = datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=SCAN_TZ)
        out.append(local.astimezone(datetime.timezone.utc).replace(tzinfo=None))
    return out


def snapshot_bucket_start(when: datetime.datetime) -> datetime.datetime:
    """Последний момент пересчёта уровней (03:05 или 15:05 по Киеву), не позже `when` (наивный UTC)
    — ТА ЖЕ сетка, на которой лежат ключи снимков. Используется на запись (ниже) и на чтение
    (bounce_simulate.py — чтобы решить, нужно ли звать get_levels_snapshot() заново для соседней свечи)."""
    local_day = when.replace(tzinfo=datetime.timezone.utc).astimezone(SCAN_TZ).date()
    candidates = scan_times_utc(local_day - datetime.timedelta(days=1)) + scan_times_utc(local_day)
    return max(t for t in candidates if t <= when.replace(microsecond=0))


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