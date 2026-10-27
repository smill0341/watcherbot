"""
Тест: как растёт банк при разном размере сделки и разном числе одновременных сделок — по месяцам.

Входы — настоящие: обычный прогон симулятора в режиме Watcher (как в бою), правила входа не меняются.
Правило торговли задано заранее и одинаково для всех месяцев (ничего не подбирается под месяц):
  • банк BANK, реинвест: размер сделки = SIZE % от ТЕКУЩЕГО банка;
  • одновременно открыто не больше K сделок; если K сделок уже открыто, новый сигнал пропускается
    (берётся тот, что пришёл раньше — без выбора);
  • выход фиксирован для всей таблицы: «как сейчас» или TP % / удержание дней (EXITS);
  • комиссий и проскальзывания нет, стопа в симуляторе нет.

Для каждой стратегии, набора зон входа и выхода — таблица «размер сделки × K».
В ячейке — во сколько раз вырос банк за месяц (x) отдельно по каждому периоду из PERIODS, через «/».
Ячейка «-» — размер * K больше 100% банка (денег на столько сделок нет).

Файлы бота не меняются. Запуск: просто запустить файл. Настройки ниже. Отчёт — в файл рядом.
"""
import os
import sys
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
STRATEGIES = ["BOUNCE", "BUYBACK"]
PERIODS = [("2026-09-01", "2026-09-30"), ("2026-08-01", "2026-08-31"), ("2026-07-01", "2026-07-31")]
DIRECTION = "LONG"                          # "LONG" или "SHORT"
EXITS = [(5, 1), (3, 2), (7, 3)]            # (TP %, удержание в днях); плюс всегда «как сейчас»
SIZES_PCT = [100, 50, 33, 25, 20, 15, 10, 5]  # размер сделки, % от текущего банка
K_LIST = [1, 2, 3, 4, 5, 10]                # максимум одновременно открытых сделок
ZONE_SETS = [("выше зоны", {"above"}), ("ниже + выше зоны", {"below", "above"}),
             ("все три зоны", {"below", "inside", "above"})]
EMA_MAX = None                              # None = без отсечки по EMA; число = только края зон не выше EMA на столько % (0 = только ниже EMA)
BANK = 5000
WORKERS = int(os.environ.get("BOUNCE_BULK_WORKERS", "4"))
COINS = None                                # None = все монеты периода
REPORT_FILE = os.path.join(_THIS_DIR, "bank_grid_report.txt")
# ======================================================================

from backend.modules.cryptano.simulator import simulate_engine

_ORIG_OUTCOME = simulate_engine.compute_trade_outcome
_CAPTURED = []


def _outcome_with_variants(entry_ts, entry_price, target_price, trade_type, df_full, max_hold_days):
    base = _ORIG_OUTCOME(entry_ts, entry_price, target_price, trade_type, df_full, max_hold_days)
    variants = {}
    for tp, hold in EXITS:
        tgt = entry_price * (1 + tp / 100.0) if trade_type == "LONG" else entry_price * (1 - tp / 100.0)
        o = _ORIG_OUTCOME(entry_ts, entry_price, tgt, trade_type, df_full, hold)
        variants[(tp, hold)] = (o["status"], o["result_percent"], o["closed_at"])
    _CAPTURED.append(variants)
    return base


def _ts(iso):
    t = datetime.datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.timestamp()


def _zone(t):
    lo, hi, e = t.get("level_min"), t.get("level_max"), t.get("entry")
    if lo is None or hi is None or not e:
        return None
    if DIRECTION == "LONG":
        return "above" if e > hi else ("below" if e < lo else "inside")
    return "above" if e < lo else ("below" if e > hi else "inside")


def _worker(task):
    coin, start, end, strategy = task
    try:
        _CAPTURED.clear()
        simulate_engine.compute_trade_outcome = _outcome_with_variants
        try:
            res = simulate_engine.run_simulation(coin, start, end, strategy=strategy, top_up=False, save_last_run=False)
        finally:
            simulate_engine.compute_trade_outcome = _ORIG_OUTCOME
        trades = res.get("trades", [])
        if len(trades) != len(_CAPTURED):
            return coin, [], f"не совпало число сделок ({len(trades)}) и выходов ({len(_CAPTURED)})"
        out = []
        for t, var in zip(trades, _CAPTURED):
            if t.get("type") != DIRECTION:
                continue
            zone = _zone(t)
            if zone is None:
                continue
            v = {"current": (t.get("status"), t.get("result_percent"), t.get("closed_at"))}
            v.update(var)
            out.append({"coin": coin, "time": t["time"], "zone": zone, "ema": t.get("ema_dist_pct"), "v": v})
        return coin, out, None
    except Exception as e:
        return coin, [], str(e)


def run_strategy(strategy, coins, start, end):
    tasks = [(c, start, end, strategy) for c in coins]
    rows, errors = [], {}
    if WORKERS <= 1:
        results = map(_worker, tasks)
    else:
        executor = ProcessPoolExecutor(max_workers=WORKERS)
        results = executor.map(_worker, tasks)
    for coin, out, err in results:
        if err:
            errors[coin] = err
        rows.extend(out)
    if WORKERS > 1:
        executor.shutdown()
    return rows, errors


# ----------------------------------------------------------------------
# БАНК
# ----------------------------------------------------------------------

def _closed(rows, key):
    """[(открыта_unix, закрыта_unix, результат_%)] — только закрытые (✅/🕐), по времени открытия."""
    out = []
    for r in rows:
        status, pct, closed_at = r["v"][key]
        if status in ("✅", "🕐") and pct is not None and closed_at:
            out.append((r["time"], max(_ts(closed_at), r["time"]), pct))
    return sorted(out)


def bank_x(trades, size_pct, k):
    """Банк BANK, реинвест. Сделка = size_pct % текущего банка; не больше k открытых одновременно,
    иначе сигнал пропускается. Возвращает во сколько раз вырос банк."""
    f = size_pct / 100.0
    cash, opened = float(BANK), []
    for o, c, p in trades:
        still = []
        for cc, m, pp in sorted(opened):
            if cc <= o:
                cash += m * (1 + pp / 100.0)
            else:
                still.append((cc, m, pp))
        opened = still
        if len(opened) >= k:
            continue
        equity = cash + sum(m for _, m, _ in opened)
        m = min(f * equity, cash)
        if m <= 0:
            continue
        cash -= m
        opened.append((c, m, p))
    for _, m, p in opened:
        cash += m * (1 + p / 100.0)
    return cash / BANK


# ----------------------------------------------------------------------
# ОТЧЁТ
# ----------------------------------------------------------------------

def _select(rows, zones):
    out = []
    for r in rows:
        if r["zone"] not in zones:
            continue
        if EMA_MAX is not None and (r.get("ema") is None or r["ema"] > EMA_MAX):
            continue
        out.append(r)
    return out


def build_section(strategy, by_period):
    """by_period: [(метка периода, rows)] в порядке PERIODS."""
    ema_txt = "без отсечки по EMA" if EMA_MAX is None else f"край зоны не выше {EMA_MAX:g}% от EMA200(4h)"
    L = [f"########## {strategy}: {DIRECTION}, {ema_txt}, банк {BANK}$ ##########",
         "Периоды (в ячейке через «/» в этом порядке): " + "  ".join(f"{i + 1}) {lab}" for i, (lab, _) in enumerate(by_period))]
    exits = [("current", "как сейчас")] + [((tp, h), f"TP {tp}% / {h:g} дн") for tp, h in EXITS]
    w = 8 * len(by_period) + 2
    for zname, zones in ZONE_SETS:
        sel = [(lab, _select(rows, zones)) for lab, rows in by_period]
        L += ["", f"=== {zname}: сделок по периодам " + " / ".join(str(len(s)) for _, s in sel) + " ==="]
        for key, kname in exits:
            trades = [(lab, _closed(s, key)) for lab, s in sel]
            L += ["", f"{kname}: закрытых сделок " + " / ".join(str(len(t)) for _, t in trades) +
                  "  |  банк, x за месяц по периодам"]
            corner = "размер / макс. сделок"
            head = f"{corner:<22}" + "".join(f"{('K=%d' % k):>{w}}" for k in K_LIST)
            L += [head, "-" * len(head)]
            for size in SIZES_PCT:
                line = f"{('%d%% банка' % size):<22}"
                for k in K_LIST:
                    if size * k > 100:
                        line += f"{'-':>{w}}"
                        continue
                    cell = "/".join(("%.2f" % bank_x(t, size, k)) if t else "-" for _, t in trades)
                    line += f"{cell:>{w}}"
                L.append(line)
    return "\n".join(L)


def main():
    sections, errs = [], {}
    data = {st: [] for st in STRATEGIES}
    for start, end in PERIODS:
        lab = f"{start}..{end}"
        coins, _cov = simulate_engine.list_coins_with_levels(start, end)
        if COINS is not None:
            coins = [c for c in coins if c in COINS]
        if not coins:
            print(f"❌ {lab}: нет монет с уровнями — период пропущен.")
            for st in STRATEGIES:
                data[st].append((lab, []))
            continue
        print(f"{lab}: монет {len(coins)}. Докачиваю недостающие свечи...")
        simulate_engine.prefetch_archive(start, end)
        for st in STRATEGIES:
            t0 = datetime.datetime.now()
            rows, errors = run_strategy(st, coins, start, end)
            print(f"[{st} {lab}] готово за {(datetime.datetime.now() - t0).total_seconds():.0f} с: "
                  f"{DIRECTION}-входов {len(rows)}, ошибок {len(errors)}")
            data[st].append((lab, rows))
            errs.update({f"{st} {lab}:{c}": e for c, e in errors.items()})
    for st in STRATEGIES:
        sections.append(build_section(st, data[st]))
    report = "\n\n".join(sections)
    report += ("\n\nКак читать:\n"
               " • ячейка — во сколько раз вырос банк за период (x1.00 = без изменений), периоды через «/» в порядке из заголовка.\n"
               " • размер сделки — % от текущего банка (реинвест); K — максимум одновременно открытых; лишние сигналы пропускаются, берётся пришедший раньше.\n"
               " • «-» — размер * K больше 100% банка. Правило одно для всех периодов, под период ничего не подбирается.\n"
               " • зоны входа: ниже зоны — вход глубже уровня; внутри — между краями; выше — вход выше верха зоны (лонг).\n"
               " • сделки ⏳ (не закрытые к концу данных) не считаются; комиссий и стопа нет.")
    if errs:
        report += "\n\nОшибки: " + ", ".join(f"{c}: {e}" for c, e in sorted(errs.items())[:20])
    print("\n" + report)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\nОтчёт сохранён: {REPORT_FILE}")


if __name__ == "__main__":
    main()