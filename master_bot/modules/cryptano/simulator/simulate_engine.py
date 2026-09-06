# modules/cryptano/simulator/simulate_engine.py
"""
Симулятор стратегий "как будто это было онлайн".

Идея: пользователь кликает точку старта на графике → мы
1) реконструируем уровни (supports/resistances) ровно на эту дату —
   через build_levels(), тем же способом, что и боевой swing_hunter.py,
   просто с обрезкой истории по бирже (params={"endTime": ...});
2) берём 15m-свечи от этой даты до сейчас из candle_store (той же базы,
   что использует дашборд и боевой watcher_plan.py);
3) прогоняем их по одной через ТЕ ЖЕ САМЫЕ производственные классы
   вотчеров (VBottomManager + evaluate_v_bottom/v_green_bottom/v_red_top),
   что и в бою — никаких отдельных копий логики, чтобы бой и симулятор
   не расходились, как разошлись master_bot и test/testswing.

Ничего в боевые файлы (macro_levels.json, watcher_state.json,
tracked_origin_levels*.json) не пишет — только читает. Единственная
точка, где симулятор трогает боевое состояние — кнопка "Добавить в
работу" на фронте, это отдельный явный шаг, сюда не входит.
"""

import os
import sys
import pandas as pd

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MASTER_BOT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _MASTER_BOT_ROOT not in sys.path:
    sys.path.insert(0, _MASTER_BOT_ROOT)

from modules.cryptano.utils.common import exchange, calculate_rsi, resolve_symbol
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.indicators import calculate_atr, calculate_ema
from modules.cryptano.utils.levels_builder import build_levels
from modules.cryptano.strategy.vbottom_manager import VBottomManager
from modules.cryptano.strategy.v_bottom_watcher import VBottomWatcher
from modules.cryptano.strategy.v_green_bottom_watcher import VGreenBottomWatcher
from modules.cryptano.strategy.v_red_top_watcher import VRedTopWatcher
from modules.cryptano.watcher_plan import (
    _find_fresh_breach,
    _find_fresh_breach_up,
    VBOTTOM_BREATH_BUFFER_PCT,
    MIN_LEVEL_SCORE,
    candle_store,
)

_WATCHER_CLS = {"VB": VBottomWatcher, "VGB": VGreenBottomWatcher, "VRT": VRedTopWatcher}

# Локальный кэш симулятора — намеренно ОТДЕЛЬНАЯ папка от боевых JSON
# (macro_levels.json и т.д.), чтобы повторные прогоны по одной и той же
# монете/дате не дёргали биржу заново, и чтобы это никак не пересекалось
# с боевыми файлами бота.
SIM_CACHE_DIR = os.path.join(_THIS_DIR, "cache")
os.makedirs(SIM_CACHE_DIR, exist_ok=True)

# Сколько свечей контекста тянем в каждый шаг для индикаторов (ATR/ATR_slow(100)/
# EMA50/RSI) — с запасом над ATR_slow, которому нужно 14+100 свечей.
CANDLE_WINDOW = 300


def _levels_at(symbol, coin, target_ts_ms):
    """Уровни ровно на дату старта — тот же build_levels(), что в бою,
    только 1d/4h свечи обрезаны по бирже через params={"endTime": ...}."""
    params = {"endTime": target_ts_ms}
    ohlcv_1d = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=365, params=params)
    if len(ohlcv_1d) < 50:
        return {"supports": [], "resistances": []}
    ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=200, params=params)

    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    df_1d = pd.DataFrame(ohlcv_1d, columns=cols)
    df_4h = pd.DataFrame(ohlcv_4h, columns=cols) if len(ohlcv_4h) >= 50 else None

    return build_levels(df_1d, df_4h, coin)


def _candles_df(symbol):
    """Вся доступная 15m-история из candle_store (та же база, что у дашборда)."""
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
    заходе тот же вотчер переиспользуется и breach_count растёт.
    """
    level_id = mgr._level_id(level, trade_type, tag)
    watcher = mgr._watchers.get(level_id)
    if watcher is None:
        watcher = _WATCHER_CLS[tag](level["min"], level["max"], trade_type, coin=coin)
        mgr._watchers[level_id] = watcher
    elif hasattr(watcher, "on_breach_start"):
        watcher.on_breach_start()
    return level_id, watcher


def _episode(mgr, level_id, strategy, direction, coin):
    watcher = mgr._watchers.get(level_id)
    if watcher is None:
        return None
    return {
        "level_id": level_id,
        "coin": coin,
        "strategy": strategy,
        "direction": direction,
        "state": getattr(watcher, "state", "UNKNOWN"),
        "events": list(getattr(watcher, "event_log", [])),
    }


def _walk_long(df_full, start_i, supports, resistances, strategy, coin):
    """
    Прогоняет V_BOTTOM или V_GREEN_BOTTOM от свечи start_i до конца df_full —
    зеркало check_v_bottom()/check_v_green_bottom() из watcher_plan.py, только
    свечи идут одна за одной по истории, а не по одной за реальный скан.
    """
    tag = "VB" if strategy == "V_BOTTOM" else "VGB"
    mgr = VBottomManager()
    tracked = None
    episodes = []
    n = len(df_full)

    for i in range(start_i, n):
        lo = max(0, i - CANDLE_WINDOW)
        window = df_full.iloc[lo:i + 1]
        if len(window) < 3:
            continue

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
                ep = _episode(mgr, level_id, strategy, "LONG", coin)
                if ep:
                    episodes.append(ep)
                tracked = None
                continue

        if strategy == "V_BOTTOM":
            mgr.evaluate_v_bottom(tracked, window, "LONG", resistances, trend="UNKNOWN", c_atr=None, coin=coin)
        else:
            mgr.evaluate_v_green_bottom(tracked, window, "LONG", resistances, trend="UNKNOWN", c_atr=c_atr, coin=coin)

    if tracked is not None:
        level_id = mgr._level_id(tracked, "LONG", tag)
        ep = _episode(mgr, level_id, strategy, "LONG", coin)
        if ep:
            episodes.append(ep)

    return episodes


def _walk_short(df_full, start_i, resistances, supports, coin):
    """Зеркало check_v_red_top() — один уровень может дать несколько
    сигналов подряд, поэтому снимаем со слежения только когда вотчер
    реально закончил работу (TRIGGERED/DEAD/IDLE), а не после 1-го сигнала."""
    mgr = VBottomManager()
    tracked = None
    seen_ids = set()
    n = len(df_full)

    for i in range(start_i, n):
        lo = max(0, i - CANDLE_WINDOW)
        window = df_full.iloc[lo:i + 1]
        if len(window) < 110:  # запас под ATR_slow(100)
            continue

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

        mgr.evaluate_v_red_top(
            tracked, window, "SHORT", supports, trend="UNKNOWN",
            c_atr=c_atr, c_atr_slow=c_atr_slow, c_ema=c_ema, c_rsi=c_rsi, coin=coin,
        )

        level_id = mgr._level_id(tracked, "SHORT", "VRT")
        seen_ids.add(level_id)
        watcher = mgr._watchers.get(level_id)
        if watcher is not None and getattr(watcher, "state", None) in ("TRIGGERED", "DEAD", "IDLE"):
            tracked = None

    episodes = []
    for level_id in seen_ids:
        ep = _episode(mgr, level_id, "V_RED_TOP", "SHORT", coin)
        if ep:
            episodes.append(ep)
    return episodes


def run_simulation(coin, start_time_str):
    """
    coin: тикер, напр. 'BTC'
    start_time_str: дата/время старта, 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM' (UTC)

    Возвращает {"coin", "start_time", "active", "history"} — те же ключи
    и тот же формат events: [{time, type, price}, ...], что и боевой
    /api/events/{coin}, чтобы фронт мог отрисовать результат теми же
    маркерами без переделки.
    """
    coin = coin.upper().replace("USDT", "").replace("/", "").strip()

    markets = load_markets_cached(exchange)
    symbol = resolve_symbol(coin, markets)
    if not symbol:
        raise ValueError(f"Монета {coin} не найдена на бирже")

    start_ts = pd.Timestamp(start_time_str, tz="UTC")
    target_ts_ms = int(start_ts.timestamp() * 1000)

    levels = _levels_at(symbol, coin, target_ts_ms)
    supports = [s for s in levels.get("supports", []) if s.get("score", 0) >= MIN_LEVEL_SCORE]
    resistances_all = levels.get("resistances", [])
    resistances_scored = [r for r in resistances_all if r.get("score", 0) >= MIN_LEVEL_SCORE]

    df_full = _candles_df(symbol)
    start_idx = int(df_full.index.searchsorted(start_ts))
    start_idx = max(0, min(start_idx, len(df_full) - 1))

    episodes = []
    if supports:
        episodes += _walk_long(df_full, start_idx, supports, resistances_all, "V_BOTTOM", coin)
        episodes += _walk_long(df_full, start_idx, supports, resistances_all, "V_GREEN_BOTTOM", coin)
    if resistances_scored:
        episodes += _walk_short(df_full, start_idx, resistances_scored, supports, coin)

    active = [e for e in episodes if e["state"] not in ("TRIGGERED", "DEAD")]
    history = [e for e in episodes if e["state"] in ("TRIGGERED", "DEAD")]

    # Уровни, которые реально проверялись в этом прогоне — отдаём отдельно,
    # чтобы фронт мог нарисовать их поверх графика (другим цветом, не как
    # боевые из macro_levels.json — это ОТДЕЛЬНАЯ реконструкция "на дату").
    levels_out = (
        [{"min": s["min"], "max": s["max"], "type": s.get("type"), "score": s.get("score"), "is_support": True} for s in supports]
        + [{"min": r["min"], "max": r["max"], "type": r.get("type"), "score": r.get("score"), "is_support": False} for r in resistances_scored]
    )

    return {
        "coin": coin,
        "start_time": start_time_str,
        "active": active,
        "history": history,
        "levels": levels_out,
    }