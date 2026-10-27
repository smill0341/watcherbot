"""
Тест: какие сделки теряет ADX-фильтр и почему.

Что делает: для каждой монеты гоняет настоящий симулятор (simulate_engine.run_simulation, режим
Watcher — как в бою) два раза:
  • база — ADX-фильтр выключен (MIN_ADX = 0);
  • с фильтром — MIN_ADX из настройки ниже (30).
Сделки сопоставляются по монете и уровню. Для каждой сделки, которая в базе была, а с фильтром
пропала («потеряна»), по записи жизни вотчера в прогоне с фильтром выясняется причина:
  • вотчер закрылся (цена ушла глубоко вниз / убежала от зоны) — вход так и не случился;
  • был новый прокол / касание зоны после пропуска, но ADX снова был ниже порога;
  • вотчер просто дожил до конца периода и ждал, ADX так и не поднялся.
Файлы бота не меняются (запись ADX по свечам — в подклассе внутри этого файла).

Запуск: просто запустить файл (Run в редакторе или `python test_adx_lost.py`).
Период, стратегии и порог — в настройках ниже. Отчёт печатается и пишется в файл рядом.
"""
import os
import sys
import datetime
import importlib
from collections import Counter, defaultdict
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
STRATEGIES = ["BOUNCE", "BUYBACK"]    # какие стратегии проверить, по очереди
START = "2026-08-01"
END = "2026-09-30"
MIN_ADX = 30                          # порог ADX в прогоне «с фильтром»; база идёт с выключенным
DIRECTIONS = ("LONG",)                # какие сделки считать: ("LONG",), ("SHORT",) или ("LONG", "SHORT")
WORKERS = int(os.environ.get("BOUNCE_BULK_WORKERS", "4"))
COINS = None                          # None = все монеты периода; или, например, {"BTC", "ETH"}
TOP_COINS = 15                        # сколько монет показать в таблице «по монетам»
LOST_LIST_LIMIT = 40                  # сколько потерянных сделок показать списком
REPORT_FILE = os.path.join(_THIS_DIR, "adx_lost_report.txt")
# ======================================================================

from backend.modules.cryptano.simulator import simulate_engine

_STRATEGY_MODULES = {
    "BUYBACK": ("bounce_manager_buyback", "bounce_watcher_buyback"),
    "BOUNCE": ("bounce_manager", "bounce_watcher"),
}
_CLASSES = {}      # strategy -> (manager_module, BaseWatcher, LogWatcher)
_BY_LEVEL = {}     # level_id -> вотчер последнего прогона «с фильтром» (в этом процессе)
_ORIG_EPISODE = simulate_engine._episode_bounce


def _episode_capture(watcher, level_id, coin, strategy="BOUNCE"):
    _BY_LEVEL[level_id] = watcher
    return _ORIG_EPISODE(watcher, level_id, coin, strategy)



def _classes(strategy):
    if strategy not in _CLASSES:
        mgr_name, w_name = _STRATEGY_MODULES[strategy]
        mgr = importlib.import_module(f"backend.modules.cryptano.strategy.{mgr_name}")
        base = importlib.import_module(f"backend.modules.cryptano.strategy.{w_name}").BounceWatcher

        class LogWatcher(base):
            """Боевой вотчер + запись ADX по каждой свече своей жизни (логику не меняет)."""

            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._adx_log = {}

            def update(self, c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels,
                       level_score=0, candle_time=None, vol_90=0.0, is_focus=True, c_atr=0.0,
                       baseline_mean=0.0, c_adx=None):
                if self.state not in ("TRIGGERED", "DEAD") and candle_time is not None:
                    a = None if (c_adx is None or c_adx != c_adx) else float(c_adx)
                    self._adx_log[int(candle_time.timestamp())] = a
                return super().update(c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels,
                                      level_score, candle_time, vol_90, is_focus, c_atr, baseline_mean, c_adx)

        _CLASSES[strategy] = (mgr, base, LogWatcher)
    return _CLASSES[strategy]


def _info(w):
    return {
        "state": getattr(w, "state", None),
        "history_log": getattr(w, "history_log", "") or "",
        "adx_log": sorted(w._adx_log.items()),
        "events": [(e.get("time"), e.get("type")) for e in getattr(w, "event_log", [])],
    }


def _worker(task):
    """Одна монета: база и прогон с фильтром. Модульного уровня — на Windows процессы стартуют заново."""
    coin, start, end, strategy = task
    try:
        mgr, base, logw = _classes(strategy)
        base.CONFIG["MIN_ADX"] = 0
        setattr(mgr, "BounceWatcher", base)
        r0 = simulate_engine.run_simulation(coin, start, end, strategy=strategy, top_up=False, save_last_run=False)
        base.CONFIG["MIN_ADX"] = MIN_ADX
        setattr(mgr, "BounceWatcher", logw)
        _BY_LEVEL.clear()
        simulate_engine._episode_bounce = _episode_capture
        try:
            r1 = simulate_engine.run_simulation(coin, start, end, strategy=strategy, top_up=False, save_last_run=False)
        finally:
            simulate_engine._episode_bounce = _ORIG_EPISODE
        infos = {lid: _info(w) for lid, w in _BY_LEVEL.items()}
        _BY_LEVEL.clear()
        keep = lambda ts: [t for t in ts if t.get("type") in DIRECTIONS]
        return coin, keep(r0.get("trades", [])), keep(r1.get("trades", [])), infos, None
    except Exception as e:
        return coin, [], [], {}, str(e)


def run_strategy(strategy, coins, start, end, workers):
    tasks = [(c, start, end, strategy) for c in coins]
    base, flt, infos, errors = [], [], {}, {}
    if workers <= 1:
        results = map(_worker, tasks)
    else:
        executor = ProcessPoolExecutor(max_workers=workers)
        results = executor.map(_worker, tasks)
    for coin, t0, t1, inf, err in results:
        if err:
            errors[coin] = err
        base.extend(t0); flt.extend(t1); infos[coin] = inf
    if workers > 1:
        executor.shutdown()
    return base, flt, infos, errors


# ----------------------------------------------------------------------
# СОПОСТАВЛЕНИЕ И ПРИЧИНЫ
# ----------------------------------------------------------------------

def compare(base_trades, var_trades):
    """Те же время/вход — без изменений; тот же уровень, но позже — сдвинулась; в базе была,
    с фильтром нет — потеряна; с фильтром есть, в базе не было — новая."""
    def key(t):
        return (t["coin"], t.get("level_id"))

    base_by_key = {}
    for t in sorted(base_trades, key=lambda x: x["time"]):
        base_by_key.setdefault(key(t), []).append(t)
    used = set()
    same, shifted, new = [], [], []
    for v in sorted(var_trades, key=lambda x: x["time"]):
        cands = base_by_key.get(key(v), [])
        exact = next((b for b in cands if id(b) not in used and b["time"] == v["time"]), None)
        if exact is not None:
            used.add(id(exact)); same.append(v); continue
        earlier = next((b for b in cands if id(b) not in used and b["time"] < v["time"]), None)
        if earlier is not None:
            used.add(id(earlier)); shifted.append((earlier, v)); continue
        new.append(v)
    lost = [b for b in base_trades if id(b) not in used]
    return same, shifted, lost, new


def _find_info(trade, infos):
    return infos.get(trade.get("level_id"))


R_DEAD_DEEP = "умер: цена ушла глубоко за зону (>3 ATR)"
R_DEAD_NEIGHBOR = "умер: цена прошла сквозь соседний уровень, этот остался позади"
R_DEAD_PRIORITY = "умер: приоритет отдан другому уровню"
R_DEAD_BORN = "умер: уровень появился уже пробитым"
R_DEAD_OTHER = "умер: другая причина (см. текст)"
R_NEW_PIERCE = "жив, был новый прокол/касание после пропуска, ADX снова ниже порога"
R_ADX_HIGH = "жив, ADX вырос выше порога, но подходящей свечи входа не было"
R_WAITED = "жив до конца периода, ADX так и не дотянул"
R_NO_INFO = "вотчер не найден в прогоне с фильтром"
R_ENTERED = "вошёл на другой свече (не сопоставилось)"


def explain(trade, infos):
    """Возвращает (причина, ADX на входе базы, макс. ADX после, новых проколов после пропуска, итог вотчера)."""
    inf = _find_info(trade, infos)
    if inf is None:
        return R_NO_INFO, None, None, 0, "?", None, ""
    T = trade["time"]
    log = inf["adx_log"]
    adx_T = None
    for t, a in log:
        if t <= T:
            adx_T = a
        else:
            break
    after_adx = [a for t, a in log if t > T and a is not None]
    max_after = max(after_adx) if after_adx else None
    ev_after = [typ for t, typ in inf["events"] if t is not None and t > T]
    pierces = sum(1 for typ in ev_after if typ.startswith("SWEEP") or typ == "ZONE_TOUCH")
    state = inf["state"]
    if state == "TRIGGERED":
        reason = R_ENTERED
    elif state == "DEAD":
        hl = inf["history_log"]
        if "глубоко" in hl:
            reason = R_DEAD_DEEP
        elif "соседней зоны" in hl or "нижнюю зону" in hl or "соседн" in hl:
            reason = R_DEAD_NEIGHBOR
        elif "Приоритет" in hl:
            reason = R_DEAD_PRIORITY
        elif "УЖЕ пробит" in hl:
            reason = R_DEAD_BORN
        else:
            reason = R_DEAD_OTHER
    elif pierces > 0:
        reason = R_NEW_PIERCE
    elif max_after is not None and max_after >= MIN_ADX:
        reason = R_ADX_HIGH
    else:
        reason = R_WAITED
    life = sum(1 for t, a in log if t > T)
    return reason, adx_T, max_after, pierces, state, life, inf["history_log"]


# ----------------------------------------------------------------------
# ОТЧЁТ
# ----------------------------------------------------------------------

def _f(v, spec="{:.2f}"):
    return "—" if v is None else spec.format(v)


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _stats(trades):
    closed = [t for t in trades if t["status"] in ("✅", "🕐") and t.get("result_percent") is not None]
    tp = [t for t in closed if t["status"] == "✅"]
    mae = [t["mae_pct"] for t in trades if t.get("mae_pct") is not None]
    return {"n": len(trades), "closed": len(closed),
            "tp_pct": (100.0 * len(tp) / len(closed)) if closed else None,
            "avg": _avg([t["result_percent"] for t in closed]),
            "sum": sum(t["result_percent"] for t in closed),
            "mae_avg": _avg(mae), "mae_worst": min(mae) if mae else None}


def _row(name, s, w=34):
    return (f"{name:<{w}}{s['n']:>8}{s['closed']:>9}{_f(s['tp_pct'], '{:.1f}'):>8}{_f(s['avg']):>9}"
            f"{_f(s['sum'], '{:.1f}'):>10}{_f(s['mae_avg']):>14}{_f(s['mae_worst']):>16}")


def _head(w=34):
    h = f"{'':<{w}}{'сделок':>8}{'закрыто':>9}{'TP %':>8}{'средн %':>9}{'сумма %':>10}{'просадка ср.':>14}{'просадка худш.':>16}"
    return [h, "-" * len(h)]


def _ts(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%m-%d %H:%M")


def build_section(strategy, base, flt, infos_by_coin, start, end, n_coins):
    same, shifted, lost, new = compare(base, flt)
    lines = [f"########## {strategy}: период {start} .. {end}, монет {n_coins} ##########",
             f"Сделки: {'/'.join(DIRECTIONS)}. База — без ADX-фильтра; с фильтром — ADX >= {MIN_ADX}. Исход как в симуляторе: TP либо дедлайн; "
             "сделки ⏳ в проценты не входят.", ""]
    lines += _head()
    lines += [_row("база (без фильтра)", _stats(base)), _row(f"с фильтром ADX >= {MIN_ADX}", _stats(flt)), ""]
    lines.append(f"Без изменений: {len(same)} | сдвинулись на более позднюю свечу: {len(shifted)} | "
                 f"потеряны: {len(lost)} | новые (в базе не было): {len(new)}")
    lines += _head()
    lines += [_row("потерянные (как в базе)", _stats(lost))]
    if shifted:
        lines.append(_row("сдвинувшиеся — в базе", _stats([b for b, v in shifted])))
        lines.append(_row("сдвинувшиеся — с фильтром", _stats([v for b, v in shifted])))
        delay = _avg([(v["time"] - b["time"]) / 900.0 for b, v in shifted])
        lines.append(f"  (сдвинувшиеся входят в среднем на {delay:.0f} свечей 15м позже)")
    if new:
        lines.append(_row("новые", _stats(new)))
    if not lost:
        lines += ["", "Потерянных сделок нет."]
        return "\n".join(lines)

    expl = [(t, explain(t, infos_by_coin.get(t["coin"], []))) for t in lost]

    # --- причины
    lines += ["", f"--- Почему потеряны ({len(lost)}) ---"]
    by_reason = defaultdict(list)
    for t, e in expl:
        by_reason[e[0]].append(t)
    h = f"{'причина':<70}{'сделок':>8}{'доля %':>8}{'TP % в базе':>13}{'средн % в базе':>16}{'прожил после, свечей (медиана)':>34}"
    lines += [h, "-" * len(h)]
    for r, ts in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        s = _stats(ts)
        lives = sorted(e[5] for t, e in expl if e[0] == r and e[5] is not None)
        med = lives[len(lives) // 2] if lives else None
        lines.append(f"{r:<70}{len(ts):>8}{100.0 * len(ts) / len(lost):>8.0f}{_f(s['tp_pct'], '{:.0f}'):>13}{_f(s['avg']):>16}{_f(med, '{:.0f}'):>34}")
    others = Counter(e[6][:90] for t, e in expl if e[0] == R_DEAD_OTHER)
    if others:
        lines.append("Тексты «другой причины»:")
        for txt, n in others.most_common(8):
            lines.append(f"  {n:>4} × {txt}")

    # --- по монетам
    base_by_coin = Counter(t["coin"] for t in base)
    lost_by_coin = Counter(t["coin"] for t in lost)
    lines += ["", f"--- По монетам (топ {TOP_COINS} по числу потерянных) ---"]
    h = f"{'монета':<12}{'потеряно':>10}{'было в базе':>13}{'доля от всех потерянных %':>28}"
    lines += [h, "-" * len(h)]
    for c, n in lost_by_coin.most_common(TOP_COINS):
        lines.append(f"{c:<12}{n:>10}{base_by_coin[c]:>13}{100.0 * n / len(lost):>28.0f}")
    top3 = sum(n for _, n in lost_by_coin.most_common(3))
    lines.append(f"Монет с потерями: {len(lost_by_coin)} из {len(base_by_coin)}; три самые «потерявшие» дают {100.0 * top3 / len(lost):.0f}% потерь.")

    # --- ADX на входе базы
    lines += ["", "--- ADX на свече входа базовой сделки (у потерянных) ---"]
    buckets = [(0, 10), (10, 15), (15, 20), (20, 25), (25, MIN_ADX)]
    adxs = [e[1] for t, e in expl]
    nodata = sum(1 for a in adxs if a is None)
    for lo, hi in buckets:
        if hi <= lo:
            continue
        lines.append(f"  ADX {lo:>2}–{hi:<2}: {sum(1 for a in adxs if a is not None and lo <= a < hi)}")
    if nodata:
        lines.append(f"  нет данных ADX: {nodata}")
    mx = [e[2] for t, e in expl if e[2] is not None]
    if mx:
        lines.append(f"  максимальный ADX за остаток жизни вотчера после пропуска (среднее по потерянным): {sum(mx) / len(mx):.1f}")

    # --- список
    lines += ["", f"--- Потерянные сделки (первые {LOST_LIST_LIMIT} по времени) ---"]
    h = (f"{'монета':<10}{'тип':<7}{'время входа':<13}{'вход':>12}{'ADX вх.':>9}{'макс ADX после':>16}"
         f"{'проколов после':>16}{'жил после':>11}{'рез. %':>8}{'просадка':>10}  причина")
    lines += [h, "-" * len(h)]
    for t, e in sorted(expl, key=lambda p: p[0]["time"])[:LOST_LIST_LIMIT]:
        lines.append(f"{t['coin']:<10}{t['type']:<7}{_ts(t['time']):<13}{t['entry']:>12.6g}{_f(e[1], '{:.1f}'):>9}"
                     f"{_f(e[2], '{:.1f}'):>16}{e[3]:>16}{_f(e[5], '{:.0f}'):>11}{_f(t.get('result_percent')):>8}{_f(t.get('mae_pct')):>10}  {e[0]}")
    return "\n".join(lines)


def main():
    coins, _cov = simulate_engine.list_coins_with_levels(START, END)
    if COINS is not None:
        coins = [c for c in coins if c in COINS]
    if not coins:
        print("❌ За период нет монет с уровнями (нужен levels_timeline_*.json за эти даты).")
        return
    print(f"Монет: {len(coins)}. Докачиваю недостающие свечи в архив (уже скачанное не качается)...")
    simulate_engine.prefetch_archive(START, END)

    sections, all_errors = [], {}
    for st in STRATEGIES:
        t0 = datetime.datetime.now()
        base, flt, infos, errors = run_strategy(st, coins, START, END, WORKERS)
        secs = (datetime.datetime.now() - t0).total_seconds()
        print(f"[{st}] готово за {secs:.0f} с: сделок в базе {len(base)}, с фильтром {len(flt)}, ошибок по монетам {len(errors)}")
        sections.append(build_section(st, base, flt, infos, START, END, len(coins)))
        all_errors.update({f"{st}:{c}": e for c, e in errors.items()})

    report = "\n\n".join(sections)
    report += ("\n\nКак читать:\n"
               " • «Потеряны» — в базе сделка была, а с фильтром вход так и не случился ни на этой, ни на следующих свечах.\n"
               " • «Сдвинулись» — вход нашёлся позже, на свече с ADX >= порога.\n"
               " • Если у потерянных в базе TP% высокий и средний результат хороший — фильтр режет хорошие сделки.\n"
               " • Если потери сидят в 2–3 монетах или в одной причине — это видно в таблицах «По монетам» и «Почему потеряны».")
    if all_errors:
        report += "\n\nОшибки по монетам (в сводку не вошли): " + ", ".join(f"{c}: {e}" for c, e in sorted(all_errors.items())[:20])
    print("\n" + report)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\nОтчёт сохранён: {REPORT_FILE}")


if __name__ == "__main__":
    main()