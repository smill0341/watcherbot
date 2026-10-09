# backend/entry_study.py
r"""
Разбор готовых сделок BOUNCE: чем хорошие входы отличаются от плохих.
Ничего не меняет и не пишет — только читает last_all_run.json и 15m-свечи
из архива симулятора (sim_candles.db), печатает отчёт в консоль.

Запуск из корня master_bot (cmd):
    python -X utf8 backend\entry_study.py > entry_study_out.txt
Другой файл результата:
    python -X utf8 backend\entry_study.py --file путь\к\last_all_run.json > entry_study_out.txt

Метрики группы: n, TP% (доля сделок, закрытых тейком за срок), средняя и
медианная просадка (mae), сумма результата, сколько сделок не закрылись.
Стоп в симуляторе не работает, поэтому главная метрика — TP% и просадка.
"""
import os
import sys
import json
import argparse
import datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd

from backend.modules.cryptano.utils.bybit import exchange, resolve_symbol, load_markets_cached
from backend.modules.cryptano.utils.indicators import calculate_atr
import backend.modules.cryptano.levels.candle_archive as candle_archive

DEFAULT_FILE = os.path.join(ROOT, "backend", "modules", "cryptano", "simulator", "last_all_run.json")
BAR = pd.Timedelta(minutes=15)


def _load_candles(symbol, t0, t1):
    rows = candle_archive.get_sim_candles(symbol, "15m", t0, t1, 1)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.drop(columns=["time"]).set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _first_touch_ts(events):
    """Время первого пробоя середины зоны (SWEEP_BOTTOM); если нет — первое касание края."""
    for kind in ("SWEEP_BOTTOM", "ZONE_TOUCH"):
        ts = [e["time"] for e in events if e.get("type") == kind and e.get("time")]
        if ts:
            return pd.Timestamp(min(ts), unit="s", tz="UTC")
    return None


def build_rows(data):
    trades = data["trades"]
    hist = {}
    for coin, eps in data.get("history_by_coin", {}).items():
        for ep in eps:
            hist[(coin, ep["level_id"])] = ep

    markets = {}
    try:
        markets = load_markets_cached(exchange)
    except Exception as e:
        print(f"# нет доступа к рынкам биржи ({e}), символы строю сам", file=sys.stderr)

    by_coin = {}
    for t in trades:
        by_coin.setdefault(t["coin"], []).append(t)

    rows, skipped = [], 0
    for coin, tl in by_coin.items():
        symbol = resolve_symbol(coin, markets) if markets else None
        symbol = symbol or f"{coin}/USDT:USDT"
        dts = [pd.Timestamp(t["date"], tz="UTC") for t in tl]
        t0, t1 = min(dts) - pd.Timedelta(days=10), max(dts) + pd.Timedelta(days=1)
        df = _load_candles(symbol, t0, t1)
        if df is None or df.empty:
            skipped += len(tl)
            continue
        atr = calculate_atr(df)
        for t, ots in zip(tl, dts):
            if ots not in df.index:
                skipped += 1
                continue
            r = df.loc[ots]
            a = float(atr.loc[ots]) if ots in atr.index else 0.0
            if not a or np.isnan(a):
                skipped += 1
                continue
            lo, hi, side = float(t["level_min"]), float(t["level_max"]), t["type"]
            e = float(t["entry"])
            w = hi - lo
            # + : цена отошла от зоны в сторону отскока; 0 : внутри зоны; - : ушла за зону
            if side == "LONG":
                dist = 0.0 if lo <= e <= hi else (e - hi if e > hi else e - lo)
            else:
                dist = 0.0 if lo <= e <= hi else (lo - e if e < lo else hi - e)
            h, l, o, c = float(r["high"]), float(r["low"]), float(r["open"]), float(r["close"])
            rng = h - l
            body_pct = abs(c - o) / rng * 100.0 if rng > 0 else 0.0
            opp = (h - max(o, c)) if side == "LONG" else (min(o, c) - l)
            opp_wick_pct = opp / rng * 100.0 if rng > 0 else 0.0

            ep = hist.get((coin, t.get("level_id")))
            touch = _first_touch_ts(ep["events"]) if ep else None
            lag_h = run_atr = None
            if touch is not None and touch <= ots:
                lag_h = (ots + BAR - touch).total_seconds() / 3600.0
                seg = df[(df.index >= touch) & (df.index <= ots)]
                if not seg.empty:
                    run = (float(seg["high"].max()) - hi) if side == "LONG" else (lo - float(seg["low"].min()))
                    run_atr = run / a

            hours_to_tp = None
            if t["status"] == "✅" and t.get("closed_at"):
                hours_to_tp = (pd.Timestamp(t["closed_at"]) - (ots + BAR)).total_seconds() / 3600.0

            rows.append({
                "coin": coin, "side": side, "date": t["date"],
                "tp": 1 if t["status"] == "✅" else 0,
                "open_at_end": 1 if t["status"] == "🕐" else 0,
                "result": t["result_percent"], "mae": t["mae_pct"], "hours_to_tp": hours_to_tp,
                "dist_atr": dist / a, "dist_w": (dist / w) if w > 0 else 0.0,
                "pos": "внутри зоны" if dist == 0 else ("отошла от зоны" if dist > 0 else "за зоной"),
                "lag_h": lag_h, "run_atr": run_atr,
                "body_pct": body_pct, "opp_wick_pct": opp_wick_pct, "range_atr": rng / a,
                "vol_mult": t.get("volume_mult"), "ema_dist": t.get("ema_dist_pct"),
                "reactions": t.get("level_reaction_count"), "width_atr": w / a,
                "width_pct": w / lo * 100.0 if lo else None,
            })
    return pd.DataFrame(rows), skipped


def _stats(g):
    return {
        "n": len(g), "TP%": 100.0 * g["tp"].mean(), "mae_ср": g["mae"].mean(), "mae_мед": g["mae"].median(),
        "сумма": g["result"].sum(), "не_закрыто": int(g["open_at_end"].sum()),
    }


def _fmt_table(rows):
    out = pd.DataFrame(rows)
    return out.to_string(index=False, float_format=lambda x: f"{x:.1f}")


NUM_FEATURES = ["dist_atr", "dist_w", "lag_h", "run_atr", "body_pct", "opp_wick_pct", "range_atr",
                "vol_mult", "ema_dist", "reactions", "width_atr", "width_pct"]


def _buckets(s, q=4):
    try:
        return pd.qcut(s, q, duplicates="drop")
    except Exception:
        return None


def report(df):
    print("=" * 70)
    print(f"СДЕЛОК В РАЗБОРЕ: {len(df)}  (LONG {int((df.side=='LONG').sum())}, SHORT {int((df.side=='SHORT').sum())})")
    print("Стопа в симуляторе нет: сделка закрывается тейком или таймаутом. TP% — доля закрытых тейком.")
    print("Малые группы (n<20) читать осторожно: разница может быть случайной.")
    print("=" * 70)

    for side in ("LONG", "SHORT"):
        d = df[df.side == side]
        if d.empty:
            continue
        print(f"\n########## {side}  (n={len(d)}) ##########")
        print("Всего:", {k: round(float(v), 1) for k, v in _stats(d).items()})

        print("\n--- Где вход относительно зоны ---")
        print(_fmt_table([{"группа": k, **_stats(g)} for k, g in d.groupby("pos")]))

        ranking = []
        tables = {}
        for f in NUM_FEATURES:
            s = d[f].dropna()
            if s.nunique() < 3:
                continue
            b = _buckets(s)
            if b is None:
                continue
            rows = []
            for iv, idx in b.groupby(b, observed=True).groups.items():
                rows.append({"диапазон": f"{iv.left:.2f} .. {iv.right:.2f}", **_stats(d.loc[idx])})
            tables[f] = rows
            if len(rows) >= 2:
                ranking.append((abs(rows[-1]["TP%"] - rows[0]["TP%"]), f, rows[0]["TP%"], rows[-1]["TP%"],
                                rows[0]["mae_ср"], rows[-1]["mae_ср"], rows[0]["n"], rows[-1]["n"]))

        print("\n--- Что сильнее всего разделяет сделки (низший квартиль против высшего) ---")
        ranking.sort(reverse=True)
        print(_fmt_table([{"признак": r[1], "TP%_низ": r[2], "TP%_верх": r[3], "mae_низ": r[4], "mae_верх": r[5],
                           "n_низ": r[6], "n_верх": r[7]} for r in ranking]))

        for f, rows in tables.items():
            print(f"\n--- {f} (по квартилям) ---")
            print(_fmt_table(rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=DEFAULT_FILE)
    args = ap.parse_args()
    with open(args.file, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"# файл: {args.file}  период: {data.get('start_time')} .. {data.get('end_time')}")
    df, skipped = build_rows(data)
    if skipped:
        print(f"# пропущено сделок (нет свечей/ATR): {skipped}")
    if df.empty:
        print("Нет данных для разбора.")
        return
    report(df)


if __name__ == "__main__":
    main()