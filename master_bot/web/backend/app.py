"""
web/backend/app.py
===================
Минимальный read-only дашборд поверх данных бота.
Ничего не пишет в файлы бота, ничего не импортирует из торговой логики,
кроме готового exchange-инстанса и resolve_symbol (чтобы тикеры совпадали
с тем, что реально использует бот).
"""

import os
import sys
import json
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# --- Гарантируем, что "modules.cryptano..." резолвится независимо от того,
# из какой директории реально запущен uvicorn ---
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))          # .../master_bot/web/backend
WEB_DIR = os.path.dirname(BACKEND_DIR)                            # .../master_bot/web
BASE_DIR = os.path.dirname(WEB_DIR)                                # .../master_bot
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.common import resolve_symbol, KNOWN_TICKER_ALIASES, price_precision_from_market


def _read_json(path: str, default: Any) -> Any:
    """
    Свой маленький safe-reader вместо storage.load_json.
    Причина: load_json в боте типизирован строго под Dict[str, Any],
    а signals.json на диске — список, а не словарь. Чтобы не биться с
    типизацией бота (и не трогать сам storage.py), читаем сами —
    поведение (пустой/битый файл -> default) то же самое.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default

CRYPTANO_DIR = os.path.join(BASE_DIR, "modules", "cryptano")
WATCHLIST_PATH = os.path.join(CRYPTANO_DIR, "watchlist.json")
MACRO_LEVELS_PATH = os.path.join(CRYPTANO_DIR, "macro_levels.json")
SIGNALS_PATH = os.path.join(CRYPTANO_DIR, "signals.json")
ACTIVE_WATCHERS_PATH = os.path.join(CRYPTANO_DIR, "active_watchers.json")

STATIC_DIR = os.path.join(WEB_DIR, "static")

app = FastAPI(title="Watcherbot Dashboard (read-only)")


@app.middleware("http")
async def no_cache_for_api(request, call_next):
    """
    Запрещаем браузеру кэшировать ответы API — это живой дашборд,
    данные должны быть всегда свежие, а не то, что браузер запомнил
    с прошлого захода на тот же URL.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response

# Состояния, которые считаем "просто наблюдение, ничего не пробито":
# у V_BOTTOM это SEARCHING, у V_GREEN_BOTTOM — WAIT_FIRST_DUMP (свои разные state-машины).
IDLE_STATES = {"SEARCHING", "WAIT_FIRST_DUMP"}
# Конечные состояния — вотчер уже отработал, ждёт удаления сборщиком мусора бота.
TERMINAL_STATES = {"TRIGGERED", "DEAD"}

# Простой in-memory кэш свечей: ключ (symbol, timeframe, limit) -> (timestamp, candles).
# Не для защиты биржи (там и так enableRateLimit), а чтобы переключение между уже
# просмотренными монетами/таймфреймами ощущалось мгновенно, без похода в сеть.
_ohlcv_cache: dict = {}
_OHLCV_CACHE_TTL_SEC = 90


@app.on_event("startup")
def _load_markets_on_startup():
    """Загружаем список рынков один раз при старте, чтобы resolve_symbol работал."""
    try:
        exchange.load_markets()
        print(f"[DASHBOARD] Markets loaded: {len(exchange.markets or {})}")
    except Exception as e:
        print(f"[DASHBOARD] ⚠️ Не удалось загрузить markets при старте: {e}")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/watchlist")
def get_watchlist():
    """Весь watchlist как есть."""
    return _read_json(WATCHLIST_PATH, default={})


@app.get("/api/watchlist/active")
def get_active_watchers(all_states: bool = Query(default=False)):
    """
    Вотчеры, у которых уровень уже пробит и идёт поиск точки входа.
    ?all_states=true — debug-режим: показать вообще все вотчеры (включая
    SEARCHING/TRIGGERED/DEAD) с счётчиками по state, чтобы понять,
    работает ли создание вотчеров вообще, или просто нет активных.
    """
    raw = _read_json(ACTIVE_WATCHERS_PATH, default={})

    if all_states:
        counts: dict = {}
        for w in raw.values():
            s = w.get("state", "UNKNOWN")
            counts[s] = counts.get(s, 0) + 1
        items = [{"level_id": lid, **w} for lid, w in raw.items()]
        items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return {"total": len(raw), "counts_by_state": counts, "watchers": items}

    result = []
    for level_id, w in raw.items():
        state = w.get("state")
        if state not in IDLE_STATES and state not in TERMINAL_STATES:
            result.append({"level_id": level_id, **w})
    result.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return result


@app.get("/api/levels/{coin}")
def get_levels(coin: str):
    """Уровни поддержки/сопротивления по монете."""
    macro = _read_json(MACRO_LEVELS_PATH, default={})
    coin = coin.upper().strip()
    data = macro.get(coin)
    if data is None:
        # Пробуем через алиас (например TON -> GRAM)
        alias = KNOWN_TICKER_ALIASES.get(coin)
        if alias:
            data = macro.get(alias)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No levels for {coin}")
    return data


@app.get("/api/signals")
def get_signals(limit: int = Query(default=50, ge=1, le=500)):
    """Последние сработавшие сигналы, свежие сверху."""
    signals = _read_json(SIGNALS_PATH, default=[])
    if not isinstance(signals, list):
        signals = []
    # date хранится как "YYYY-MM-DD HH:MM" — сортируется лексикографически верно
    signals_sorted = sorted(signals, key=lambda s: s.get("date") or "", reverse=True)
    return signals_sorted[:limit]


@app.get("/api/ohlcv/{coin}")
def get_ohlcv(coin: str, timeframe: str = "15m", limit: int = Query(default=200, ge=10, le=1000)):
    """
    Свечи по монете напрямую с биржи, тикер резолвится так же, как у бота.
    Кэшируется на _OHLCV_CACHE_TTL секунд по ключу (symbol, timeframe, limit) —
    переключение туда-обратно между уже открытыми монетами/таймфреймами не
    бьёт биржу заново каждый раз.
    """
    coin = coin.upper().strip()
    try:
        symbol = resolve_symbol(coin, exchange.markets)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"resolve_symbol error: {e}")

    if not symbol:
        raise HTTPException(status_code=404, detail=f"Symbol not found on exchange for {coin}")

    # Точность цены — из реальных данных биржи для этого рынка (важно для
    # монет с ценой << $1, вроде SHIB1000, иначе цены на графике обнуляются).
    market_info = exchange.markets.get(symbol, {}) if exchange.markets else {}
    price_precision = price_precision_from_market(market_info, default=4)

    cache_key = (symbol, timeframe, limit)
    now = time.time()
    cached = _ohlcv_cache.get(cache_key)
    if cached and (now - cached[0]) < _OHLCV_CACHE_TTL_SEC:
        candles = cached[1]
    else:
        try:
            raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"fetch_ohlcv failed for {symbol}: {e}")

        candles = [
            {
                "time": int(c[0] / 1000),  # lightweight-charts ждёт секунды, ccxt отдаёт мс
                "open": c[1],
                "high": c[2],
                "low": c[3],
                "close": c[4],
                "volume": c[5],
            }
            for c in raw
        ]
        _ohlcv_cache[cache_key] = (now, candles)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "price_precision": price_precision,
        "candles": candles,
    }


# ---------------------------------------------------------------------------
# Статика (одностраничный фронт)
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))