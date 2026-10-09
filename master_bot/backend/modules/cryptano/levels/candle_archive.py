"""
modules/cryptano/levels/candle_archive.py
=========================================
Своя база свечей ТОЛЬКО для симулятора: database/sim_candles.db, таймфреймы
15m (сам прогон) и 4h (EMA200). Остальное (1d/1W/1M) симулятор по-прежнему
берёт из общей базы candles.db.

Общую базу (candle_store.py), бота и дашборд этот файл не трогает и не
использует: другой файл базы, другие таблицы, ничего общего.

Как работает:
  - Выбрал в симуляторе любой период — ensure_for_sim() сам смотрит, какие
    куски свечей уже есть, и докачивает с биржи ТОЛЬКО недостающее.
  - Глубины и чистки нет: что скачали — лежит, пока сам не удалишь файл базы.
  - Учёт "что уже скачано" — таблица coverage (интервалы, по монете и ТФ).
    Интервал записывается ПОСЛЕ каждой успешно полученной пачки, поэтому
    обрыв посреди докачки не оставляет дыр: следующий запуск продолжит с
    того места, где остановились.
  - Незакрытые свечи не пишутся (только закрытые).
  - Дошли до листинга монеты (биржа вернула меньше лимита) — запоминаем
    первую свечу (таблица listing) и дальше назад эту монету не качаем.

Что нужно симулятору на период [start, end] (константы ниже):
  15m: от start - 7 суток (прогрев индикаторов/окна) до end + MAX_HOLD_DAYS
       (честный исход сделок в конце периода), не позже "сейчас".
  4h : от start - 100 суток (прогрев EMA200: вес стартового значения после
       600 баров ~0.0006) до end.

Докачка делается ТОЛЬКО из одного процесса, последовательно (одна монета за
раз, пауза между запросами) — bulk-симулятор вызывает её в главном процессе
до раздачи монет воркерам; воркеры только читают.
"""

import os
import sqlite3
import threading
import time
import datetime

from backend.modules.cryptano.utils.paths import DATABASE_DIR

DB_PATH = os.path.join(DATABASE_DIR, "sim_candles.db")
os.makedirs(DATABASE_DIR, exist_ok=True)

EXCHANGE_MAX_LIMIT = 999     # потолок Bybit за один запрос (как в candle_store)
REQUEST_DELAY_SEC = 1.0      # пауза между запросами (как в candle_store)

TF_SEC = {"15m": 15 * 60, "4h": 4 * 60 * 60}

SIM_WARMUP_15M_DAYS = 7      # запас назад для 15m
SIM_WARMUP_4H_DAYS = 100     # запас назад для 4h (EMA200)

_lock = threading.RLock()
_schema_ready = False


class ArchiveError(RuntimeError):
    """Не удалось полностью докачать нужный диапазон свечей."""


# ----------------------------------------------------------------------------
# База
# ----------------------------------------------------------------------------

def _get_conn():
    global _schema_ready
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    if not _schema_ready:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, timeframe, timestamp)
            )
            """
        )
        # Уже докачанные интервалы: from_ts..to_ts — время ОТКРЫТИЯ первой и
        # последней свечи интервала включительно (unix-секунды).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS coverage (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                from_ts INTEGER NOT NULL,
                to_ts INTEGER NOT NULL,
                PRIMARY KEY (symbol, timeframe, from_ts)
            )
            """
        )
        # Первая свеча монеты на бирже (дальше назад качать нечего).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listing (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                first_ts INTEGER NOT NULL,
                PRIMARY KEY (symbol, timeframe)
            )
            """
        )
        conn.commit()
        _schema_ready = True
    return conn


def _to_sec(x):
    """pandas.Timestamp / datetime (tz-aware, UTC) / число -> unix-секунды."""
    if hasattr(x, "timestamp"):
        return int(x.timestamp())
    return int(x)


def _align_up(x, step):
    return -(-x // step) * step


def _align_down(x, step):
    return (x // step) * step


def _last_closed_open_ts(step):
    """Время открытия последней ЗАКРЫТОЙ свечи на данный момент."""
    return _align_down(int(time.time()), step) - step


def _fmt(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ----------------------------------------------------------------------------
# Учёт докачанного (coverage / listing)
# ----------------------------------------------------------------------------

def _read_intervals(conn, symbol, timeframe):
    return conn.execute(
        "SELECT from_ts, to_ts FROM coverage WHERE symbol=? AND timeframe=? ORDER BY from_ts",
        (symbol, timeframe),
    ).fetchall()


def _add_interval(conn, symbol, timeframe, from_ts, to_ts, step):
    """Добавляет интервал и склеивает пересекающиеся/соседние."""
    items = sorted(_read_intervals(conn, symbol, timeframe) + [(from_ts, to_ts)])
    merged = []
    for f, t in items:
        if merged and f <= merged[-1][1] + step:
            if t > merged[-1][1]:
                merged[-1] = (merged[-1][0], t)
        else:
            merged.append((f, t))
    conn.execute("DELETE FROM coverage WHERE symbol=? AND timeframe=?", (symbol, timeframe))
    conn.executemany(
        "INSERT INTO coverage (symbol, timeframe, from_ts, to_ts) VALUES (?, ?, ?, ?)",
        [(symbol, timeframe, f, t) for f, t in merged],
    )
    conn.commit()


def _get_listing(conn, symbol, timeframe):
    row = conn.execute(
        "SELECT first_ts FROM listing WHERE symbol=? AND timeframe=?", (symbol, timeframe)
    ).fetchone()
    return row[0] if row else None


def _set_listing(conn, symbol, timeframe, first_ts):
    old = _get_listing(conn, symbol, timeframe)
    if old is None or first_ts < old:
        conn.execute(
            "INSERT OR REPLACE INTO listing (symbol, timeframe, first_ts) VALUES (?, ?, ?)",
            (symbol, timeframe, first_ts),
        )
        conn.commit()


def _missing(a, b, intervals, step):
    """Части [a, b], которых нет в уже докачанных интервалах."""
    out = []
    cursor = a
    for f, t in intervals:
        if t < cursor:
            continue
        if f > b:
            break
        if f > cursor:
            out.append((cursor, min(f - step, b)))
        cursor = max(cursor, t + step)
        if cursor > b:
            break
    if cursor <= b:
        out.append((cursor, b))
    return out


# ----------------------------------------------------------------------------
# Докачка
# ----------------------------------------------------------------------------

def _insert_candles(conn, symbol, timeframe, rows, step):
    """rows — ccxt: [ts_ms, o, h, l, c, v]. Пишем только ЗАКРЫТЫЕ свечи."""
    now = int(time.time())
    data = [
        (symbol, timeframe, int(r[0] // 1000), r[1], r[2], r[3], r[4], r[5])
        for r in rows
        if int(r[0] // 1000) + step <= now
    ]
    if data:
        conn.executemany(
            """INSERT OR REPLACE INTO candles
               (symbol, timeframe, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            data,
        )
        conn.commit()


def _download(exchange, conn, symbol, timeframe, a, b, step):
    """Докачивает [a, b] (время открытия свечей) назад от b пачками по
    EXCHANGE_MAX_LIMIT. Каждая полученная пачка сразу попадает в coverage."""
    print(f"🔄 [archive] {symbol} {timeframe}: качаю {_fmt(a)} .. {_fmt(b)}")
    hi = b                       # верх ещё не засчитанной части
    until_ms = b * 1000 + 1      # свеча с открытием b должна попасть в выдачу
    total = 0
    first_page = True
    while True:
        try:
            batch = exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=None, limit=EXCHANGE_MAX_LIMIT,
                params={"until": until_ms},
            )
        except Exception as e:
            raise ArchiveError(f"{symbol} {timeframe}: ошибка биржи при докачке: {e}")

        if not batch:
            # Пусто на самой первой пачке — не засчитываем (может быть сбой
            # ответа), следующий запуск просто повторит запрос. Пусто на
            # следующих — значит дальше назад ничего нет.
            if not first_page:
                _add_interval(conn, symbol, timeframe, a, hi, step)
            break

        _insert_candles(conn, symbol, timeframe, batch, step)
        total += len(batch)
        oldest = int(batch[0][0] // 1000)

        if len(batch) < EXCHANGE_MAX_LIMIT:
            # Биржа отдала меньше лимита — дошли до листинга монеты.
            _set_listing(conn, symbol, timeframe, oldest)
            _add_interval(conn, symbol, timeframe, a, hi, step)
            break

        if oldest <= a:
            _add_interval(conn, symbol, timeframe, a, hi, step)
            break

        _add_interval(conn, symbol, timeframe, oldest, hi, step)
        hi = oldest - step
        until_ms = oldest * 1000 - 1
        first_page = False
        time.sleep(REQUEST_DELAY_SEC)
    print(f"   ✅ [archive] {symbol} {timeframe}: получено {total} свечей")


def ensure_range(exchange, symbol, timeframe, start, end):
    """Гарантирует, что свечи [start, end] есть в архиве; недостающее
    докачивает. start/end — pandas.Timestamp (tz-aware, UTC) или unix-секунды.
    Конец автоматически ограничен последней ЗАКРЫТОЙ свечой. Если после
    докачки диапазон не закрыт полностью — ArchiveError."""
    step = TF_SEC[timeframe]
    a = _align_up(_to_sec(start), step)
    b = min(_align_down(_to_sec(end), step), _last_closed_open_ts(step))
    if a > b:
        return
    with _lock:
        conn = _get_conn()
        try:
            first = _get_listing(conn, symbol, timeframe)
            if first is not None:
                a = max(a, first)     # раньше листинга свечей не существует
            if a > b:
                return
            for ma, mb in _missing(a, b, _read_intervals(conn, symbol, timeframe), step):
                _download(exchange, conn, symbol, timeframe, ma, mb, step)
            # Проверка: всё ли закрыто
            first = _get_listing(conn, symbol, timeframe)
            if first is not None:
                a = max(a, first)
            left = _missing(a, b, _read_intervals(conn, symbol, timeframe), step) if a <= b else []
            if left:
                raise ArchiveError(
                    f"{symbol} {timeframe}: нет свечей за {_fmt(left[0][0])} .. {_fmt(left[-1][1])}"
                )
        finally:
            conn.close()


# ----------------------------------------------------------------------------
# Чтение
# ----------------------------------------------------------------------------

def get_candles(symbol, timeframe, since=None, until=None):
    """Список dict {time, open, high, low, close, volume} (time — unix-секунды),
    как у candle_store.get_candles. since/until — границы по времени открытия
    свечи, включительно (pandas.Timestamp или unix-секунды); не заданы — всё,
    что есть по монете."""
    q = ("SELECT timestamp, open, high, low, close, volume FROM candles "
         "WHERE symbol=? AND timeframe=?")
    args = [symbol, timeframe]
    if since is not None:
        q += " AND timestamp >= ?"
        args.append(_to_sec(since))
    if until is not None:
        q += " AND timestamp <= ?"
        args.append(_to_sec(until))
    q += " ORDER BY timestamp ASC"
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute(q, args).fetchall()
        finally:
            conn.close()
    return [
        {"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
        for r in rows
    ]


# ----------------------------------------------------------------------------
# То, что нужно симулятору на период
# ----------------------------------------------------------------------------

def sim_window(timeframe, start, end, forward_days):
    """Окно свечей (с, по) в unix-секундах, которое нужно симулятору на период
    [start, end]. forward_days — на сколько дней вперёд после end смотрит
    честный исход сделки (MAX_HOLD_DAYS)."""
    s, e = _to_sec(start), _to_sec(end)
    if timeframe == "15m":
        return s - SIM_WARMUP_15M_DAYS * 86400, e + int(forward_days) * 86400
    if timeframe == "4h":
        return s - SIM_WARMUP_4H_DAYS * 86400, e
    raise ValueError(f"Неизвестный таймфрейм архива: {timeframe}")


def ensure_for_sim(exchange, symbol, start, end, forward_days):
    """Докачивает всё, что нужно симулятору по монете на период [start, end]
    (15m и 4h). ArchiveError — если докачать не удалось."""
    for tf in ("15m", "4h"):
        a, b = sim_window(tf, start, end, forward_days)
        ensure_range(exchange, symbol, tf, a, b)


def get_sim_candles(symbol, timeframe, start, end, forward_days):
    """Свечи из архива в том же окне, что докачивает ensure_for_sim."""
    a, b = sim_window(timeframe, start, end, forward_days)
    return get_candles(symbol, timeframe, since=a, until=b)