"""
Тест: по каким признакам сделки получают глубокую просадку (MAE) — и какие входы можно
отсечь, чтобы убрать просадки и минусы, потеряв как можно меньше хорошего.

Что делает:
  1. Гоняет настоящий симулятор (режим Watcher, как в бою) по каждому периоду из PERIODS
     и собирает все сделки стратегии. Результат кладёт в файл-кэш: повторный запуск
     пересчёта не требует (поставь USE_CACHE = False, чтобы пересчитать).
  2. Сделка «плохая», если её MAE (худшая просадка от входа) <= DD_LIMIT.
  3. По каждому признаку (тип уровня, касания, объём, дистанция до EMA, ширина зоны,
     удаление входа от зоны, час, монета...) показывает, где доля плохих сделок выше.
  4. Перебирает простые правила «не входить, если признак выше/ниже порога» и показывает
     лучшие: сколько плохих отрезали, сколько хороших потеряли, что стало с MAE и суммой %.
     Правило считается надёжным только если оно помогает В КАЖДОМ периоде, а не только в сумме.

Файлы бота не меняются: тест только читает результаты симулятора.

Запуск: просто запустить файл (Run в редакторе или `python test_drawdown_features.py`).
Период(ы), стратегия и порог просадки — в настройках ниже.
"""
import os
import sys
import json
import datetime
from concurrent.futures import ProcessPoolExecutor

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = _THIS_DIR
while not os.path.isdir(os.path.join(_ROOT, "backend")) and os.path.dirname(_ROOT) != _ROOT:
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ======================================================================
# НАСТРОЙКИ
# ======================================================================
STRATEGY = "BOUNCE"                    # "BOUNCE" или "BUYBACK"
PERIODS = [                            # чем больше периодов, тем надёжнее вывод; без уровней за период — пропускается
    ("2026-07-01", "2026-07-31"),
    ("2026-08-01", "2026-08-31"),
    ("2026-09-01", "2026-09-30"),
]
DD_LIMIT = -15.0                       # сделка с MAE не лучше этого значения (в %) считается «плохой»
MAX_REMOVED_SHARE = 0.5                # правило не должно отсекать больше этой доли сделок
MIN_BUCKET = 8                         # корзины меньше этого размера в таблицах признаков не показываем
TOP_RULES = 15
WORKERS = int(os.environ.get("BOUNCE_BULK_WORKERS", "4"))
USE_CACHE = True
CACHE_FILE = os.path.join(_THIS_DIR, f"drawdown_trades_{STRATEGY.lower()}.json")
REPORT_FILE = os.path.join(_THIS_DIR, f"drawdown_report_{STRATEGY.lower()}.txt")
# ======================================================================

import pandas as pd
from backend.modules.cryptano.simulator import simulate_engine

NUM_FEATURES = ["level_reaction_count", "volume_mult", "ema_dist_pct", "rr", "zone_width_pct", "entry_dist_pct", "hour"]
CAT_FEATURES = ["type", "source", "level_type", "method", "weekday", "coin"]


# ----------------------------------------------------------------------
# СБОР СДЕЛОК
# ----------------------------------------------------------------------

def collect_period(start, end):
    coins, _cov = simulate_engine.list_coins_with_levels(start, end)
    if not coins:
        print(f"⚠️ {start}..{end}: нет монет с уровнями (нужен levels_timeline_*.json за период) — пропускаю.")
        return None
    print(f"{start}..{end}: монет {len(coins)}. Докачиваю недостающие свечи (скачанное не качается)...")
    simulate_engine.prefetch_archive(start, end)
    tasks = [(c, start, end, STRATEGY, {}) for c in coins]
    trades, errors = [], {}
    t0 = datetime.datetime.now()
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for coin, payload, err in ex.map(simulate_engine._simulate_one_coin_worker, tasks):
            if err:
                errors[coin] = err
            else:
                trades.extend(payload.get("trades", []))
    print(f"  готово за {(datetime.datetime.now() - t0).total_seconds():.0f} с: сделок {len(trades)}, ошибок по монетам {len(errors)}")
    return trades


def enrich_with_entry_candle(trades, start, end):
    """Дописывает в сделки OHLC свечи входа (из архива симулятора) — нужно для теней.
    Возвращает True, если что-то добавили."""
    need = [t for t in trades if "c_high" not in t]
    if not need:
        return False
    markets = simulate_engine.load_markets_cached(simulate_engine.exchange)
    s, e = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    by_coin = {}
    for t in need:
        by_coin.setdefault(t["coin"], []).append(t)
    for coin, items in by_coin.items():
        try:
            symbol = simulate_engine.resolve_symbol(coin, markets)
            df = simulate_engine._candles_df(symbol, s, e, top_up=False)
        except Exception as ex:
            print(f"  ⚠️ {coin}: свечи для теней не загружены ({ex})")
            continue
        for t in items:
            idx = pd.Timestamp(t["time"], unit="s", tz="UTC")
            if idx in df.index:
                r = df.loc[idx]
                t.update(c_open=float(r["open"]), c_high=float(r["high"]), c_low=float(r["low"]), c_close=float(r["close"]))
    return True


def load_all_trades():
    cache = {}
    if USE_CACHE and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    rows = []
    for start, end in PERIODS:
        key = f"{start}..{end}"
        if key not in cache:
            trades = collect_period(start, end)
            if trades is None:
                continue
            cache[key] = trades
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, default=str)
        if enrich_with_entry_candle(cache[key], *key.split("..")):
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, default=str)
        for t in cache[key]:
            rows.append(dict(t, period=key))
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# ПРИЗНАКИ
# ----------------------------------------------------------------------

def prepare(df):
    df = df[df["status"].isin(["✅", "🕐"]) & df["result_percent"].notna() & df["mae_pct"].notna()].copy()
    long_ = df["type"] == "LONG"
    width = (df["level_max"] - df["level_min"]) / df["level_min"] * 100.0
    df["zone_width_pct"] = width
    # на сколько % вход ушёл от зоны в сторону сделки (LONG — выше верха, SHORT — ниже низа)
    df["entry_dist_pct"] = ((df["entry"] - df["level_max"]) / df["level_max"] * 100.0).where(
        long_, (df["level_min"] - df["entry"]) / df["level_min"] * 100.0)
    ts = pd.to_datetime(df["time"], unit="s", utc=True)
    df["hour"] = ts.dt.hour
    df["weekday"] = ts.dt.day_name().str[:3]
    for c in ("volume_mult", "ema_dist_pct", "level_reaction_count", "rr"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("c_open", "c_high", "c_low", "c_close"):
        if c not in df.columns:
            df[c] = float("nan")
    rng = (df["c_high"] - df["c_low"]).where(lambda x: x > 0)
    top = df[["c_open", "c_close"]].max(axis=1)
    bot = df[["c_open", "c_close"]].min(axis=1)
    df["upper_wick_pct"] = (df["c_high"] - top) / rng * 100.0
    df["lower_wick_pct"] = (bot - df["c_low"]) / rng * 100.0
    df["bad"] = df["mae_pct"] <= DD_LIMIT
    df["level_type"] = df["level_type"].fillna("?").astype(str)
    df["method"] = df["method"].fillna("?").astype(str)
    return df


# ----------------------------------------------------------------------
# ОТЧЁТ
# ----------------------------------------------------------------------

def _row(label, g):
    n = len(g)
    return (f"{str(label)[:28]:<28}{n:>6}{100.0 * g['bad'].mean():>8.1f}{g['mae_pct'].mean():>9.2f}"
            f"{100.0 * (g['status'] == '✅').mean():>7.1f}{g['result_percent'].mean():>9.2f}{g['result_percent'].sum():>10.1f}")


TABLE_HEAD = f"{'группа':<28}{'сделок':>6}{'плохих%':>8}{'MAE ср':>9}{'TP %':>7}{'ср. %':>9}{'сумма %':>10}"


def feature_tables(df):
    out = []
    for f in CAT_FEATURES:
        sub = df[df[f].notna()]
        if sub[f].nunique() < 2:
            continue
        out += [f"— {f}", TABLE_HEAD]
        groups = sorted(sub.groupby(f), key=lambda kv: -kv[1]["bad"].mean())
        shown = 0
        for val, g in groups:
            if len(g) >= MIN_BUCKET and (f != "coin" or shown < 12):
                out.append(_row(val, g))
                shown += 1
        out.append("")
    for f in NUM_FEATURES:
        sub = df[df[f].notna()]
        if sub[f].nunique() < 4:
            continue
        try:
            q = pd.qcut(sub[f], 4, duplicates="drop")
        except ValueError:
            continue
        out += [f"— {f} (по квартилям)", TABLE_HEAD]
        for iv, g in sub.groupby(q, observed=True):
            if len(g) >= MIN_BUCKET:
                out.append(_row(f"{iv.left:.3g} .. {iv.right:.3g}", g))
        out.append("")
    return out


DIST_BINS = [-1e9, -3.0, -1.0, 0.0, 1.0, 2.0, 4.0, 1e9]
DIST_LABELS = ["-3% и глубже (за зоной)", "-3..-1% (внутри/за зоной)", "-1..0% (у края, внутри)",
               "0..1% от края", "1..2% от края", "2..4% от края", "4%+ от края (далеко)"]


def distance_section(df):
    """Сколько сделок было ДАЛЕКО от уровня и чем кончились — отдельно LONG и SHORT.
    Расстояние = на сколько % цена входа ушла от края зоны В СТОРОНУ СДЕЛКИ
    (LONG — выше верха зоны, SHORT — ниже низа зоны). Плюс = вход дальше от зоны,
    минус = вход внутри зоны или за ней."""
    out = []
    for side in ("LONG", "SHORT", None):
        g = df if side is None else df[df["type"] == side]
        if g.empty:
            continue
        out += [f"— {'ВСЕ' if side is None else side}: расстояние входа от края зоны (плюс = дальше от зоны в сторону сделки)", TABLE_HEAD]
        cats = pd.cut(g["entry_dist_pct"], DIST_BINS, labels=DIST_LABELS)
        for lab in DIST_LABELS:
            sub = g[cats == lab]
            if len(sub):
                out.append(_row(lab, sub))
        out.append("")
    return out


def long_above_section(df):
    """LONG, вход выше верха зоны: сколько сделок и какие просадки (просадка = на сколько %
    цена ушла против сделки после входа)."""
    g = df[df["type"] == "LONG"]
    bins = [("0..1%", 0.0, 1.0), ("1..2%", 1.0, 2.0), ("2..4%", 2.0, 4.0), ("4%+", 4.0, 1e9)]
    head = f"{'вход выше верха зоны':<24}{'сделок':>8}{'средняя просадка':>18}{'худшая просадка':>17}"
    out = [head, "-" * len(head)]

    def line(label, sub):
        if len(sub):
            out.append(f"{label:<24}{len(sub):>8}{sub['mae_pct'].mean():>17.2f}%{sub['mae_pct'].min():>16.2f}%")
    for label, lo, hi in bins:
        line(label, g[(g["entry_dist_pct"] > lo) & (g["entry_dist_pct"] <= hi)] if lo > 0 else g[(g["entry_dist_pct"] > 0) & (g["entry_dist_pct"] <= hi)])
    line("ВСЕГО выше зоны", g[g["entry_dist_pct"] > 0])
    line("не выше зоны (внутри/за ней)", g[g["entry_dist_pct"] <= 0])
    line("все LONG", g)
    return out


def volume_section(df):
    """Сколько сделок при каком множителе объёма и какие просадки — LONG, SHORT и все вместе."""
    bins = [("2.5..3", 2.5, 3.0), ("3..4", 3.0, 4.0), ("4..6", 4.0, 6.0), ("6..10", 6.0, 10.0), ("10+", 10.0, 1e9)]
    out = []
    for side in ("LONG", "SHORT", None):
        g = df if side is None else df[df["type"] == side]
        out += [f"— {'ВСЕ' if side is None else side}",
                f"{'множитель объёма':<20}{'сделок':>8}{'средняя просадка':>18}{'худшая просадка':>17}{'ср. результат':>15}"]
        for label, lo, hi in bins:
            sub = g[(g["volume_mult"] >= lo) & (g["volume_mult"] < hi)]
            if len(sub):
                out.append(f"{label:<20}{len(sub):>8}{sub['mae_pct'].mean():>17.2f}%{sub['mae_pct'].min():>16.2f}%{sub['result_percent'].mean():>14.2f}%")
        out.append("")
    return out


def long_levels_section(df):
    """Только LONG: сделки и просадки по типу уровня и по числу касаний уровня."""
    g = df[df["type"] == "LONG"]
    out = []

    def table(data, title, key, order=None, width=60):
        out.extend([f"— {title}", f"{'группа':<{width}}{'сделок':>8}{'средняя просадка':>18}{'худшая просадка':>17}{'ср. результат':>15}"])
        groups = list(data.groupby(key))
        groups = sorted(groups, key=lambda kv: kv[1]["mae_pct"].mean()) if order is None else sorted(groups, key=lambda kv: order(kv[0]))
        for val, sub in groups:
            out.append(f"{str(val)[:width - 1]:<{width}}{len(sub):>8}{sub['mae_pct'].mean():>17.2f}%{sub['mae_pct'].min():>16.2f}%{sub['result_percent'].mean():>14.2f}%")
        out.append("")

    table(g, "LONG по типу уровня (сверху — самые глубокие просадки)", "level_type")
    touches = g.assign(touches=g["level_reaction_count"].fillna(-1).clip(upper=4).astype(int).map(
        lambda v: "нет данных" if v < 0 else ("4 и больше" if v >= 4 else str(v))))
    table(touches, "LONG по числу касаний уровня", "touches", order=lambda v: v, width=24)
    return out


def wick_section(df):
    """LONG: тень свечи входа (в % от размаха свечи) и просадки."""
    g = df[(df["type"] == "LONG") & df["upper_wick_pct"].notna()]
    if g.empty:
        return ["Нет данных о свечах входа (не загрузились свечи из архива)."]
    bins = [("0..10%", 0, 10), ("10..25%", 10, 25), ("25..40%", 25, 40), ("40..60%", 40, 60), ("60%+", 60, 101)]
    out = []
    for title, col in (("ВЕРХНЯЯ тень (отказ вверх)", "upper_wick_pct"), ("НИЖНЯЯ тень (хвост вниз)", "lower_wick_pct")):
        out += [f"— LONG, {title} свечи входа, % от размаха свечи",
                f"{'тень':<14}{'сделок':>8}{'средняя просадка':>18}{'худшая просадка':>17}{'ср. результат':>15}"]
        for label, lo, hi in bins:
            sub = g[(g[col] >= lo) & (g[col] < hi)]
            if len(sub):
                out.append(f"{label:<14}{len(sub):>8}{sub['mae_pct'].mean():>17.2f}%{sub['mae_pct'].min():>16.2f}%{sub['result_percent'].mean():>14.2f}%")
        out.append("")
    return out


def _lift(mask_removed, g):
    """Доля плохих, которых отрезали, минус доля остальных, которых потеряли (по группе g)."""
    bad, good = g["bad"], ~g["bad"]
    if bad.sum() == 0 or good.sum() == 0:
        return None
    return float((mask_removed & bad).sum() / bad.sum() - (mask_removed & good).sum() / good.sum())


def candidate_rules(df):
    rules = []
    for f in NUM_FEATURES:
        sub = df[df[f].notna()]
        if sub[f].nunique() < 4:
            continue
        for p in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
            cut = float(sub[f].quantile(p))
            rules.append((f"{f} <= {cut:.3g}", lambda d, f=f, c=cut: d[f].notna() & (d[f] <= c)))
            rules.append((f"{f} >= {cut:.3g}", lambda d, f=f, c=cut: d[f].notna() & (d[f] >= c)))
    for f in CAT_FEATURES:
        if f == "coin":
            continue
        for val in df[f].dropna().unique():
            rules.append((f"{f} == {val}", lambda d, f=f, v=val: d[f] == v))
    return rules


def rule_report(df):
    total = len(df)
    periods = sorted(df["period"].unique())
    scored = []
    for name, fn in candidate_rules(df):
        removed = fn(df)
        share = removed.mean()
        if share == 0 or share > MAX_REMOVED_SHARE:
            continue
        lifts = {p: _lift(removed[df["period"] == p], df[df["period"] == p]) for p in periods}
        if any(v is None for v in lifts.values()):
            continue
        scored.append((name, removed, lifts, _lift(removed, df)))
    consistent = [s for s in scored if all(v > 0 for v in s[2].values())]
    consistent.sort(key=lambda s: -s[3])

    lines = [f"Всего сделок {total}; плохих (MAE <= {DD_LIMIT:g}%) {int(df['bad'].sum())} ({100.0 * df['bad'].mean():.1f}%); "
             f"средний MAE {df['mae_pct'].mean():.2f}%; сумма {df['result_percent'].sum():.1f}%; "
             f"средний результат {df['result_percent'].mean():.2f}%.",
             f"Правил проверено {len(scored)}, из них помогает В КАЖДОМ периоде: {len(consistent)}.", ""]
    if not consistent:
        lines.append("Ни одно простое правило не помогло стабильно во всех периодах — значит, по этим признакам "
                     "просадку отсечь нельзя (или периодов мало).")
        return lines
    head = (f"{'правило (не входить, если...)':<34}{'отсечено':>9}{'плох.отс':>9}{'хор.потер':>10}"
            f"{'плох% до→после':>16}{'MAE до→после':>15}{'сумма% до→после':>20}")
    lines += [head, "-" * len(head)]
    bad_total = int(df["bad"].sum())
    good_total = total - bad_total
    for name, removed, lifts, lift in consistent[:TOP_RULES]:
        kept = df[~removed]
        rb = int((removed & df["bad"]).sum())
        rg = int((removed & ~df["bad"]).sum())
        lines.append(
            f"{name:<34}{int(removed.sum()):>9}{f'{rb}/{bad_total}':>9}{f'{rg}/{good_total}':>10}"
            f"{f'{100.0 * df.bad.mean():.1f}→{100.0 * kept.bad.mean():.1f}':>16}"
            f"{f'{df.mae_pct.mean():.1f}→{kept.mae_pct.mean():.1f}':>15}"
            f"{f'{df.result_percent.sum():.0f}→{kept.result_percent.sum():.0f}':>20}")
    lines += ["", "По периодам для лучших правил (выигрыш = доля отсечённых плохих минус доля потерянных хороших):"]
    for name, removed, lifts, lift in consistent[:5]:
        lines.append(f"  {name}: " + ", ".join(f"{p}: {v:+.2f}" for p, v in lifts.items()))
    return lines


def main():
    df = load_all_trades()
    if df.empty:
        print("❌ Сделок нет ни в одном периоде.")
        return
    df = prepare(df)
    if df.empty:
        print("❌ Нет закрытых сделок с посчитанным исходом.")
        return
    lines = [f"Стратегия {STRATEGY}, периоды: " + ", ".join(sorted(df['period'].unique())), ""]
    lines += ["ПО ПЕРИОДАМ", TABLE_HEAD] + [_row(p, g) for p, g in df.groupby("period")] + [""]
    lines += ["LONG: ВХОД ВЫШЕ УРОВНЯ — СДЕЛКИ И ПРОСАДКИ", ""] + long_above_section(df) + [""]
    lines += ["LONG: ТЕНЬ СВЕЧИ ВХОДА И ПРОСАДКИ", ""] + wick_section(df)
    lines += ["LONG: УРОВНИ — ТИП И КАСАНИЯ", ""] + long_levels_section(df)
    lines += ["ОБЪЁМ: СДЕЛКИ И ПРОСАДКИ ПО МНОЖИТЕЛЮ ОБЪЁМА", ""] + volume_section(df)
    lines += ["ДАЛЕКО ОТ УРОВНЯ (расстояние входа от края зоны)", ""] + distance_section(df)
    lines += ["ПО ПРИЗНАКАМ (группы отсортированы по доле плохих сделок)", ""] + feature_tables(df)
    lines += ["ПРАВИЛА ОТСЕЧЕНИЯ", ""] + rule_report(df)
    lines += ["", "Как читать:",
              " • «плохих%» — доля сделок с просадкой хуже порога. Ищем группы, где она заметно выше общей.",
              " • В правилах хорошо: отсечено много плохих, потеряно мало хороших, MAE и «плох%» после заметно лучше,",
              "   сумма % не падает. Выигрыш должен быть плюсовым в каждом периоде — иначе это случайность.",
              " • Если лучшие правила отсекают почти поровну плохих и хороших — признак просадку не предсказывает.",
              " • Выигрыш около нуля (меньше ~0.1) — шум: при сотнях проверенных правил что-то всегда выглядит полезным.",
              "   Настоящий признак даёт заметный плюс в каждом периоде и держится при смене стратегии/периода."]
    report = "\n".join(lines)
    print("\n" + report)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\nОтчёт сохранён: {REPORT_FILE}")


if __name__ == "__main__":
    main()