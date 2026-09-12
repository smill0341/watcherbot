"""
web/backend/candle_store.py
============================
SQLite-кэш свечей для дашборда. НЕ трогает торговую логику бота —
работает поверх того же exchange-инстанса, что и остальной backend,
только читает биржу (fetch_ohlcv), в файлы/базу бота ничего не пишет.

Стратегия:
  - Первая докачка по (symbol, timeframe) — ~BACKFILL_DAYS назад пачками
    по EXCHANGE_MAX_LIMIT свечей (потолок Bybit за один запрос), с паузой
    REQUEST_DELAY_SEC между запросами (та же норма, что уже используется
    в остальном боте — см. time.sleep(0.3) в watcher_plan.py/swing_hunter.py).
  - Дальше — только "хвост": докачиваем свечи новее последней сохранённой.
  - Чистка: для каждого (symbol, timeframe) свечи старше её собственного
    окна из BACKFILL_DAYS_MAP периодически удаляются — так база держит
    скользящее окно постоянного размера, а BACKFILL_DAYS_MAP остаётся
    единственным местом, где настраивается глубина истории.
"""

import os
import sqlite3
import threading
import time
import bisect

# candles.db переехал в общую накопительную папку (modules/cryptano/database/),
# вместе с levels_timeline_*.json — путь считаем напрямую (без импорта
# modules.cryptano.*), этот файл остаётся самодостаточным для дашборда,
# как и было.
DB_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "modules", "cryptano", "database", "candles.db",
))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

BACKFILL_DAYS = 60           # Базовое значение (чтобы не сломать принты в app.py)
BACKFILL_DAYS_MAP = {        # Динамическая глубина скачивания для каждого таймфрейма
    "15m": 60,
    "1h": 180,
    "4h": 180,
    "1d": 365,
    "1w": 1460,               
    "1M": 2000               
}
EXCHANGE_MAX_LIMIT = 999
REQUEST_DELAY_SEC = 0.6     

TIMEFRAME_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
    "1w": 7 * 24 * 60 * 60 * 1000,
    "1M": 30 * 24 * 60 * 60 * 1000,
}

_lock = threading.RLock()


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _lock:
        conn = _get_conn()
        try:
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
            conn.commit()
        finally:
            conn.close()


def _insert_candles(symbol, timeframe, rows):
    """rows: список в ccxt-формате [ts_ms, open, high, low, close, volume]."""
    if not rows:
        return
    with _lock:
        conn = _get_conn()
        try:
            conn.executemany(
                """INSERT OR REPLACE INTO candles
                   (symbol, timeframe, timestamp, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (symbol, timeframe, int(r[0] // 1000), r[1], r[2], r[3], r[4], r[5])
                    for r in rows
                ],
            )
            conn.commit()
        finally:
            conn.close()


def backfill_symbol(exchange, symbol, timeframe, days=None):
    """
    Полная докачка истории назад, пачками по EXCHANGE_MAX_LIMIT.
    """
    if days is None:
        days = BACKFILL_DAYS_MAP.get(timeframe, BACKFILL_DAYS)
        
    since_ms = int((time.time() - days * 86400) * 1000)
    tf_ms = TIMEFRAME_MS.get(timeframe, 15 * 60 * 1000)

    while True:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=EXCHANGE_MAX_LIMIT)
        except Exception as e:
            print(f"⚠️ [candle_store] backfill {symbol} {timeframe}: {e}")
            break
        if not batch:
            break
        _insert_candles(symbol, timeframe, batch)
        print(f"🔄 Стягиваю историю {symbol} ({timeframe}): скачано {len(batch)} свечей...")
        last_ts = batch[-1][0]
        if len(batch) < EXCHANGE_MAX_LIMIT or last_ts + tf_ms > time.time() * 1000:
            break  # дошли до текущего момента
        since_ms = last_ts + tf_ms
        time.sleep(REQUEST_DELAY_SEC)


def _last_timestamp(symbol, timeframe):
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(timestamp) FROM candles WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        conn.close()


def _first_timestamp(symbol, timeframe):
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT MIN(timestamp) FROM candles WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        conn.close()


def top_up_tail(exchange, symbol, timeframe):
    """Докачивает новые свечи вперед и недостающую историю назад."""
    last_ts = _last_timestamp(symbol, timeframe)
    if last_ts is None:
        backfill_symbol(exchange, symbol, timeframe)
        return
        
    tf_ms = TIMEFRAME_MS.get(timeframe, 15 * 60 * 1000)
    target_days = BACKFILL_DAYS_MAP.get(timeframe, BACKFILL_DAYS)
    target_since_ms = int((time.time() - target_days * 86400) * 1000)
    
    # 1. Сначала докачиваем новые свечи вперед до текущего момента
    since_ms = last_ts * 1000
    while True:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=EXCHANGE_MAX_LIMIT)
        except Exception as e:
            print(f"⚠️ [candle_store] top_up {symbol} {timeframe}: {e}")
            break
            
        if not batch:
            break
            
        _insert_candles(symbol, timeframe, batch)
        last_fetched_ts = batch[-1][0]
        
        if len(batch) < EXCHANGE_MAX_LIMIT or last_fetched_ts + tf_ms > time.time() * 1000:
            break
            
        since_ms = last_fetched_ts + tf_ms
        time.sleep(REQUEST_DELAY_SEC)

    # 2. Проверяем наличие дыры сзади. Если история короче лимита — докачиваем в прошлое
    first_ts = _first_timestamp(symbol, timeframe)
    if first_ts and (first_ts * 1000) > target_since_ms + tf_ms:
        past_since_ms = target_since_ms
        while past_since_ms < first_ts * 1000:
            try:
                batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=past_since_ms, limit=EXCHANGE_MAX_LIMIT)
            except Exception as e:
                print(f"⚠️ [candle_store] past backfill {symbol} {timeframe}: {e}")
                break
            if not batch:
                break
                
            _insert_candles(symbol, timeframe, batch)
            last_fetched_ts = batch[-1][0]
            
            if len(batch) < EXCHANGE_MAX_LIMIT or last_fetched_ts >= first_ts * 1000:
                break
                
            past_since_ms = last_fetched_ts + tf_ms
            time.sleep(REQUEST_DELAY_SEC)

def get_candles(symbol, timeframe, limit=None, around=None):
    """Возвращает список dict {time, open, high, low, close, volume}, time — unix-секунды.

    limit без around — последние `limit` свечей (как раньше, живой график).
    around (unix-секунды) — окно ВОКРУГ конкретного момента в прошлом:
    примерно half=limit/2 свечей до и after этой точки. Нужно для перехода
    по клику на сигнал/сделку из истории — там время может быть недели
    назад, "последние N" его физически не покажут.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT timestamp, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND timeframe=? ORDER BY timestamp ASC",
            (symbol, timeframe),
        ).fetchall()
    finally:
        conn.close()

    if around is not None and rows:
        timestamps = [r[0] for r in rows]
        idx = bisect.bisect_left(timestamps, around)
        half = (limit or 200) // 2
        start = max(0, idx - half)
        end = min(len(rows), idx + half)
        rows = rows[start:end]
    elif limit:
        rows = rows[-limit:]

    return [
        {"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
        for r in rows
    ]


def has_data(symbol, timeframe):
    return _last_timestamp(symbol, timeframe) is not None


def cleanup_old():
    """Чистит каждую (symbol, timeframe) по её собственному окну из
    BACKFILL_DAYS_MAP — единое место правды для глубины истории.
    Так база держит скользящее окно: top_up_tail() дописывает новые
    свечи вперёд, cleanup_old() отрезает всё, что вышло за окно сзади,
    и общее количество свечей на (symbol, timeframe) остаётся примерно
    постоянным."""
    with _lock:
        conn = _get_conn()
        try:
            pairs = conn.execute(
                "SELECT DISTINCT symbol, timeframe FROM candles"
            ).fetchall()
            for symbol, timeframe in pairs:
                days = BACKFILL_DAYS_MAP.get(timeframe, BACKFILL_DAYS)
                cutoff = int(time.time() - days * 86400)
                conn.execute(
                    "DELETE FROM candles WHERE symbol=? AND timeframe=? AND timestamp < ?",
                    (symbol, timeframe, cutoff),
                )
            conn.commit()
        finally:
            conn.close()