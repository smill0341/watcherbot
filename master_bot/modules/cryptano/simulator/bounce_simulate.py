# modules/cryptano/simulator/bounce_simulate.py
"""
Симулятор BOUNCE — честный реплей по истории, через НАСТОЯЩИЕ боевые
классы (BounceManager/BounceWatcher из modules.cryptano.strategy), а не
отдельную копию логики. Всё, что чинится в бою, автоматически действует
и здесь — расхождения между "тестером" и боем, как раньше с test/testswing,
тут в принципе невозможны, потому что это буквально один и тот же код.

На каждый вызов создаётся СВОЙ, изолированный BounceManager (BounceParent()) —
ничего не пишет в боевые файлы (bounce_state.json, active_watchers.json,
signals.json). Уровни на каждую свечу берутся из levels_history.py (снимки
раз в ~12ч, та же "машина времени", что уже строит swing_hunter в бою) —
не сегодняшние/не стартовые, а честно те, что были актуальны в тот момент.

Логи вотчеров — своя папка (modules/cryptano/simulator/logs/), не
пересекается с боевыми bounce_logs/*.log (см. BounceWatcher.log_dir_override).
"""
import os
import datetime
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed

from modules.cryptano.utils.common import exchange, resolve_symbol
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.indicators import calculate_atr
from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.utils.paths import DATABASE_DIR
from modules.cryptano.levels_history import get_levels_snapshot, snapshot_bucket_start
from modules.cryptano.strategy.bounce_manager import BounceManager
from modules.cryptano.strategy.bounce_parent import BounceParent
from modules.cryptano.strategy.bounce_watcher import BounceWatcher
from modules.cryptano.watcher_plan import candle_store



SIM_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LAST_RUN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run.json")
# Отдельный файл под результат bulk-прогона (все монеты разом) — НЕ тот же
# LAST_RUN_FILE, что у одиночного симулятора. Иначе прогон по списку монет
# затирал бы last_run.json результатом случайной последней обработанной
# монеты, и после F5 одиночная страница показывала бы не то, что человек
# реально запускал вручную в последний раз.
LAST_ALL_RUN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_all_run.json")
# Прогресс bulk-прогона — отдельный файл, пишется ПО ХОДУ цикла (после
# каждой монеты), а не одним куском в конце. Основной POST-запрос долгий
# и отдаёт ответ только когда всё готово — прогресс же читает отдельный
# лёгкий GET, пока фронт ждёт основной ответ (см. simulate_bounce_bulk_progress
# в app.py и опрос в sim.js::runBulkSimulation).
BULK_PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bulk_progress.json")

WINDOW = 60  # то же окно, что и в боевом реплее (watcher_plan.py::check_bounce) — evaluate_bounce
             # читает максимум последние ~52 строки, окно с запасом, не вся история разом.

# Сколько монет считать ОДНОВРЕМЕННО в bulk-прогоне — отдельными процессами
# (ProcessPoolExecutor), не потоками: в Python потоки не ускоряют такую
# CPU-тяжёлую работу (мешает GIL), а отдельные процессы — ускоряют, примерно
# во столько раз, сколько воркеров, вплоть до реального числа ядер
# процессора. 4 — безопасное значение по умолчанию, не съедающее машину
# целиком; можно поднять через переменную окружения BOUNCE_BULK_WORKERS,
# не трогая код.
BULK_MAX_WORKERS = int(os.environ.get("BOUNCE_BULK_WORKERS", "4"))


def _reset_sim_log(coin):
    """Чистит лог этой монеты в SIM_LOG_DIR перед КАЖДЫМ новым прогоном
    симуляции — не ждём рестарта процесса. BounceWatcher._dbg() сам
    ротирует/обрезает лог, но только один раз за жизнь процесса (по
    (log_dir, coin) в BounceWatcher._initialized_log_coins) — сервер
    симулятора не перезапускается между прогонами, поэтому без этой
    функции каждый новый "Старт" молча дописывался бы в тот же файл
    поверх предыдущего прогона. Специфично для симулятора: боевой лог
    (bounce_logs/) эта функция не трогает и трогать не должна — там
    история между рестартами бота нужна.
    """
    log_path = os.path.join(SIM_LOG_DIR, f"{coin}.log")
    try:
        os.makedirs(SIM_LOG_DIR, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"=== Новый прогон симуляции {coin} — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC ===\n")
    except Exception as e:
        print(f"⚠️ [BOUNCE SIM] Не удалось очистить лог {coin}: {e}")


def _candles_df(symbol, top_up=True):
    """Вся доступная 15m-история из candle_store (та же база, что у дашборда
    и боевого watcher_plan.py) — никакого похода на биржу за свечами.

    top_up=True (по умолчанию, как и раньше) — перед чтением подтягивает
    самые свежие свечи с биржи (candle_store.top_up_tail), нужно для
    одиночного ручного прогона, где период часто доходит до "сейчас".

    top_up=False — читает ТОЛЬКО то, что уже лежит локально, ни одного
    сетевого похода. Для bulk-прогона по списку монет: период всегда в
    прошлом (историческая дата "до"), а последовательная докачка по
    каждой из десятков монет была бы чистыми лишними секундами без
    какой-либо пользы для результата."""
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


def _episode(watcher, level_id, coin):
    return {
        "level_id": level_id,
        "coin": coin,
        "strategy": "BOUNCE",
        "mode": getattr(watcher, "mode", None),
        "direction": getattr(watcher, "trade_type", None),
        "state": getattr(watcher, "state", "UNKNOWN"),
        "level_min": getattr(watcher, "min", None),
        "level_max": getattr(watcher, "max", None),
        "level_date": getattr(watcher, "level_date", None),
        "level_type": getattr(watcher, "level_type", None),
        "level_score": getattr(watcher, "level_score", None),
        "activated_at": getattr(watcher, "activated_at", None),
        "born_at": getattr(watcher, "born_at", None),
        "events": list(getattr(watcher, "event_log", [])),
    }


def _compute_trade_outcome(entry_ts, entry_price, target_price, trade_type, df_full, max_hold_days):
    """Честный проход ВПЕРЁД по свечам от входа — определяет исход сделки.

    Закрывает сделку только TP либо дедлайн (MAX_HOLD_DAYS) — тот же самый
    принцип, что уже описан в BounceWatcher.CONFIG['SL_PCT']: "держим для
    отображения/справки — НЕ закрывает сделку, на тестах реального
    стоп-лосса не было". Это не отдельное решение для симулятора, это
    honest-копия того, как боевая система вообще считает исход.

    Заодно считает MAE (худшую просадку от входа до момента разрешения) —
    entry_ts должен быть tz-aware (тот же индекс, что у df_full)."""
    deadline_ts = entry_ts + pd.Timedelta(days=max_hold_days)
    future = df_full[(df_full.index > entry_ts) & (df_full.index <= deadline_ts)]

    worst_adverse_pct = 0.0
    for _, row in future.iterrows():
        if trade_type == "LONG":
            adverse_pct = (float(row["low"]) - entry_price) / entry_price * 100.0
        else:
            adverse_pct = (entry_price - float(row["high"])) / entry_price * 100.0
        if adverse_pct < worst_adverse_pct:
            worst_adverse_pct = adverse_pct

        hit = (float(row["high"]) >= target_price) if trade_type == "LONG" else (float(row["low"]) <= target_price)
        if hit:
            result_pct = (target_price - entry_price) / entry_price * 100.0 if trade_type == "LONG" \
                else (entry_price - target_price) / entry_price * 100.0
            return {
                "status": "✅",
                "result_percent": round(result_pct, 2),
                "closed_at": row.name.isoformat(),
                "mae_pct": round(worst_adverse_pct, 2),
            }

    have_full_window = not df_full.empty and df_full.index[-1] >= deadline_ts
    if have_full_window and not future.empty:
        # Дошли до дедлайна, TP так и не случился — закрываем по последней
        # доступной цене периода (тот же принцип, что history.py::
        # check_and_update при MAX_HOLD_DAYS, просто честно по истории,
        # а не по текущему тикеру биржи).
        last_close = float(future.iloc[-1]["close"])
        result_pct = (last_close - entry_price) / entry_price * 100.0 if trade_type == "LONG" \
            else (entry_price - last_close) / entry_price * 100.0
        return {
            "status": "🕐",
            "result_percent": round(result_pct, 2),
            "closed_at": future.index[-1].isoformat(),
            "mae_pct": round(worst_adverse_pct, 2),
        }

    # Данных пока не хватает до дедлайна (сделка случилась слишком близко к
    # концу доступной истории) — честно показываем как ещё открытую, а не
    # выдумываем закрытие раньше времени.
    return {
        "status": "⏳",
        "result_percent": None,
        "closed_at": None,
        "mae_pct": round(worst_adverse_pct, 2) if not future.empty else None,
    }


def run_bounce_simulation(coin, start_time_str, end_time_str=None, top_up=True, save_last_run=True,
                           allow_long=True, allow_short=True):
    """
    coin: тикер, напр. 'BTC'
    start_time_str: 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM' (UTC) — начало реплея
    end_time_str: конец реплея, по умолчанию — самая свежая доступная свеча
    top_up: докачивать ли свежие свечи перед прогоном (см. _candles_df) —
        False используется bulk-прогоном (run_bulk_bounce_simulation), где
        период исторический и докачка "до сейчас" не нужна ни одной из
        десятков монет по очереди.
    save_last_run: сохранять ли результат в LAST_RUN_FILE (для восстановления
        одиночной страницы симулятора после F5) — False для bulk, чтобы
        цикл по списку монет не затирал этот файл результатом случайной
        последней обработанной монеты; bulk сохраняет свой агрегат отдельно
        (см. LAST_ALL_RUN_FILE / run_bulk_bounce_simulation).
    allow_long/allow_short: какие направления вообще пускать в поиск сделок
        — та же пара флагов, что process_candle() в bounce_manager.py уже
        принимает для боевого бота (см. watcher_plan.py::check_bounce).
        По умолчанию оба включены — прежнее поведение не меняется, если
        эндпоинт их не передаёт.

    Возвращает {"coin", "start_time", "end_time", "active", "history",
    "messages"} — active/history в том же формате events, что и боевой
    /api/events/{coin} (фронт может рисовать теми же маркерами без
    переделки), messages — готовые текстовые карточки найденных сделок
    (для ленты внизу страницы симулятора).
    """
    coin = coin.upper().replace("USDT", "").replace("/", "").strip()

    markets = load_markets_cached(exchange)
    symbol = resolve_symbol(coin, markets)
    if not symbol:
        raise ValueError(f"Монета {coin} не найдена на бирже")

    df_full = _candles_df(symbol, top_up=top_up)

    start_ts = pd.Timestamp(start_time_str, tz="UTC")
    end_ts = pd.Timestamp(end_time_str, tz="UTC") if end_time_str else df_full.index[-1]
    replay_df = df_full[(df_full.index >= start_ts) & (df_full.index <= end_ts)]
    if replay_df.empty:
        raise ValueError(f"Нет свечей в диапазоне {start_time_str} .. {end_time_str or 'сейчас'} для {coin}")

    atr_series = calculate_atr(df_full)

    # Свой, полностью изолированный менеджер — ничего общего с боевым
    # bounce_mgr (модуль modules.cryptano.live_scan), ничего не пишет в
    # боевые jsonbank/*.json. Своя папка логов, не bounce_logs/.
    os.makedirs(SIM_LOG_DIR, exist_ok=True)
    _reset_sim_log(coin)  # новый прогон — старый лог этой монеты не тащим
    bounce_mgr = BounceManager(BounceParent(), log_dir_override=SIM_LOG_DIR)

    messages = []
    trades = []
    df_index = df_full.index

    # Снимок уровней физически меняется раз в 12 часов (см. levels_history.py::
    # snapshot_bucket_start — та же сетка 00:00/12:00, на которой пишутся
    # снимки), а свечей внутри одного бакета — до 48 (15м × 12ч). Без кэша
    # get_levels_snapshot() звался бы заново на каждой из них с одним и тем же
    # результатом. Тут держим последний бакет + его снимок и зовём функцию
    # заново только при переходе в новый бакет — сам get_levels_snapshot()
    # не тронут, его семантика (в т.ч. для боевого рескана) не меняется.
    last_bucket = None
    cached_snapshot = None

    for ts, row in replay_df.iterrows():
        pos = df_index.searchsorted(ts, side="right")
        df_slice = df_full.iloc[max(0, pos - WINDOW):pos]
        if len(df_slice) < 52:
            continue  # недостаточно истории ДО этой свечи для индикаторов

        c_high, c_low, c_close = float(row["high"]), float(row["low"]), float(row["close"])
        c_atr = float(atr_series.loc[ts]) if ts in atr_series.index else 0.0

        ts_naive = ts.tz_localize(None) if ts.tzinfo is not None else ts
        ts_naive_dt = ts_naive.to_pydatetime()

        bucket = snapshot_bucket_start(ts_naive_dt)
        if bucket != last_bucket:
            cached_snapshot = get_levels_snapshot(ts_naive_dt)
            last_bucket = bucket
        snapshot = cached_snapshot

        if snapshot is not None:
            coin_macro = snapshot.get(coin) or {}
            supports = coin_macro.get("supports", [])
            resistances = coin_macro.get("resistances", [])
        else:
            supports, resistances = [], []

        orders, _draw_events = bounce_mgr.process_candle(
            c_low, c_high, c_close, supports, resistances, df_slice,
            allow_long=allow_long, allow_short=allow_short, c_atr=c_atr, coin=coin,
        )

        # Устанавливаем born_at на основе activated_at (дату активации уровня)
        for watcher in bounce_mgr._watchers.values():
            if not getattr(watcher, "born_at", None) and getattr(watcher, "activated_at", None):
                activated_str = watcher.activated_at
                try:
                    activated_dt = datetime.datetime.fromisoformat(activated_str.replace('Z', '+00:00'))
                    watcher.born_at = activated_dt.strftime('%Y-%m-%d')
                except Exception:
                    pass

        for order in orders:
            d = order["decision"]
            tt = order["trade_type"]
            entry, sl, tp = d.get("entry_price", 0.0), d.get("sl", 0.0), d.get("tp", 0.0)
            rr = ((tp - entry) / (entry - sl)) if tt == "LONG" and entry > sl else \
                 ((entry - tp) / (sl - entry)) if tt == "SHORT" and sl > entry else 0

            level_id_val = d.get("level_id", "") or ""
            mode = level_id_val.split("__")[-1] if "__" in level_id_val else None
            source_label = f"BOUNCE_{tt}" + (f"_{mode}" if tt == "SHORT" and mode else "")

            outcome = _compute_trade_outcome(
                ts, entry, tp, tt, df_full,
                BounceWatcher.CONFIG.get("MAX_HOLD_DAYS", 14),
            )

            trades.append({
                "date": ts_naive.strftime("%Y-%m-%d %H:%M"),
                "time": int(ts_naive.timestamp()),
                "coin": coin,
                "type": tt,
                "source": source_label,
                "entry": entry,
                "target": tp,
                "stop": sl,
                "rr": round(rr, 2),
                "level_id": level_id_val or None,
                "level_min": order.get("level", {}).get("min"),
                "level_max": order.get("level", {}).get("max"),
                "volume": d.get("volume", float(row["volume"])),
                "volume_mult": d.get("volume_mult"),
                "reason": d.get("reason", ""),
                # То же самое, что уже показывает боевой дашборд для реальных
                # сигналов (см. history.py::save_signal/check_and_update) —
                # только тут исход честно посчитан по истории, а не ждёт
                # реального тикера биржи.
                "status": outcome["status"],
                "result_percent": outcome["result_percent"],
                "closed_at": outcome["closed_at"],
                "mae_pct": outcome["mae_pct"],
                "method": "volume",
                "method_value": d.get("volume", float(row["volume"])),
                "method_mult": d.get("volume_mult"),
            })

            emoji = "🟢" if tt == "LONG" else "🔴"
            messages.append(
                f"{emoji} *BOUNCE {tt}* _{coin}_ — {ts_naive.strftime('%Y-%m-%d %H:%M')}\n"
                f"Entry: `{entry:.8f}`  SL: `{sl:.8f}`  TP: `{tp:.8f}`  R/R: `{rr:.2f}`\n"
                f"{d.get('reason', '')}  Level: `{d.get('level_id', 'unknown')}`"
            )

    active, history = [], []
    for level_id, watcher in bounce_mgr._watchers.items():
        ep = _episode(watcher, level_id, coin)
        (history if ep["state"] in ("TRIGGERED", "DEAD") else active).append(ep)

    # Только те зоны, где реально была сделка (TRIGGERED) — иначе за месяц
    # это может быть десяток+ зон разом, друг на друге, нечитаемо.
    # Несостоявшиеся попытки (DEAD/SCANNING) всё ещё видны как точки на
    # графике — просто без своей линии зоны.
    levels_out = [
        {
            "min": ep["level_min"], "max": ep["level_max"],
            "is_support": ep["direction"] == "LONG",
            "type": ep["level_type"], "score": ep["level_score"],
        }
        for ep in history
        if ep["state"] == "TRIGGERED" and ep["level_min"] is not None and ep["level_max"] is not None
    ]

    result = {
        "coin": coin,
        "start_time": start_time_str,
        "end_time": end_time_str or df_full.index[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "active": active,
        "history": history,
        "levels": levels_out,
        "trades": trades,
        "messages": messages,
    }

    # Переживает обновление страницы — при открытии /simulator фронт сам
    # подтягивает последний результат (см. /api/simulate_bounce/last),
    # не нужно перезапускать симуляцию заново после F5.
    if save_last_run:
        try:
            save_json_atomic(LAST_RUN_FILE, result, indent=2)
        except Exception as e:
            print(f"⚠️ [BOUNCE SIM] Не удалось сохранить последний результат: {e}")

    return result


def get_last_run():
    """Последний сохранённый результат симуляции — для восстановления
    страницы после F5 (см. /api/simulate_bounce/last в app.py). None, если
    ни разу ещё не запускали в этом jsonbank (или файл почистили)."""
    data = load_json(LAST_RUN_FILE, default={})
    return data if isinstance(data, dict) and data else None


def _timeline_months_in_range(start_ts, end_ts):
    """Список месячных ярлыков ('YYYY_MM'), которые нужно открыть, чтобы
    покрыть весь период [start_ts, end_ts] — обычно один, два на границе
    месяца."""
    months = []
    cur = start_ts.replace(day=1)
    end_marker = end_ts.replace(day=1)
    while cur <= end_marker:
        months.append(cur.strftime("%Y_%m"))
        cur = (cur + pd.offsets.MonthBegin(1))
    return months


def list_coins_with_levels(start_time_str, end_time_str=None):
    """Список монет, у которых хотя бы В ОДНОМ снимке таймлайна внутри
    периода [start_time_str, end_time_str] были уровни (supports ИЛИ
    resistances не пустые) — объединение (union), не пересечение: монета,
    получившая уровни в середине периода, всё равно должна попасть в
    bulk-прогон, а не быть отсеяна как "не всегда присутствовала".

    НЕ использует /api/macro/coins (macro_levels.json) — тот список
    сегодняшний (топ-70 по текущему объёму), не имеет отношения к тому,
    что реально было актуально в запрошенном историческом периоде.
    Читает те же файлы database/levels_timeline_YYYY_MM.json, что и
    get_levels_snapshot() — просто не одну точку времени, а весь диапазон.

    Возвращает (coins_sorted, coverage) — coverage: {coin: {"snapshots_with_levels": N,
    "snapshots_total": M}} — сколько из всех снимков периода у монеты реально
    были уровни, для колонки "покрытие" в bulk-отчёте (честный ноль сделок
    из-за отсутствия уровней большую часть периода должен отличаться от
    честного ноля при наличии уровней всё время)."""
    start_ts = pd.Timestamp(start_time_str)
    end_ts = pd.Timestamp(end_time_str) if end_time_str else pd.Timestamp.utcnow().tz_localize(None)

    coins = set()
    coverage = {}
    total_snapshots = 0

    for month_label in _timeline_months_in_range(start_ts, end_ts):
        path = os.path.join(DATABASE_DIR, f"levels_timeline_{month_label}.json")
        timeline = load_json(path, default={})
        if not isinstance(timeline, dict):
            continue
        for time_str, snapshot in timeline.items():
            try:
                ts = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            if ts < start_ts.to_pydatetime() or ts > end_ts.to_pydatetime():
                continue
            if not isinstance(snapshot, dict):
                continue
            total_snapshots += 1
            for coin, data in snapshot.items():
                if not isinstance(data, dict):
                    continue
                has_levels = bool(data.get("supports")) or bool(data.get("resistances"))
                entry = coverage.setdefault(coin, {"snapshots_with_levels": 0})
                if has_levels:
                    entry["snapshots_with_levels"] += 1
                    coins.add(coin)

    for entry in coverage.values():
        entry["snapshots_total"] = total_snapshots

    return sorted(coins), coverage


def _aggregate_trades(trades):
    """Схлопывает список сделок ОДНОЙ монеты (тот же формат, что уже строит
    run_bounce_simulation в поле 'trades') в короткую сводку для строки
    bulk-отчёта — та же честная разбивка по статусу (✅/🕐/⏳), что уже
    видна в таблице одиночного симулятора, просто посчитана числом."""
    long_trades = [t for t in trades if t["type"] == "LONG"]
    short_trades = [t for t in trades if t["type"] == "SHORT"]

    def _side_stats(side_trades):
        closed = [t for t in side_trades if t["status"] in ("✅", "🕐")]
        wins = [t for t in closed if (t.get("result_percent") or 0) > 0]
        avg_result = (sum(t["result_percent"] for t in closed) / len(closed)) if closed else None
        avg_mae = (sum(t["mae_pct"] for t in side_trades if t.get("mae_pct") is not None)
                   / len([t for t in side_trades if t.get("mae_pct") is not None])) \
            if any(t.get("mae_pct") is not None for t in side_trades) else None
        return {
            "count": len(side_trades),
            "closed": len(closed),
            "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(closed) * 100.0, 1) if closed else None,
            "avg_result_pct": round(avg_result, 2) if avg_result is not None else None,
            "avg_mae_pct": round(avg_mae, 2) if avg_mae is not None else None,
        }

    return {
        "total_trades": len(trades),
        "long": _side_stats(long_trades),
        "short": _side_stats(short_trades),
    }


def _write_bulk_progress(done, total, current_coin, finished=False):
    """Пишет прогресс bulk-прогона на диск — вызывается после каждой
    монеты. Не должна ронять сам прогон, если запись не удалась (диск
    занят и т.п.) — это вспомогательная информация для UI, не критичная
    для результата."""
    try:
        save_json_atomic(BULK_PROGRESS_FILE, {
            "done": done,
            "total": total,
            "current_coin": current_coin,
            "finished": finished,
            "updated_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2)
    except Exception as e:
        print(f"⚠️ [BOUNCE SIM] Не удалось записать прогресс bulk-прогона: {e}")


def get_bulk_progress():
    """Текущий прогресс bulk-прогона для опроса с фронта (см.
    /api/simulate_bounce_bulk/progress). {} если ни разу не запускали."""
    data = load_json(BULK_PROGRESS_FILE, default={})
    return data if isinstance(data, dict) and data else {}


def _needs_topup(symbol, max_age_seconds=24 * 3600):
    """True, если локальные 15m-свечи этой монеты либо отсутствуют, либо
    отстают от реальности больше чем на max_age_seconds — тогда стоит один
    раз донести сеть за свежим хвостом. Иначе можно честно читать
    локальные данные без похода в сеть — для большинства активно
    отслеживаемых ботом монет они и так свежие (бот сам их топит по ходу
    работы), а проверка сама по себе ничего не качает — чистый локальный
    SQLite-запрос (см. candle_store.last_candle_age_seconds).

    Важно НЕ путать с тем, почему сделка вообще может остаться "⏳" —
    даже при полностью свежих данных сделка, открытая позже чем
    (сейчас − MAX_HOLD_DAYS), физически не может быть разрешена: дедлайн
    ещё не наступил в реальности, это не пробел в данных. Эта проверка
    закрывает только случай "дедлайн давно прошёл в календаре, а
    локальная история этого не знает", не пытается предсказывать будущее."""
    if candle_store is None:
        return False
    age = candle_store.last_candle_age_seconds(symbol, "15m")
    return age is None or age > max_age_seconds


def _simulate_one_coin_worker(task):
    """Раннер ОДНОЙ монеты — вызывается в ОТДЕЛЬНОМ процессе
    (ProcessPoolExecutor, см. run_bulk_bounce_simulation). Обязательно
    функция МОДУЛЬНОГО уровня (не замыкание/метод) — на Windows
    multiprocessing использует spawn, а не fork: аргументы и сама функция
    маршалятся через pickle, чтобы попасть в новый процесс, а вложенные/
    локальные функции pickle не берёт в принципе.

    task — один кортеж (не именованные kwargs) с ВСЕМИ входными данными
    сразу: так проще прокинуть через executor.submit без путаницы с
    порядком аргументов между процессами.

    Возвращает (coin, payload, error) — сама функция ничего не пишет
    (ни прогресс, ни файлы): запись — забота главного процесса, чтобы не
    ловить гонку записи от нескольких процессов разом в один файл."""
    coin, start_time_str, end_time_str, top_up, allow_long, allow_short = task
    try:
        sim_result = run_bounce_simulation(
            coin, start_time_str, end_time_str,
            top_up=top_up, save_last_run=False,
            allow_long=allow_long, allow_short=allow_short,
        )
        payload = {"trades": sim_result.get("trades", []), "history": sim_result.get("history", [])}
        return coin, payload, None
    except Exception as e:
        return coin, None, str(e)


def run_bulk_bounce_simulation(start_time_str, end_time_str=None, allow_long=True, allow_short=True):
    """Гоняет run_bounce_simulation по КАЖДОЙ монете из list_coins_with_levels
    за период, без перезаписи LAST_RUN_FILE одиночного симулятора
    (save_last_run=False — свой файл, LAST_ALL_RUN_FILE).

    Монеты считаются ПАРАЛЛЕЛЬНО, отдельными процессами (ProcessPoolExecutor,
    BULK_MAX_WORKERS штук одновременно) — не потоками: в Python потоки не
    ускоряют такую CPU-тяжёлую работу (мешает GIL, реального параллелизма
    не будет), а отдельные процессы — ускоряют, примерно во столько раз,
    сколько воркеров одновременно (вплоть до реального числа ядер).
    Каждая монета полностью независима от остальных (свой вход, свой
    выход, никакого общего состояния) — идеальный случай для этого.

    Сетевая докачка НЕ отключена наглухо — включается точечно, только для
    монет, чья локальная история реально отстала (см. _needs_topup).
    Без этого сделки, чей 14-дневный дедлайн уже прошёл в календаре, но
    локальные свечи этого коин ещё не знают, честно повисали бы статусом
    "⏳" навсегда — не потому что дедлайн не наступил, а просто потому что
    у нас не было данных это проверить. Для большинства монет (бот их и
    так топит по ходу работы) эта проверка — локальный SQLite-запрос и
    ничего не качает, так что общая скорость bulk от этого почти не
    страдает, а результат честный. Сам _needs_topup (и resolve_symbol)
    считается ЗАРАНЕЕ, в главном процессе, до раздачи задач по воркерам —
    дешевле сделать это один раз тут, чем тащить в каждый процесс.

    Одна упавшая монета не должна рушить весь batch — ошибка ловится
    поштучно (внутри _simulate_one_coin_worker) и попадает в
    results[coin]['error'], остальные монеты всё равно считаются.

    allow_long/allow_short: та же пара, что и у run_bounce_simulation —
    прокидывается в КАЖДЫЙ прогон монеты одинаково.

    Кроме агрегата (results) возвращает ещё:
    - trades: ПЛОСКИЙ список всех сделок по всем монетам (каждая уже несёт
      своё поле "coin") — для вкладки "Все сделки" на фронте, тот же формат,
      что и у одиночного симулятора (просто по всем монетам разом).
    - history_by_coin: {coin: [...эпизоды TRIGGERED/DEAD зон...]} — ТОЛЬКО
      для монет, у которых были сделки (иначе кликать всё равно нечего).
      Ни "active" (несостоявшиеся сканы), ни свечи сюда не попадают — не
      нужны для клика по сделке (см. findGroupEpisodes на фронте — он
      смотрит только simHistory/history), и это самая тяжёлая часть,
      раздувать bulk-ответ ими незачем.

    Всё это (агрегат + trades + history_by_coin) сохраняется в
    LAST_ALL_RUN_FILE ЦЕЛИКОМ — тот же принцип, что и у одиночного
    симулятора (LAST_RUN_FILE): посчитали один раз, дальше и обновление
    страницы, и клик по сделке работают с уже готовыми данными, без
    повторного прогона симуляции.
    """
    coins, coverage = list_coins_with_levels(start_time_str, end_time_str)
    total = len(coins)
    markets = load_markets_cached(exchange)

    results = {}
    all_trades = []
    history_by_coin = {}
    coins_with_trades = 0

    _write_bulk_progress(0, total, None, finished=False)

    # Готовим ВСЕ задания заранее (top_up-решение требует candle_store/
    # резолва символа — дёшево и один раз, в главном процессе), потом
    # раздаём их по воркерам. Порядок завершения не совпадает с порядком
    # отправки (кто первый досчитал, тот первый и вернулся) — это нормально,
    # прогресс просто считает готовые штуки, не заботится о порядке.
    tasks = []
    for coin in coins:
        symbol = resolve_symbol(coin, markets)
        top_up = _needs_topup(symbol) if symbol else False
        tasks.append((coin, start_time_str, end_time_str, top_up, allow_long, allow_short))

    done = 0
    with ProcessPoolExecutor(max_workers=BULK_MAX_WORKERS) as executor:
        future_to_coin = {executor.submit(_simulate_one_coin_worker, task): task[0] for task in tasks}
        for future in as_completed(future_to_coin):
            coin = future_to_coin[future]
            try:
                _, payload, error = future.result()
            except Exception as e:
                # Сам процесс-воркер мог упасть целиком (не исключение
                # ВНУТРИ run_bounce_simulation, а например настоящий крэш
                # процесса) — ловим и это, не даём одной монете обрушить
                # весь batch.
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
        "coins_scanned": len(coins),
        "coins_with_trades": coins_with_trades,
        "results": results,
        "trades": all_trades,
        "history_by_coin": history_by_coin,
    }

    try:
        save_json_atomic(LAST_ALL_RUN_FILE, result, indent=2)
    except Exception as e:
        print(f"⚠️ [BOUNCE SIM] Не удалось сохранить bulk-результат: {e}")

    return result


def get_last_all_run():
    """Последний сохранённый результат bulk-прогона — свой файл, не
    пересекается с get_last_run() одиночного симулятора."""
    data = load_json(LAST_ALL_RUN_FILE, default={})
    return data if isinstance(data, dict) and data else None