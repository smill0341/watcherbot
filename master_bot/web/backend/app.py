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
# Избранные монеты (звёздочка ⭐ в списке монет). ОТДЕЛЬНЫЙ файл, не поле
# внутри watchlist.json: watchlist сам чистится/пополняется при каждом
# пересчёте уровней, и избранное терялось бы, когда монета на время выпадает
# из топа. Формат — просто список монет: ["BTC", "DASH"].
FAVORITES_PATH = os.path.join(JSONBANK_DIR, "favorites.json")
SIGNALS_PATH = os.path.join(JSONBANK_DIR, "signals.json")
FOOTBALL_SIGNALS_PATH = os.path.join(JSONBANK_DIR, "football_signals.json")
FOOTBALL_STATUS_PATH = os.path.join(JSONBANK_DIR, "football_status.json")
NBA_SIGNALS_PATH = os.path.join(JSONBANK_DIR, "nba_signals.json")
NBA_STATUS_PATH = os.path.join(JSONBANK_DIR, "nba_status.json")
RESCAN_STATUS_PATH = os.path.join(JSONBANK_DIR, "rescan_status.json")
ACTIVE_WATCHERS_PATH = os.path.join(JSONBANK_DIR, "active_watchers.json")
WATCHER_HISTORY_PATH = os.path.join(JSONBANK_DIR, "watcher_history.json")
# Та же лента, куда пишет Notifier (см. dashboard_actions.py) — "заглушка
# вместо telebot", реального Telegram тут всё равно нет, просто общий
# JSON-список последних сообщений для дашборда. Массовый рескан пишет сюда
# напрямую, без импорта Notifier — не нужен весь его __init__, нужна
# только запись.
NOTIFICATIONS_PATH = os.path.join(JSONBANK_DIR, "notifications.json")
MAX_NOTIFICATIONS = 200  # то же число, что MAX_NOTIFICATIONS в dashboard_actions.py

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
    
    🔥 НОВОЕ: Возвращаем source и added_at, приоритет для MANUAL (ручных).
    """
    wl = _read_json(WATCHLIST_PATH, default={})
    if not isinstance(wl, dict):
        wl = {}
    macro = _read_json(MACRO_LEVELS_PATH, default={})
    # Ручные зоны (кнопка ➕) — coin может иметь custom-зону независимо от
    # того, как сам coin попал в watchlist (SWING_HUNTER или MANUAL) — это
    # две разные вещи: "монета добавлена руками" vs "у монеты есть ручная
    # зона". Раньше на дашборде это было визуально неотличимо от обычной
    # монеты, добавленный уровень терялся в общем списке.
    custom_db = _read_json(CUSTOM_LEVELS_PATH, default={})

    def _has_custom_levels(coin):
        c = custom_db.get(coin) or custom_db.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})
        return bool((c or {}).get("supports")) or bool((c or {}).get("resistances"))

    favorites = set(_read_favorites())

    with_levels = []
    without_levels = []
    for coin, meta in wl.items():
        if coin == "_meta":
            continue
        has_levels = coin in macro or KNOWN_TICKER_ALIASES.get(coin) in macro
        clean_meta = {k: v for k, v in (meta or {}).items() if k != "direction"}

        # 🔥 Сохраняем source и added_at
        source = clean_meta.get("source", "SWING_HUNTER")
        added_at = clean_meta.get("added_at", "")

        entry = {
            "coin": coin,
            "has_levels": has_levels,
            "source": source,
            "added_at": added_at,
            "has_custom_levels": _has_custom_levels(coin),
            "favorite": coin in favorites,
            **{k: v for k, v in clean_meta.items() if k not in ["source", "added_at", "direction"]}
        }
        (with_levels if has_levels else without_levels).append(entry)

    # ПОРЯДОК В СПИСКЕ — ЕДИНСТВЕННОЕ место, где он решается (фронт
    # выводит как пришло, своей сортировки не делает):
    #   1. ⭐ избранные (по алфавиту);
    #   2. ✋ монеты с ручными зонами (свежие сверху);
    #   3. монеты, добавленные вручную, без зон (свежие сверху);
    #   4. остальные по алфавиту.
    def sort_by_priority(items):
        fav = sorted([i for i in items if i.get("favorite")], key=lambda x: x["coin"])
        rest = [i for i in items if not i.get("favorite")]
        with_custom = sorted([i for i in rest if i.get("has_custom_levels")],
                             key=lambda x: x.get("added_at") or "", reverse=True)
        manual = sorted([i for i in rest if not i.get("has_custom_levels") and i.get("source") == "MANUAL"],
                        key=lambda x: x.get("added_at") or "", reverse=True)
        auto = sorted([i for i in rest if not i.get("has_custom_levels") and i.get("source") != "MANUAL"],
                      key=lambda x: x["coin"])
        return fav + with_custom + manual + auto

    with_levels = sort_by_priority(with_levels)
    without_levels = sort_by_priority(without_levels)

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

    🔥 НОВОЕ: Добавляем source и added_at из watchlist для приоритизации ручных.

    ЧИТАЕТ НАПРЯМУЮ ИЗ ПАМЯТИ (bounce_mgr/v_bottom_mgr), а не из
    active_watchers.json на диске. Раньше (во времена, когда дашборд был
    отдельным процессом) этот файл был единственным честным способом
    что-либо узнать о состоянии сканера — но он обновлялся только раз в
    скан-цикл, поэтому список "в работе" мог на какое-то время отставать
    от реальности (а после рескана/удаления с дашборда — расходиться с
    ней куда серьёзнее, см. общий докстринг слияния в run_web.py). После
    слияния дашборда и сканера в один процесс это отставание больше не
    нужно ничем оправдывать — читаем ту же самую память, которую видит
    сам сканер, под тем же _watcher_lock, что и любая другая мутация.
    Сам файл active_watchers.json при этом никуда не делся — его по-прежнему
    пишет save_watcher_state()/_write_active_watchers_snapshot() (нужен для
    персистентности между рестартами), просто дашборд его больше не читает.
    """
    from modules.cryptano.live_scan import v_bottom_mgr, bounce_mgr, _watcher_lock
    from background_tasks import _build_active_watchers_export

    with _watcher_lock:
        raw = _build_active_watchers_export(v_bottom_mgr, bounce_mgr)
    wl = _read_json(WATCHLIST_PATH, default={})  # 🔥 Загружаем watchlist

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
    
    # 🔥 НОВОЕ: Добавляем source, added_at и favorite из watchlist/favorites.json
    favorites = set(_read_favorites())
    for w in result:
        coin = w.get("coin")
        if coin and coin in wl:
            w["source"] = wl[coin].get("source", "SWING_HUNTER")
            w["added_at"] = wl[coin].get("added_at", "")
        else:
            w["source"] = "SWING_HUNTER"
            w["added_at"] = ""
        w["favorite"] = coin in favorites

    # Порядок — тот же принцип, что и в /api/watchlist (sort_by_priority
    # там же): избранные всегда сверху, дальше ручные (свежие сверху),
    # дальше остальные по алфавиту. Одна и та же логика приоритета в
    # обоих местах, фронт больше свою копию сортировки не делает.
    def sort_by_priority(items):
        fav = sorted([w for w in items if w.get("favorite")], key=lambda x: x.get("coin") or "")
        rest = [w for w in items if not w.get("favorite")]
        manual = sorted([w for w in rest if w.get("source") == "MANUAL"],
                       key=lambda x: x.get("added_at") or "", reverse=True)
        auto = sorted([w for w in rest if w.get("source") != "MANUAL"],
                     key=lambda x: x.get("coin") or "")
        return fav + manual + auto

    result = sort_by_priority(result)
    return result


# POST /api/rescan/{coin} (старый рескан монеты, кнопка 🔄) УДАЛЁН. Он
# откатывал монете "последнюю обработанную свечу" назад (по умолчанию на
# неделю) и гнал всё заново внутри боевого бота. Смотреть прошлое монеты —
# теперь только симулятор (кнопка 📈 на дашборде открывает /simulator?coin=XXX).
# Точечный рескан вотчера (/api/rescan_watcher) и "рескан всех" остаются.
# /api/rescan_status тоже остаётся — через него дашборд видит идущий
# пересчёт уровней (_global_rebuild).


@app.post("/api/rescan_watcher/{coin}")
def trigger_single_watcher_rescan(coin: str, level_id: str = Query(..., description="level_id конкретного BOUNCE-вотчера (как в /api/watchers)")):
    """Точечный рескан ОДНОГО существующего BOUNCE-вотчера — в отличие от
    старого (удалённого) рескана монеты НЕ трогает остальных живых вотчеров этой же монеты
    (см. watcher_plan.py::rescan_single_bounce_watcher за полным объяснением
    механизма и честным предупреждением про is_focus).

    Синхронный, не через флаг-файл + фоновый поток, как у /api/rescan:
    это прогон ОДНОГО уровня, а не всей монеты, обычно заметно короче.
    Использует _watcher_lock — та же блокировка, что и боевой скан/рескан
    монеты, чтобы не читать/писать вотчер параллельно с боевым циклом."""
    coin = coin.upper().strip()
    from modules.cryptano.live_scan import v_bottom_mgr, bounce_mgr, _watcher_lock
    from modules.cryptano.watcher_plan import rescan_single_bounce_watcher
    from background_tasks import export_dashboard_state

    _watcher_lock.acquire()
    try:
        result = rescan_single_bounce_watcher(coin, level_id, bounce_mgr)
        # Без этого watcher.state меняется только в памяти bounce_mgr —
        # active_watchers.json (тот самый файл, из которого /api/watchers
        # отдаёт список "в работе") пересобирается ТОЛЬКО этой функцией, и
        # без явного вызова остаётся старым до следующего боевого скан-цикла.
        export_dashboard_state(v_bottom_mgr, bounce_mgr)
    finally:
        _watcher_lock.release()

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/rescan_all_watchers")
def trigger_rescan_all_watchers():
    """
    Массовый точечный рескан ВСЕХ живых BOUNCE-вотчеров (всё, что сейчас
    сидит в bounce_mgr._watchers — т.е. список "в работе" на дашборде),
    один за другим, тем же самым rescan_single_bounce_watcher, что и
    /api/rescan_watcher — просто в цикле, без клика по каждой монете.

    Последовательно (не параллельно) — по просьбе Джека: параллельный
    прогон бил бы по бирже кучей запросов свечей одновременно и не давал
    бы предсказуемого порядка в итоговом уведомлении. На практике один
    вотчер сканируется быстро, так что вся пачка всё равно укладывается
    в разумное время (тоже его слова: "оно быстро все равно").

    Эндпоинт синхронный и отдаёт готовый результат сразу — как и точечный
    /api/rescan_watcher, только на всю пачку сразу.

    Уведомление — ОДНО сводное на всю пачку (список того, что изменилось),
    а не по одному на каждую монету, как раньше присылал боевой рескан —
    здесь так же, только явным списком в тексте одного сообщения.
    Пишем прямо в notifications.json (та же лента, что ведёт Notifier в
    run_web.py) — реального Telegram у дашборда всё равно нет.
    """
    from modules.cryptano.live_scan import v_bottom_mgr, bounce_mgr, _watcher_lock
    from modules.cryptano.watcher_plan import rescan_single_bounce_watcher
    from background_tasks import export_dashboard_state

    _watcher_lock.acquire()
    try:
        # Снимок списка ДО начала прогона — рескан меняет .state существующих
        # вотчеров, но не создаёт новых записей в _watchers, так что снимок
        # ключей/монет в начале однозначно описывает "что было в работе".
        #
        # Уже DEAD/TRIGGERED на момент снимка — не рескан(ир)уем вообще:
        # rescan_single_bounce_watcher честно сбрасывает вотчера (reset_
        # for_rescan) и прогоняет его историю заново, находит ту же самую
        # сделку, что уже случилась и уже есть в signals.json — раньше это
        # плодило дубль записи в "Результатах" со старой датой сделки, но
        # новым моментом появления в списке (см. обсуждение бага). Такой
        # вотчер и так со дня на день уберёт обычная автоматическая чистка
        # (clear_dead_watchers, каждые 15 мин) — трогать его тут незачем.
        targets = [
            (getattr(w, "coin", None), level_id, getattr(w, "state", None))
            for level_id, w in list(bounce_mgr._watchers.items())
            if getattr(w, "state", None) not in ("DEAD", "TRIGGERED")
        ]

        results: list[dict[str, Any]] = []
        for coin, level_id, state_before in targets:
            r: dict[str, Any]
            try:
                r = rescan_single_bounce_watcher(coin, level_id, bounce_mgr)
            except Exception as e:
                r = {"error": str(e)}
            r["coin"] = coin
            r["level_id"] = level_id
            r["state_before"] = state_before
            results.append(r)

        # Один общий пересбор active_watchers.json в конце всей пачки —
        # не после каждого вотчера (то же соображение, что в докстринге
        # trigger_single_watcher_rescan, просто один раз на всех).
        export_dashboard_state(v_bottom_mgr, bounce_mgr)
    finally:
        _watcher_lock.release()

    ok = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]
    newly_dead = [
        r for r in ok
        if r.get("final_state") in ("DEAD", "TRIGGERED")
        and r.get("state_before") not in ("DEAD", "TRIGGERED")
    ]
    still_active = [
        r for r in ok
        if r.get("final_state") not in ("DEAD", "TRIGGERED")
    ]

    lines = [f"🔄 Рескан всех BOUNCE-вотчеров: проверено {len(targets)}"]
    if newly_dead:
        coin_list = ", ".join(f"{r['coin']} ({r['level_id']})" for r in newly_dead)
        lines.append(f"❌ Оказались мёртвыми ({len(newly_dead)}): {coin_list}")
    if failed:
        coin_list = ", ".join(f"{r['coin']} ({r['level_id']}): {r['error']}" for r in failed)
        lines.append(f"⚠️ Ошибки рескана ({len(failed)}): {coin_list}")
    lines.append(f"✅ Остались активными: {len(still_active)}")
    summary_text = "\n".join(lines)

    try:
        items = _read_json(NOTIFICATIONS_PATH, default=None)
        if not isinstance(items, list):
            items = []
        items.append({
            "timestamp": datetime.datetime.now().isoformat(),
            "chat_id": "web-dashboard",
            "text": summary_text,
        })
        items = items[-MAX_NOTIFICATIONS:]
        _write_json_atomic(NOTIFICATIONS_PATH, items)
    except Exception as e:
        # Уведомление — не критично для самого рескана, не роняем ответ из-за него.
        print(f"[app.py] Не удалось записать сводное уведомление рескана: {e}")

    return {
        "status": "ok",
        "total": len(targets),
        "newly_dead": [{"coin": r["coin"], "level_id": r["level_id"]} for r in newly_dead],
        "failed": [{"coin": r["coin"], "level_id": r["level_id"], "error": r["error"]} for r in failed],
        "still_active": len(still_active),
        "summary": summary_text,
    }


@app.post("/api/reset_watchers")
def trigger_reset_watchers():
    """
    Полный сброс живых вотчеров (не уровней!). Чистит v_bottom_mgr/
    bounce_mgr (вотчеры+кладбище), tracked_origin_levels(_vrt),
    watcher_cooldown_cache. НЕ трогает macro_levels.json/watchlist.json —
    для пересчёта самих уровней отдельная кнопка (/api/rebuild_levels).

    Вызывает dashboard_actions.py::reset_all_watchers() напрямую и синхронно —
    операция быстрая (только мутация словарей в памяти + один
    save_watcher_state()), HTTP-ответ отдаётся уже по факту сброса, а не
    "заявка принята, проверь через секунду". Раньше это была заявка через
    reset_watchers.flag, который run_web.py проверял отдельным опросом
    каждую секунду — тот же процесс, что и сейчас, только раньше это был
    ОТДЕЛЬНЫЙ процесс без прямого доступа к bounce_mgr/v_bottom_mgr
    дашборда (см. докстринг run_web.py)."""
    from dashboard_actions import reset_all_watchers
    try:
        reset_all_watchers()
        return {"status": "ok", "message": "Вотчеры сброшены"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


is_rebuilding = False

@app.post("/api/rebuild_levels")
def trigger_rebuild_levels():
    """
    Полный пересчёт уровней (macro_levels.json) с нуля — то же самое, что
    команда 'rebuild' в консоли Scanner. Дольше, чем сброс вотчеров (фетчит
    историю по ~70 монетам), поэтому запускается в фоновом потоке, не
    блокируя HTTP-ответ — раньше это была заявка через rebuild_levels.flag,
    теперь dashboard_actions.py::rebuild_levels() вызывается напрямую."""
    import threading as _threading
    from dashboard_actions import rebuild_levels
    global is_rebuilding

    def _worker():
        global is_rebuilding
        is_rebuilding = True
        try:
            rebuild_levels()
        finally:
            is_rebuilding = False

    try:
        _threading.Thread(target=_worker, daemon=True).start()
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

def _read_favorites() -> list:
    data = _read_json(FAVORITES_PATH, default=[])
    return [str(c).upper() for c in data] if isinstance(data, list) else []


@app.post("/api/favorites/toggle")
def toggle_favorite(payload: dict = Body(...)):
    """Звёздочка ⭐ у монеты в списке: добавить в избранное / убрать.
    Тело: {"coin": "BTC"}. Ответ: {"coin", "favorite": true|false}."""
    coin = str(payload.get("coin", "")).upper().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    favs = _read_favorites()
    if coin in favs:
        favs = [c for c in favs if c != coin]
        is_fav = False
    else:
        favs.append(coin)
        is_fav = True
    try:
        _write_json_atomic(FAVORITES_PATH, favs)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить favorites.json: {e}")
    return {"coin": coin, "favorite": is_fav}


@app.post("/api/watchlist/remove")
def remove_manual_coin(payload: dict = Body(...)):
    """Убрать из списка монету, которая попала туда ТОЛЬКО из-за ручных зон
    (source="MANUAL", см. add_custom_level) и у которой ручных зон больше
    нет. Обычные монеты (SWING_HUNTER) так убирать нельзя — их всё равно
    вернёт следующий пересчёт уровней. Тело: {"coin": "RAYDIUM"}."""
    coin = str(payload.get("coin", "")).upper().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    wl = _read_json(WATCHLIST_PATH, default={})
    meta = wl.get(coin)
    if meta is None:
        raise HTTPException(status_code=404, detail="Монеты нет в списке")
    if (meta or {}).get("source") != "MANUAL":
        raise HTTPException(status_code=400, detail="Убрать можно только монету, добавленную вручную")
    custom = _read_json(CUSTOM_LEVELS_PATH, default={})
    c = custom.get(coin) or {}
    if c.get("supports") or c.get("resistances"):
        raise HTTPException(status_code=400, detail="Сначала удали ручные зоны монеты")
    wl.pop(coin, None)
    try:
        _write_json_atomic(WATCHLIST_PATH, wl)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить watchlist.json: {e}")
    return {"status": "ok", "coin": coin, "removed": True}


@app.post("/api/levels/custom/update")
def update_custom_level(payload: dict = Body(...)):
    """Редактирование ОДНОЙ ручной зоны (✏️ в окне ручных зон).
    Тело: {"coin", "side": "support"|"resistance", "old_min", "old_max",
    "min", "max"}. Зона ищется по точным старым min/max (своего id у ручных
    зон нет — так же ищет и удаление). Меняются границы и дата (сегодня):
    для бота это новая зона. Живой вотчер на старых границах, если есть,
    доживает как был — так же, как при удалении зоны."""
    coin = str(payload.get("coin", "")).upper().strip()
    side = str(payload.get("side", "")).lower().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    if side not in ("support", "resistance"):
        raise HTTPException(status_code=400, detail="side должен быть 'support' или 'resistance'")
    try:
        old_min = float(payload["old_min"]); old_max = float(payload["old_max"])
        new_min = float(payload["min"]); new_max = float(payload["max"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="old_min/old_max/min/max обязательны и должны быть числами")
    if new_min >= new_max:
        raise HTTPException(status_code=400, detail="min должен быть меньше max")

    custom = _read_json(CUSTOM_LEVELS_PATH, default={})
    key = "supports" if side == "support" else "resistances"
    zones = (custom.get(coin) or {}).get(key, [])
    for z in zones:
        if abs(float(z.get("min", 0)) - old_min) < 1e-9 and abs(float(z.get("max", 0)) - old_max) < 1e-9:
            z["min"] = new_min
            z["max"] = new_max
            z["date"] = datetime.date.today().isoformat()
            break
    else:
        raise HTTPException(status_code=404, detail="Такой уровень не найден (возможно, уже удалён)")
    try:
        _write_json_atomic(CUSTOM_LEVELS_PATH, custom)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить custom_levels.json: {e}")
    return {"status": "ok", "coin": coin, "side": side, "min": new_min, "max": new_max}


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


def _remove_custom_level_entry(coin: str, side: str, zone_min: float, zone_max: float) -> bool:
    """Убирает ОДНУ зону из custom_levels.json по точному совпадению
    min/max — общая логика, которую использует и /api/levels/custom/delete
    (ручное удаление зоны из модалки), и /api/watchers/delete (автоматическая
    подчистка, когда удаляемый BOUNCE-вотчер оказывается ручным уровнем,
    см. её докстринг). Возвращает True, если что-то реально удалено."""
    custom = _read_json(CUSTOM_LEVELS_PATH, default={})
    coin_data = custom.get(coin)
    if not coin_data:
        return False

    key = "supports" if side == "support" else "resistances"
    zones = coin_data.get(key, [])

    def _close(a, b):
        return abs(a - b) < 1e-9

    new_zones = [
        z for z in zones
        if not (_close(float(z.get("min", 0)), zone_min) and _close(float(z.get("max", 0)), zone_max))
    ]

    if len(new_zones) == len(zones):
        return False

    coin_data[key] = new_zones
    if not coin_data.get("supports") and not coin_data.get("resistances"):
        custom.pop(coin, None)
    else:
        custom[coin] = coin_data

    _write_json_atomic(CUSTOM_LEVELS_PATH, custom)
    return True


@app.post("/api/levels/custom/delete")
def delete_custom_level(payload: dict = Body(...)):
    """Удаление вручную добавленного уровня из custom_levels.json (кнопка
    "✕" у зоны в модалке "Управление ручными зонами" на дашборде).

    Тело запроса: {"coin": "RAYDIUM", "side": "support"|"resistance",
    "min": 1.05, "max": 1.10} — те же min/max, что были при добавлении
    (их отдаёт GET /api/levels/{coin}, там ручные зоны помечены
    "type": "MANUAL"). У ручных зон нет своего id, поэтому ищем зону по
    точному совпадению min/max в указанных монете и стороне — этого
    достаточно, дублей с одинаковыми min/max для одной монеты в UI не
    заводится (кнопка "+" не запрещает завести две одинаковые зоны, но
    это уже осознанный выбор пользователя, а не баг этого эндпоинта).

    НЕ трогает watchlist.json — монета остаётся в списке наблюдения (у
    неё вполне может быть ещё один ручной уровень или обычные уровни от
    SWING_HUNTER); чистка "призраков" в watchlist — отдельная механика в
    background_tasks.py, тут её сознательно не дублируем."""
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

    try:
        removed = _remove_custom_level_entry(coin, side, zone_min, zone_max)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить custom_levels.json: {e}")

    if not removed:
        raise HTTPException(status_code=404, detail="Такой уровень не найден (возможно, уже удалён)")

    return {"status": "ok", "coin": coin, "side": side, "deleted": True}


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
    
    # Берём снимок и его точное время
    from modules.cryptano.levels_history import _load_timeline_cached, _latest_in_timeline
    month_label = when_dt.strftime("%Y_%m")
    timeline = _load_timeline_cached(month_label)
    snapshot_time = None
    snapshot = None
    
    # Ищем снимок и его точное время
    if isinstance(timeline, dict) and timeline:
        best_time = None
        for time_str, snap in timeline.items():
            try:
                ts = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            if ts > when_dt:
                continue
            if best_time is None or ts > best_time:
                best_time = ts
                snapshot = snap
                snapshot_time = ts
    
    if snapshot is None:
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
    
    # Добавляем snapshot_time к каждому уровню чтобы фронт знал точное время появления
    snapshot_unix = int(snapshot_time.timestamp()) if snapshot_time else None
    result = dict(data)
    for side in ('supports', 'resistances'):
        for z in result.get(side, []):
            z['snapshot_time'] = snapshot_unix
    return result


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
    Пишется напрямую сканером (background_tasks.py),файл маленький, безопасно опрашивать часто."""
    global is_rebuilding
    status = _read_json(RESCAN_STATUS_PATH, default={})
    if not isinstance(status, dict):
        status = {}
    if is_rebuilding:
        status["_global_rebuild"] = {"status": "running"}
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


@app.post("/api/signals/delete")
def delete_signals(signal_keys: list = Body(...)):
    """Удаляет сигналы по составному ключу (time, coin, level_id) — НЕ по
    голому time.

    Раньше удаление шло только по time (unix-секунды свечи входа) — а это
    НЕ уникальный идентификатор одного сигнала: свечи многих монет часто
    закрываются в одну и ту же секунду (сетка 15м/1ч у всех одна), особенно
    после рескана, когда несколько сигналов у разных монет легко попадают
    на один и тот же таймстамп. В итоге выделение и удаление ОДНОГО сигнала
    в таблице удаляло вообще все сигналы с этим же time — по факту всех
    монет, у кого совпало время входа, а не только отмеченный.

    Тело запроса теперь — список объектов {"time":.., "coin":.., "level_id":..}
    (level_id может быть null у старых сигналов без него) — ровно то, что
    однозначно отличает одну строку таблицы "Результаты" от другой."""
    signals = _read_json(SIGNALS_PATH, default=[])
    if not isinstance(signals, list):
        return {"deleted": 0}

    def _key(s):
        return (s.get('time'), s.get('coin'), s.get('level_id'))

    keys_to_delete = set()
    for k in signal_keys:
        if isinstance(k, dict):
            keys_to_delete.add((k.get('time'), k.get('coin'), k.get('level_id')))
        else:
            # Обратная совместимость: если фронт вдруг прислал голый time
            # (старый формат) — как и раньше, удаляем всё с этим time.
            keys_to_delete.add(k)

    def _should_delete(s):
        return _key(s) in keys_to_delete or s.get('time') in keys_to_delete

    filtered = [s for s in signals if not _should_delete(s)]
    deleted_count = len(signals) - len(filtered)

    if deleted_count > 0:
        _write_json_atomic(SIGNALS_PATH, filtered)

    return {"deleted": deleted_count}


@app.post("/api/watchers/delete")
def delete_watchers(level_ids: list = Body(...)):
    """Удаляет вотчеры по level_id — НАВСЕГДА, из живой памяти процесса, не
    только из файла-снимка active_watchers.json.

    Раньше это редактировало ТОЛЬКО файл: bounce_mgr._watchers/
    v_bottom_mgr._watchers в памяти ничего не знали об удалении, и
    следующий же export_dashboard_state() (плановый 15-минутный скан-цикл
    или любой рескан монеты) пересобирал active_watchers.json заново из
    памяти — удалённые вотчеры молча возвращались обратно на дашборд.

    Использует тот же _watcher_lock, что и боевой скан/рескан (см.
    /api/rescan_watcher выше), чтобы не удалить вотчер ровно в момент, когда
    его же обрабатывает боевой цикл в другом потоке.

    Перед физическим удалением BOUNCE-вотчера архивирует его в
    watcher_history.json — тем же ключом (f"{coin}_BOUNCE_{level_id}") и
    в том же формате, что естественная смерть (см. export_dashboard_state/
    crypto_orchestrator). Без этого клик по СТАРОМУ сигналу в "Результаты"
    от уже удалённого вручную вотчера ничего не находил — ни в active
    (вотчера уже нет), ни в history (туда запись попадала ТОЛЬКО из
    естественной смерти, а не из ручного удаления) — buildFocusFromLevelId
    на фронте возвращал null, и зона/точки пути переставали рисоваться,
    хотя entry/target/stop ещё оставались видны (они не зависят от
    найденного вотчера). final_state помечаем "DELETED" — чтобы отличать
    от честного TRIGGERED/DEAD, если это когда-то понадобится на дашборде.
    V_BOTTOM/VGB/VRT сознательно не архивируем здесь — их сейчас никто не
    трогает и не просил чинить.

    ЕСЛИ УДАЛЯЕМЫЙ BOUNCE-ВОТЧЕР — РУЧНОЙ УРОВЕНЬ (custom_levels.json):
    дополнительно удаляет саму зону оттуда же (см. _remove_custom_level_entry).
    Раньше это была ловушка: level_id детерминирован от min/max/trade_type
    (BounceParent._level_id), а ручная зона в custom_levels.json никуда не
    девается сама по себе — стереть можно было только вотчер, а зона
    оставалась, и ближайший же скан-цикл (или точечный/массовый рескан)
    заново её "трогал" и пересоздавал вотчер с ТЕМ ЖЕ level_id, как будто
    удаления и не было. Для ручного уровня "удалить вотчер" и "удалить
    зону" теперь одно действие. Ответ содержит removed_custom_levels —
    сколько зон подчистилось заодно (0, если среди удалённых не было
    ручных)."""
    from modules.cryptano.live_scan import v_bottom_mgr, bounce_mgr, _watcher_lock
    from background_tasks import export_dashboard_state

    _watcher_lock.acquire()
    try:
        deleted_count = 0
        removed_custom_levels = 0
        archived_bounce = {}
        now_iso = datetime.datetime.now().isoformat()
        for level_id in level_ids:
            found = False
            bc_watcher = bounce_mgr._watchers.get(level_id)
            if bc_watcher is not None:
                w_coin = getattr(bc_watcher, "coin", None)
                if w_coin:
                    hist_key = f"{w_coin}_BOUNCE_{level_id}"
                    archived_bounce[hist_key] = {
                        "coin": w_coin,
                        "direction": getattr(bc_watcher, "trade_type", None),
                        "strategy": "BOUNCE",
                        "mode": getattr(bc_watcher, "mode", None),
                        "final_state": "DELETED",
                        "history_log": getattr(bc_watcher, "history_log", ""),
                        "level_min": getattr(bc_watcher, "min", None),
                        "level_max": getattr(bc_watcher, "max", None),
                        "level_date": getattr(bc_watcher, "level_date", None),
                        "level_type": getattr(bc_watcher, "level_type", None),
                        "level_score": getattr(bc_watcher, "level_score", None),
                        "events": getattr(bc_watcher, "event_log", []),
                        "died_at": now_iso,
                    }
                # Если этот вотчер — ручной уровень (custom_levels.json),
                # удаления только вотчера мало: сама зона остаётся на месте,
                # и её же снова "тронет" ближайший скан-цикл — level_id
                # детерминирован от min/max/trade_type (см. BounceParent.
                # _level_id), так что вотчер тут же пересоздастся заново с
                # тем же самым id, будто удаления и не было. Поэтому здесь
                # же чистим и саму зону в custom_levels.json — для ручного
                # уровня "удалить вотчер" и "удалить зону" должны быть одним
                # действием; отдельная кнопка в модалке "Управление ручными
                # зонами" (🎯) остаётся для случая, когда зону нужно убрать
                # ДО того, как по ней вообще родился вотчер.
                w_min = getattr(bc_watcher, "min", None)
                w_max = getattr(bc_watcher, "max", None)
                w_trade_type = getattr(bc_watcher, "trade_type", None)
                w_side = "support" if w_trade_type == "LONG" else "resistance" if w_trade_type == "SHORT" else None
                if w_coin and w_side and w_min is not None and w_max is not None:
                    try:
                        if _remove_custom_level_entry(w_coin, w_side, float(w_min), float(w_max)):
                            removed_custom_levels += 1
                    except Exception as e:
                        print(f"⚠️ [WATCHERS DELETE] Не удалось проверить/удалить ручную зону {w_coin} {w_side} {w_min}-{w_max}: {e}")
                del bounce_mgr._watchers[level_id]
                found = True
            if level_id in v_bottom_mgr._watchers:
                del v_bottom_mgr._watchers[level_id]
                found = True
            if found:
                deleted_count += 1

        if archived_bounce:
            try:
                history_db = _read_json(WATCHER_HISTORY_PATH, default={})
                history_db.update(archived_bounce)
                _write_json_atomic(WATCHER_HISTORY_PATH, history_db)
            except Exception as e:
                print(f"⚠️ [WATCHERS DELETE] Не удалось заархивировать в watcher_history.json: {e}")

        if deleted_count > 0:
            # Пересобирает active_watchers.json из уже очищенной памяти —
            # тот же приём, что и после точечного рескана выше, без него
            # дашборд не увидел бы удаление до следующего планового цикла.
            export_dashboard_state(v_bottom_mgr, bounce_mgr)
    finally:
        _watcher_lock.release()

    return {"deleted": deleted_count, "removed_custom_levels": removed_custom_levels}


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