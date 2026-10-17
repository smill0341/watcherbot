# modules/cryptano/simulator/simulate_engine.py
"""
Ядро симулятора: ОДИН файл на все стратегии (BOUNCE, BUYBACK, VB/VGB/VRT).

Что делает: идёт по истории 15m-свечей из архива симулятора (candle_archive,
sim_candles.db), на каждой свече берёт уровни из локальной базы снимков
(levels_history.py), отдаёт свечу БОЕВЫМ менеджерам стратегий (те же классы,
что в боте — никакой отдельной копии логики) и собирает сделки с честным
исходом (outcome.py).

Три семейства (ключ стратегии для файлов результата и прогресса):
  "BOUNCE", "BUYBACK" — BounceManager / BuybackManager.process_candle,
                        уровни из 12-часовых снимков на каждую свечу;
  "V"                 — VManager.step для VB / VGB / VRT (какие из трёх —
                        параметр strategies), уровни обновляются раз в день.

Общее для всех: пути к файлам, последний результат, bulk по всем монетам
(параллельные процессы, прогресс, сводка), докачка архива, сбор сделок.
У каждого семейства свои файлы результата и прогресса — прогоны не затирают
друг друга.

Точки входа:
  run_simulation(coin, start, end, strategy="BOUNCE" | "BUYBACK" | "V", ...)
  run_bulk_simulation(start, end, strategy=..., coin_filter, blocked, ...)
  get_last_run / get_last_all_run / get_bulk_progress (strategy)
  prefetch_archive(start, end)  — докачка свечей без сайта
    python -m backend.modules.cryptano.simulator.simulate_engine 2026-07-01 2026-07-31

Боевых файлов не пишет. Логи вотчеров — modules/cryptano/simulator/logs/.
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

from backend.modules.cryptano.utils.bybit import exchange, resolve_symbol, load_markets_cached
from backend.modules.cryptano.utils.indicators import calculate_atr, calculate_ema, calculate_rsi
from backend.modules.cryptano.utils.storage import load_json, save_json_atomic
from backend.modules.cryptano.utils.paths import DATABASE_DIR
import backend.modules.cryptano.levels.candle_archive as candle_archive
from backend.modules.cryptano.levels.levels_history import get_levels_snapshot, snapshot_bucket_start
from backend.modules.cryptano.levels.level_filter import LevelFilter, _num
from backend.modules.cryptano.strategy.bounce_manager import BounceManager
from backend.modules.cryptano.strategy.bounce_watcher import BounceWatcher
from backend.modules.cryptano.strategy.bounce_manager_buyback import BounceManager as BuybackManager
from backend.modules.cryptano.strategy.bounce_watcher_buyback import BounceWatcher as BuybackWatcher, BUYBACK
from backend.modules.cryptano.strategy.v_manager import VManager
from backend.modules.cryptano.backstage.watcher_plan import MIN_LEVEL_SCORE
from backend.modules.cryptano.simulator.outcome import compute_trade_outcome, make_ema_dist_fn


# ======================================================================
# НАСТРОЙКИ И ФАЙЛЫ
# ======================================================================

# Сколько дней вперёд после конца периода нужны свечи для честного исхода сделки.
FORWARD_DAYS = BounceWatcher.CONFIG.get("MAX_HOLD_DAYS", 14)

# Дедлайн сделки V-семьи (своего конфига у неё нет) — тот же ориентир, что у BOUNCE.
V_MAX_HOLD_DAYS = 14

# Окно свечей, которое менеджер получает на каждом шаге.
WINDOW = 60           # bounce: evaluate читает максимум ~52 последние строки
CANDLE_WINDOW = 300   # V: запас под ATR_slow(100), EMA50, RSI

# Теги V-стратегий (те же, что ключи вкл/выкл на дашборде и префикс level_id).
ALL_V_STRATEGY_TAGS = ("VB", "VGB", "VRT")

# Сколько монет считать одновременно в bulk (отдельные ПРОЦЕССЫ, не потоки —
# из-за GIL потоки такую CPU-работу не ускоряют). Меняется переменной
# окружения BOUNCE_BULK_WORKERS.
BULK_MAX_WORKERS = int(os.environ.get("BOUNCE_BULK_WORKERS", "4"))

SIM_LOG_DIR = os.path.join(_THIS_DIR, "logs")
SIM_LOG_DIR_BUYBACK = os.path.join(_THIS_DIR, "logs_buyback")

# Файлы по семействам: результат одиночного прогона, результат bulk, прогресс bulk.
_STRATEGY_FILES = {
    "BOUNCE": {
        "log_dir": SIM_LOG_DIR,
        "last_run": os.path.join(_THIS_DIR, "last_run.json"),
        "last_all_run": os.path.join(_THIS_DIR, "last_all_run.json"),
        "bulk_progress": os.path.join(_THIS_DIR, "bulk_progress.json"),
    },
    "BUYBACK": {
        "log_dir": SIM_LOG_DIR_BUYBACK,
        "last_run": os.path.join(_THIS_DIR, "last_run_buyback.json"),
        "last_all_run": os.path.join(_THIS_DIR, "last_all_run_buyback.json"),
        "bulk_progress": os.path.join(_THIS_DIR, "bulk_progress_buyback.json"),
    },
    "V": {
        "log_dir": SIM_LOG_DIR,
        "last_run": os.path.join(_THIS_DIR, "last_run_v.json"),
        "last_all_run": os.path.join(_THIS_DIR, "last_all_run_v.json"),
        "bulk_progress": os.path.join(_THIS_DIR, "bulk_progress_v.json"),
    },
}


def _norm_strategy(strategy):
    st = (strategy or "BOUNCE").strip().upper()
    if st not in _STRATEGY_FILES:
        raise ValueError(f"Неизвестная стратегия: {strategy}")
    return st


def _sim_path(strategy, key):
    return _STRATEGY_FILES[_norm_strategy(strategy)][key]


def _watcher_cls(strategy):
    return BuybackWatcher if _norm_strategy(strategy) == "BUYBACK" else BounceWatcher


def _new_mgr(tag):
    """Свежий V-менеджер на один прогон; логи вотчеров — в папку симулятора."""
    return VManager(tag, log_dir_override=_sim_path("V", "log_dir"))


# ======================================================================
# ОБЩЕЕ: свечи, монеты периода, сводка, прогресс, последний результат
# ======================================================================

def _reset_sim_log(coin, log_dir=None):
    """Чистит лог этой монеты в SIM_LOG_DIR перед КАЖДЫМ новым прогоном
    симуляции ЛЮБОЙ стратегии (BOUNCE и V-семейство делят один файл {coin}.log;
    её же зовёт simulate_engine.run_simulation) — не ждём рестарта процесса. BounceWatcher._dbg() сам
    ротирует/обрезает лог, но только один раз за жизнь процесса (по
    (log_dir, coin) в BounceWatcher._initialized_log_coins) — сервер
    симулятора не перезапускается между прогонами, поэтому без этой
    функции каждый новый "Старт" молча дописывался бы в тот же файл
    поверх предыдущего прогона. Специфично для симулятора: боевой лог
    (bounce_logs/) эта функция не трогает и трогать не должна — там
    история между рестартами бота нужна.
    """
    log_dir = log_dir or SIM_LOG_DIR
    log_path = os.path.join(log_dir, f"{coin}.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"=== Новый прогон симуляции {coin} — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC ===\n")
        # Старый отдельный лог V_BOTTOM ({coin}_V.log) больше не пишется —
        # одна монета = один лог {coin}.log. Подчищаем остаток прошлых версий.
        legacy = os.path.join(log_dir, f"{coin}_V.log")
        if os.path.exists(legacy):
            os.remove(legacy)
    except Exception as e:
        print(f"⚠️ [BOUNCE SIM] Не удалось очистить лог {coin}: {e}")


def _candles_df(symbol, start_ts, end_ts, top_up=True, forward_days=FORWARD_DAYS):
    """15m-свечи периода [start_ts, end_ts] из АРХИВА симулятора
    (candle_archive, база sim_candles.db) — не из общей базы дашборда/бота.
    В окно входят запас назад (7 суток) и запас вперёд (forward_days — для
    честного исхода сделок в конце периода), см. candle_archive.sim_window.

    top_up=True — перед чтением докачивает с биржи недостающее (одиночный
    ручной прогон). top_up=False — только читает то, что уже лежит в архиве:
    для bulk-прогона, где докачка уже сделана в главном процессе до раздачи
    монет воркерам (см. _ensure_archive_for_coins).

    start_ts/end_ts — pandas.Timestamp, tz-aware (UTC)."""
    if top_up:
        candle_archive.ensure_for_sim(exchange, symbol, start_ts, end_ts, forward_days)
    candles = candle_archive.get_sim_candles(symbol, "15m", start_ts, end_ts, forward_days)
    if not candles:
        raise RuntimeError(f"Нет 15m-свечей {symbol} в архиве симулятора за этот период")

    df = pd.DataFrame(candles)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.drop(columns=["time"]).set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


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
    _run_bounce в поле 'trades') в короткую сводку для строки
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


def _ensure_archive_for_coins(coins, markets, start_time_str, end_time_str, forward_days, progress_cb):
    """Докачивает архив симулятора (15m + 4h) по списку монет на период —
    ПОСЛЕДОВАТЕЛЬНО, по одной монете, в ГЛАВНОМ процессе, до раздачи монет
    воркерам: докачка пишет в SQLite и ходит на биржу, ей нужна очередь, а не
    параллельные процессы. Воркеры потом только читают готовое.

    progress_cb(done, total, label) — запись прогресса (у BOUNCE и V-семьи
    свои файлы прогресса). Возвращает {coin: текст ошибки} для монет, по
    которым докачать не удалось — их считать нельзя (данных нет), они
    попадают в отчёт как error."""
    total = len(coins)
    start_ts = pd.Timestamp(start_time_str, tz="UTC")
    data_end_ts = pd.Timestamp(end_time_str, tz="UTC") if end_time_str else pd.Timestamp.now(tz="UTC")
    failed = {}
    for i, coin in enumerate(coins, 1):
        progress_cb(i - 1, total, f"качаю свечи {coin}")
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            failed[coin] = f"Монета {coin} не найдена на бирже"
            continue
        try:
            candle_archive.ensure_for_sim(exchange, symbol, start_ts, data_end_ts, forward_days)
        except Exception as e:
            failed[coin] = f"Нет данных: {e}"
    return failed


def _write_bulk_progress(done, total, current_coin, finished=False, strategy="BOUNCE"):
    """Пишет прогресс bulk-прогона на диск после каждой монеты (читает его
    лёгкий GET, пока основной POST ещё считает). Не должна ронять прогон."""
    try:
        save_json_atomic(_sim_path(strategy, "bulk_progress"), {
            "done": done,
            "total": total,
            "current_coin": current_coin,
            "finished": finished,
            "updated_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2)
    except Exception as e:
        print(f"⚠️ [SIM] Не удалось записать прогресс bulk-прогона: {e}")


def get_bulk_progress(strategy="BOUNCE"):
    """Прогресс bulk-прогона для опроса с фронта. {} если ни разу не запускали."""
    data = load_json(_sim_path(strategy, "bulk_progress"), default={})
    return data if isinstance(data, dict) and data else {}


def get_last_run(strategy="BOUNCE"):
    """Последний сохранённый результат одиночного прогона — страница
    симулятора берёт его после F5. None, если ещё не запускали."""
    data = load_json(_sim_path(strategy, "last_run"), default={})
    return data if isinstance(data, dict) and data else None


def get_last_all_run(strategy="BOUNCE"):
    """Последний сохранённый результат bulk-прогона (свой файл)."""
    data = load_json(_sim_path(strategy, "last_all_run"), default={})
    return data if isinstance(data, dict) and data else None


# ======================================================================
# СЕМЕЙСТВО BOUNCE / BUYBACK: BounceManager.process_candle
# ======================================================================

def _sim_hold_days(sim_cfg, strategy="BOUNCE"):
    """MAX_HOLD_DAYS для прогона: свои из sim_cfg (режим Sim) или боевые (Watcher)."""
    if sim_cfg:
        v = _num(sim_cfg.get("max_hold_days"))
        if v is not None and v > 0:
            return int(v)
    return _watcher_cls(strategy).CONFIG.get("MAX_HOLD_DAYS", 14)


def _settings_snapshot(sim_cfg, strategy="BOUNCE"):
    """Какие настройки были у прогона — кладётся в результат, UI показывает их строкой."""
    strategy = _norm_strategy(strategy)
    if not sim_cfg:
        snap: dict = {"mode": "watcher", "strategy": strategy}
        if strategy == "BUYBACK":
            snap["entry_mode"] = "buyback"
            snap["buyback"] = dict(BUYBACK)
        return snap
    res: dict = {
        "mode": "sim", "strategy": strategy,
        "tp": sim_cfg.get("tp"), "sl": sim_cfg.get("sl"),
        "short_min": sim_cfg.get("short_min"), "long_max": sim_cfg.get("long_max"),
        "max_hold_days": _sim_hold_days(sim_cfg, strategy),
        "entry_mode": "buyback" if strategy == "BUYBACK" else (sim_cfg.get("entry_mode") or "volume"),
    }
    if strategy == "BUYBACK":
        res["buyback"] = dict(BUYBACK)
    return res


def _episode_bounce(watcher, level_id, coin, strategy="BOUNCE"):
    return {
        "level_id": level_id,
        "coin": coin,
        "strategy": strategy,
        "mode": getattr(watcher, "mode", None),
        "direction": getattr(watcher, "trade_type", None),
        "state": getattr(watcher, "state", "UNKNOWN"),
        "level_min": getattr(watcher, "min", None),
        "level_max": getattr(watcher, "max", None),
        "level_date": getattr(watcher, "level_date", None),
        "level_type": getattr(watcher, "level_type", None),
        "level_score": getattr(watcher, "level_score", None),
        "level_reaction_count": getattr(watcher, "level_reaction_count", None),
        "activated_at": getattr(watcher, "activated_at", None),
        "born_at": getattr(watcher, "born_at", None),
        "events": list(getattr(watcher, "event_log", [])),
    }


def _run_bounce(coin, start_time_str, end_time_str=None, top_up=True,
                allow_long=True, allow_short=True, sim_cfg=None, strategy="BOUNCE"):
    """
    strategy: "BOUNCE" (основной) или "BUYBACK" (отдельная стратегия: свой
        менеджер/вотчер, свои логи и файлы результата). BUYBACK берёт
        направления из BUYBACK['ALLOW_LONG'/'ALLOW_SHORT'] (шорт выключен).
    sim_cfg: None — режим Watcher (всё как в бою: TP/SL/пороги EMA из боевых
        config.json и coin_settings.json). Словарь {tp, sl, short_min, long_max,
        max_hold_days} — режим Sim: свои общие настройки симулятора (по монетам
        не бывает), боевые файлы не читаются и не меняются.
    coin: тикер, напр. 'BTC'
    start_time_str: 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM' (UTC) — начало реплея
    end_time_str: конец реплея, по умолчанию — самая свежая доступная свеча
    top_up: докачивать ли недостающие свечи в архив симулятора перед
        прогоном (см. _candles_df) — False используется bulk-прогоном
        (run_bulk_simulation): там докачка уже сделана в главном
        процессе до раздачи монет воркерам.
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
    strategy = _norm_strategy(strategy)
    log_dir = _sim_path(strategy, "log_dir")
    if strategy == "BUYBACK":
        allow_long = bool(allow_long and BUYBACK["ALLOW_LONG"])
        allow_short = bool(allow_short and BUYBACK["ALLOW_SHORT"])
    coin = coin.upper().replace("USDT", "").replace("/", "").strip()

    markets = load_markets_cached(exchange)
    symbol = resolve_symbol(coin, markets)
    if not symbol:
        raise ValueError(f"Монета {coin} не найдена на бирже")

    start_ts = pd.Timestamp(start_time_str, tz="UTC")
    data_end_ts = pd.Timestamp(end_time_str, tz="UTC") if end_time_str else pd.Timestamp.now(tz="UTC")
    hold_days = _sim_hold_days(sim_cfg, strategy)
    fwd_days = max(FORWARD_DAYS, hold_days)
    df_full = _candles_df(symbol, start_ts, data_end_ts, top_up=top_up, forward_days=fwd_days)

    end_ts = pd.Timestamp(end_time_str, tz="UTC") if end_time_str else df_full.index[-1]
    replay_df = df_full[(df_full.index >= start_ts) & (df_full.index <= end_ts)]
    if replay_df.empty:
        raise ValueError(f"Нет свечей в диапазоне {start_time_str} .. {end_time_str or 'сейчас'} для {coin}")

    atr_series = calculate_atr(df_full)
    # EMA200(4h): 4h-свечи из архива симулятора (запас 100 суток назад от старта), а не из 15m
    ema_dist_fn = make_ema_dist_fn(
        df_full, candles_4h=candle_archive.get_sim_candles(symbol, "4h", start_ts, data_end_ts, fwd_days),
    )

    # Свой, полностью изолированный менеджер — ничего общего с боевым
    # bounce_mgr из background_tasks, ничего не пишет в
    # боевые jsonbank/*.json. Своя папка логов, не bounce_logs/.
    os.makedirs(log_dir, exist_ok=True)
    _reset_sim_log(coin, log_dir)  # новый прогон — старый лог этой монеты не тащим
    if strategy == "BUYBACK":
        bounce_mgr = BuybackManager(log_dir_override=log_dir, entry_mode="buyback")
    else:
        bounce_mgr = BounceManager(log_dir_override=log_dir, entry_mode=(sim_cfg or {}).get("entry_mode"))
    bounce_mgr.set_ema_dist_fn(coin, ema_dist_fn)  # EMA для отчёта (ema_dist_birth)
    # Фильтр уровней по EMA (levels/level_filter.py) — те же пороги из config.json,
    # что и в бою: общие + свои у монеты.
    if sim_cfg:
        # Режим Sim: пороги — свои общие (None = без фильтра), по монетам не бывает.
        level_filter = LevelFilter(coin, ema_dist_fn,
                                   thresholds=(_num(sim_cfg.get("short_min")), _num(sim_cfg.get("long_max"))))
    else:
        level_filter = LevelFilter(coin, ema_dist_fn)

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

        supports, resistances = level_filter.apply(ts, supports, resistances)
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
            if sim_cfg and entry:
                # Режим Sim: TP/SL от цены входа по СВОИМ процентам симулятора
                # (вотчер посчитал их по боевым — здесь пересчитываем).
                _tp, _sl = _num(sim_cfg.get("tp")), _num(sim_cfg.get("sl"))
                if _tp is not None:
                    tp = entry * (1 + _tp / 100.0) if tt == "LONG" else entry * (1 - _tp / 100.0)
                if _sl is not None:
                    sl = entry * (1 - _sl / 100.0) if tt == "LONG" else entry * (1 + _sl / 100.0)
            rr = ((tp - entry) / (entry - sl)) if tt == "LONG" and entry > sl else \
                 ((entry - tp) / (sl - entry)) if tt == "SHORT" and sl > entry else 0

            level_id_val = d.get("level_id", "") or ""
            mode = level_id_val.split("__")[-1] if "__" in level_id_val else None
            source_label = f"{strategy}_{tt}" + (f"_{mode}" if tt == "SHORT" and mode else "")

            outcome = compute_trade_outcome(
                ts, entry, tp, tt, df_full, hold_days,
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
                "level_type": d.get("level_type", ""),
                "level_reaction_count": getattr(bounce_mgr._watchers.get(level_id_val), "level_reaction_count", None),
                "volume": d.get("volume", float(row["volume"])),
                "volume_mult": d.get("volume_mult"),
                "reason": d.get("reason", ""),
                # То же самое, что уже показывает боевой дашборд для реальных
                # сигналов (см. history.py::save_signal/update_open_signals) —
                # только тут исход честно посчитан по истории, а не ждёт
                # реального тикера биржи.
                "status": outcome["status"],
                "result_percent": outcome["result_percent"],
                "closed_at": outcome["closed_at"],
                "mae_pct": outcome["mae_pct"],
                # Та же цифра, по которой работал EMA-фильтр: край зоны (LONG — верх,
                # SHORT — низ) от EMA200(4h) в момент рождения вотчера. Если вотчера
                # нет/значения нет — старый расчёт по цене входа.
                "ema_dist_pct": getattr(bounce_mgr._watchers.get(level_id_val), "ema_dist_birth", None),
                "method": d.get("method", "volume"),
                "pattern_route": d.get("pattern_route"),
                "method_value": d.get("volume", float(row["volume"])),
                "method_mult": d.get("volume_mult"),
            })

            emoji = "🟢" if tt == "LONG" else "🔴"
            messages.append(
                f"{emoji} *{strategy} {tt}* _{coin}_ — {ts_naive.strftime('%Y-%m-%d %H:%M')}\n"
                f"Entry: `{entry:.8f}`  SL: `{sl:.8f}`  TP: `{tp:.8f}`  R/R: `{rr:.2f}`\n"
                f"{d.get('reason', '')}  Level: `{d.get('level_id', 'unknown')}`"
            )

    active, history = [], []
    for level_id, watcher in bounce_mgr._watchers.items():
        ep = _episode_bounce(watcher, level_id, coin, strategy)
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
        "settings": _settings_snapshot(sim_cfg, strategy),
        "strategy": strategy,
    }

    return result


# ======================================================================
# СЕМЕЙСТВО V (VB / VGB / VRT): VManager.step
# ======================================================================

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
    одного снимка на дату старта на весь прогон (см. докстринг модуля): иначе уровень, появившийся ПОСЛЕ даты старта, весь
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


def _episode_v(mgr, level_id, strategy, direction, coin, level=None):
    """level — тот самый словарь уровня (min/max/type/date/score), на
    котором создан вотчер (см. вызовы ниже — там ещё есть `tracked` в
    скоупе, поэтому передаём его напрямую, а не пытаемся угадывать через
    getattr(watcher, "min", ...) — у V-вотчеров нет гарантии, что такие
    атрибуты вообще называются так же, как у BounceWatcher).

    БЕЗ этого поля level_min/level_max/level_type/level_date/level_score
    отсутствовали в episode вовсе — фронт (sim.js::drawTradeDetail) не
    находил границы зоны и молча ничего не рисовал: ни полосы уровня, ни
    маркеров пути, ни даже точки входа (тот же формат, что уже строит
    _episode_bounce(), чтобы фронт рисовал обе семьи одним
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
    """Собирает запись сделки из результата VManager.feed (result — тот же
    словарь entry_price/sl/tp/level_id, что уходит в save_signal() в
    watcher_plan.py, см. check_v) — тот же формат,
    что и trades у BOUNCE (_run_bounce), чтобы фронт рисовал обе
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
    зеркало check_v() из watcher_plan.py,
    только свечи идут одна за одной по истории, а не по одной за реальный
    скан.

    🔥 В отличие от прежней версии — после срабатывания сигнала уровень
    СНИМАЕТСЯ со слежения (tracked = None), а не держится до конца периода
    навсегда. Это зеркалит check_v() в бою (там — явный
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
    mgr = _new_mgr(tag)
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

        c_atr = None
        if strategy == "V_GREEN_BOTTOM":
            atr_series = calculate_atr(window, 14)
            if not atr_series.empty and pd.notna(atr_series.iloc[-1]):
                c_atr = float(atr_series.iloc[-1])

        # replay=False: путь вотчера мы ведём сами, свеча за свечой
        tracked, result, cancelled = mgr.step(coin, supports, resistances, window, c_atr=c_atr, replay=False)
        if tracked is None:
            continue

        if cancelled or result:
            if result:
                trades.append(_build_trade(result, tracked, strategy, "LONG", coin, window.index[-1], df_full, ema_dist_fn))
            ep = _episode_v(mgr, mgr.level_id(tracked), strategy, "LONG", coin, tracked)
            if ep:
                episodes.append(ep)

    tracked = mgr.tracked.get(coin)  # уровень, который вели до конца периода
    if tracked is not None:
        ep = _episode_v(mgr, mgr.level_id(tracked), strategy, "LONG", coin, tracked)
        if ep:
            episodes.append(ep)

    return episodes, trades


def _walk_short(df_full, start_i, end_i, coin, seen_levels=None, ema_dist_fn=None):
    """Зеркало check_v() — один уровень может дать несколько
    сигналов подряд, поэтому снимаем со слежения только когда вотчер
    реально закончил работу (TRIGGERED/DEAD/IDLE), а не после 1-го сигнала
    (это уже так и было — V_RED_TOP, в отличие от VB/VGB выше, и раньше
    умел находить несколько сделок подряд на одном уровне).

    resistances — score-отфильтрованный (для поиска пробоя, зеркалит
    check_v). supports_all — БЕЗ фильтра по score (opposite_levels
    для V_RED_TOP) — 🐛 раньше сюда по ошибке передавался
    отфильтрованный supports; в бою (watcher_plan.py::check_v,
    строка `supports = coin_macro.get("supports", [])`) фильтра там нет
    вообще — починил, чтобы не расходиться с боевым поведением.

    Уровни, как и в _walk_long, обновляются раз в календарный день (см.
    _refresh_levels_if_needed), а не заморожены на дату старта."""
    mgr = _new_mgr("VRT")
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

        tracked, result, _ = mgr.step(coin, resistances, supports_all, window, c_atr=c_atr, replay=False,
                                      c_atr_slow=c_atr_slow, c_ema=c_ema, c_rsi=c_rsi)
        if tracked is None:
            continue

        level_id = mgr.level_id(tracked)
        seen_ids.add(level_id)
        level_by_id.setdefault(level_id, tracked)

        if result:
            entry_ts = window.index[-1]
            dedup_key = (level_id, result.get("level_id"), str(entry_ts))
            if dedup_key not in recorded_keys:
                recorded_keys.add(dedup_key)
                trades.append(_build_trade(result, tracked, "V_RED_TOP", "SHORT", coin, entry_ts, df_full, ema_dist_fn))

    episodes = []
    for level_id in seen_ids:
        ep = _episode_v(mgr, level_id, "V_RED_TOP", "SHORT", coin, level_by_id.get(level_id))
        if ep:
            episodes.append(ep)
    return episodes, trades


def _run_v(coin, start_time_str, end_time_str=None, top_up=True, strategies=None):
    """
    coin: тикер, напр. 'BTC'
    start_time_str: дата/время старта, 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM' (UTC)
    end_time_str: конец периода, по умолчанию — самая свежая доступная свеча
    strategies: список тегов из ALL_V_STRATEGY_TAGS ("VB"/"VGB"/"VRT") —
        какие из трёх реально гонять. None (по умолчанию) = все три, старое
        поведение не меняется для вызовов без этого параметра.
    top_up: докачивать ли недостающие свечи в архив симулятора перед
        прогоном — False для bulk (см. run_bulk_simulation): там докачка
        уже сделана в главном процессе до раздачи монет воркерам.

    Возвращает {"coin", "start_time", "end_time", "active", "history",
    "levels", "trades"} — trades в том же формате, что у BOUNCE-симулятора
    (_run_bounce), events — тот же формат,
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

    # ⚠️ Больше НЕТ единой проверки "а есть ли уровни на дату старта" —
    # раньше от неё зависело, запускать ли вообще проход (`if supports:`).
    # Но уровни теперь читаются заново каждый день ВНУТРИ самого прохода
    # (см. _walk_long/_walk_short → _refresh_levels_if_needed) — если на
    # старте пусто, а уровень появится через неделю, проход его всё равно
    # найдёт сам. Гейтить по дню старта значило бы пропускать именно те
    # случаи, ради которых это всё и делалось.
    data_end_ts = pd.Timestamp(end_time_str, tz="UTC") if end_time_str else pd.Timestamp.now(tz="UTC")
    df_full = _candles_df(symbol, start_ts, data_end_ts, top_up=top_up, forward_days=V_MAX_HOLD_DAYS)
    _reset_sim_log(coin, _sim_path("V", "log_dir"))  # новый прогон — лог монеты перезаписывается (одна монета = один лог)
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
    # расстояние входа от EMA200(4h), %; 4h-свечи — из архива симулятора (запас 100 суток назад от старта)
    ema_dist_fn = make_ema_dist_fn(
        df_full, candles_4h=candle_archive.get_sim_candles(symbol, "4h", start_ts, data_end_ts, V_MAX_HOLD_DAYS),
    )
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
        "strategy": "V",
        "strategies": strategies,
        "active": active,
        "history": history,
        "levels": levels_out,
        "trades": trades,
    }

    return result


# ======================================================================
# ОДИН ВХОД: одиночный прогон и bulk
# ======================================================================

def run_simulation(coin, start_time_str, end_time_str=None, strategy="BOUNCE", top_up=True,
                   save_last_run=True, **opts):
    """Один прогон по одной монете.

    strategy: "BOUNCE" | "BUYBACK" | "V".
    opts зависят от семейства:
      BOUNCE/BUYBACK: allow_long, allow_short, sim_cfg (см. _run_bounce);
      V: strategies — список тегов из ALL_V_STRATEGY_TAGS (пусто = все три).
    top_up: докачивать ли свечи в архив перед прогоном (False у bulk — там
        докачка уже сделана в главном процессе).
    save_last_run: сохранять ли результат для восстановления страницы после
        F5 (False у bulk, чтобы цикл по монетам не затирал файл).
    """
    strategy = _norm_strategy(strategy)
    run = _run_v if strategy == "V" else _run_bounce
    if strategy != "V":
        opts["strategy"] = strategy
    result = run(coin, start_time_str, end_time_str, top_up=top_up, **opts)
    if save_last_run:
        try:
            save_json_atomic(_sim_path(strategy, "last_run"), result, indent=2)
        except Exception as e:
            print(f"⚠️ [SIM {strategy}] Не удалось сохранить последний результат: {e}")
    return result


def _simulate_one_coin_worker(task):
    """Одна монета в отдельном процессе. Функция модульного уровня (на Windows
    multiprocessing = spawn, вложенные функции pickle не берёт). Ничего не
    пишет сама — прогресс и файлы пишет главный процесс (нет гонки записи).
    task = (coin, start, end, strategy, opts). Возвращает (coin, payload, error)."""
    coin, start_time_str, end_time_str, strategy, opts = task
    try:
        sim_result = run_simulation(coin, start_time_str, end_time_str, strategy=strategy,
                                    top_up=False, save_last_run=False, **opts)
        payload = {"trades": sim_result.get("trades", []), "history": sim_result.get("history", [])}
        return coin, payload, None
    except Exception as e:
        return coin, None, str(e)


def run_bulk_simulation(start_time_str, end_time_str=None, strategy="BOUNCE", coin_filter=None,
                        blocked=None, **opts):
    """Прогон по ВСЕМ монетам, у которых были уровни в периоде (list_coins_with_levels).

    coin_filter — множество тикеров (текущий whitelist/greylist) или None =
        все монеты периода. blocked — монеты, заблокированные в симуляторе.
    opts — те же, что у run_simulation (allow_long/allow_short/sim_cfg или strategies).

    Свечи докачиваются ДО раздачи монет воркерам (последовательно, в главном
    процессе); воркеры только читают. Одна упавшая монета не валит прогон —
    её ошибка попадает в results[coin]["error"].

    Возвращает {results (сводка по монете), trades (плоский список всех
    сделок), history_by_coin (эпизоды только для монет со сделками), ...} и
    сохраняет всё в last_all_run файл семейства."""
    strategy = _norm_strategy(strategy)
    coins, coverage = list_coins_with_levels(start_time_str, end_time_str)
    if coin_filter is not None:
        coins = [c for c in coins if c in coin_filter]
    if blocked:
        coins = [c for c in coins if c not in blocked]
    total = len(coins)

    if strategy == "V":
        forward_days = V_MAX_HOLD_DAYS
    else:
        forward_days = max(FORWARD_DAYS, _sim_hold_days(opts.get("sim_cfg"), strategy))

    results = {}
    all_trades = []
    history_by_coin = {}
    coins_with_trades = 0

    _write_bulk_progress(0, total, None, finished=False, strategy=strategy)

    failed = _ensure_archive_for_coins(
        coins, load_markets_cached(exchange), start_time_str, end_time_str, forward_days,
        lambda d, t, label: _write_bulk_progress(d, t, label, finished=False, strategy=strategy),
    )
    for coin, err in failed.items():
        results[coin] = {"error": err}
    tasks = [(coin, start_time_str, end_time_str, strategy, opts) for coin in coins if coin not in failed]

    done = len(failed)
    _write_bulk_progress(done, total, None, finished=False, strategy=strategy)
    with ProcessPoolExecutor(max_workers=BULK_MAX_WORKERS) as executor:
        future_to_coin = {executor.submit(_simulate_one_coin_worker, task): task[0] for task in tasks}
        for future in as_completed(future_to_coin):
            coin = future_to_coin[future]
            try:
                _, payload, error = future.result()
            except Exception as e:
                # сам процесс-воркер мог упасть целиком — не даём одной монете обрушить batch
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
            _write_bulk_progress(done, total, coin, finished=False, strategy=strategy)

    _write_bulk_progress(total, total, None, finished=True, strategy=strategy)

    result = {
        "start_time": start_time_str,
        "end_time": end_time_str,
        "coins_scanned": len(coins),
        "coins_with_trades": coins_with_trades,
        "results": results,
        "trades": all_trades,
        "history_by_coin": history_by_coin,
        "strategy": strategy,
    }
    if strategy == "V":
        result["strategies"] = list(opts.get("strategies") or ALL_V_STRATEGY_TAGS)
    else:
        result["settings"] = _settings_snapshot(opts.get("sim_cfg"), strategy)

    try:
        save_json_atomic(_sim_path(strategy, "last_all_run"), result, indent=2)
    except Exception as e:
        print(f"⚠️ [SIM {strategy}] Не удалось сохранить bulk-результат: {e}")

    return result


def prefetch_archive(start_time_str, end_time_str=None):
    """Заранее докачивает архив симулятора (15m + 4h) за период — без сайта.
    Монеты те же, что у bulk (list_coins_with_levels): для периода уже должен
    быть посчитан файл уровней levels_timeline_*.json (precalc_for_bot.py).
    Уже скачанное повторно не качается. Возвращает {coin: текст ошибки}."""
    coins, _ = list_coins_with_levels(start_time_str, end_time_str)
    if not coins:
        print("❌ За этот период нет монет с уровнями. Сначала посчитай уровни "
              "(precalc_for_bot.py, MONTHS_TO_CALC) на этот период.")
        return {}
    markets = load_markets_cached(exchange)
    print(f"📦 Период {start_time_str} .. {end_time_str or 'сейчас'}: монет {len(coins)}. "
          f"Качаю недостающие свечи (15m + 4h) в архив симулятора...")
    failed = _ensure_archive_for_coins(
        coins, markets, start_time_str, end_time_str, FORWARD_DAYS,
        lambda d, t, label: print(f"[{d + 1}/{t}] {label}"),
    )
    if failed:
        print(f"⚠️ Не удалось докачать {len(failed)} монет (в bulk они попадут в отчёт как error):")
        for coin, err in failed.items():
            print(f"   {coin}: {err}")
    print(f"✅ Готово: {len(coins) - len(failed)} из {len(coins)} монет в архиве.")
    return failed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python -m backend.modules.cryptano.simulator.simulate_engine "
              "ГГГГ-ММ-ДД [ГГГГ-ММ-ДД конец]")
        sys.exit(1)
    prefetch_archive(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)