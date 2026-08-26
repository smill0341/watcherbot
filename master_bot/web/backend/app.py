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
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))          # .../master_bot/web/backend
WEB_DIR = os.path.dirname(BACKEND_DIR)                            # .../master_bot/web
BASE_DIR = os.path.dirname(WEB_DIR)                                # .../master_bot
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.common import resolve_symbol, KNOWN_TICKER_ALIASES, price_precision_from_market
import candle_store


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
WATCHER_HISTORY_PATH = os.path.join(CRYPTANO_DIR, "watcher_history.json")

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


@app.on_event("startup")
def _load_markets_on_startup():
    """Загружаем список рынков один раз при старте, чтобы resolve_symbol работал."""
    try:
        exchange.load_markets()
        print(f"[DASHBOARD] Markets loaded: {len(exchange.markets or {})}")
    except Exception as e:
        print(f"[DASHBOARD] ⚠️ Не удалось загрузить markets при старте: {e}")

    candle_store.init_db()

    # Фоновая докачка истории по монетам из watchlist. Список читается заново
    # на каждом проходе — если watchlist пополнился/сократился, воркер сам
    # подхватит изменения на следующем цикле, без рестарта дашборда.
    def _candle_backfill_worker():
        WATCHLIST_REFRESH_INTERVAL_SEC = 180
        while True:
            try:
                wl = _read_json(WATCHLIST_PATH, default={})
                coins = list(wl.keys()) if isinstance(wl, dict) else []
                for coin in coins:
                    try:
                        symbol = resolve_symbol(coin, exchange.markets)
                    except Exception:
                        continue
                    if not symbol:
                        continue
                    for tf in candle_store.TIMEFRAME_MS.keys():
                        if candle_store.has_data(symbol, tf):
                            candle_store.top_up_tail(exchange, symbol, tf)
                        else:
                            print(f"[DASHBOARD] Backfill старт: {symbol} {tf} (~{candle_store.BACKFILL_DAYS}д)")
                            candle_store.backfill_symbol(exchange, symbol, tf)
                        time.sleep(candle_store.REQUEST_DELAY_SEC)
                candle_store.cleanup_old()
            except Exception as e:
                print(f"⚠️ [DASHBOARD] candle backfill worker: {e}")
            time.sleep(WATCHLIST_REFRESH_INTERVAL_SEC)

    threading.Thread(target=_candle_backfill_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/watchlist")
def get_watchlist():
    """
    Watchlist, размеченный по наличию уровней в macro_levels.json:
    "with_levels" — монеты, по которым уровни уже построены (можно смотреть
    осмысленно), "without_levels" — ещё нет (обычно временно, до ближайшего
    построения уровней по расписанию/вручную через 'rebuild' в консоли).
    Список каждый раз считается заново от watchlist.json — если он
    поменялся (монета добавилась/ушла, уровни досчитались), это сразу видно.
    """
    wl = _read_json(WATCHLIST_PATH, default={})
    if not isinstance(wl, dict):
        wl = {}
    macro = _read_json(MACRO_LEVELS_PATH, default={})

    with_levels = []
    without_levels = []
    for coin, meta in wl.items():
        has_levels = coin in macro or KNOWN_TICKER_ALIASES.get(coin) in macro
        entry = {"coin": coin, "has_levels": has_levels, **(meta or {})}
        (with_levels if has_levels else without_levels).append(entry)

    with_levels.sort(key=lambda x: x.get("added_at") or "", reverse=True)
    without_levels.sort(key=lambda x: x.get("added_at") or "", reverse=True)

    return {"with_levels": with_levels, "without_levels": without_levels}


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


@app.post("/api/rescan/{coin}")
def trigger_rescan(coin: str):
    """Ставит флаг-заявку на сброс памяти монеты для боевого сканера."""
    coin = coin.upper().strip()
    flag_path = os.path.join(CRYPTANO_DIR, f"rescan_{coin}.flag")
    try:
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write("1")
        return {"status": "ok", "message": f"Rescan requested for {coin}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/history/all")
def get_global_history():
    """Отдает глобальную историю всех умерших/отработавших вотчеров."""
    history_raw = _read_json(WATCHER_HISTORY_PATH, default={})
    if not isinstance(history_raw, dict):
        return []
    
    # Собираем в список и сортируем (самые свежие события сверху)
    history = list(history_raw.values())
    history.sort(key=lambda x: x.get("died_at") or "", reverse=True)
    
    # Отдаем топ-50 последних записей, чтобы не перегружать интерфейс
    return history[:50]

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


@app.get("/api/macro/coins")
def get_macro_coins():
    """
    Список монет, прошедших фильтр по обороту (те же тикеры, что лежат
    ключами в macro_levels.json — их туда кладёт swing_hunter.build_macro_levels,
    топ-70 по объёму). Для панели "Симуляция" — просто список, без расчётов.
    """
    macro = _read_json(MACRO_LEVELS_PATH, default={})
    coins = sorted(c for c in macro.keys() if c != "_meta")
    return {"coins": coins, "count": len(coins)}


@app.post("/api/simulate/{coin}")
def simulate_watcher(coin: str, start: str = Query(..., description="Дата/время старта симуляции, UTC. 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM'")):
    """
    Прогоняет боевые классы вотчеров (VBottomManager + evaluate_v_bottom/
    v_green_bottom/v_red_top — те же, что использует бот, без отдельной
    копии логики) по истории от указанной даты до сейчас.

    Импорт торговой логики — намеренно ЛОКАЛЬНЫЙ (внутри функции, а не
    на верху файла): дашборд в остальном read-only и не тянет модули
    стратегий при обычном старте, симулятор — единственное исключение,
    и то только когда его реально дёрнули.

    Ничего не пишет: результат просто возвращается, боевые
    macro_levels.json / watcher_state.json / tracked_origin_levels*.json
    не трогаются.
    """
    from modules.cryptano.simulator.simulate_engine import run_simulation
    try:
        return run_simulation(coin, start)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка симуляции: {e}")


@app.get("/api/signals")
def get_signals(limit: int = Query(default=50, ge=1, le=500)):
    """Последние сработавшие сигналы, свежие сверху."""
    signals = _read_json(SIGNALS_PATH, default=[])
    if not isinstance(signals, list):
        signals = []
    # date хранится как "YYYY-MM-DD HH:MM" — сортируется лексикографически верно
    signals_sorted = sorted(signals, key=lambda s: s.get("date") or "", reverse=True)
    return signals_sorted[:limit]


@app.get("/api/events/{coin}")
def get_watcher_events(coin: str):
    """
    Путь вотчеров по монете — точки скана для наложения на график.
    "active" — вотчеры, которые прямо сейчас в работе (из active_watchers.json).
    "history" — последний УМЕРШИЙ/СРАБОТАВШИЙ вотчер по каждой стратегии
    (из watcher_history.json, только один слепок на монету+стратегию —
    перезаписывается при следующей смерти по этому же ключу).

    Каждая запись содержит "events": [{"time": unix_seconds, "type": str, "price": float}, ...]
    — time уже в unix-секундах, совместим напрямую с time свечей из /api/ohlcv.
    """
    coin = coin.upper().strip()

    active_raw = _read_json(ACTIVE_WATCHERS_PATH, default={})
    active = [
        {"level_id": lid, **w}
        for lid, w in active_raw.items()
        if (w.get("coin") or "").upper() == coin
    ]

    history_raw = _read_json(WATCHER_HISTORY_PATH, default={})
    history = [
        {"key": key, **w}
        for key, w in history_raw.items()
        if (w.get("coin") or "").upper() == coin
    ]

    return {"coin": coin, "active": active, "history": history}


@app.get("/api/ohlcv/{coin}")
def get_ohlcv(coin: str, timeframe: str = "15m", limit: int = Query(default=200, ge=10, le=6000)):
    """
    Свечи по монете — из локальной SQLite-истории (candle_store), а не
    напрямую с биржи каждый раз. Для монет из watchlist история уже
    докачана фоновым воркером (~2 месяца, см. candle_store.BACKFILL_DAYS).
    Если монеты в базе ещё нет вообще (открыли не-watchlist монету
    напрямую) — докачиваем её здесь же, синхронно: это разовая пауза
    в несколько секунд на первое открытие, дальше она уже в базе.
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

    if timeframe not in candle_store.TIMEFRAME_MS:
        raise HTTPException(status_code=400, detail=f"Unsupported timeframe: {timeframe}")

    if not candle_store.has_data(symbol, timeframe):
        candle_store.backfill_symbol(exchange, symbol, timeframe)
    else:
        # ПРИНУДИТЕЛЬНАЯ ДОКАЧКА: всегда стягиваем хвост до текущей секунды при открытии графика
        candle_store.top_up_tail(exchange, symbol, timeframe)

    candles = candle_store.get_candles(symbol, timeframe, limit=limit)

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