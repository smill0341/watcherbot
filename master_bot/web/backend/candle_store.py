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
  - Чистка: свечи старше RETENTION_DAYS периодически удаляются.
"""

import os
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candles.db")

BACKFILL_DAYS = 60           # ~2 месяца — примерно как глубина в симуляторе
RETENTION_DAYS = 90          # чистка старше этого
EXCHANGE_MAX_LIMIT = 999     # Bybit отдаёт максимум ~999 свечей за один запрос
REQUEST_DELAY_SEC = 0.3      # пауза между запросами к бирже (норма как в остальном боте)

TIMEFRAME_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
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


def backfill_symbol(exchange, symbol, timeframe, days=BACKFILL_DAYS):
    """
    Полная докачка истории на `days` назад, пачками по EXCHANGE_MAX_LIMIT.
    Синхронная и потенциально не быстрая (несколько секунд на монету) —
    вызывать либо из фонового потока, либо один раз лениво по запросу.
    """
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


def top_up_tail(exchange, symbol, timeframe):
    """Докачивает только то, что новее последней сохранённой свечи (1 запрос)."""
    last_ts = _last_timestamp(symbol, timeframe)
    if last_ts is None:
        backfill_symbol(exchange, symbol, timeframe)
        return
    since_ms = last_ts * 1000
    try:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=EXCHANGE_MAX_LIMIT)
    except Exception as e:
        print(f"⚠️ [candle_store] top_up {symbol} {timeframe}: {e}")
        return
    _insert_candles(symbol, timeframe, batch)
    print(f"📥 Докачано {len(batch)} свежих свечей для {symbol} ({timeframe})")


def get_candles(symbol, timeframe, limit=None):
    """Возвращает список dict {time, open, high, low, close, volume}, time — unix-секунды."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT timestamp, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND timeframe=? ORDER BY timestamp ASC",
            (symbol, timeframe),
        ).fetchall()
    finally:
        conn.close()
    if limit:
        rows = rows[-limit:]
    return [
        {"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
        for r in rows
    ]


def has_data(symbol, timeframe):
    return _last_timestamp(symbol, timeframe) is not None


def cleanup_old(days=RETENTION_DAYS):
    cutoff = int(time.time() - days * 86400)
    with _lock:
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM candles WHERE timestamp < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()