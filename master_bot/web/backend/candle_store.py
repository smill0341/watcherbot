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
import datetime

# candles.db переехал в общую накопительную папку (modules/cryptano/database/),
# вместе с levels_timeline_*.json — путь считаем напрямую (без импорта
# modules.cryptano.*), этот файл остаётся самодостаточным для дашборда,
# как и было.
DB_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "modules", "cryptano", "database", "candles.db",
))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

BACKFILL_DAYS = 60
BACKFILL_DAYS_MAP = {
    "15m": 60,
    "1h": 180,
    "4h": 180,
    "1d": None,  # None означает "копать всю историю до листинга"
    "1w": None,  
    "1M": None   
}
EXCHANGE_MAX_LIMIT = 999
REQUEST_DELAY_SEC = 0.7    

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
            # Новая таблица для памяти о дне (защита от лишних запросов)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS genesis_flags (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    last_check INTEGER NOT NULL,
                    PRIMARY KEY (symbol, timeframe)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

def _get_genesis_check(symbol, timeframe):
    conn = _get_conn()
    try:
        row = conn.execute("SELECT last_check FROM genesis_flags WHERE symbol=? AND timeframe=?", (symbol, timeframe)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def _set_genesis_check(symbol, timeframe):
    conn = _get_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO genesis_flags (symbol, timeframe, last_check) VALUES (?, ?, ?)", (symbol, timeframe, int(time.time())))
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
    Использует обратную пагинацию (от новых свечей к старым), чтобы
    корректно загружать новые монеты, история которых короче BACKFILL_DAYS.
    """
    if days is None:
        days = BACKFILL_DAYS_MAP.get(timeframe, BACKFILL_DAYS)
        
    if days is None:
        target_since_ms = 0
    else:
        target_since_ms = int((time.time() - days * 86400) * 1000)
        
    current_until_ms = int(time.time() * 1000)
    total_downloaded = 0

    while True:
        try:
            # Идем назад: запрашиваем свечи ДО current_until_ms, не используя since
            batch = exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=None, limit=EXCHANGE_MAX_LIMIT, 
                params={"until": current_until_ms}
            )
        except Exception as e:
            print(f"⚠️ [candle_store] backfill {symbol} {timeframe}: {e}")
            break
            
        if not batch:
            if days is None:
                _set_genesis_check(symbol, timeframe)
            break
            
        _insert_candles(symbol, timeframe, batch)
        oldest_in_batch = batch[0][0]
        total_downloaded += len(batch)
        print(f"🔄 Стягиваю историю {symbol} ({timeframe}): скачано {total_downloaded} свечей...")
        
        # Если биржа отдала меньше лимита — мы уперлись в момент листинга монеты
        if len(batch) < EXCHANGE_MAX_LIMIT:
            if days is None:
                _set_genesis_check(symbol, timeframe)
            break
            
        # Если самая старая свеча в скачанной пачке зашла за лимит дней — стоп
        if days is not None and oldest_in_batch <= target_since_ms:
            break
            
        # Сдвигаем границу поиска на время старейшей свечи минус 1мс для следующего запроса
        current_until_ms = oldest_in_batch - 1
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
    """Гибридная докачка: малые ТФ до лимита дней, старшие ТФ — до упора с флагом на 30 дней."""
    target_days = BACKFILL_DAYS_MAP.get(timeframe, BACKFILL_DAYS)
    is_infinite = target_days is None
    
    # 1. Проверяем флаг дна (только для больших ТФ)
    skip_deep = False
    if is_infinite:
        last_check = _get_genesis_check(symbol, timeframe)
        if time.time() - last_check < 30 * 86400:
            skip_deep = True
        target_since_ms = 0
    else:
        target_since_ms = int((time.time() - target_days * 86400) * 1000)

    last_ts = _last_timestamp(symbol, timeframe)
    tf_ms = TIMEFRAME_MS.get(timeframe, 15 * 60 * 1000)

    # 2. Если база пустая — делаем первый стартовый запрос (самые свежие 999 свечей)
    if last_ts is None:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=None, limit=EXCHANGE_MAX_LIMIT)
            if batch:
                _insert_candles(symbol, timeframe, batch)
                last_ts = batch[-1][0] // 1000
        except Exception as e:
            print(f"⚠️ [candle_store] init fetch {symbol} {timeframe}: {e}")
            return
            
    if last_ts is None:
        return

    # 3. Качаем ВПЕРЕД (новые свечи от последней сохраненной до сейчас)
    since_ms = (last_ts * 1000) + tf_ms
    while since_ms < time.time() * 1000:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=EXCHANGE_MAX_LIMIT)
        except Exception as e:
            print(f"⚠️ [candle_store] forward fetch {symbol} {timeframe}: {e}")
            break
        if not batch:
            break
            
        _insert_candles(symbol, timeframe, batch)
        last_fetched_ts = batch[-1][0]
        
        if len(batch) < EXCHANGE_MAX_LIMIT or last_fetched_ts + tf_ms > time.time() * 1000:
            break
            
        since_ms = last_fetched_ts + tf_ms
        time.sleep(REQUEST_DELAY_SEC)

    # 4. Качаем В ПРОШЛОЕ (копаем историю)
    if skip_deep:
        return  # Дно проверено менее 30 дней назад, экономим запросы

    first_ts = _first_timestamp(symbol, timeframe)
    if not first_ts:
        return
        
    current_until_ms = first_ts * 1000
    
    # Если это младший ТФ и мы уже скачали его норму в днях — выходим
    if not is_infinite and current_until_ms <= target_since_ms + tf_ms:
        return

    while True:
        try:
            # Идем назад: запрашиваем 999 свечей ДО current_until_ms
            batch = exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=None, limit=EXCHANGE_MAX_LIMIT, 
                params={"until": int(current_until_ms - 1)}
            )
        except Exception as e:
            print(f"⚠️ [candle_store] past backfill {symbol} {timeframe}: {e}")
            break
            
        if not batch:
            # Биржа вернула пустоту - это листинг
            if is_infinite:
                _set_genesis_check(symbol, timeframe)
            break
            
        _insert_candles(symbol, timeframe, batch)
        oldest_in_batch = batch[0][0]
        
        # Если биржа отдала меньше 999 свечей — мы ударились в листинг прямо внутри этой пачки
        if len(batch) < EXCHANGE_MAX_LIMIT:
            if is_infinite:
                _set_genesis_check(symbol, timeframe)
            break
            
        # Для младших ТФ: если самая старая свеча в пачке зашла за лимит дней — стоп
        if not is_infinite and oldest_in_batch <= target_since_ms:
            break
            
        # Шагаем дальше в прошлое
        current_until_ms = oldest_in_batch
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


def last_candle_age_seconds(symbol, timeframe):
    """Сколько секунд назад была последняя ЛОКАЛЬНО сохранённая свеча — без
    похода в сеть (простой SQLite MAX(timestamp)). None, если для этой
    монеты вообще нет данных. Используется bulk-симулятором (см.
    bounce_simulate.run_bulk_bounce_simulation), чтобы решить, нужна ли
    докачка хвоста ИМЕННО этой монете, а не гонять top_up_tail() по всем
    подряд — большинство активно отслеживаемых ботом монет и так свежие."""
    ts = _last_timestamp(symbol, timeframe)
    if ts is None:
        return None
    return time.time() - ts


def cleanup_redundant_spot():
    """Одноразовая миграционная чистка (не для регулярного вызова из
    cleanup_old): убирает спот-серии (символ БЕЗ ':USDT', например
    'BCH/USDT'), для которых в базе уже есть своп-версия того же символа
    ('BCH/USDT:USDT') на том же таймфрейме. После правки resolve_symbol()
    (своп теперь пробуется первым) такие спот-строки больше никогда не
    запрашиваются и не докачиваются — просто занимают место.

    Спот для монет, у которых своп НЕ листингован на Bybit, не трогает —
    там спот по-прежнему единственный источник (resolve_symbol падает на
    него как на запасной вариант), удалять нечего.

    Возвращает (удалено_пар, оставлено_спот_без_свопа) для отчёта."""
    with _lock:
        conn = _get_conn()
        try:
            pairs = conn.execute("SELECT DISTINCT symbol, timeframe FROM candles").fetchall()
            spot_pairs = [(s, tf) for s, tf in pairs if ":USDT" not in s]
            removed = 0
            kept = 0
            for symbol, timeframe in spot_pairs:
                swap_symbol = f"{symbol}:USDT"
                has_swap = conn.execute(
                    "SELECT 1 FROM candles WHERE symbol=? AND timeframe=? LIMIT 1",
                    (swap_symbol, timeframe),
                ).fetchone()
                if has_swap:
                    conn.execute(
                        "DELETE FROM candles WHERE symbol=? AND timeframe=?",
                        (symbol, timeframe),
                    )
                    removed += 1
                    print(f"🧹 [candle_store] Удалил спот {symbol} ({timeframe}) — есть своп {swap_symbol}")
                else:
                    kept += 1
            conn.commit()
            print(f"🧹 [candle_store] Готово: удалено {removed} спот-серий, оставлено {kept} "
                  f"(своп для них не листингован, спот там по-прежнему нужен)")
            return removed, kept
        finally:
            conn.close()


def cleanup_old():
    """Чистит только те таймфреймы, у которых задан жесткий лимит в днях."""
    with _lock:
        conn = _get_conn()
        try:
            pairs = conn.execute("SELECT DISTINCT symbol, timeframe FROM candles").fetchall()
            for symbol, timeframe in pairs:
                days = BACKFILL_DAYS_MAP.get(timeframe, BACKFILL_DAYS)
                if days is None:
                    continue  # Для старших ТФ не удаляем ничего
                
                cutoff = int(time.time() - days * 86400)
                conn.execute(
                    "DELETE FROM candles WHERE symbol=? AND timeframe=? AND timestamp < ?",
                    (symbol, timeframe, cutoff),
                )
            conn.commit()
        finally:
            conn.close()