# modules/cryptano/simulator/outcome.py
"""
Общий "честный" подсчёт исхода сделки по истории — раньше жил только
внутри bounce_simulate.py (_compute_trade_outcome), скопирован сюда,
чтобы simulate_engine.py (V_BOTTOM/V_GREEN_BOTTOM/V_RED_TOP) считал исход
ТЕМ ЖЕ САМЫМ способом, а не своей отдельной копией — иначе результаты
BOUNCE и V-семьи в симуляторе почти наверняка разъехались бы в мелочах
(округление, что считается "закрытием", MAE) при первой же независимой
правке одного из двух файлов.

Логика не поменялась при переносе — просто вынесена в общий модуль и
переименована в публичное имя (без ведущего "_", т.к. теперь используется
за пределами одного файла).
"""

import pandas as pd


def compute_trade_outcome(entry_ts, entry_price, target_price, trade_type, df_full, max_hold_days):
    """Честный проход ВПЕРЁД по свечам от входа — определяет исход сделки.

    Закрывает сделку только TP либо дедлайн (max_hold_days) — тот же самый
    принцип, что уже описан в BounceWatcher.CONFIG['SL_PCT']: "держим для
    отображения/справки — НЕ закрывает сделку, на тестах реального
    стоп-лосса не было". Это не отдельное решение для одной стратегии,
    это единый способ, каким симулятор вообще считает исход — для BOUNCE
    и для V_BOTTOM/V_GREEN_BOTTOM/V_RED_TOP одинаково.

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

EMA_DIST_PERIOD = 200      # "тяжёлая" EMA — та же EMA200 по 4h, что рисует дашборд
EMA_DIST_TF = "4h"


def make_ema_dist_fn(df_full, period=EMA_DIST_PERIOD, tf=EMA_DIST_TF):
    """Готовит (ОДИН раз на прогон монеты) EMA200 по 4h-свечам, собранным из
    15m-истории df_full, и возвращает функцию f(entry_ts, entry_price) ->
    расстояние входа от EMA в % ((цена - EMA) / EMA * 100; плюс = выше EMA,
    минус = ниже) либо None, если данных для EMA не хватает.

    Без подглядывания в будущее: берётся только 4h-свеча, ЗАКРЫВШАЯСЯ не
    позже момента входа (entry_ts — открытие 15m-свечи сигнала, вход по
    её закрытию, т.е. entry_ts + 15 минут). Формирующаяся 4h-свеча не
    участвует. EMA считается как на графике (ewm, adjust=False), но
    значение отдаём только когда позади есть минимум `period` закрытых 4h-
    свечей (~33 дня) — иначе EMA ещё "прогревается" и врёт."""
    try:
        close_4h = df_full["close"].resample(tf).last().dropna()
        if len(close_4h) < period:
            return lambda entry_ts, entry_price: None
        ema = close_4h.ewm(span=period, adjust=False).mean()
        bucket = pd.Timedelta(tf)
        close_times = ema.index + bucket   # момент закрытия каждой 4h-свечи
    except Exception:
        return lambda entry_ts, entry_price: None

    def _fn(entry_ts, entry_price):
        try:
            entry_end = entry_ts + pd.Timedelta(minutes=15)
            pos = int(close_times.searchsorted(entry_end, side="right")) - 1
            if pos + 1 < period:
                return None
            v = float(ema.iloc[pos])
            if v <= 0 or not entry_price:
                return None
            return round((float(entry_price) - v) / v * 100.0, 1)
        except Exception:
            return None

    return _fn