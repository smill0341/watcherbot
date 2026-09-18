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
import datetime
import threading
import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Приглушаем access-лог uvicorn для эндпоинтов, которые фронт опрашивает
# часто (рескан-статус и подобное) — иначе консоль забивается строками
# "GET /api/rescan_status 200 OK" на каждый опрос, за которыми не видно
# реально важных логов (докачка свечей, ошибки и т.д.). Не трогает сами
# запросы — только то, что от них печатается в консоль.
_NOISY_ACCESS_LOG_PATHS = ("/api/rescan_status",)


class _QuietPollingEndpoints(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not any(path in msg for path in _NOISY_ACCESS_LOG_PATHS)


logging.getLogger("uvicorn.access").addFilter(_QuietPollingEndpoints())

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))          # .../master_bot/web/backend
WEB_DIR = os.path.dirname(BACKEND_DIR)                            # .../master_bot/web
BASE_DIR = os.path.dirname(WEB_DIR)                                # .../master_bot
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")               # тот же файл, что main.py (автобот/fasttrade)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.common import resolve_symbol, KNOWN_TICKER_ALIASES, price_precision_from_market
from modules.cryptano.levels_history import get_levels_snapshot
import candle_store

# Стратегии Watcher'а, которые можно вкл/выкл через дашборд — тот же
# набор тегов, что "BC"/"VB"/"VGB"/"VRT" в background_tasks.py::_STRATEGY_BY_TAG.
STRATEGY_TAGS = ("VB", "VGB", "VRT", "BOUNCE")


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


def _write_json_atomic(path: str, data: Any) -> None:
    """Атомарная запись (tmp + os.replace) — та же схема, что
    save_json_atomic у бота, но своя копия, чтобы дашборд не тянул
    модуль бота только ради одной функции (см. docstring _read_json)."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_path, path)

CRYPTANO_DIR = os.path.join(BASE_DIR, "modules", "cryptano")
JSONBANK_DIR = os.path.join(CRYPTANO_DIR, "jsonbank")
WATCHLIST_PATH = os.path.join(JSONBANK_DIR, "watchlist.json")
MACRO_LEVELS_PATH = os.path.join(JSONBANK_DIR, "macro_levels.json")
# Ручные уровни с дашборда — ОТДЕЛЬНЫЙ файл (не внутри macro_levels.json,
# который целиком перезаписывается каждый пересчёт swing_hunter — ручная
# запись там не пережила бы следующий скан). Тот же CUSTOM_LEVELS_FILE,
# что читает движок сканера (watcher_plan.py::get_merged_levels_for_coin).
CUSTOM_LEVELS_PATH = os.path.join(JSONBANK_DIR, "custom_levels.json")
SIGNALS_PATH = os.path.join(JSONBANK_DIR, "signals.json")
FOOTBALL_SIGNALS_PATH = os.path.join(JSONBANK_DIR, "football_signals.json")
FOOTBALL_STATUS_PATH = os.path.join(JSONBANK_DIR, "football_status.json")
NBA_SIGNALS_PATH = os.path.join(JSONBANK_DIR, "nba_signals.json")
NBA_STATUS_PATH = os.path.join(JSONBANK_DIR, "nba_status.json")
RESCAN_STATUS_PATH = os.path.join(JSONBANK_DIR, "rescan_status.json")
ACTIVE_WATCHERS_PATH = os.path.join(JSONBANK_DIR, "active_watchers.json")
WATCHER_HISTORY_PATH = os.path.join(JSONBANK_DIR, "watcher_history.json")

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
IDLE_STATES = {"SEARCHING", "WAIT_FIRST_DUMP", "WAIT_PUMP"}
# Конечные состояния — вотчер уже отработал, ждёт удаления сборщиком мусора бота.
TERMINAL_STATES = {"TRIGGERED", "DEAD"}


@app.on_event("startup")
def _load_markets_on_startup():
    """
    Загружаем список рынков при старте, чтобы resolve_symbol работал.
    Раньше при неудаче (сеть/биржа моргнула) ошибка тихо проглатывалась,
    а дашборд всё равно продолжал запускаться — с пустым списком рынков
    НАВСЕГДА (следующей попытки не было), из-за чего resolve_symbol падал
    на каждой монете до следующего ручного рестарта дашборда. Теперь —
    несколько попыток с паузой, один сетевой сбой не должен ронять всё.
    """
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            exchange.load_markets()
            print(f"[DASHBOARD] Markets loaded: {len(exchange.markets or {})}")
            break
        except Exception as e:
            print(f"[DASHBOARD] ⚠️ Попытка {attempt}/{attempts} загрузить markets не удалась: {e}")
            if attempt < attempts:
                time.sleep(3)
            else:
                print("[DASHBOARD] ❌ Markets не загрузились после всех попыток — график/resolve_symbol не будут работать, пока не перезапустишь дашборд.")

    candle_store.init_db()

    # Фоновая докачка истории по монетам из watchlist.json (сейчас пишет туда
    # только minute_radar в swing_hunter.py — см. get_watchlist()). Список
    # читается заново на каждом проходе — если watchlist пополнился, воркер
    # сам подхватит изменения на следующем цикле, без рестарта дашборда.
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
                            days = candle_store.BACKFILL_DAYS_MAP.get(tf, candle_store.BACKFILL_DAYS)
                            print(f"[DASHBOARD] Backfill старт: {symbol} {tf} (~{days}д)")
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
    осмысленно), "without_levels" — ещё нет. Каждый скан
    (background_tasks.py::crypto_orchestrator) сам добавляет в watchlist.json
    любую монету из macro_levels.json, которой там ещё нет — так что на
    практике тут всегда все топ-70. Отдельно ещё minute_radar (swing_hunter.py)
    и позже ручное добавление/фильтры могут добавлять монеты со своим "source".

    "direction" в watchlist.json — старое поле, оставшееся у записей,
    добавленных ДО того, как синхронизация перестала его писать (скан
    всегда проверяет обе стороны по факту наличия supports/resistances,
    direction на это никогда не влиял). Явно вырезаем его тут, чтобы
    старые залежавшиеся записи не показывали на дашборде то, чего больше
    нет ни в одной новой записи и что реально ни на что не влияет.
    """
    wl = _read_json(WATCHLIST_PATH, default={})
    if not isinstance(wl, dict):
        wl = {}
    macro = _read_json(MACRO_LEVELS_PATH, default={})

    with_levels = []
    without_levels = []
    for coin, meta in wl.items():
        if coin == "_meta":
            continue  # служебный ключ (сюда попасть не должен, но на случай старых прогонов)
        has_levels = coin in macro or KNOWN_TICKER_ALIASES.get(coin) in macro
        clean_meta = {k: v for k, v in (meta or {}).items() if k != "direction"}
        entry = {"coin": coin, "has_levels": has_levels, **clean_meta}
        (with_levels if has_levels else without_levels).append(entry)

    with_levels.sort(key=lambda x: x.get("added_at") or "", reverse=True)
    without_levels.sort(key=lambda x: x.get("added_at") or "", reverse=True)

    return {"with_levels": with_levels, "without_levels": without_levels}


def _is_active_watcher(w: dict) -> bool:
    """
    Единое условие "в работе": не в терминальном состоянии (не было сделки,
    не умер по дальнему уходу цены). Раньше для BOUNCE было отдельное более
    узкое условие (currently_pierced для MIRROR, climax_stage для CLIMAX) —
    это заставляло вотчер мигать туда-сюда в списке "в работе" при каждом
    откате цены через середину зоны, хотя по правилам проекта вотчер должен
    считаться "в работе" непрерывно, пока не случится сделка ИЛИ цена не
    уйдёт далеко от зоны слежения — то же самое условие, что и у остальных
    трёх стратегий (V_BOTTOM/V_GREEN_BOTTOM/V_RED_TOP), никакого отдельного
    случая для BOUNCE больше не требуется.
    """
    state = w.get("state")
    if state in TERMINAL_STATES:
        return False
    if w.get("strategy") == "BOUNCE":
        return True
    return state not in IDLE_STATES


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

    result = [
        {"level_id": level_id, **w}
        for level_id, w in raw.items()
        if _is_active_watcher(w)
    ]
    result.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return result


@app.post("/api/rescan/{coin}")
def trigger_rescan(coin: str, since: Optional[int] = Query(default=None, description="unix-секунды — честно повторить путь монеты (BOUNCE) от этой даты до сейчас. Без даты — дефолт неделя назад (см. background_tasks.py).")):
    """Ставит флаг-заявку на рескан для боевого сканера.

    Раньше флаг был просто маркером "сбрось память V_BOTTOM/VGB/VRT по
    этой монете". Теперь несёт JSON с `since` — для BOUNCE это не "забудь
    и сканируй заново вслепую", а "убей текущих живых вотчеров (заархивируй
    их) и честно пересобери реальный путь монеты от этой даты через уже
    готовый механизм реплея (bounce_mgr.last_processed_time), как будто
    бот сканировал её всё это время."""
    coin = coin.upper().strip()
    flag_path = os.path.join(CRYPTANO_DIR, f"rescan_{coin}.flag")
    try:
        with open(flag_path, "w", encoding="utf-8") as f:
            json.dump({"since": since}, f)
        msg = f"Rescan requested for {coin}"
        if since is not None:
            msg += f" (с {datetime.datetime.utcfromtimestamp(since).strftime('%Y-%m-%d %H:%M')} UTC)"
        return {"status": "ok", "message": msg}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reset_watchers")
def trigger_reset_watchers():
    """
    Полный сброс живых вотчеров (не уровней!) — ставит флаг, который
    run_web.py проверяет КАЖДУЮ СЕКУНДУ (не привязано к 15-минутному
    каскаду скана, см. _flag_listener в run_web.py), поэтому применяется
    почти сразу. Чистит v_bottom_mgr/bounce_mgr (вотчеры+кладбище),
    tracked_origin_levels(_vrt), watcher_cooldown_cache. НЕ трогает
    macro_levels.json/watchlist.json — для пересчёта самих уровней
    отдельная кнопка (/api/rebuild_levels).
    """
    flag_path = os.path.join(CRYPTANO_DIR, "reset_watchers.flag")
    try:
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write("1")
        return {"status": "ok", "message": "Reset requested — применится в течение секунды"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rebuild_levels")
def trigger_rebuild_levels():
    """
    Полный пересчёт уровней (macro_levels.json) с нуля — то же самое, что
    команда 'rebuild' в консоли Scanner. Дольше, чем сброс вотчеров (фетчит
    историю по ~70 монетам), поэтому отдельная кнопка, не совмещённая со
    сбросом вотчеров.
    """
    flag_path = os.path.join(CRYPTANO_DIR, "rebuild_levels.flag")
    try:
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write("1")
        return {"status": "ok", "message": "Rebuild requested — может занять пару минут"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/signals/clear")
def clear_signals():
    """
    Очистка списка сигналов (signals.json). В отличие от reset_watchers/
    rebuild_levels — без флага, пишем сразу: signals.json нигде не живёт
    в памяти сканера (save_signal только дописывает файл, никто не читает
    его обратно для торговых решений), поэтому прямая перезапись безопасна
    и применяется мгновенно, без ожидания скан-цикла.
    """
    try:
        _write_json_atomic(SIGNALS_PATH, [])
        return {"status": "ok", "message": "Список сигналов очищен"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/history/clear")
def clear_history():
    """
    Очистка "ПОСЛЕДНИЙ СКАН" (watcher_history.json) — снимков последней
    смерти/срабатывания по каждому вотчеру. Как и signals.json, этот файл
    только пишется сканером (load-merge-save при каждой смерти), не читается
    обратно для торговой логики — прямая перезапись безопасна.
    """
    try:
        _write_json_atomic(WATCHER_HISTORY_PATH, {})
        return {"status": "ok", "message": "История очищена"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/strategies")
def get_strategies():
    """Текущее вкл/выкл каждой стратегии Watcher'а. Отсутствие ключа в
    config.json трактуется как ВКЛЮЧЕНО (True) — то же правило, что
    в background_tasks.py::crypto_orchestrator, чтобы дашборд и реальный
    диспетчер бота никогда не расходились в интерпретации пустого конфига."""
    config = _read_json(CONFIG_FILE, default={})
    saved = config.get("crypto", {}).get("strategies", {})
    return {tag: saved.get(tag, True) for tag in STRATEGY_TAGS}


@app.post("/api/strategies/{tag}/toggle")
def toggle_strategy(tag: str):
    """Переключает одну стратегию, не трогая остальные. Пишет в тот же
    config.json, что и Telegram-бот (main.py) — диспетчер (background_tasks.py)
    перечитывает конфиг каждую секунду, поэтому применяется без рестарта."""
    tag = tag.upper().strip()
    if tag not in STRATEGY_TAGS:
        raise HTTPException(status_code=400, detail=f"Неизвестная стратегия: {tag}")
    try:
        config = _read_json(CONFIG_FILE, default={})
        if "crypto" not in config:
            config["crypto"] = {}
        if "strategies" not in config["crypto"]:
            config["crypto"]["strategies"] = {}
        current = config["crypto"]["strategies"].get(tag, True)
        config["crypto"]["strategies"][tag] = not current
        _write_json_atomic(CONFIG_FILE, config)
        return {"status": "ok", "tag": tag, "enabled": not current}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bounce_direction")
def get_bounce_direction():
    """LONG/SHORT — вкл/выкл направления сделок BOUNCE. ОДИН И ТОТ ЖЕ ключ
    (crypto.allow_long/allow_short в config.json) читают и боевой бот
    (run_web.py/background_tasks.py::check_bounce), и симулятор
    (bounce_simulate.py) — единая точка правды, не два места, которые
    могут разойтись. Отсутствие ключа — ВКЛЮЧЕНО (True), та же конвенция,
    что у /api/strategies (см. get_strategies выше)."""
    config = _read_json(CONFIG_FILE, default={})
    crypto_cfg = config.get("crypto", {})
    return {
        "allow_long": crypto_cfg.get("allow_long", True),
        "allow_short": crypto_cfg.get("allow_short", True),
    }


@app.post("/api/bounce_direction/{direction}/toggle")
def toggle_bounce_direction(direction: str):
    """Переключает LONG или SHORT по отдельности, не трогая другое
    направление. Пишет в тот же config.json — боевой бот перечитывает его
    каждый цикл сам (см. get_bounce_direction), применяется без рестарта
    дашборда/сканера."""
    direction = direction.lower().strip()
    if direction not in ("long", "short"):
        raise HTTPException(status_code=400, detail=f"Направление должно быть long или short, получено: {direction}")
    key = f"allow_{direction}"
    try:
        config = _read_json(CONFIG_FILE, default={})
        if "crypto" not in config:
            config["crypto"] = {}
        current = config["crypto"].get(key, True)
        config["crypto"][key] = not current
        _write_json_atomic(CONFIG_FILE, config)
        return {"status": "ok", "direction": direction, "enabled": not current}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
class TpSlRequest(BaseModel):
    bounce_tp_pct: float
    bounce_sl_pct: float

@app.get("/api/config/bounce_tpsl")
def get_bounce_tpsl():
    config = _read_json(CONFIG_FILE, default={})
    crypto_cfg = config.get("crypto", {})
    return {
        "bounce_tp_pct": crypto_cfg.get("bounce_tp_pct", 7.0),
        "bounce_sl_pct": crypto_cfg.get("bounce_sl_pct", 50.0),
    }

@app.post("/api/config/bounce_tpsl")
def set_bounce_tpsl(payload: TpSlRequest):
    try:
        config = _read_json(CONFIG_FILE, default={})
        if "crypto" not in config:
            config["crypto"] = {}
        config["crypto"]["bounce_tp_pct"] = payload.bounce_tp_pct
        config["crypto"]["bounce_sl_pct"] = payload.bounce_sl_pct
        _write_json_atomic(CONFIG_FILE, config)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
class DirectionSetRequest(BaseModel):
    allow_long: bool
    allow_short: bool

@app.post("/api/bounce_direction/set")
def set_bounce_direction(payload: DirectionSetRequest):
    """Прямая установка LONG/SHORT из выпадающего списка."""
    try:
        config = _read_json(CONFIG_FILE, default={})
        if "crypto" not in config:
            config["crypto"] = {}
        
        config["crypto"]["allow_long"] = payload.allow_long
        config["crypto"]["allow_short"] = payload.allow_short
        
        _write_json_atomic(CONFIG_FILE, config)
        return {"status": "ok", "allow_long": payload.allow_long, "allow_short": payload.allow_short}
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
    """Уровни поддержки/сопротивления по монете — macro (swing_hunter) +
    ручные (custom_levels.json), слитые в один ответ. Слияние тут, а не
    отдельным полем в ответе — фронт (график) не должен ничего знать про
    два источника, просто рисует то, что пришло, как и раньше."""
    macro = _read_json(MACRO_LEVELS_PATH, default={})
    custom = _read_json(CUSTOM_LEVELS_PATH, default={})
    coin = coin.upper().strip()
    data = macro.get(coin)
    if data is None:
        # Пробуем через алиас (например TON -> GRAM)
        alias = KNOWN_TICKER_ALIASES.get(coin)
        if alias:
            data = macro.get(alias)

    custom_coin = custom.get(coin, {})
    custom_supports = custom_coin.get("supports", [])
    custom_resistances = custom_coin.get("resistances", [])

    if data is None:
        if not custom_supports and not custom_resistances:
            raise HTTPException(status_code=404, detail=f"No levels for {coin}")
        # Ручные уровни есть, а macro для монеты нет вообще (например,
        # SWING_HUNTER её пока не считал/не подхватил по объёму) —
        # честно отдаём то, что реально есть, не 404.
        return {"supports": list(custom_supports), "resistances": list(custom_resistances)}

    merged = dict(data)
    merged["supports"] = list(data.get("supports", [])) + list(custom_supports)
    merged["resistances"] = list(data.get("resistances", [])) + list(custom_resistances)
    return merged

@app.post("/api/levels/custom")
def add_custom_level(payload: dict = Body(...)):
    """Ручное добавление уровня с дашборда (модалка "➕" у Watchlist).

    Тело запроса: {"coin": "RAYDIUM", "side": "support"|"resistance",
    "min": 1.05, "max": 1.10, "score": 10.0 (опционально)}.

    Пишет в СВОЙ файл (custom_levels.json), не трогая macro_levels.json —
    его целиком перезаписывает swing_hunter при каждом пересчёте, ручная
    правка внутри него не пережила бы следующий скан. Оба файла читаются
    и сливаются на чтении — здесь (GET /api/levels/{coin}) и в движке
    сканера (watcher_plan.py::get_merged_levels_for_coin).

    Если монеты ещё нет в watchlist.json — добавляет её сразу же, с
    source="MANUAL" (НЕ "SWING_HUNTER") — существующая чистка "призраков"
    в background_tasks.py трогает только source="SWING_HUNTER", так что
    вручную добавленная монета не будет удалена из watchlist, даже если
    у неё никогда не появится macro-уровней от swing_hunter."""
    coin = str(payload.get("coin", "")).upper().strip()
    side = str(payload.get("side", "")).lower().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    if side not in ("support", "resistance"):
        raise HTTPException(status_code=400, detail="side должен быть 'support' или 'resistance'")
    try:
        zone_min = float(payload["min"])
        zone_max = float(payload["max"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="min/max обязательны и должны быть числами")
    if zone_min >= zone_max:
        raise HTTPException(status_code=400, detail="min должен быть меньше max")
    score = payload.get("score", 10.0)
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 10.0

    zone = {
        "min": zone_min,
        "max": zone_max,
        "score": score,
        "type": "MANUAL",
        "date": datetime.date.today().isoformat(),
        "reaction_count": 0,
    }

    try:
        custom = _read_json(CUSTOM_LEVELS_PATH, default={})
        if coin not in custom:
            custom[coin] = {"supports": [], "resistances": []}
        key = "supports" if side == "support" else "resistances"
        custom[coin].setdefault(key, []).append(zone)
        _write_json_atomic(CUSTOM_LEVELS_PATH, custom)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить custom_levels.json: {e}")

    try:
        wl = _read_json(WATCHLIST_PATH, default={})
        if coin not in wl:
            wl[coin] = {"source": "MANUAL", "added_at": datetime.datetime.now().isoformat()}
            _write_json_atomic(WATCHLIST_PATH, wl)
    except Exception as e:
        # Уровень уже сохранён выше — не откатываем его из-за сбоя с
        # watchlist, просто сообщаем, что эта часть не удалась.
        return {"status": "partial", "detail": f"Уровень сохранён, но не удалось обновить watchlist.json: {e}"}

    return {"status": "ok", "coin": coin, "side": side, "zone": zone}


@app.get("/api/levels_at/{coin}")
def get_levels_at(coin: str, when: int = Query(..., description="Unix-время (секунды, UTC) — на какой момент показать уровни")):
    """То же самое, что /api/levels/{coin}, но не 'сейчас', а честный
    исторический снимок на момент `when` — через levels_history.py::
    get_levels_snapshot(), ту же функцию, что использует рескан и
    bounce_simulate.py (см. modules/cryptano/levels_history.py). Читает
    database/levels_timeline_YYYY_MM.json (снимки раз в ~12ч), а не
    сегодняшний macro_levels.json — нужно для симулятора: при выборе даты
    "От" показывать то, что бот реально видел в тот момент, а не текущее
    состояние уровней."""
    coin = coin.upper().strip()
    when_dt = datetime.datetime.utcfromtimestamp(when)
    snapshot = get_levels_snapshot(when_dt)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"No levels history for {coin} at this time")

    data = snapshot.get(coin)
    if data is None:
        alias = KNOWN_TICKER_ALIASES.get(coin)
        if alias:
            data = snapshot.get(alias)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No levels for {coin} at this time")
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


@app.post("/api/simulate_bounce/{coin}")
def simulate_bounce(
    coin: str,
    start: str = Query(..., description="Дата/время старта симуляции, UTC. 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM'"),
    end: Optional[str] = Query(default=None, description="Конец периода, по умолчанию — самая свежая доступная свеча"),
    allow_long: bool = Query(default=True, description="Искать LONG-сделки"),
    allow_short: bool = Query(default=True, description="Искать SHORT-сделки"),
):
    """
    То же самое, что /api/simulate/{coin}, только для BOUNCE — прогоняет
    настоящие BounceManager/BounceWatcher (см. modules.cryptano.strategy),
    на КАЖДУЮ свечу честно беря уровни из levels_history.py (снимки раз в
    ~12ч), а не замороженные на дату старта. Свой изолированный менеджер —
    ничего боевого не трогает, свои логи (modules/cryptano/simulator/logs/).
    """
    from modules.cryptano.simulator.bounce_simulate import run_bounce_simulation
    try:
        return run_bounce_simulation(coin, start, end, allow_long=allow_long, allow_short=allow_short)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка симуляции BOUNCE: {e}")


@app.get("/api/simulate_bounce/last")
def simulate_bounce_last():
    """Последний сохранённый результат BOUNCE-симуляции — фронт дёргает это
    при открытии /simulator, чтобы результат пережил F5 (см.
    bounce_simulate.get_last_run/save_json_atomic(LAST_RUN_FILE, ...))."""
    from modules.cryptano.simulator.bounce_simulate import get_last_run
    return get_last_run() or {}


@app.post("/api/simulate_bounce_bulk")
def simulate_bounce_bulk(
    start: str = Query(..., description="Дата/время старта периода, UTC. 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM'"),
    end: Optional[str] = Query(default=None, description="Конец периода, по умолчанию — сейчас"),
    allow_long: bool = Query(default=True, description="Искать LONG-сделки"),
    allow_short: bool = Query(default=True, description="Искать SHORT-сделки"),
):
    """
    Прогоняет BOUNCE-симуляцию по ВСЕМ монетам, у которых были уровни
    хоть в одном снимке таймлайна за указанный период (см.
    bounce_simulate.list_coins_with_levels) — НЕ по сегодняшнему топ-70
    из /api/macro/coins, это разные списки для исторического периода.

    Без сетевой докачки свечей (top_up=False на каждую монету) и без
    перезаписи last_run.json одиночного симулятора (свой файл,
    last_all_run.json) — см. докстринг run_bulk_bounce_simulation.

    Одна упавшая монета не валит весь прогон — её ошибка просто попадает
    в results[coin]['error'], остальные монеты считаются как обычно.
    """
    from modules.cryptano.simulator.bounce_simulate import run_bulk_bounce_simulation
    try:
        return run_bulk_bounce_simulation(start, end, allow_long=allow_long, allow_short=allow_short)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка bulk-симуляции BOUNCE: {e}")


@app.get("/api/simulate_bounce_bulk/progress")
def simulate_bounce_bulk_progress():
    """Прогресс идущего сейчас (или последнего завершённого) bulk-прогона —
    фронт опрашивает это раз в секунду, пока ждёт ответ на сам
    POST /api/simulate_bounce_bulk (тот долгий, отвечает только в конце).
    {} если bulk ещё ни разу не запускали."""
    from modules.cryptano.simulator.bounce_simulate import get_bulk_progress
    return get_bulk_progress() or {}


@app.get("/api/simulate_bounce_bulk/last")
def simulate_bounce_bulk_last():
    """Последний сохранённый результат bulk-прогона — свой файл
    (last_all_run.json), не пересекается с одиночным /api/simulate_bounce/last."""
    from modules.cryptano.simulator.bounce_simulate import get_last_all_run
    return get_last_all_run() or {}


@app.get("/api/signals")
def get_signals(limit: int = Query(default=50, ge=1, le=500)):
    """Последние сработавшие сигналы, свежие сверху."""
    signals = _read_json(SIGNALS_PATH, default=[])
    if not isinstance(signals, list):
        signals = []
    # date хранится как "YYYY-MM-DD HH:MM" — сортируется лексикографически верно
    signals_sorted = sorted(signals, key=lambda s: s.get("date") or "", reverse=True)
    return signals_sorted[:limit]


@app.get("/api/sports/football")
def get_football_signals(limit: int = Query(default=50, ge=1, le=500)):
    """Последние футбольные сигналы (см. modules/footballnogoal/football.py), свежие сверху."""
    signals = _read_json(FOOTBALL_SIGNALS_PATH, default=[])
    if not isinstance(signals, list):
        signals = []
    signals_sorted = sorted(signals, key=lambda s: s.get("date") or "", reverse=True)
    return signals_sorted[:limit]


@app.get("/api/sports/nba")
def get_nba_signals(limit: int = Query(default=50, ge=1, le=500)):
    """Последние NBA-сигналы (см. modules/playerpropsbasket/player_props.py), свежие сверху."""
    signals = _read_json(NBA_SIGNALS_PATH, default=[])
    if not isinstance(signals, list):
        signals = []
    signals_sorted = sorted(signals, key=lambda s: s.get("date") or "", reverse=True)
    return signals_sorted[:limit]


def _sports_status(sport: str, status_path: str):
    """status ("RUNNING"/"STOPPED") — из того же config.json, что и остальные
    переключатели, снимок последнего скана — из отдельного status-файла
    (пишут football.py/player_props.py после каждой проверки)."""
    config = _read_json(CONFIG_FILE, default={})
    running = config.get(sport, {}).get("status", "STOPPED") == "RUNNING"
    last_scan = _read_json(status_path, default={})
    if not isinstance(last_scan, dict):
        last_scan = {}
    return {"running": running, "last_scan": last_scan}


@app.get("/api/sports/football/status")
def get_football_status():
    return _sports_status("football", FOOTBALL_STATUS_PATH)


@app.get("/api/sports/nba/status")
def get_nba_status():
    return _sports_status("nba", NBA_STATUS_PATH)


@app.post("/api/sports/{sport}/toggle")
def toggle_sport(sport: str):
    """Включает/выключает football/nba-монитор — та же логика, что и
    /api/strategies/{tag}/toggle: пишет в config.json, монитор перечитывает
    его каждый цикл сам (см. run_football_monitor/run_nba_monitor), поэтому
    применяется без рестарта дашборда/сканера."""
    sport = sport.lower().strip()
    if sport not in ("football", "nba"):
        raise HTTPException(status_code=400, detail=f"Неизвестный спорт: {sport}")
    try:
        config = _read_json(CONFIG_FILE, default={})
        if sport not in config:
            config[sport] = {}
        current = config[sport].get("status", "STOPPED")
        new_status = "STOPPED" if current == "RUNNING" else "RUNNING"
        config[sport]["status"] = new_status
        _write_json_atomic(CONFIG_FILE, config)
        return {"status": "ok", "sport": sport, "running": new_status == "RUNNING"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rescan_status")
def get_rescan_status():
    """Статус ручного рескана по монетам — {coin: {status: running|done, ...}}.
    Пишется напрямую сканером (background_tasks.py), не через
    telegram-заглушку — файл маленький, безопасно опрашивать часто."""
    status = _read_json(RESCAN_STATUS_PATH, default={})
    if not isinstance(status, dict):
        status = {}
    return status


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
def get_ohlcv(
    coin: str,
    timeframe: str = "15m",
    limit: Optional[int] = Query(default=None, ge=1, description="если не задан — вся история, что есть в базе (её глубину задаёт candle_store.BACKFILL_DAYS_MAP)"),
    around: Optional[int] = Query(default=None, description="unix-секунды — окно вокруг этого момента вместо последних `limit` свечей (переход по клику на старый сигнал/сделку)"),
):
    """
    Свечи по монете — из локальной SQLite-истории (candle_store), а не
    напрямую с биржи каждый раз. Для монет из watchlist история уже
    докачана фоновым воркером на глубину candle_store.BACKFILL_DAYS_MAP
    для этого таймфрейма (единственное место, где настраивается окно).
    Если монеты в базе ещё нет вообще (открыли не-watchlist монету
    напрямую) — докачиваем её здесь же, синхронно: это разовая пауза
    в несколько секунд на первое открытие, дальше она уже в базе.

    Без `limit` отдаётся вся история, что есть в базе для этого
    (symbol, timeframe) — её глубину и держит постоянной cleanup_old(),
    так что "вся история" на практике и есть окно BACKFILL_DAYS_MAP.

    `around` — unix-секунды: вернуть окно СВЕЧЕЙ вокруг этого момента
    (примерно limit/2 до и после), а не последние `limit`. Нужно для
    клика по старому сигналу — "последние свечи" его физически не покажут.
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

    candles = candle_store.get_candles(symbol, timeframe, limit=limit, around=around)

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


@app.get("/simulator")
def simulator_page():
    """Отдельная страница симулятора — тот же app.js, что на главной, но
    своя разметка (только список монет с уровнями + график + управление
    симуляцией, без watchlist/активных/сигналов). Все loadXxx()-функции в
    app.js сами проверяют, есть ли на странице их элемент, прежде чем
    что-то делать — на этой странице действуют только те, что реально нужны."""
    return FileResponse(os.path.join(STATIC_DIR, "simulator.html"))


@app.get("/sports")
def sports_page():
    """Футбол/NBA — отдельная страница со своим лёгким JS (sports.js), не
    имеет отношения к крипто-графику/app.js вообще, поэтому не переиспользует
    его — своя простая разметка, свои два запроса к /api/sports/*."""
    return FileResponse(os.path.join(STATIC_DIR, "sports.html"))