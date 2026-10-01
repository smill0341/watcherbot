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
import uuid
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
# Сработавшие объёмные триггеры (watcher_plan.py::check_volume_triggers) —
# СВОЙ файл, не пересекается с bounce_mgr/v_bottom_mgr вообще: это не
# вотчер стратегии, а просто "напоминание" без TP/SL/состояний.
# get_active_watchers подмешивает его в общий список "В работе",
# delete_watchers умеет удалять из него же по level_id.
MANUAL_WATCHERS_PATH = os.path.join(JSONBANK_DIR, "manual_watchers.json")
# Поток А (боевой)/Поток Б (радар) — см. swing_hunter.py::get_battle_symbols
# / build_greylist_candidates. Те же физические файлы, что и там (там путь
# строится от os.path.dirname(MACRO_LEVELS_FILE) — тот же jsonbank).
WHITELIST_PATH = os.path.join(JSONBANK_DIR, "whitelist.json")
BLACKLIST_PATH = os.path.join(JSONBANK_DIR, "blacklist.json")
GREYLIST_PATH = os.path.join(JSONBANK_DIR, "greylist.json")
GREYLIST_LEVELS_PATH = os.path.join(JSONBANK_DIR, "greylist_levels.json")
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
        # 🎯 Объёмные триггеры (side="volume") — тоже ручные зоны, просто
        # без min/max. Раньше тут проверялись только supports/resistances —
        # монета с одним лишь volume-триггером не получала метку ✋ на
        # дашборде и не поднималась в приоритете вместе с остальными
        # ручными зонами (см. sort_by_priority ниже).
        return bool((c or {}).get("supports")) or bool((c or {}).get("resistances")) or bool((c or {}).get("volume_triggers"))

    def _is_ignore_auto(coin):
        c = custom_db.get(coin) or custom_db.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})
        return bool((c or {}).get("ignore_auto"))

    favorites = set(_read_favorites())
    # Свой TP/SL монеты (меню "⋮" -> ⚖️) — только для бейджа/подсказки на
    # дашборде, реально применяется в BounceWatcher.__init__ (см. app.py::
    # /api/config/coin_tpsl). Пусто у монеты -> используется общий.
    coin_tp_sl_cfg = _read_json(CONFIG_FILE, default={}).get("crypto", {}).get("coin_tp_sl", {}) or {}

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
            "ignore_auto": _is_ignore_auto(coin),
            "favorite": coin in favorites,
            "coin_tp_sl": coin_tp_sl_cfg.get(coin) or None,
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

    # 🎯 Ручные объёмные триггеры — читаются напрямую из manual_watchers.json,
    # никак не связаны с bounce_mgr/v_bottom_mgr (см. watcher_plan.py::
    # check_volume_triggers). Подмешиваем ДО блока ниже, который проставляет
    # source/added_at/favorite по watchlist — он отработает и для них тоже
    # (монета уже есть в watchlist с source="MANUAL", см. add_custom_level).
    manual_watchers = _read_json(MANUAL_WATCHERS_PATH, default=[])
    if isinstance(manual_watchers, list):
        for mw in manual_watchers:
            result.append({
                "level_id": mw.get("level_id"),
                "coin": mw.get("coin"),
                "direction": None,
                "strategy": "VOL_SPIKE",
                "mode": None,
                "state": "MANUAL_ALERT",
                "history_log": f"Объём {mw.get('fired_volume')} ≥ цель {mw.get('target_volume')} ({mw.get('fired_at', '')})",
                "level_min": None,
                "level_max": None,
                "level_date": None,
                "level_type": None,
                "level_score": None,
                "activated_at": None,
                "events": [],
                "updated_at": mw.get("created_at"),
            })

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


# level_id всегда "{TAG}_{trade_type}_{min}_{max}" (см. тот же словарь в
# background_tasks.py::_STRATEGY_BY_TAG) — по TAG однозначно восстанавливаем
# strategy, не таская сюда импорт всего background_tasks.py ради одной строки.
_TAG_TO_STRATEGY = {"VB": "V_BOTTOM", "VGB": "V_GREEN_BOTTOM", "VRT": "V_RED_TOP", "BC": "BOUNCE"}


@app.get("/api/watcher_debug_log/{level_id}")
def get_watcher_debug_log(level_id: str, coin: str = Query(default=None, description="Монета сигнала — нужна только для архивного фолбэка (умерший вотчер), для живого не обязательна")):
    """Лог ОДНОГО вотчера — то, что реально видит код (все _dbg() события:
    скан/пропуски с причинами/проколы/вход), не смешанный с другими
    уровнями/режимами той же монеты, как в общем файле на диске
    (bounce_logs/{coin}.log, logs/watchers/{coin}.log).

    Два источника, в этом порядке:
    1. ЖИВОЙ вотчер — кольцевой буфер watcher.debug_ring в памяти (см.
       _DEBUG_RING_SIZE в BounceWatcher/VBottomWatcher/VGreenBottomWatcher/
       VRedTopWatcher), последние ~400 строк. level_id ищем и в
       v_bottom_mgr._watchers (VB/VGB/VRT), и в bounce_mgr._watchers
       (BOUNCE) — один и тот же id не может быть сразу в обоих.
    2. АРХИВ (вотчер уже умер) — снимок debug_log, который
       background_tasks.py::export_dashboard_state/crypto_orchestrator
       сохраняет в watcher_history.json в момент смерти, под ключом
       "{coin}_{strategy}_{level_id}" (BOUNCE) или тем же форматом у
       V-семейства (см. комментарий там же). Тут нужен coin — level_id сам
       по себе (координаты уровня) не гарантирует уникальность без монеты,
       а strategy восстанавливаем из TAG в самом level_id.
    """
    from modules.cryptano.live_scan import v_bottom_mgr, bounce_mgr, _watcher_lock

    with _watcher_lock:
        watcher = v_bottom_mgr._watchers.get(level_id) or bounce_mgr._watchers.get(level_id)
        if watcher is not None:
            ring = getattr(watcher, "debug_ring", None)
            lines = list(ring) if ring is not None else []
            w_coin = getattr(watcher, "coin", None)
            return {"level_id": level_id, "coin": w_coin, "lines": lines, "source": "live"}

    # Вотчер не живой — пробуем архив, но только если знаем монету.
    if coin:
        tag = level_id.split("_", 1)[0]
        strategy = _TAG_TO_STRATEGY.get(tag)
        if strategy:
            history_db = _read_json(WATCHER_HISTORY_PATH, default={})
            hist_key = f"{coin}_{strategy}_{level_id}"
            record = history_db.get(hist_key)
            if record:
                return {
                    "level_id": level_id,
                    "coin": coin,
                    "lines": record.get("debug_log", []),
                    "source": "archive",
                    "died_at": record.get("died_at"),
                }

    raise HTTPException(status_code=404, detail="Лог не найден — вотчер умер и в архиве этого уровня ещё нет (записан только с момента обновления)")


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

class CoinTpSlRequest(BaseModel):
    coin: str
    tp: Optional[float] = None
    sl: Optional[float] = None

@app.get("/api/config/coin_tpsl/{coin}")
def get_coin_tpsl(coin: str):
    """Свой TP/SL монеты (watchlist, меню "⋮"). Пусто = используется общий
    bounce_tp_pct/bounce_sl_pct (см. /api/config/bounce_tpsl)."""
    config = _read_json(CONFIG_FILE, default={})
    coin_cfg = config.get("crypto", {}).get("coin_tp_sl", {}).get(coin)
    if not coin_cfg:
        return {"coin": coin, "tp": None, "sl": None}
    return {"coin": coin, "tp": coin_cfg.get("tp"), "sl": coin_cfg.get("sl")}

@app.post("/api/config/coin_tpsl")
def set_coin_tpsl(payload: CoinTpSlRequest):
    """Установить или сбросить свой TP/SL для монеты. tp/sl оба null (или
    оба отсутствуют) -> запись для монеты удаляется, монета возвращается на
    общий TP/SL."""
    try:
        config = _read_json(CONFIG_FILE, default={})
        if "crypto" not in config:
            config["crypto"] = {}
        if "coin_tp_sl" not in config["crypto"]:
            config["crypto"]["coin_tp_sl"] = {}
        if payload.tp is None and payload.sl is None:
            config["crypto"]["coin_tp_sl"].pop(payload.coin, None)
        else:
            config["crypto"]["coin_tp_sl"][payload.coin] = {
                "tp": payload.tp,
                "sl": payload.sl,
            }
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
    # 🎯 Объёмные триггеры — не зона (нет min/max), график их не рисует и не
    # должен, но окно "Управление ручными зонами" (см. app.js::
    # renderManageZonesList) должно уметь их показать/изменить/удалить —
    # для этого отдаём их отдельным ключом, supports/resistances не трогаем.
    custom_volume_triggers = custom_coin.get("volume_triggers", [])

    if data is None:
        if not custom_supports and not custom_resistances and not custom_volume_triggers:
            raise HTTPException(status_code=404, detail=f"No levels for {coin}")
        # Ручные уровни есть, а macro для монеты нет вообще (например,
        # SWING_HUNTER её пока не считал/не подхватил по объёму) —
        # честно отдаём то, что реально есть, не 404.
        return {"supports": list(custom_supports), "resistances": list(custom_resistances), "volume_triggers": list(custom_volume_triggers)}

    merged = dict(data)
    merged["supports"] = list(data.get("supports", [])) + list(custom_supports)
    merged["resistances"] = list(data.get("resistances", [])) + list(custom_resistances)
    merged["volume_triggers"] = list(custom_volume_triggers)
    return merged


@app.get("/api/greylist")
def get_greylist():
    """Витрина кандидатов Потока Б (радар, см. swing_hunter.py::
    build_greylist_candidates) — монеты, которые сканер нашёл сам по
    объёму, но которые НЕ в бою (macro_levels.json их не содержит,
    watcher_plan.py их физически не видит). Дашборд показывает их в
    отдельном окне "🔭 Серый список", а не в обычном Watchlist."""
    greylist = _read_json(GREYLIST_PATH, default={})
    if not isinstance(greylist, dict):
        greylist = {}
    greylist_levels = _read_json(GREYLIST_LEVELS_PATH, default={})
    if not isinstance(greylist_levels, dict):
        greylist_levels = {}
    # Ручная зона (➕ доступна и в сером списке) переводит монету на боевой
    # рельс — см. build_macro_levels в swing_hunter.py: coin получает
    # заглушку в macro_levels.json, а реальные уровни для вотчера идут из
    # custom_levels.json. Тут это нужно только для значка ✋ на дашборде.
    custom_db = _read_json(CUSTOM_LEVELS_PATH, default={})
    if not isinstance(custom_db, dict):
        custom_db = {}

    def _has_custom_levels(coin):
        c = custom_db.get(coin) or {}
        return bool(c.get("supports")) or bool(c.get("resistances")) or bool(c.get("volume_triggers"))

    def _is_ignore_auto(coin):
        c = custom_db.get(coin) or {}
        return bool(c.get("ignore_auto"))

    favorites = set(_read_favorites())

    result = []
    for coin, entry in greylist.items():
        if coin == "_meta" or not isinstance(entry, dict):
            continue
        lv = greylist_levels.get(coin) or {}
        result.append({
            "coin": coin,
            "symbol": entry.get("symbol"),
            "volume": entry.get("volume"),
            "discovered_at": entry.get("discovered_at"),
            "last_seen": entry.get("last_seen"),
            "supports_count": len(lv.get("supports", [])),
            "resistances_count": len(lv.get("resistances", [])),
            "has_custom_levels": _has_custom_levels(coin),
            "ignore_auto": _is_ignore_auto(coin),
            "favorite": coin in favorites,
        })

    # Избранные — по алфавиту сверху, остальные — по алфавиту следом (та же
    # схема приоритета, что и в get_watchlist()::sort_by_priority). Раньше
    # тут была сортировка по объёму — с добавлением избранного и ручного
    # добавления монет (без объёма вообще, см. /api/greylist/add) сортировка
    # по объёму перестала быть осмысленной для всего списка.
    result.sort(key=lambda r: (not r["favorite"], r["coin"]))
    return result


@app.get("/api/levels/greylist/{coin}")
def get_greylist_levels(coin: str):
    """Уровни ОДНОГО кандидата из greylist_levels.json — для графика,
    когда пользователь кликает по монете в окне "🔭 Серый список".
    Намеренно ОТДЕЛЬНЫЙ эндпоинт от GET /api/levels/{coin} (боевые +
    ручные) — смешивать их в одном ответе нельзя, это была бы утечка
    кандидата, которого watcher_plan.py не должен видеть, туда же, где
    смотрит боевой дашборд."""
    coin = coin.upper().strip()
    greylist_levels = _read_json(GREYLIST_LEVELS_PATH, default={})
    data = greylist_levels.get(coin) if isinstance(greylist_levels, dict) else None
    if data is None:
        raise HTTPException(status_code=404, detail=f"No greylist levels for {coin}")
    return data


@app.post("/api/greylist/promote")
def promote_greylist_coin(payload: dict = Body(...)):
    """[✅ Белый] / [🚫 Чёрный] в окне "Серый список". Тело: {"coin",
    "action": "whitelist"|"blacklist"}. Убирает монету из greylist.json и
    greylist_levels.json (кандидат решён, витрину дальше захламлять не
    нужно) и дописывает тикер в нужный список. Статус монеты официально
    меняется на СЛЕДУЮЩЕМ скане swing_hunter.py — тут только переносим её
    между списками, ничего не пересчитываем сразу же."""
    coin = str(payload.get("coin", "")).upper().strip()
    action = str(payload.get("action", "")).lower().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    if action not in ("whitelist", "blacklist"):
        raise HTTPException(status_code=400, detail="action должен быть 'whitelist' или 'blacklist'")

    target_path = WHITELIST_PATH if action == "whitelist" else BLACKLIST_PATH
    target_list = _read_json(target_path, default=[])
    if not isinstance(target_list, list):
        target_list = []
    if coin not in target_list:
        target_list.append(coin)
        try:
            _write_json_atomic(target_path, target_list)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить {os.path.basename(target_path)}: {e}")

    greylist = _read_json(GREYLIST_PATH, default={})
    if isinstance(greylist, dict) and coin in greylist:
        del greylist[coin]
        try:
            _write_json_atomic(GREYLIST_PATH, greylist)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить greylist.json: {e}")

    greylist_levels = _read_json(GREYLIST_LEVELS_PATH, default={})
    if isinstance(greylist_levels, dict) and coin in greylist_levels:
        del greylist_levels[coin]
        try:
            _write_json_atomic(GREYLIST_LEVELS_PATH, greylist_levels)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить greylist_levels.json: {e}")

    return {"status": "ok", "coin": coin, "action": action}


@app.post("/api/watchlist/demote")
def demote_watchlist_coin(payload: dict = Body(...)):
    """⋮-меню на строке боевого Watchlist → "В серый список" / "В чёрный
    список". Обратная операция к /api/greylist/promote: монета уходит ИЗ
    whitelist.json, поэтому убираем её и из macro_levels.json (иначе она
    провисит в Watchlist до следующего боевого скана). Тело: {"coin",
    "action": "greylist"|"blacklist"}.

    Для action=greylist — сразу переносим уже посчитанные уровни из
    macro_levels.json в greylist_levels.json (копия supports/resistances,
    updated_at=сейчас), чтобы окно "Серый список" не показывало пустого
    кандидата, пока его не пересчитает радар. Для action=blacklist уровни
    просто отбрасываем — чёрному списку они не нужны.

    Активные вотчеры (active_watchers.json) и ручные зоны
    (custom_levels.json) НЕ трогаем — они живут своей жизнью независимо
    от того, боевая монета сейчас или нет."""
    coin = str(payload.get("coin", "")).upper().strip()
    action = str(payload.get("action", "")).lower().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    if action not in ("greylist", "blacklist"):
        raise HTTPException(status_code=400, detail="action должен быть 'greylist' или 'blacklist'")

    # 1) Убираем из whitelist.json
    whitelist = _read_json(WHITELIST_PATH, default=[])
    if not isinstance(whitelist, list):
        whitelist = []
    if coin in whitelist:
        whitelist = [c for c in whitelist if str(c).upper().strip() != coin]
        try:
            _write_json_atomic(WHITELIST_PATH, whitelist)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить whitelist.json: {e}")

    # 2) Забираем (и удаляем) уровни монеты из macro_levels.json — общие
    # для обеих веток, дальше решаем, куда их деть.
    macro = _read_json(MACRO_LEVELS_PATH, default={})
    if not isinstance(macro, dict):
        macro = {}
    coin_levels = macro.pop(coin, None)
    if coin_levels is not None:
        try:
            _write_json_atomic(MACRO_LEVELS_PATH, macro)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить macro_levels.json: {e}")

    if action == "blacklist":
        blacklist = _read_json(BLACKLIST_PATH, default=[])
        if not isinstance(blacklist, list):
            blacklist = []
        if coin not in blacklist:
            blacklist.append(coin)
            try:
                _write_json_atomic(BLACKLIST_PATH, blacklist)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Не удалось сохранить blacklist.json: {e}")
        # На всякий случай чистим и возможную старую запись в greylist —
        # blacklist побеждает везде (см. get_battle_symbols/
        # build_greylist_candidates), но витрина не должна её всё равно показывать.
        greylist = _read_json(GREYLIST_PATH, default={})
        if isinstance(greylist, dict) and coin in greylist:
            del greylist[coin]
            try:
                _write_json_atomic(GREYLIST_PATH, greylist)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Не удалось сохранить greylist.json: {e}")
        greylist_levels = _read_json(GREYLIST_LEVELS_PATH, default={})
        if isinstance(greylist_levels, dict) and coin in greylist_levels:
            del greylist_levels[coin]
            try:
                _write_json_atomic(GREYLIST_LEVELS_PATH, greylist_levels)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Не удалось сохранить greylist_levels.json: {e}")

    else:  # action == "greylist"
        try:
            symbol = resolve_symbol(coin, exchange.markets) or resolve_symbol(KNOWN_TICKER_ALIASES.get(coin, ""), exchange.markets)
        except Exception:
            symbol = None

        scan_time = datetime.datetime.now().isoformat()

        greylist = _read_json(GREYLIST_PATH, default={})
        if not isinstance(greylist, dict):
            greylist = {}
        existing = greylist.get(coin) or {}
        greylist[coin] = {
            "symbol": symbol,
            "volume": existing.get("volume"),
            "discovered_at": existing.get("discovered_at", scan_time),
            "last_seen": scan_time,
        }
        try:
            _write_json_atomic(GREYLIST_PATH, greylist)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить greylist.json: {e}")

        if coin_levels is not None:
            greylist_levels = _read_json(GREYLIST_LEVELS_PATH, default={})
            if not isinstance(greylist_levels, dict):
                greylist_levels = {}
            greylist_levels[coin] = {
                "supports": coin_levels.get("supports", []),
                "resistances": coin_levels.get("resistances", []),
                "updated_at": scan_time,
            }
            try:
                _write_json_atomic(GREYLIST_LEVELS_PATH, greylist_levels)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Не удалось сохранить greylist_levels.json: {e}")

    return {"status": "ok", "coin": coin, "action": action}


@app.get("/api/blacklist")
def get_blacklist():
    """Чёрный список (blacklist.json) — плоский список тикеров, которые
    swing_hunter.py игнорирует и в Потоке А (боевом), и в Потоке Б (радаре),
    см. get_battle_symbols/build_greylist_candidates. Дашборд показывает
    его как одну из трёх сворачиваемых секций в левой панели."""
    blacklist = _read_json(BLACKLIST_PATH, default=[])
    if not isinstance(blacklist, list):
        blacklist = []
    return sorted({str(c).upper().strip() for c in blacklist if str(c).strip()})


@app.post("/api/blacklist/add")
def add_to_blacklist(payload: dict = Body(...)):
    """Добавить тикер в чёрный список вручную (➕ в заголовке секции
    "Чёрный список"). НЕ трогает whitelist.json/greylist.json — если монета
    уже была там, следующий скан swing_hunter.py сам её вычистит оттуда
    (blacklist — абсолютный приоритет, см. get_battle_symbols/
    build_greylist_candidates), тут форсировать это незачем."""
    coin = str(payload.get("coin", "")).upper().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    blacklist = _read_json(BLACKLIST_PATH, default=[])
    if not isinstance(blacklist, list):
        blacklist = []
    if coin not in blacklist:
        blacklist.append(coin)
        try:
            _write_json_atomic(BLACKLIST_PATH, blacklist)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить blacklist.json: {e}")
    return {"status": "ok", "coin": coin, "added": True}


@app.post("/api/whitelist/add")
def add_to_whitelist(payload: dict = Body(...)):
    """Быстрое добавление тикера в whitelist.json вручную (➕ в заголовке,
    режим Watchlist) — БЕЗ указания уровней/объёма, просто "монета теперь в
    бою".

    Уровни считаются СРАЗУ ЖЕ, синхронно (build_levels_for_single_coin —
    та же функция, что и для любых других разовых добавлений), а не
    откладываются до ближайшего 🏗️/rebuild — иначе монета molча висела бы
    в whitelist.json, ничем не отличаясь для пользователя от "ничего не
    произошло": rebuild гоняет ~70+ монет и может не случиться ещё долго,
    а обычный скан-цикл синхронизирует watchlist.json (то, что реально
    видно на дашборде) из macro_levels.json ТОЛЬКО в background_tasks.py,
    отдельно от rebuild. Тут делаем и то, и другое сами, за один запрос:
    честная пауза в несколько секунд (реальный поход на биржу за свечами),
    зато монета появляется в Watchlist сразу по ответу этого эндпоинта, а
    не когда-то потом.

    Blacklist — абсолютный приоритет (см. get_battle_symbols): если монета
    там, добавление в whitelist ничего не изменит в бою, поэтому отказываем
    сразу, а не молча добавляем бесполезную запись."""
    coin = str(payload.get("coin", "")).upper().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")

    blacklist = _read_json(BLACKLIST_PATH, default=[])
    if isinstance(blacklist, list) and coin in {str(c).upper().strip() for c in blacklist}:
        raise HTTPException(status_code=400, detail=f"{coin} в чёрном списке — сначала уберите её оттуда")

    whitelist = _read_json(WHITELIST_PATH, default=[])
    if not isinstance(whitelist, list):
        whitelist = []
    if coin not in whitelist:
        whitelist.append(coin)
        try:
            _write_json_atomic(WHITELIST_PATH, whitelist)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить whitelist.json: {e}")

    # Если монета уже была кандидатом радара — витрину дальше не захламляем,
    # она теперь боевая (та же чистка, что и в promote_greylist_coin).
    greylist = _read_json(GREYLIST_PATH, default={})
    if isinstance(greylist, dict) and coin in greylist:
        del greylist[coin]
        try:
            _write_json_atomic(GREYLIST_PATH, greylist)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить greylist.json: {e}")
    greylist_levels = _read_json(GREYLIST_LEVELS_PATH, default={})
    if isinstance(greylist_levels, dict) and coin in greylist_levels:
        del greylist_levels[coin]
        try:
            _write_json_atomic(GREYLIST_LEVELS_PATH, greylist_levels)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить greylist_levels.json: {e}")

    # watchlist.json — ОТДЕЛЬНЫЙ от whitelist.json файл, тот самый, что
    # реально читает дашборд (GET /api/watchlist). source="SWING_HUNTER" —
    # ЭТО НЕ ручная зона/уровень (то, что помечается "MANUAL"), а обычная
    # боевая монета из whitelist.json, просто без уровней технического
    # анализа до этого момента. С source="SWING_HUNTER" на неё действуют
    # обычные правила: чистка "призраков" в background_tasks.py уберёт её
    # из watchlist.json сама, если монету потом уберут из whitelist.json
    # (⋮-меню/правка файла руками) — никакой отдельной кнопки/пометки
    # "убрать монету из списка" ей не нужно, она не особенная.
    try:
        wl = _read_json(WATCHLIST_PATH, default={})
        if not isinstance(wl, dict):
            wl = {}
        if coin not in wl:
            wl[coin] = {"source": "SWING_HUNTER", "added_at": datetime.datetime.now().isoformat()}
            _write_json_atomic(WATCHLIST_PATH, wl)
    except Exception as e:
        print(f"[whitelist/add] не удалось обновить watchlist.json для {coin}: {e}")

    levels_built = False
    try:
        from modules.cryptano.swing_hunter import build_levels_for_single_coin
        levels_built = bool(build_levels_for_single_coin(coin))
    except Exception as e:
        print(f"[whitelist/add] не удалось построить уровни для {coin}: {e}")

    return {"status": "ok", "coin": coin, "added": True, "levels_built": levels_built}


@app.post("/api/greylist/add")
def add_to_greylist(payload: dict = Body(...)):
    """Быстрое добавление тикера-кандидата в greylist.json вручную (➕ в
    заголовке, режим Greylist) — БЕЗ объёма/уровней, просто "хочу видеть эту
    монету в серой витрине". supports_count/resistances_count будут 0, пока
    её не подхватит ближайший радар (build_greylist_candidates) — либо пока
    не нарисовать ей ручную зону (➕ "Добавить уровень"), тогда она сразу
    станет торгуемой (см. build_macro_levels в swing_hunter.py).

    Отказываем, если монета уже боевая (whitelist) или в чёрном списке —
    в обоих случаях запись в greylist была бы бессмысленной/противоречивой."""
    coin = str(payload.get("coin", "")).upper().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")

    blacklist = _read_json(BLACKLIST_PATH, default=[])
    if isinstance(blacklist, list) and coin in {str(c).upper().strip() for c in blacklist}:
        raise HTTPException(status_code=400, detail=f"{coin} в чёрном списке — сначала уберите её оттуда")

    whitelist = _read_json(WHITELIST_PATH, default=[])
    if isinstance(whitelist, list) and coin in {str(c).upper().strip() for c in whitelist}:
        raise HTTPException(status_code=400, detail=f"{coin} уже в Watchlist (whitelist)")

    try:
        symbol = resolve_symbol(coin, exchange.markets) or resolve_symbol(KNOWN_TICKER_ALIASES.get(coin, ""), exchange.markets)
    except Exception:
        symbol = None

    greylist = _read_json(GREYLIST_PATH, default={})
    if not isinstance(greylist, dict):
        greylist = {}
    existing = greylist.get(coin) or {}
    now = datetime.datetime.now().isoformat()
    greylist[coin] = {
        "symbol": symbol,
        "volume": existing.get("volume"),
        "discovered_at": existing.get("discovered_at", now),
        "last_seen": now,
    }
    try:
        _write_json_atomic(GREYLIST_PATH, greylist)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить greylist.json: {e}")

    return {"status": "ok", "coin": coin, "added": True}


@app.post("/api/blacklist/remove")
def remove_from_blacklist(payload: dict = Body(...)):
    """Убрать тикер из чёрного списка (🗑 у строки в секции "Чёрный
    список"). Монета просто становится снова видимой для радара — попадёт
    ли она в серый список заново, решит следующий скан по факту объёма,
    тут ничего форсом не переносим."""
    coin = str(payload.get("coin", "")).upper().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    blacklist = _read_json(BLACKLIST_PATH, default=[])
    if not isinstance(blacklist, list):
        blacklist = []
    if coin not in blacklist:
        raise HTTPException(status_code=404, detail="Монеты нет в чёрном списке")
    blacklist = [c for c in blacklist if str(c).upper().strip() != coin]
    try:
        _write_json_atomic(BLACKLIST_PATH, blacklist)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить blacklist.json: {e}")
    return {"status": "ok", "coin": coin, "removed": True}


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
    вернёт следующий пересчёт уровней. Тело: {"coin": "RAYDIUM"}.

    Заодно чистим whitelist.json, если монета там есть (см. add_to_whitelist
    — "➕ Просто добавить монету" кладёт её и туда, и в watchlist.json с
    source="MANUAL"). Раньше это было не нужно — MANUAL-монеты в
    whitelist.json никогда не попадали, их держала в бою только ручная зона
    через _add_manual_zone_coins. Теперь попадают, и без этой чистки монета
    молча возвращалась бы обратно на ближайшем 🏗️/скан-цикле — "удаление"
    было бы только на вид."""
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

    whitelist = _read_json(WHITELIST_PATH, default=[])
    if isinstance(whitelist, list) and coin in whitelist:
        whitelist = [c for c in whitelist if str(c).upper().strip() != coin]
        try:
            _write_json_atomic(WHITELIST_PATH, whitelist)
        except Exception as e:
            return {"status": "partial", "coin": coin, "removed": True,
                    "detail": f"Убрана с дашборда, но не удалось почистить whitelist.json: {e}"}

    return {"status": "ok", "coin": coin, "removed": True}


@app.post("/api/levels/custom/update")
def update_custom_level(payload: dict = Body(...)):
    """Редактирование ОДНОЙ ручной зоны (✏️ в окне ручных зон).
    Тело для support/resistance: {"coin", "side": "support"|"resistance",
    "old_min", "old_max", "min", "max"}. Зона ищется по точным старым
    min/max (своего id у ручных зон нет — так же ищет и удаление). Меняются
    границы и дата (сегодня): для бота это новая зона. Живой вотчер на
    старых границах, если есть, доживает как был — так же, как при
    удалении зоны.

    Тело для side="volume": {"coin", "side": "volume", "id", "target_volume"} —
    матчится по id (см. add_custom_level), "old_target" вместо "id" остаётся
    фолбэком для триггеров, добавленных до этой правки."""
    coin = str(payload.get("coin", "")).upper().strip()
    side = str(payload.get("side", "")).lower().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    if side not in ("support", "resistance", "volume"):
        raise HTTPException(status_code=400, detail="side должен быть 'support', 'resistance' или 'volume'")

    if side == "volume":
        trigger_id = payload.get("id")
        try:
            new_target = float(payload["target_volume"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="target_volume обязателен и должен быть числом")
        if new_target <= 0:
            raise HTTPException(status_code=400, detail="target_volume должен быть больше нуля")
        old_target = payload.get("old_target")
        try:
            old_target = float(old_target) if old_target is not None else None
        except (TypeError, ValueError):
            old_target = None
        if not trigger_id and old_target is None:
            raise HTTPException(status_code=400, detail="Нужен 'id' (или, для старых записей, 'old_target')")

        custom = _read_json(CUSTOM_LEVELS_PATH, default={})
        triggers = (custom.get(coin) or {}).get("volume_triggers", [])
        found = None
        for t in triggers:
            if trigger_id and t.get("id") == trigger_id:
                found = t
                break
            if not trigger_id and old_target is not None and abs(float(t.get("target", 0)) - old_target) < 1e-9:
                found = t
                break
        if found is None:
            raise HTTPException(status_code=404, detail="Такой триггер не найден (возможно, уже удалён/сработал)")
        found["target"] = new_target
        try:
            _write_json_atomic(CUSTOM_LEVELS_PATH, custom)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить custom_levels.json: {e}")
        return {"status": "ok", "coin": coin, "side": side, "target_volume": new_target}

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
    if side not in ("support", "resistance", "volume"):
        raise HTTPException(status_code=400, detail="side должен быть 'support', 'resistance' или 'volume'")

    # ❄️ "Заморозка" — галочка в той же модалке добавления уровня/объёма:
    # монета переходит на торговлю ТОЛЬКО по ручным зонам/объёму, авто-уровни
    # (whitelist/macro_levels.json) для неё игнорируются полностью, даже если
    # они посчитаны и лежат в macro_levels.json — см.
    # watcher_plan.py::get_merged_levels_for_coin. Флаг ставится/снимается
    # при КАЖДОМ добавлении уровня — отдельного переключателя нет специально,
    # чтобы не плодить лишний UI (см. обсуждение с пользователем).
    ignore_auto = bool(payload.get("ignore_auto", False))

    try:
        custom = _read_json(CUSTOM_LEVELS_PATH, default={})
        if coin not in custom:
            custom[coin] = {"supports": [], "resistances": [], "volume_triggers": []}
        custom[coin]["ignore_auto"] = ignore_auto

        if side == "volume":
            try:
                target_vol = float(payload["target_volume"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(status_code=400, detail="target_volume обязателен и должен быть числом")

            custom[coin].setdefault("volume_triggers", []).append({
                "id": uuid.uuid4().hex[:10],
                "target": target_vol,
                "added_at": datetime.datetime.now().isoformat(),
                "timeframe": "15m"
            })
            _write_json_atomic(CUSTOM_LEVELS_PATH, custom)
            zone = {"target_volume": target_vol}
        else:
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
            key = "supports" if side == "support" else "resistances"
            custom[coin].setdefault(key, []).append(zone)
            _write_json_atomic(CUSTOM_LEVELS_PATH, custom)
    except HTTPException:
        raise
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


def _remove_custom_volume_trigger(coin: str, trigger_id: Optional[str] = None, target: Optional[float] = None) -> bool:
    """Убирает ОДИН объёмный триггер из custom_levels.json[coin]["volume_triggers"].

    Матчит по id, если он есть (все триггеры, добавленные ПОСЛЕ этой
    правки, его имеют — см. add_custom_level). Если id не передан
    (старые записи без id, добавленные до этой правки) — фолбэк на
    точное совпадение target. Возвращает True, если что-то реально
    удалено."""
    custom = _read_json(CUSTOM_LEVELS_PATH, default={})
    coin_data = custom.get(coin)
    if not coin_data:
        return False

    triggers = coin_data.get("volume_triggers", [])

    def _close(a, b):
        return abs(a - b) < 1e-9

    if trigger_id:
        new_triggers = [t for t in triggers if t.get("id") != trigger_id]
    elif target is not None:
        new_triggers = [t for t in triggers if not _close(float(t.get("target", 0)), target)]
    else:
        return False

    if len(new_triggers) == len(triggers):
        return False

    coin_data["volume_triggers"] = new_triggers
    if not coin_data.get("supports") and not coin_data.get("resistances") and not new_triggers:
        custom.pop(coin, None)
    else:
        custom[coin] = coin_data

    _write_json_atomic(CUSTOM_LEVELS_PATH, custom)
    return True


@app.post("/api/levels/custom/delete")
def delete_custom_level(payload: dict = Body(...)):
    """Удаление вручную добавленного уровня из custom_levels.json (кнопка
    "✕"/🗑 у зоны в модалке "Управление ручными зонами" на дашборде).

    Тело запроса для support/resistance: {"coin", "side": "support"|
    "resistance", "min", "max"} — те же min/max, что были при добавлении
    (их отдаёт GET /api/levels/{coin}, там ручные зоны помечены
    "type": "MANUAL"). У ручных зон нет своего id, поэтому ищем зону по
    точному совпадению min/max в указанных монете и стороне — этого
    достаточно, дублей с одинаковыми min/max для одной монеты в UI не
    заводится (кнопка "+" не запрещает завести две одинаковые зоны, но
    это уже осознанный выбор пользователя, а не баг этого эндпоинта).

    Тело запроса для side="volume": {"coin", "side": "volume", "id"} —
    объёмные триггеры, в отличие от зон, СВОЙ id имеют (см. add_custom_level),
    поэтому матчатся по нему, а не по значению; "target_volume" вместо
    "id" остаётся фолбэком для триггеров, добавленных до этой правки.

    НЕ трогает watchlist.json — монета остаётся в списке наблюдения (у
    неё вполне может быть ещё один ручной уровень или обычные уровни от
    SWING_HUNTER); чистка "призраков" в watchlist — отдельная механика в
    background_tasks.py, тут её сознательно не дублируем."""
    coin = str(payload.get("coin", "")).upper().strip()
    side = str(payload.get("side", "")).lower().strip()
    if not coin:
        raise HTTPException(status_code=400, detail="Не указана монета (coin)")
    if side not in ("support", "resistance", "volume"):
        raise HTTPException(status_code=400, detail="side должен быть 'support', 'resistance' или 'volume'")

    if side == "volume":
        trigger_id = payload.get("id")
        target = payload.get("target_volume")
        try:
            target = float(target) if target is not None else None
        except (TypeError, ValueError):
            target = None
        if not trigger_id and target is None:
            raise HTTPException(status_code=400, detail="Нужен 'id' (или, для старых записей, 'target_volume')")
        try:
            removed = _remove_custom_volume_trigger(coin, trigger_id=trigger_id, target=target)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить custom_levels.json: {e}")
        if not removed:
            raise HTTPException(status_code=404, detail="Такой триггер не найден (возможно, уже удалён/сработал)")
        return {"status": "ok", "coin": coin, "side": side, "deleted": True}

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


def _macro_coin_set():
    data = _read_json(MACRO_LEVELS_PATH, default={})
    return {c for c in data.keys() if c != "_meta"}


def _greylist_coin_set():
    data = _read_json(GREYLIST_LEVELS_PATH, default={})
    return {c for c in data.keys() if c != "_meta"}


@app.get("/api/macro/coins")
def get_macro_coins(source: str = Query(default="whitelist", description="'whitelist' | 'greylist' | 'both' — какой список монет отдать панели SimList")):
    """
    Список монет для левой панели "🧪 SimList" (бывш. "Монеты для симуляции").

    source=whitelist (по умолчанию, старое поведение без параметра) — те же
    тикеры, что лежат ключами в macro_levels.json (их туда кладёт
    swing_hunter.build_macro_levels, топ-70 по объёму) — боевой список.

    source=greylist — кандидаты Потока Б (см. /api/greylist), ключи
    greylist_levels.json — те же монеты, что видны в окне "🔭 Серый
    список" на дашборде, но НЕ в бою. Даёт потестить стратегию на монетах,
    которые ещё не взяты в работу, до того как их туда брать.

    source=both — объединение обоих множеств (без дублей).

    Во всех случаях — просто список тикеров, без расчётов, само тестирование
    (свечи/уровни) не зависит от того, откуда взят список."""
    if source == "both":
        coins_set = _macro_coin_set() | _greylist_coin_set()
    elif source == "greylist":
        coins_set = _greylist_coin_set()
    else:
        source = "whitelist"
        coins_set = _macro_coin_set()
    coins = sorted(coins_set)
    return {"coins": coins, "count": len(coins), "source": source}


def _coin_source_filter_set(coin_source: Optional[str]):
    """None/"" -> None (без фильтра — все монеты с историей, старое
    поведение bulk до появления SimList). "whitelist"/"greylist"/"both" ->
    множество ТЕКУЩИХ тикеров этого списка (те же ключи, что отдаёт
    /api/macro/coins) — bulk потом пересекает его со списком монет, у
    которых реально есть уровни в истории за период (list_coins_with_levels),
    так что монета должна пройти оба фильтра: быть в выбранном списке
    СЕЙЧАС и иметь историю уровней ЗА ПЕРИОД."""
    if not coin_source:
        return None
    if coin_source == "both":
        return _macro_coin_set() | _greylist_coin_set()
    if coin_source == "greylist":
        return _greylist_coin_set()
    return _macro_coin_set()


@app.post("/api/simulate/{coin}")
def simulate_watcher(
    coin: str,
    start: str = Query(..., description="Дата/время старта симуляции, UTC. 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM'"),
    end: Optional[str] = Query(default=None, description="Конец периода, по умолчанию — самая свежая доступная свеча"),
    strategies: Optional[str] = Query(default=None, description="VB,VGB,VRT через запятую — какие из V-стратегий гонять. Пусто = все три (как раньше)"),
):
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
    tags = [t.strip().upper() for t in strategies.split(",") if t.strip()] if strategies else None
    try:
        return run_simulation(coin, start, end, strategies=tags)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка симуляции: {e}")


@app.get("/api/simulate/last")
def simulate_watcher_last():
    """Последний сохранённый результат V-симуляции (V_BOTTOM/VGB/VRT) —
    фронт дёргает это при открытии /simulator, чтобы результат пережил F5,
    тот же принцип, что и у /api/simulate_bounce/last, свой файл (см.
    simulate_engine.py::LAST_RUN_FILE)."""
    from modules.cryptano.simulator.simulate_engine import get_last_run
    return get_last_run() or {}


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
    coin_source: Optional[str] = Query(default=None, description="'whitelist' | 'greylist' — ограничить прогон только монетами этого списка (сейчас, на момент запуска). Пусто/не задано = все монеты с историей за период, как раньше."),
):
    """
    Прогоняет BOUNCE-симуляцию по ВСЕМ монетам, у которых были уровни
    хоть в одном снимке таймлайна за указанный период (см.
    bounce_simulate.list_coins_with_levels) — НЕ по сегодняшнему топ-70
    из /api/macro/coins, это разные списки для исторического периода.
    coin_source дополнительно пересекает этот список с текущим
    whitelist/greylist (см. _coin_source_filter_set) — два режима сразу:
    "по всем" (coin_source не задан) и "по выбранному списку".

    Без сетевой докачки свечей (top_up=False на каждую монету) и без
    перезаписи last_run.json одиночного симулятора (свой файл,
    last_all_run.json) — см. докстринг run_bulk_bounce_simulation.

    Одна упавшая монета не валит весь прогон — её ошибка просто попадает
    в results[coin]['error'], остальные монеты считаются как обычно.
    """
    from modules.cryptano.simulator.bounce_simulate import run_bulk_bounce_simulation
    coin_filter = _coin_source_filter_set(coin_source)
    try:
        return run_bulk_bounce_simulation(start, end, allow_long=allow_long, allow_short=allow_short, coin_filter=coin_filter)
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


@app.post("/api/simulate_bulk")
def simulate_bulk(
    start: str = Query(..., description="Дата/время старта периода, UTC. 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM'"),
    end: Optional[str] = Query(default=None, description="Конец периода, по умолчанию — сейчас"),
    strategies: Optional[str] = Query(default=None, description="VB,VGB,VRT через запятую — какие из V-стратегий гонять. Пусто = все три"),
    coin_source: Optional[str] = Query(default=None, description="'whitelist' | 'greylist' — ограничить прогон только монетами этого списка (сейчас, на момент запуска). Пусто/не задано = все монеты с историей за период, как раньше."),
):
    """
    Bulk-прогон V-семьи (V_BOTTOM/V_GREEN_BOTTOM/V_RED_TOP) по ВСЕМ
    монетам, у которых были уровни хоть в одном снимке периода — тот же
    принцип, что и /api/simulate_bounce_bulk, свой файл прогресса/
    результата (см. simulate_engine.py), друг другу не мешают. coin_source
    — тот же необязательный фильтр по текущему whitelist/greylist, что и
    у /api/simulate_bounce_bulk (см. _coin_source_filter_set).

    Уровни для каждой монеты читаются из локальной базы снимков
    (levels_history.py), сеть не трогают — см. докстринг simulate_engine.py.
    """
    from modules.cryptano.simulator.simulate_engine import run_bulk_v_simulation
    tags = [t.strip().upper() for t in strategies.split(",") if t.strip()] if strategies else None
    coin_filter = _coin_source_filter_set(coin_source)
    try:
        return run_bulk_v_simulation(start, end, strategies=tags, coin_filter=coin_filter)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка bulk-симуляции V: {e}")


@app.get("/api/simulate_bulk/progress")
def simulate_bulk_progress():
    """Прогресс идущего сейчас (или последнего завершённого) bulk-прогона
    V-семьи — фронт опрашивает раз в секунду, пока ждёт основной POST.
    {} если bulk ещё ни разу не запускали."""
    from modules.cryptano.simulator.simulate_engine import get_bulk_progress
    return get_bulk_progress() or {}


@app.get("/api/simulate_bulk/last")
def simulate_bulk_last():
    """Последний сохранённый результат bulk-прогона V-семьи — свой файл,
    не пересекается с /api/simulate_bounce_bulk/last."""
    from modules.cryptano.simulator.simulate_engine import get_last_all_run
    return get_last_all_run() or {}


@app.get("/api/signals")
def get_signals(limit: int = Query(default=50, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    """Сработавшие сигналы, свежие сверху, страницей limit/offset (таблица
    "Последние сигналы" — страничная пагинация, см. app.js::loadSignals).

    Раньше отдавала голый список (последние `limit`) — теперь объект
    {"items": [...], "total": N}: total нужен фронту, чтобы посчитать
    число страниц и знать, когда листать дальше некуда."""
    signals = _read_json(SIGNALS_PATH, default=[])
    if not isinstance(signals, list):
        signals = []
    # date хранится как "YYYY-MM-DD HH:MM" — сортируется лексикографически верно
    signals_sorted = sorted(signals, key=lambda s: s.get("date") or "", reverse=True)
    return {"items": signals_sorted[offset:offset + limit], "total": len(signals_sorted)}


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


@app.get("/api/indicators/{coin}")
def get_indicators(coin: str, timeframe: str = "15m"):
    """Индикаторы для графика (Supertrend/ADX/HMA/VWAP/Envelope) — только
    отображение, торговой логики не касается. Считаются по тем же свечам из
    candle_store, что и /api/ohlcv, поэтому времена точек совпадают со свечами.
    Любая ошибка здесь возвращает пустой набор — график не страдает."""
    coin = coin.upper().strip()
    try:
        symbol = resolve_symbol(coin, exchange.markets)
    except Exception:
        return {"indicators": {}}
    if not symbol or timeframe not in candle_store.TIMEFRAME_MS:
        return {"indicators": {}}
    try:
        from chart_indicators import compute_indicators  # type: ignore
        candles = candle_store.get_candles(symbol, timeframe, limit=None, around=None)
        return {"indicators": compute_indicators(candles)}
    except Exception as e:
        return {"indicators": {}, "error": str(e)}


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

        # 🎯 Ручные объёмные триггеры (manual_watchers.json) — не в
        # bounce_mgr/v_bottom_mgr вообще, поэтому отдельная независимая
        # чистка тем же списком level_ids — тот же "❌ Удалить" в панели
        # "В работе" одинаково работает и для них.
        manual_watchers = _read_json(MANUAL_WATCHERS_PATH, default=[])
        if isinstance(manual_watchers, list) and manual_watchers:
            level_ids_set = set(level_ids)
            new_manual = [mw for mw in manual_watchers if mw.get("level_id") not in level_ids_set]
            removed_manual = len(manual_watchers) - len(new_manual)
            if removed_manual:
                _write_json_atomic(MANUAL_WATCHERS_PATH, new_manual)
                deleted_count += removed_manual

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