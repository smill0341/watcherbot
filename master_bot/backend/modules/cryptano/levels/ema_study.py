"""
modules/cryptano/levels/ema_study.py
====================================
Тест "как ведёт себя монета относительно EMA200 за год" — без уровней и без
стратегий, только свечи 4h. Нужен, чтобы понять, есть ли у монеты СВОЯ
стабильная высота над/под EMA (тогда личный порог фильтра имеет смысл) или
она от месяца к месяцу разная (тогда нет).

Что считает (по каждой монете whitelist бота):
  - EMA200 по 4h — та же, что у бота (ewm, adjust=False); считается с запасом
    100 суток до окна анализа, баров с индексом < 200 не считаем (как в
    outcome.make_ema_dist_fn).
  - высота над EMA:  (high / EMA - 1) * 100, %  — как далеко цена доходила ВВЕРХ;
    глубина под EMA: (1 - low / EMA) * 100, %  — как далеко ВНИЗ.
  - "выход" за порог T% (T = 5/10/15/20/30): начинается, когда высота (глубина)
    достигла T, заканчивается, когда упала ниже T/2 — чтобы дрожь у самого порога
    не считалась десять раз. У каждого выхода: пик, и через сколько суток после
    начала закрытие 4h вернулось к EMA (вверх: close <= EMA, вниз: close >= EMA).
  - по каждому месяцу: сколько выходов началось, медиана и 90-й процентиль
    высоты, максимум; по монете: насколько это стабильно от месяца к месяцу.

Свечи — из архива симулятора (candle_archive, sim_candles.db): недостающие 4h
докачиваются сами, повторный запуск не качает. Боевой код и симулятор не
трогает, ничего кроме архива свечей и двух CSV в текущей папке не пишет.

Запуск (cmd, из корня master_bot):
    python -m backend.modules.cryptano.levels.ema_study
    python -m backend.modules.cryptano.levels.ema_study --days 365
    python -m backend.modules.cryptano.levels.ema_study --coins BTC,ETH,SOL
"""

import os
import sys
import argparse

import numpy as np
import pandas as pd

THRESHOLDS = (5, 10, 15, 20, 30)   # пороги выхода, % от EMA
EMA_PERIOD = 200
WARMUP_DAYS = 100                  # запас назад для прогрева EMA (600 баров 4h)
BARS_PER_DAY = 6                   # 4h
MIN_MONTH_BARS = 25 * BARS_PER_DAY # месяц считается "полным" при >= 25 суток баров
MIN_FULL_MONTHS = 3                # монеты с меньшим числом полных месяцев не печатаем


# ----------------------------------------------------------------------------
# Расчёт по одной монете
# ----------------------------------------------------------------------------

def find_excursions(depth, mask, thr, back):
    """Выходы за порог thr.
    depth — высота/глубина в % по барам; mask — бары, которые вообще считаем;
    back — True на барах, где закрытие вернулось к EMA.
    Возвращает [(индекс_начала, пик, баров_до_возврата_или_None)]."""
    n = len(depth)
    half = thr / 2.0
    out = []
    i = 0
    while i < n:
        if mask[i] and depth[i] >= thr:
            j = i
            peak = depth[i]
            while j + 1 < n and mask[j + 1] and depth[j + 1] >= half:
                j += 1
                if depth[j] > peak:
                    peak = depth[j]
            nxt = np.flatnonzero(back[i + 1:])
            ret = int(nxt[0]) + 1 if len(nxt) else None
            out.append((i, float(peak), ret))
            i = j + 1
        else:
            i += 1
    return out


def analyze_coin(df, start_ts):
    """df — 4h-свечи (индекс UTC, колонки open/high/low/close), только закрытые.
    Считаются бары с индексом >= EMA_PERIOD (EMA прогрета) и временем >= start_ts.
    Возвращает {"up": {...}, "down": {...}, "month": массив 'ГГГГ-ММ' по барам,
    "mask": маска посчитанных баров} либо None, если баров мало."""
    if len(df) < EMA_PERIOD + MIN_MONTH_BARS:
        return None
    close = df["close"].values.astype(float)
    ema = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean().values
    valid = np.arange(len(df)) >= EMA_PERIOD
    mask = valid & (df.index >= start_ts)
    if mask.sum() < MIN_MONTH_BARS:
        return None
    height = (df["high"].values / ema - 1.0) * 100.0      # вверх
    depth = (1.0 - df["low"].values / ema) * 100.0        # вниз
    month = np.asarray(df.index.strftime("%Y-%m"))
    res = {"month": month, "mask": mask, "idx": df.index}
    for name, series, back in (("up", height, close <= ema), ("down", depth, close >= ema)):
        res[name] = {
            "series": series,
            "exc": {t: find_excursions(series, mask, t, back) for t in THRESHOLDS},
        }
    return res


def month_rows(coin, res):
    """Строки "монета x месяц x направление"."""
    rows = []
    mask, month = res["mask"], res["month"]
    for direction in ("up", "down"):
        series = res[direction]["series"]
        for m in sorted(set(month[mask])):
            sel = mask & (month == m)
            vals = series[sel]
            row = {
                "coin": coin, "month": m, "dir": direction,
                "bars": int(sel.sum()), "full": int(sel.sum() >= MIN_MONTH_BARS),
                "p50": round(float(np.percentile(vals, 50)), 2),
                "p90": round(float(np.percentile(vals, 90)), 2),
                "max": round(float(vals.max()), 2),
            }
            for t in THRESHOLDS:
                row[f"n{t}"] = sum(1 for (i, _, _) in res[direction]["exc"][t] if month[i] == m)
            rows.append(row)
    return rows


def coin_summary(coin, res, rows):
    """Итог по монете и направлению: типичная высота, стабильность по месяцам,
    сколько выходов в месяц, как быстро возвращается к EMA."""
    out = []
    mask, month = res["mask"], res["month"]
    for direction in ("up", "down"):
        full = [r for r in rows if r["dir"] == direction and r["full"]]
        if len(full) < MIN_FULL_MONTHS:
            continue
        full_months = {r["month"] for r in full}
        vals = res[direction]["series"][mask & np.isin(month, list(full_months))]
        p90s = [r["p90"] for r in full]
        s = {
            "coin": coin, "dir": direction, "months": len(full),
            "p50": round(float(np.percentile(vals, 50)), 2),
            "p90": round(float(np.percentile(vals, 90)), 2),
            "p90_month_min": min(p90s), "p90_month_max": max(p90s),
        }
        for t in THRESHOLDS:
            ns = [r[f"n{t}"] for r in full]
            s[f"n{t}_per_month"] = round(float(np.mean(ns)), 1)
            s[f"months_with_n{t}"] = sum(1 for n in ns if n >= 1)
            rets = [ret for (i, _, ret) in res[direction]["exc"][t]
                    if month[i] in full_months and ret is not None]
            s[f"ret_med_days_{t}"] = round(float(np.median(rets)) / BARS_PER_DAY, 1) if rets else None
        out.append(s)
    return out


# ----------------------------------------------------------------------------
# Данные и отчёт
# ----------------------------------------------------------------------------

def _load_df(candle_archive, symbol, start, end):
    c = candle_archive.get_candles(symbol, "4h", since=start - pd.Timedelta(days=WARMUP_DAYS), until=end)
    if not c:
        return None
    df = pd.DataFrame(c)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.drop(columns=["time"]).set_index("timestamp")[["open", "high", "low", "close", "volume"]].astype(float)


def _fmt(v, w, nd=1):
    return f"{'—':>{w}}" if v is None else f"{v:>{w}.{nd}f}"


def _print_table(title, rows):
    print(f"\n=== {title} ===")
    print(f"{'монета':<10}{'мес':>4}{'p50%':>7}{'p90%':>7}{'p90 мес мин-макс':>19}"
          f"{'n10/мес':>9}{'n15/мес':>9}{'n20/мес':>9}{'мес с n15>=1':>14}{'возврат(15%) дн':>17}")
    for s in sorted(rows, key=lambda r: -r["p90"]):
        rng = f"{s['p90_month_min']:.1f}-{s['p90_month_max']:.1f}"
        mw = f"{s['months_with_n15']}/{s['months']}"
        print(f"{s['coin']:<10}{s['months']:>4}{s['p50']:>7.1f}{s['p90']:>7.1f}{rng:>19}"
              f"{s['n10_per_month']:>9.1f}{s['n15_per_month']:>9.1f}{s['n20_per_month']:>9.1f}"
              f"{mw:>14}{_fmt(s['ret_med_days_15'], 17)}")


def main():
    ap = argparse.ArgumentParser(description="Как монета ходит относительно EMA200(4h) за период")
    ap.add_argument("--days", type=int, default=365, help="сколько суток анализировать (по умолчанию 365)")
    ap.add_argument("--coins", default="", help="тикеры через запятую; по умолчанию whitelist бота")
    args = ap.parse_args()

    try:
        from backend.modules.cryptano.utils.bybit import exchange
        import backend.modules.cryptano.levels.candle_archive as candle_archive
        from backend.modules.cryptano.levels.swing_hunter import get_battle_symbols
    except ImportError as e:
        print(f"ОШИБКА импорта ({e}). Запускай из корня master_bot: "
              f"python -m backend.modules.cryptano.levels.ema_study")
        sys.exit(1)

    end = pd.Timestamp.now(tz="UTC")
    start = end - pd.Timedelta(days=args.days)

    symbols = list(get_battle_symbols())
    if args.coins.strip():
        want = {c.strip().upper() for c in args.coins.split(",") if c.strip()}
        symbols = [s for s in symbols if s.split("/")[0].replace(":USDT", "").upper() in want]
    if not symbols:
        print("ОШИБКА: нет монет для анализа.")
        sys.exit(1)

    print(f"📦 {len(symbols)} монет, период {start.date()} .. {end.date()} "
          f"(+{WARMUP_DAYS} суток прогрева EMA). Докачиваю недостающие 4h в архив...")
    failed = {}
    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {symbol}")
        try:
            candle_archive.ensure_range(exchange, symbol, "4h", start - pd.Timedelta(days=WARMUP_DAYS), end)
        except Exception as e:
            failed[symbol] = str(e)
            print(f"   ⚠️ {e}")

    all_months, all_summary, skipped = [], [], []
    for symbol in symbols:
        coin = symbol.split("/")[0].replace(":USDT", "")
        df = _load_df(candle_archive, symbol, start, end)
        res = analyze_coin(df, start) if df is not None else None
        if res is None:
            skipped.append(coin)
            continue
        rows = month_rows(coin, res)
        all_months += rows
        all_summary += coin_summary(coin, res, rows)

    up = [s for s in all_summary if s["dir"] == "up"]
    down = [s for s in all_summary if s["dir"] == "down"]
    _print_table("ВЫШЕ EMA200 (4h): как высоко уходит цена (по high)", up)
    _print_table("НИЖЕ EMA200 (4h): как глубоко уходит цена (по low)", down)

    print("\nКак читать: p50/p90 — типичная и высокая высота над EMA, %. 'p90 мес мин-макс' — "
          "разброс p90 между месяцами: узкий = у монеты стабильный характер, широкий = нельзя "
          "верить своему порогу. n15/мес — сколько раз в месяц цена уходила на 15%+ от EMA. "
          "'мес с n15>=1' — в скольких месяцах это вообще было. 'возврат' — через сколько суток "
          "после начала выхода закрытие вернулось к EMA (медиана).")
    if skipped:
        print(f"\nПропущено (мало истории для EMA200 + {MIN_MONTH_BARS // BARS_PER_DAY} суток): {', '.join(skipped)}")
    if failed:
        print(f"\n⚠️ Не докачались ({len(failed)}): {', '.join(s.split('/')[0] for s in failed)}")

    f_months = os.path.abspath("ema_study_months.csv")
    f_coins = os.path.abspath("ema_study_coins.csv")
    pd.DataFrame(all_months).to_csv(f_months, sep=";", decimal=",", index=False, encoding="utf-8-sig")
    pd.DataFrame(all_summary).to_csv(f_coins, sep=";", decimal=",", index=False, encoding="utf-8-sig")
    print(f"\n💾 По месяцам: {f_months}\n💾 По монетам: {f_coins}")


if __name__ == "__main__":
    main()