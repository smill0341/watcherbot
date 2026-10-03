# modules/cryptano/simulator/simulate_engine.py
"""
Симулятор стратегий "как будто это было онлайн".

Идея: пользователь кликает точку старта на графике → мы
1) берём 15m-свечи от этой даты до сейчас из candle_store (той же базы,
   что использует дашборд и боевой watcher_plan.py);
2) прогоняем их по одной через ТЕ ЖЕ САМЫЕ производственные классы
   вотчеров (VBottomManager + evaluate_v_bottom/v_green_bottom/v_red_top),
   что и в бою — никаких отдельных копий логики, чтобы бой и симулятор
   не расходились, как разошлись master_bot и test/testswing;
3) уровни (supports/resistances) на каждом шаге берём из локальной базы
   снимков (levels_history.py::get_levels_snapshot()) — та же "машина
   времени", что и у BOUNCE-симулятора.

Уровни ОБНОВЛЯЮТСЯ раз в календарный день по ходу прогона (см.
_refresh_levels_if_needed), а не замораживаются один раз на дату старта.
Причина: при заморозке уровень, появившийся ПОСЛЕ даты старта (бот
пересчитал macro_levels уже во время прогона), был для симулятора
невидим до конца теста — хотя цена его реально пробивала. Уже открытый
tracked-уровень при обновлении не меняется, обновление касается только
поиска НОВЫХ пробоев и opposite_levels для расчёта TP.

Ничего в боевые файлы (macro_levels.json, watcher_state.json,
tracked_origin_levels*.json) не пишет — только читает. Единственная
точка, где симулятор трогает боевое состояние — кнопка "Добавить в
работу" на фронте, это отдельный явный шаг, сюда не входит.

🔥 РАСШИРЕНИЕ (паритет с BOUNCE-симулятором, bounce_simulate.py):
- Можно выбрать, какие из V_BOTTOM/V_GREEN_BOTTOM/V_RED_TOP реально
  гонять (strategies=None — все три, как раньше).
- Результат теперь несёт "trades" — тот же формат, что у BOUNCE
  (entry/target/stop/status/result_percent/...), таблица сделок на
  фронте больше не "не поддерживается бэкендом".
- Есть bulk-прогон по всем монетам периода (run_bulk_v_simulation),
  как run_bulk_bounce_simulation у BOUNCE — свой прогресс-файл и свой
  файл последнего результата, не пересекаются с bounce-файлами.

Логирование внутри симулятора убрано целиком — будет сделано отдельно,
внутри самих стратегий (там оно разное для каждой), а не здесь.

Независимо найден и починен баг: _walk_short передавал в
evaluate_v_red_top score-отфильтрованный supports вместо
нефильтрованного, как в бою (watcher_plan.py::check_v_red_top).
"""

import os
import sys
import datetime
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MASTER_BOT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _MASTER_BOT_ROOT not in sys.path:
    sys.path.insert(0, _MASTER_BOT_ROOT)

from backend.modules.cryptano.utils.common import exchange, calculate_rsi, resolve_symbol
from backend.modules.cryptano.utils.market_cache import load_markets_cached
from backend.modules.cryptano.utils.indicators import calculate_atr, calculate_ema
from backend.modules.cryptano.utils.storage import load_json, save_json_atomic
import backend.modules.cryptano.levels.candle_store as candle_store
from backend.modules.cryptano.levels.levels_history import get_levels_snapshot
from backend.modules.cryptano.strategy.vbottom_manager import VBottomManager
from backend.modules.cryptano.strategy.v_bottom_watcher import VBottomWatcher
from backend.modules.cryptano.strategy.v_green_bottom_watcher import VGreenBottomWatcher
from backend.modules.cryptano.strategy.v_red_top_watcher import VRedTopWatcher
from backend.modules.cryptano.watcher_plan import (
    _find_fresh_breach,
    _find_fresh_breach_up,
    VBOTTOM_BREATH_BUFFER_PCT,
    MIN_LEVEL_SCORE,
)
from backend.modules.cryptano.simulator.outcome import compute_trade_outcome, make_ema_dist_fn, load_candles_4h
# list_coins_with_levels/_aggregate_trades — тот же "какие монеты вообще
# имели уровни в этом периоде" и "схлопнуть сделки в сводку по монете",
# что уже строит bounce_simulate.py для своего bulk-отчёта. Переиспользуем
# один код вместо копии — иначе отчёты BOUNCE/V бы неизбежно разъехались
# при следующей независимой правке одного из файлов.
from backend.modules.cryptano.simulator.bounce_simulate import list_coins_with_levels, _aggregate_trades, SIM_LOG_DIR, _reset_sim_log

_WATCHER_CLS = {"VB": VBottomWatcher, "VGB": VGreenBottomWatcher, "VRT": VRedTopWatcher}

# Теги стратегий V-семьи — те же самые, что уже используются как ключи
# конфига вкл/выкл на боевом дашборде (см. app.py::STRATEGY_TAGS) и как
# префикс level_id у боевых вотчеров (см. background_tasks.py::
# _STRATEGY_BY_TAG). Здесь — список того, что вообще можно выбрать в
# симуляторе; отсутствие параметра strategies = гонять все три (старое
# поведение, обратная совместимость с уже существующими вызовами).
ALL_V_STRATEGY_TAGS = ("VB", "VGB", "VRT")
_TAG_TO_STRATEGY_NAME = {"VB": "V_BOTTOM", "VGB": "V_GREEN_BOTTOM", "VRT": "V_RED_TOP"}

# Локальный кэш симулятора — намеренно ОТДЕЛЬНАЯ папка от боевых JSON
# (macro_levels.json и т.д.), чтобы повторные прогоны по одной и той же
# монете/дате не дёргали биржу заново, и чтобы это никак не пересекалось
# с боевыми файлами бота.
SIM_CACHE_DIR = os.path.join(_THIS_DIR, "cache")
os.makedirs(SIM_CACHE_DIR, exist_ok=True)

# Сколько свечей контекста тянем в каждый шаг для индикаторов (ATR/ATR_slow(100)/
# EMA50/RSI) — с запасом над ATR_slow, которому нужно 14+100 свечей.
CANDLE_WINDOW = 300

# Дедлайн для честного исхода сделки (compute_trade_outcome) — TP либо
# закрытие по дедлайну, без искусственного стоп-лосса. То же значение по
# умолчанию, что у BOUNCE (BounceWatcher.CONFIG['MAX_HOLD_DAYS']) — V-семья
# своего отдельного конфига для этого не имеет, берём тот же ориентир,
# чтобы результаты обеих семей были сравнимы друг с другом.
V_MAX_HOLD_DAYS = 14

# Свои файлы — НЕ пересекаются с LAST_RUN_FILE/LAST_ALL_RUN_FILE/
# BULK_PROGRESS_FILE у BOUNCE (bounce_simulate.py), иначе одиночный/bulk
# прогон одной семьи затирал бы результат другой.
LAST_RUN_FILE = os.path.join(_THIS_DIR, "last_run_v.json")
LAST_ALL_RUN_FILE = os.path.join(_THIS_DIR, "last_all_run_v.json")
BULK_PROGRESS_FILE = os.path.join(_THIS_DIR, "bulk_progress_v.json")

# Тот же принцип, что и BULK_MAX_WORKERS у BOUNCE — отдельная переменная
# окружения, чтобы не делить один и тот же лимит между двумя семьями,
# если когда-нибудь захочется гонять bulk BOUNCE и bulk V параллельно.
BULK_MAX_WORKERS = int(os.environ.get("V_BULK_WORKERS", "4"))


def _levels_at(symbol, coin, target_ts_ms, start_dt=None):
    """Уровни ровно на дату старта — ОДИН раз, до начала прохода по свечам,
    и дальше статично на весь прогон (та же схема, что была и раньше —
    меняется только ОТКУДА берутся данные, не когда и как часто).

    Источник — локальная база снимков (levels_history.py::
    get_levels_snapshot()), та же самая "машина времени", что уже
    использует BOUNCE-симулятор (bounce_simulate.py). Снимок — это
    macro_levels.json, который сам боевой бот сохранял во время своих
    сканов (раз в ~12ч, см. snapshot_bucket_start), а не пересчёт заново
    через build_levels() — то есть ровно то, что боевой бот "видел" на
    эту дату, без похода на биржу вообще.

    start_dt — tz-aware pandas.Timestamp даты старта симуляции (тот же
    момент, что дал target_ts_ms). Если снимка на эту дату/месяц вообще
    нет (see get_levels_snapshot — есть fallback на предыдущий месяц без
    проверки свежести), coin_data будет пустым и вернём пустые списки —
    вызывающий код (run_simulation) это уже умеет обрабатывать как
    "уровней нет"."""
    if start_dt is None:
        start_dt = pd.Timestamp(target_ts_ms, unit="ms", tz="UTC")
    start_naive = start_dt.tz_localize(None) if start_dt.tzinfo is not None else start_dt
    snapshot = get_levels_snapshot(start_naive.to_pydatetime())
    coin_data = (snapshot or {}).get(coin) or {}
    return {
        "supports": coin_data.get("supports", []),
        "resistances": coin_data.get("resistances", []),
    }


def _levels_day_bucket(ts):
    """Календарный день (UTC, без времени) свечи — граница, на которой
    _refresh_levels_if_needed() решает, что пора взять новый снимок.

    ⚠️ Сознательно НЕ 12-часовая сетка снимков (snapshot_bucket_start в
    levels_history.py, которой пользуется бой) — пробовали 1-в-1 с боем,
    но по факту уровни между соседними 12ч-снимками почти не отличаются,
    а сделок и так немного (4-6 в месяц), так что смысла нет — решили
    остаться на дне, посмотреть на результатах."""
    ts_naive = ts.tz_localize(None) if ts.tzinfo is not None else ts
    return ts_naive.normalize()


def _refresh_levels_if_needed(coin, ts, cache):
    """Обновляет уровни НЕ ЧАЩЕ раза в календарный день — вместо заморозки
    одного снимка на дату старта на весь прогон (см. _levels_at выше и
    докстринг модуля): иначе уровень, появившийся ПОСЛЕ даты старта, весь
    прогон оставался бы невидимым, хотя цена его реально пробивала.

    cache живёт на весь один _walk_long()/_walk_short() (создаётся там,
    передаётся сюда на каждой свече) — при смене дня заново читает
    get_levels_snapshot(), иначе отдаёт то, что уже лежит в cache. Как и
    раньше, только ЛОКАЛЬНАЯ БД (levels_history.py), никакого похода на
    биржу внутри прохода по свечам.

    В cache после вызова: "day", "supports" (score-отфильтрованный, для
    поиска пробоя вниз), "supports_all" (БЕЗ фильтра — opposite_levels для
    V_RED_TOP), "resistances_all" (БЕЗ фильтра — opposite_levels для
    VB/VGB), "resistances_scored" (score-отфильтрованный, для поиска
    пробоя вверх)."""
    day = _levels_day_bucket(ts)
    if day == cache.get("day"):
        return
    ts_naive_dt = (ts.tz_localize(None) if ts.tzinfo is not None else ts).to_pydatetime()
    snapshot = get_levels_snapshot(ts_naive_dt)
    coin_data = (snapshot or {}).get(coin) or {}
    supports_all = coin_data.get("supports", [])
    resistances_all = coin_data.get("resistances", [])
    cache["day"] = day
    cache["supports_all"] = supports_all
    cache["supports"] = [s for s in supports_all if s.get("score", 0) >= MIN_LEVEL_SCORE]
    cache["resistances_all"] = resistances_all
    cache["resistances_scored"] = [r for r in resistances_all if r.get("score", 0) >= MIN_LEVEL_SCORE]


def _remember_seen_levels(seen, levels, is_support):
    """Копит уникальные уровни, реально попадавшиеся во время прохода (для
    отрисовки на графике, levels_out в run_simulation) — раз уровни теперь
    могут меняться раз в день внутри одного прогона, единственного
    "снимка на старт" для этого больше нет, показываем СОЮЗ всего, что
    встретилось. Дедуп по (min, max, type, is_support) — один и тот же
    уровень с одинаковыми границами из соседних дней не дублируем."""
    if seen is None:
        return
    for lv in levels:
        key = (round(float(lv["min"]), 8), round(float(lv["max"]), 8), lv.get("type"), is_support)
        if key not in seen:
            seen[key] = {
                "min": lv["min"], "max": lv["max"], "type": lv.get("type"),
                "score": lv.get("score"), "is_support": is_support,
            }


def _candles_df(symbol, top_up=True):
    """Вся доступная 15m-история из candle_store (та же база, что у дашборда).

    top_up=True (по умолчанию) — перед чтением подтягивает самые свежие
    свечи с биржи. top_up=False — только локальные данные, без единого
    сетевого похода (для bulk-прогона по списку монет, где период всегда
    в прошлом — см. тот же приём в bounce_simulate.py::_candles_df)."""
    if candle_store is None:
        raise RuntimeError("candle_store недоступен")
    if top_up and candle_store.has_data(symbol, "15m"):
        candle_store.top_up_tail(exchange, symbol, "15m")
    candles = candle_store.get_candles(symbol, "15m")
    if not candles:
        raise RuntimeError(f"Нет локальной 15m-истории для {symbol} — монета ещё не в candle_store")

    df = pd.DataFrame(candles)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.drop(columns=["time"]).set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _pre_register(mgr, level, trade_type, tag, coin):
    """
    Возвращает вотчер для этого уровня, создавая его только если он ещё
    не существует (зеркалит is_new_watcher-ветку внутри evaluate_*, но без
    срабатывания _replay_watcher — мы уже сами честно нашли точку пробоя
    проходом по свечам, повторный реплей в узком окне CANDLE_WINDOW только
    всё портит).

    on_breach_start() вызывается ТОЛЬКО если вотчер уже существовал —
    ровно как в бою: на первом пробитии вотчера ещё нет физически, поэтому
    notify_breach() там не находит его и ничего не делает; на повторном
    заходе тот же вотчер переиспользуется и breach_count растёт."""
    level_id = mgr._level_id(level, trade_type, tag)
    watcher = mgr._watchers.get(level_id)
    if watcher is None:
        # log_dir_override=SIM_LOG_DIR — та же папка, что у BOUNCE-симулятора
        # (modules/cryptano/simulator/logs/), но файл называется {coin}_V.log,
        # не {coin}.log, чтобы не пересекаться с логами BOUNCE (см.
        # VBottomWatcher._dbg). Без этого V-вотчеры писали в боевую
        # logs/watchers/ — симулятор их логи там никто не смотрит.
        watcher = _WATCHER_CLS[tag](level["min"], level["max"], trade_type, coin=coin, log_dir_override=SIM_LOG_DIR)
        mgr._watchers[level_id] = watcher
    elif hasattr(watcher, "on_breach_start"):
        watcher.on_breach_start()
    return level_id, watcher


def _episode(mgr, level_id, strategy, direction, coin, level=None):
    """level — тот самый словарь уровня (min/max/type/date/score), на
    котором создан вотчер (см. вызовы ниже — там ещё есть `tracked` в
    скоупе, поэтому передаём его напрямую, а не пытаемся угадывать через
    getattr(watcher, "min", ...) — у V-вотчеров нет гарантии, что такие
    атрибуты вообще называются так же, как у BounceWatcher).

    БЕЗ этого поля level_min/level_max/level_type/level_date/level_score
    отсутствовали в episode вовсе — фронт (sim.js::drawTradeDetail) не
    находил границы зоны и молча ничего не рисовал: ни полосы уровня, ни
    маркеров пути, ни даже точки входа (тот же формат, что уже строит
    _episode() в bounce_simulate.py, чтобы фронт рисовал обе семьи одним
    и тем же кодом)."""
    watcher = mgr._watchers.get(level_id)
    if watcher is None:
        return None
    level = level or {}
    return {
        "level_id": level_id,
        "coin": coin,
        "strategy": strategy,
        "direction": direction,
        "state": getattr(watcher, "state", "UNKNOWN"),
        "level_min": level.get("min"),
        "level_max": level.get("max"),
        "level_date": level.get("date"),
        "level_type": level.get("type"),
        "level_score": level.get("score"),
        "level_reaction_count": level.get("reaction_count"),
        "events": list(getattr(watcher, "event_log", [])),
    }


def _build_trade(result, tracked, strategy, direction, coin, entry_ts, df_full, ema_dist_fn=None):
    """Собирает запись сделки из результата evaluate_v_bottom/
    v_green_bottom/v_red_top (result — тот же словарь entry_price/sl/tp/
    level_id, что уже уходит в save_signal() в watcher_plan.py, см. её
    check_v_bottom/check_v_green_bottom/check_v_red_top) — тот же формат,
    что и trades у BOUNCE (bounce_simulate.py), чтобы фронт рисовал обе
    семьи ОДНОЙ и той же таблицей/функцией без переделки.

    level_type берём из tracked (не из result — result его не несёт, ровно
    как в боевом коде: там тоже tracked.get("type") идёт в save_signal,
    а не что-то из result)."""
    entry_price = result.get("entry_price", 0.0)
    sl = result.get("sl", 0.0)
    tp = result.get("tp", 0.0)
    level_id = result.get("level_id", "unknown")

    if direction == "LONG":
        rr = (tp - entry_price) / (entry_price - sl) if entry_price > sl else 0
    else:
        rr = (entry_price - tp) / (sl - entry_price) if sl > entry_price else 0

    outcome = compute_trade_outcome(entry_ts, entry_price, tp, direction, df_full, V_MAX_HOLD_DAYS)

    return {
        "date": entry_ts.strftime("%Y-%m-%d %H:%M"),
        "time": int(entry_ts.timestamp()),
        "coin": coin,
        "type": direction,
        "source": strategy,
        "entry": entry_price,
        "target": tp,
        "stop": sl,
        "rr": round(rr, 2),
        "level_id": level_id,
        "level_min": tracked.get("min"),
        "level_max": tracked.get("max"),
        "level_type": tracked.get("type"),
        "level_reaction_count": tracked.get("reaction_count"),
        "status": outcome["status"],
        "result_percent": outcome["result_percent"],
        "closed_at": outcome["closed_at"],
        "mae_pct": outcome["mae_pct"],
        "ema_dist_pct": ema_dist_fn(entry_ts, entry_price) if ema_dist_fn else None,
        # V-семья не завязана на объём как триггер (в отличие от BOUNCE) —
        # оставляем method пустым, фронт сам покажет "—" в колонке "Метод"
        # (см. _tradeRowHtml в sim.js: метод рисуется только для method=="volume").
        "method": None,
        "method_value": None,
        "method_mult": None,
    }


def _walk_long(df_full, start_i, end_i, strategy, coin, seen_levels=None, ema_dist_fn=None):
    """
    Прогоняет V_BOTTOM или V_GREEN_BOTTOM от свечи start_i до end_i —
    зеркало check_v_bottom()/check_v_green_bottom() из watcher_plan.py,
    только свечи идут одна за одной по истории, а не по одной за реальный
    скан.

    🔥 В отличие от прежней версии — после срабатывания сигнала уровень
    СНИМАЕТСЯ со слежения (tracked = None), а не держится до конца периода
    навсегда. Это зеркалит check_v_bottom() в бою (там — явный
    `del tracked_levels[track_key]` сразу после сигнала) и нужно, чтобы на
    длинном историческом периоде (недели/месяцы, особенно в bulk-прогоне)
    находились ВСЕ независимые пробои подряд, а не только самый первый.

    Уровни (supports/resistances) больше НЕ статичны на весь прогон — раз
    в календарный день перечитываются из локальной БД снимков (см.
    _refresh_levels_if_needed): иначе уровень, появившийся ПОСЛЕ даты
    старта, весь прогон оставался бы невидимым, хотя цена его реально
    пробивала. tracked, который уже в работе, при этом не меняется —
    обновление касается только поиска НОВЫХ пробоев и opposite_levels для
    расчёта TP."""
    tag = "VB" if strategy == "V_BOTTOM" else "VGB"
    mgr = VBottomManager()
    tracked = None
    episodes = []
    trades = []
    levels_cache = {}

    for i in range(start_i, end_i + 1):
        lo = max(0, i - CANDLE_WINDOW)
        window = df_full.iloc[lo:i + 1]
        if len(window) < 3:
            continue

        candle_ts = window.index[-1]
        _refresh_levels_if_needed(coin, candle_ts, levels_cache)
        supports = levels_cache["supports"]
        resistances = levels_cache["resistances_all"]
        _remember_seen_levels(seen_levels, supports, True)
        _remember_seen_levels(seen_levels, levels_cache["resistances_scored"], False)

        c_close = float(window["close"].iloc[-1])
        c_low = float(window["low"].iloc[-1])
        prev_close = float(window["close"].iloc[-2])

        c_atr = None
        if strategy == "V_GREEN_BOTTOM":
            atr_series = calculate_atr(window, 14)
            if not atr_series.empty and pd.notna(atr_series.iloc[-1]):
                c_atr = float(atr_series.iloc[-1])

        if tracked is None:
            found = _find_fresh_breach(supports, c_close, c_low, prev_close)
            if found is None:
                continue
            tracked = dict(found)
            level_id, watcher = _pre_register(mgr, tracked, "LONG", tag, coin)
        else:
            origin_max = tracked["max"] * (1 + VBOTTOM_BREATH_BUFFER_PCT / 100.0)
            if c_close > origin_max:
                level_id = mgr._level_id(tracked, "LONG", tag)
                watcher = mgr._watchers.get(level_id)
                if watcher is not None and hasattr(watcher, "_reset_chain"):
                    watcher._reset_chain()
                ep = _episode(mgr, level_id, strategy, "LONG", coin, tracked)
                if ep:
                    episodes.append(ep)
                tracked = None
                continue

        if strategy == "V_BOTTOM":
            result = mgr.evaluate_v_bottom(tracked, window, "LONG", resistances, trend="UNKNOWN", c_atr=None, coin=coin)
        else:
            result = mgr.evaluate_v_green_bottom(tracked, window, "LONG", resistances, trend="UNKNOWN", c_atr=c_atr, coin=coin)

        if result and result.get("allow"):
            entry_ts = window.index[-1]
            trades.append(_build_trade(result, tracked, strategy, "LONG", coin, entry_ts, df_full, ema_dist_fn))

            level_id = mgr._level_id(tracked, "LONG", tag)
            ep = _episode(mgr, level_id, strategy, "LONG", coin, tracked)
            if ep:
                episodes.append(ep)
            tracked = None

    if tracked is not None:
        level_id = mgr._level_id(tracked, "LONG", tag)
        ep = _episode(mgr, level_id, strategy, "LONG", coin, tracked)
        if ep:
            episodes.append(ep)

    return episodes, trades


def _walk_short(df_full, start_i, end_i, coin, seen_levels=None, ema_dist_fn=None):
    """Зеркало check_v_red_top() — один уровень может дать несколько
    сигналов подряд, поэтому снимаем со слежения только когда вотчер
    реально закончил работу (TRIGGERED/DEAD/IDLE), а не после 1-го сигнала
    (это уже так и было — V_RED_TOP, в отличие от VB/VGB выше, и раньше
    умел находить несколько сделок подряд на одном уровне).

    resistances — score-отфильтрованный (для поиска пробоя, зеркалит
    check_v_red_top). supports_all — БЕЗ фильтра по score (opposite_levels
    для evaluate_v_red_top) — 🐛 раньше сюда по ошибке передавался
    отфильтрованный supports; в бою (watcher_plan.py::check_v_red_top,
    строка `supports = coin_macro.get("supports", [])`) фильтра там нет
    вообще — починил, чтобы не расходиться с боевым поведением.

    Уровни, как и в _walk_long, обновляются раз в календарный день (см.
    _refresh_levels_if_needed), а не заморожены на дату старта."""
    mgr = VBottomManager()
    tracked = None
    seen_ids = set()
    trades = []
    recorded_keys = set()
    # level_id -> словарь уровня (min/max/type/date/score), на котором он
    # создан — нужно для _episode() в конце функции. Одна SHORT-зона может
    # дать несколько сигналов подряд, поэтому просто "текущий tracked" на
    # момент конца функции не годится — level_id-ов может быть больше
    # одного (разные уровни последовательно по ходу периода), поэтому
    # запоминаем КАЖДЫЙ level_id со своим уровнем сразу, как он появился.
    level_by_id = {}
    levels_cache = {}

    for i in range(start_i, end_i + 1):
        lo = max(0, i - CANDLE_WINDOW)
        window = df_full.iloc[lo:i + 1]
        if len(window) < 110:  # запас под ATR_slow(100)
            continue

        candle_ts = window.index[-1]
        _refresh_levels_if_needed(coin, candle_ts, levels_cache)
        resistances = levels_cache["resistances_scored"]
        supports_all = levels_cache["supports_all"]
        _remember_seen_levels(seen_levels, levels_cache["supports"], True)
        _remember_seen_levels(seen_levels, resistances, False)

        c_close = float(window["close"].iloc[-1])
        c_high = float(window["high"].iloc[-1])
        prev_close = float(window["close"].iloc[-2])

        atr_series = calculate_atr(window, 14)
        c_atr = float(atr_series.iloc[-1]) if not atr_series.empty and pd.notna(atr_series.iloc[-1]) else None
        atr_slow_series = atr_series.rolling(window=100).mean()
        c_atr_slow = (
            float(atr_slow_series.iloc[-1])
            if not atr_slow_series.empty and pd.notna(atr_slow_series.iloc[-1])
            else c_atr
        )
        ema_series = calculate_ema(window, period=50)
        c_ema = float(ema_series.iloc[-1]) if not ema_series.empty and pd.notna(ema_series.iloc[-1]) else None
        rsi_series = calculate_rsi(window.reset_index(drop=True))
        c_rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty and pd.notna(rsi_series.iloc[-1]) else None

        if tracked is None:
            found = _find_fresh_breach_up(resistances, c_close, c_high, prev_close)
            if found is None:
                continue
            tracked = dict(found)
            level_id, watcher = _pre_register(mgr, tracked, "SHORT", "VRT", coin)

        result = mgr.evaluate_v_red_top(
            tracked, window, "SHORT", supports_all, trend="UNKNOWN",
            c_atr=c_atr, c_atr_slow=c_atr_slow, c_ema=c_ema, c_rsi=c_rsi, coin=coin,
        )

        level_id = mgr._level_id(tracked, "SHORT", "VRT")
        seen_ids.add(level_id)
        level_by_id.setdefault(level_id, tracked)

        if result and result.get("allow"):
            entry_ts = window.index[-1]
            dedup_key = (level_id, result.get("level_id"), str(entry_ts))
            if dedup_key not in recorded_keys:
                recorded_keys.add(dedup_key)
                trades.append(_build_trade(result, tracked, "V_RED_TOP", "SHORT", coin, entry_ts, df_full, ema_dist_fn))

        watcher = mgr._watchers.get(level_id)
        if watcher is not None and getattr(watcher, "state", None) in ("TRIGGERED", "DEAD", "IDLE"):
            tracked = None

    episodes = []
    for level_id in seen_ids:
        ep = _episode(mgr, level_id, "V_RED_TOP", "SHORT", coin, level_by_id.get(level_id))
        if ep:
            episodes.append(ep)
    return episodes, trades


def run_simulation(coin, start_time_str, end_time_str=None, strategies=None, top_up=True, save_last_run=True):
    """
    coin: тикер, напр. 'BTC'
    start_time_str: дата/время старта, 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM' (UTC)
    end_time_str: конец периода, по умолчанию — самая свежая доступная свеча
    strategies: список тегов из ALL_V_STRATEGY_TAGS ("VB"/"VGB"/"VRT") —
        какие из трёх реально гонять. None (по умолчанию) = все три, старое
        поведение не меняется для вызовов без этого параметра.
    top_up: докачивать ли свежий хвост свечей перед прогоном — False для
        bulk (см. run_bulk_v_simulation), где период исторический.
    save_last_run: сохранять ли результат в LAST_RUN_FILE для восстановления
        одиночной страницы симулятора после F5 — False для bulk.

    Возвращает {"coin", "start_time", "end_time", "active", "history",
    "levels", "trades"} — trades в том же формате, что у BOUNCE-симулятора
    (bounce_simulate.py::run_bounce_simulation), events — тот же формат,
    что и боевой /api/events/{coin}.
    """
    coin = coin.upper().replace("USDT", "").replace("/", "").strip()
    strategies = list(strategies) if strategies else list(ALL_V_STRATEGY_TAGS)
    strategies = [t for t in strategies if t in ALL_V_STRATEGY_TAGS] or list(ALL_V_STRATEGY_TAGS)

    markets = load_markets_cached(exchange)
    symbol = resolve_symbol(coin, markets)
    if not symbol:
        raise ValueError(f"Монета {coin} не найдена на бирже")

    start_ts = pd.Timestamp(start_time_str, tz="UTC")
    target_ts_ms = int(start_ts.timestamp() * 1000)

    # ⚠️ Больше НЕТ единой проверки "а есть ли уровни на дату старта" —
    # раньше от неё зависело, запускать ли вообще проход (`if supports:`).
    # Но уровни теперь читаются заново каждый день ВНУТРИ самого прохода
    # (см. _walk_long/_walk_short → _refresh_levels_if_needed) — если на
    # старте пусто, а уровень появится через неделю, проход его всё равно
    # найдёт сам. Гейтить по дню старта значило бы пропускать именно те
    # случаи, ради которых это всё и делалось.
    df_full = _candles_df(symbol, top_up=top_up)
    _reset_sim_log(coin)  # новый прогон — лог монеты перезаписывается (одна монета = один лог)
    start_idx = int(df_full.index.searchsorted(start_ts))
    start_idx = max(0, min(start_idx, len(df_full) - 1))

    end_idx = len(df_full) - 1
    if end_time_str:
        end_ts = pd.Timestamp(end_time_str, tz="UTC")
        end_idx = int(df_full.index.searchsorted(end_ts, side="right")) - 1
        end_idx = max(start_idx, min(end_idx, len(df_full) - 1))

    episodes = []
    trades = []
    # {(min, max, type, is_support): {...}} — все уровни, реально
    # встретившиеся вотчерам за весь проход (не только на дату старта, см.
    # _remember_seen_levels), заполняется всеми вызванными _walk_*.
    seen_levels = {}
    ema_dist_fn = make_ema_dist_fn(df_full, candles_4h=load_candles_4h(candle_store, symbol))  # расстояние входа от EMA200(4h), %
    if "VB" in strategies:
        ep, tr = _walk_long(df_full, start_idx, end_idx, "V_BOTTOM", coin, seen_levels=seen_levels, ema_dist_fn=ema_dist_fn)
        episodes += ep
        trades += tr
    if "VGB" in strategies:
        ep, tr = _walk_long(df_full, start_idx, end_idx, "V_GREEN_BOTTOM", coin, seen_levels=seen_levels, ema_dist_fn=ema_dist_fn)
        episodes += ep
        trades += tr
    if "VRT" in strategies:
        ep, tr = _walk_short(df_full, start_idx, end_idx, coin, seen_levels=seen_levels, ema_dist_fn=ema_dist_fn)
        episodes += ep
        trades += tr

    trades.sort(key=lambda t: t["time"])

    active = [e for e in episodes if e["state"] not in ("TRIGGERED", "DEAD")]
    history = [e for e in episodes if e["state"] in ("TRIGGERED", "DEAD")]

    # Уровни, которые реально проверялись в этом прогоне — отдаём отдельно,
    # чтобы фронт мог нарисовать их поверх графика (другим цветом, не как
    # боевые из macro_levels.json — это ОТДЕЛЬНАЯ реконструкция "на дату").
    # Раз уровни теперь меняются по ходу прогона (раз в день) — тут СОЮЗ
    # всего, что встретилось за весь период, а не только "на дату старта".
    levels_out = list(seen_levels.values())

    result = {
        "coin": coin,
        "start_time": start_time_str,
        "end_time": end_time_str or df_full.index[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "strategies": strategies,
        "active": active,
        "history": history,
        "levels": levels_out,
        "trades": trades,
    }

    # Переживает обновление страницы — та же схема, что и у BOUNCE
    # (LAST_RUN_FILE у каждой семьи свой, см. докстринг модуля).
    if save_last_run:
        try:
            save_json_atomic(LAST_RUN_FILE, result, indent=2)
        except Exception as e:
            print(f"⚠️ [V SIM] Не удалось сохранить последний результат: {e}")

    return result


def get_last_run():
    """Последний сохранённый результат V-симуляции — для восстановления
    страницы после F5 (см. /api/simulate/last в app.py). None, если ни
    разу ещё не запускали (или файл почистили)."""
    data = load_json(LAST_RUN_FILE, default={})
    return data if isinstance(data, dict) and data else None


def _write_bulk_progress(done, total, current_coin, finished=False):
    """Тот же приём, что и у BOUNCE (см. bounce_simulate.py) — свой файл,
    не пересекается с BULK_PROGRESS_FILE у BOUNCE."""
    try:
        save_json_atomic(BULK_PROGRESS_FILE, {
            "done": done,
            "total": total,
            "current_coin": current_coin,
            "finished": finished,
            "updated_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2)
    except Exception as e:
        print(f"⚠️ [V SIM] Не удалось записать прогресс bulk-прогона: {e}")


def get_bulk_progress():
    """Прогресс bulk V-прогона для опроса с фронта. {} если ни разу не запускали."""
    data = load_json(BULK_PROGRESS_FILE, default={})
    return data if isinstance(data, dict) and data else {}


def _simulate_one_coin_worker(task):
    """Раннер ОДНОЙ монеты в отдельном процессе (см. тот же приём и то же
    объяснение — модульный уровень, не замыкание — в
    bounce_simulate.py::_simulate_one_coin_worker: multiprocessing на
    Windows использует spawn, аргументы и функция маршалятся через pickle,
    вложенные функции pickle не берёт."""
    coin, start_time_str, end_time_str, strategies, top_up = task
    try:
        sim_result = run_simulation(
            coin, start_time_str, end_time_str,
            strategies=strategies, top_up=top_up, save_last_run=False,
        )
        payload = {"trades": sim_result.get("trades", []), "history": sim_result.get("history", [])}
        return coin, payload, None
    except Exception as e:
        return coin, None, str(e)


def run_bulk_v_simulation(start_time_str, end_time_str=None, strategies=None, coin_filter=None):
    """Гоняет run_simulation по КАЖДОЙ монете из list_coins_with_levels за
    период — та же функция, что уже фильтрует монеты для bulk BOUNCE
    (bounce_simulate.py), список монет с уровнями в периоде общий для
    обеих семей, отдельного своего искать незачем.

    coin_filter — необязательное множество тикеров (см. app.py::
    _coin_source_filter_set). Если задано, из coins остаются только те,
    что входят и в историю за период, И в этот набор — второй режим bulk
    ("по выбранному списку"), не по всем монетам сразу.

    Уровни для каждой монеты читаются из локальной БД снимков (_levels_at)
    — без похода на биржу, так что это не медленнее bulk BOUNCE. Сетевая
    докачка свечей (top_up) всё равно отключена (top_up=False), т.к.
    период исторический.

    Возвращает тот же формат, что и run_bulk_bounce_simulation (results/
    trades/history_by_coin), чтобы фронт (renderBulkReport/
    renderBulkTradesTable в sim.js) рисовал обе семьи одним и тем же кодом
    без переделки."""
    coins, coverage = list_coins_with_levels(start_time_str, end_time_str)
    if coin_filter is not None:
        coins = [c for c in coins if c in coin_filter]
    total = len(coins)

    results = {}
    all_trades = []
    history_by_coin = {}
    coins_with_trades = 0

    _write_bulk_progress(0, total, None, finished=False)

    tasks = [(coin, start_time_str, end_time_str, strategies, False) for coin in coins]

    done = 0
    with ProcessPoolExecutor(max_workers=BULK_MAX_WORKERS) as executor:
        future_to_coin = {executor.submit(_simulate_one_coin_worker, task): task[0] for task in tasks}
        for future in as_completed(future_to_coin):
            coin = future_to_coin[future]
            try:
                _, payload, error = future.result()
            except Exception as e:
                payload, error = None, str(e)

            if error is not None:
                results[coin] = {"error": error}
            else:
                trades = (payload or {}).get("trades", [])
                agg = _aggregate_trades(trades)
                cov = (coverage or {}).get(coin, {})
                agg["levels_coverage"] = {
                    "snapshots_with_levels": cov.get("snapshots_with_levels", 0),
                    "snapshots_total": cov.get("snapshots_total", 0),
                }
                results[coin] = agg
                if agg["total_trades"] > 0:
                    coins_with_trades += 1
                    all_trades.extend(trades)
                    history_by_coin[coin] = (payload or {}).get("history", [])

            done += 1
            _write_bulk_progress(done, total, coin, finished=False)

    _write_bulk_progress(total, total, None, finished=True)

    result = {
        "start_time": start_time_str,
        "end_time": end_time_str,
        "strategies": list(strategies) if strategies else list(ALL_V_STRATEGY_TAGS),
        "coins_scanned": len(coins),
        "coins_with_trades": coins_with_trades,
        "results": results,
        "trades": all_trades,
        "history_by_coin": history_by_coin,
    }

    try:
        save_json_atomic(LAST_ALL_RUN_FILE, result, indent=2)
    except Exception as e:
        print(f"⚠️ [V SIM] Не удалось сохранить bulk-результат: {e}")

    return result


def get_last_all_run():
    """Последний сохранённый результат bulk V-прогона — свой файл, не
    пересекается с get_last_all_run() у BOUNCE."""
    data = load_json(LAST_ALL_RUN_FILE, default={})
    return data if isinstance(data, dict) and data else None