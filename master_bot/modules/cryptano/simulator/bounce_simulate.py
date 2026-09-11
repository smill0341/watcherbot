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

from modules.cryptano.utils.common import exchange, resolve_symbol
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.indicators import calculate_atr
from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.levels_history import get_levels_snapshot
from modules.cryptano.strategy.bounce_manager import BounceManager
from modules.cryptano.strategy.bounce_parent import BounceParent
from modules.cryptano.watcher_plan import candle_store

SIM_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LAST_RUN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run.json")

WINDOW = 60  # то же окно, что и в боевом реплее (watcher_plan.py::check_bounce) — evaluate_bounce
             # читает максимум последние ~52 строки, окно с запасом, не вся история разом.


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


def _candles_df(symbol):
    """Вся доступная 15m-история из candle_store (та же база, что у дашборда
    и боевого watcher_plan.py) — никакого похода на биржу за свечами."""
    if candle_store is None:
        raise RuntimeError("candle_store недоступен")
    if candle_store.has_data(symbol, "15m"):
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
        "events": list(getattr(watcher, "event_log", [])),
    }


def run_bounce_simulation(coin, start_time_str, end_time_str=None):
    """
    coin: тикер, напр. 'BTC'
    start_time_str: 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM' (UTC) — начало реплея
    end_time_str: конец реплея, по умолчанию — самая свежая доступная свеча

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

    df_full = _candles_df(symbol)

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

    for ts, row in replay_df.iterrows():
        pos = df_index.searchsorted(ts, side="right")
        df_slice = df_full.iloc[max(0, pos - WINDOW):pos]
        if len(df_slice) < 52:
            continue  # недостаточно истории ДО этой свечи для индикаторов

        c_high, c_low, c_close = float(row["high"]), float(row["low"]), float(row["close"])
        c_atr = float(atr_series.loc[ts]) if ts in atr_series.index else 0.0

        ts_naive = ts.tz_localize(None) if ts.tzinfo is not None else ts
        snapshot = get_levels_snapshot(ts_naive.to_pydatetime())
        if snapshot is not None:
            coin_macro = snapshot.get(coin) or {}
            supports = coin_macro.get("supports", [])
            resistances = coin_macro.get("resistances", [])
        else:
            supports, resistances = [], []

        orders, _draw_events = bounce_mgr.process_candle(
            c_low, c_high, c_close, supports, resistances, df_slice,
            allow_long=True, allow_short=True, c_atr=c_atr, coin=coin,
        )

        for order in orders:
            d = order["decision"]
            tt = order["trade_type"]
            entry, sl, tp = d.get("entry_price", 0.0), d.get("sl", 0.0), d.get("tp", 0.0)
            rr = ((tp - entry) / (entry - sl)) if tt == "LONG" and entry > sl else \
                 ((entry - tp) / (sl - entry)) if tt == "SHORT" and sl > entry else 0

            level_id_val = d.get("level_id", "") or ""
            mode = level_id_val.split("__")[-1] if "__" in level_id_val else None
            source_label = f"BOUNCE_{tt}" + (f"_{mode}" if tt == "SHORT" and mode else "")

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