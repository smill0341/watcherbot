"""
Тест: LONG не входит на свече с большой нижней тенью — ищет вход дальше (по тому же паттерну).

Что делает: гоняет настоящий симулятор (simulate_engine.run_simulation, режим Watcher — как в
бою) по всем монетам периода несколько раз:
  • базовый прогон — боевой вотчер без изменений (с ADX-фильтром из настройки MIN_ADX ниже);
  • по прогону на каждый порог из WICK_VARIANTS — тот же вотчер, но если у свечи, на которой он
    вошёл бы в LONG, нижняя тень >= порога (в % от размаха свечи) — вход на ней пропускается,
    вотчер остаётся жить и ищет следующую подходящую свечу ровно по тем же правилам.
Правило живёт только в этом файле (подкласс WickSkipWatcher), файлы бота не меняются.
Затронут только LONG; SHORT не трогаем и в статистике не показываем.

Запуск: просто запустить файл (Run в редакторе или `python test_wick_skip.py`).
Период, стратегия и пороги — в настройках ниже. Нужен готовый архив свечей и файл уровней за
период (как для обычного bulk). Отчёт печатается и пишется в файл рядом.
"""
import os
import sys
import datetime
from concurrent.futures import ProcessPoolExecutor

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# Корень проекта (папка, в которой лежит backend/) — чтобы файл запускался прямо
# кнопкой Run / двойным кликом, без `python -m` и без правки путей.
_ROOT = _THIS_DIR
while not os.path.isdir(os.path.join(_ROOT, "backend")) and os.path.dirname(_ROOT) != _ROOT:
    _ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ======================================================================
# НАСТРОЙКИ
# ======================================================================
STRATEGY = "BUYBACK"                  # "BUYBACK" или "BOUNCE"
START = "2026-08-01"
END = "2026-09-30"
WICK_VARIANTS = [40, 50]              # порог нижней тени в % от размаха свечи входа; базовый прогон идёт всегда
MIN_ADX = 30                          # ADX-фильтр на время теста (0 = выключен), одинаковый во всех прогонах
WORKERS = int(os.environ.get("BOUNCE_BULK_WORKERS", "4"))
COINS = None                          # None = все монеты периода; или, например, {"BTC", "ETH"}
SHIFTED_LIST_LIMIT = 40               # сколько сдвинувшихся сделок показать списком
REPORT_FILE = os.path.join(_THIS_DIR, "wick_skip_report.txt")
# ======================================================================

import importlib

from backend.modules.cryptano.simulator import simulate_engine

# Менеджер и вотчер выбранной стратегии (импорт по имени, чтобы базовый класс был один на запуск).
_STRATEGY_MODULES = {
    "BUYBACK": ("bounce_manager_buyback", "bounce_watcher_buyback"),
    "BOUNCE": ("bounce_manager", "bounce_watcher"),
}
_manager_module = importlib.import_module(f"backend.modules.cryptano.strategy.{_STRATEGY_MODULES[STRATEGY][0]}")
_BaseWatcher = importlib.import_module(f"backend.modules.cryptano.strategy.{_STRATEGY_MODULES[STRATEGY][1]}").BounceWatcher


class WickSkipWatcher(_BaseWatcher):
    """Боевой вотчер + пропуск входа в LONG, если у свечи входа нижняя тень >= WICK_PCT.

    Вотчер перед каждым входом спрашивает _adx_ok() (общая проверка «можно ли входить на этой
    свече»; если нет — пропуск, поиск идёт дальше). Тут к ней добавляется проверка тени."""
    WICK_PCT = None
    _cur_candle = None

    def _update_impl(self, c_open, c_high, c_low, c_close, *args, **kwargs):
        self._cur_candle = (float(c_open), float(c_high), float(c_low), float(c_close))
        return super()._update_impl(c_open, c_high, c_low, c_close, *args, **kwargs)

    def _lower_wick_pct(self):
        if self._cur_candle is None:
            return 0.0
        o, h, l, c = self._cur_candle
        rng = h - l
        return (min(o, c) - l) / rng * 100.0 if rng > 0 else 0.0

    def _wick_blocks(self):
        return bool(self.WICK_PCT) and self.trade_type == "LONG" and self._lower_wick_pct() >= self.WICK_PCT

    def _adx_ok(self):
        return super()._adx_ok() and not self._wick_blocks()

    def _adx_fail(self):
        if not super()._adx_ok():
            return super()._adx_fail()
        return f"Нижняя тень {self._lower_wick_pct():.0f}% (>= {self.WICK_PCT:g}%)"


def _apply_variant(wick_pct):
    """Подменяет класс вотчера в менеджере на время прогона (в этом процессе)."""
    _BaseWatcher.CONFIG["MIN_ADX"] = MIN_ADX
    if wick_pct is None:
        setattr(_manager_module, "BounceWatcher", _BaseWatcher)
    else:
        WickSkipWatcher.WICK_PCT = wick_pct
        setattr(_manager_module, "BounceWatcher", WickSkipWatcher)


def _worker(task):
    """Одна монета в одном варианте. Модульного уровня — на Windows процессы стартуют заново."""
    coin, start, end, wick_pct = task
    try:
        _apply_variant(wick_pct)
        res = simulate_engine.run_simulation(coin, start, end, strategy=STRATEGY,
                                             top_up=False, save_last_run=False)
        return coin, [t for t in res.get("trades", []) if t.get("type") == "LONG"], None
    except Exception as e:
        return coin, [], str(e)


def run_variant(wick_pct, coins, start, end, workers):
    tasks = [(c, start, end, wick_pct) for c in coins]
    trades, errors = [], {}
    if workers <= 1:
        results = map(_worker, tasks)
    else:
        executor = ProcessPoolExecutor(max_workers=workers)
        results = executor.map(_worker, tasks)
    for coin, coin_trades, err in results:
        if err:
            errors[coin] = err
        trades.extend(coin_trades)
    if workers > 1:
        executor.shutdown()
    return trades, errors


# ----------------------------------------------------------------------
# СТАТИСТИКА
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
    return {
        "n": len(trades),
        "closed": len(closed),
        "tp_pct": (100.0 * len(tp) / len(closed)) if closed else None,
        "avg": _avg([t["result_percent"] for t in closed]),
        "sum": sum(t["result_percent"] for t in closed),
        "mae_avg": _avg(mae),
        "mae_worst": min(mae) if mae else None,
    }


def compare_with_base(base_trades, var_trades):
    """Сопоставление по монете + уровню (на уровень — одна сделка за жизнь вотчера):
    те же время/вход — без изменений; тот же уровень, но позже — сдвинулась; в базе была,
    в варианте нет — потеряна; в варианте есть, в базе не было — новая."""
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


def _entry_change_pct(old, new):
    return (new["entry"] - old["entry"]) / old["entry"] * 100.0


def build_report(runs, start, end, n_coins):
    base = runs[None]["trades"]
    lines = [f"Тест «LONG не входит на свече с большой нижней тенью» — {STRATEGY}, период {start} .. {end}, монет {n_coins}",
             f"Только LONG. ADX-фильтр на время теста: {MIN_ADX or 'выключен'}. Исход как в симуляторе: TP (+7%) либо дедлайн; "
             "сделки ⏳ (ещё открыты) в проценты не входят.",
             "Просадка = худшее отклонение цены против сделки от входа до закрытия (в %, отрицательная).", ""]
    head = f"{'вариант':<16}{'сделок':>8}{'закрыто':>9}{'TP %':>8}{'средн %':>9}{'сумма %':>10}{'просадка ср.':>14}{'просадка худш.':>16}"
    lines += [head, "-" * len(head)]
    for w, run in runs.items():
        s = _stats(run["trades"])
        name = "база (сейчас)" if w is None else f"тень >= {w:g}%"
        lines.append(f"{name:<16}{s['n']:>8}{s['closed']:>9}{_f(s['tp_pct'], '{:.1f}'):>8}{_f(s['avg']):>9}"
                     f"{_f(s['sum'], '{:.1f}'):>10}{_f(s['mae_avg']):>14}{_f(s['mae_worst']):>16}")

    lists = []
    for w, run in runs.items():
        if w is None:
            continue
        same, shifted, lost, new = compare_with_base(base, run["trades"])
        lines += ["", f"=== Тень >= {w:g}%: что изменилось против базы ===",
                  f"Без изменений: {len(same)} | сдвинулись на более позднюю свечу: {len(shifted)} | "
                  f"потеряны (вход так и не нашёлся): {len(lost)} | новые (в базе не было): {len(new)}"]
        if shifted:
            delay = _avg([(v["time"] - b["time"]) / 900.0 for b, v in shifted])
            lines += [f"Сдвинувшиеся ({len(shifted)}): вход в среднем на {delay:.1f} свечей позже (15м), "
                      f"цена входа в среднем {_f(_avg([_entry_change_pct(b, v) for b, v in shifted]), '{:+.2f}')}% от прежней",
                      f"  просадка ср.: было {_f(_avg([b.get('mae_pct') for b, v in shifted]))} -> стало {_f(_avg([v.get('mae_pct') for b, v in shifted]))}",
                      f"  результат ср. %: было {_f(_avg([b.get('result_percent') for b, v in shifted if b['status'] != '⏳']))} -> "
                      f"стало {_f(_avg([v.get('result_percent') for b, v in shifted if v['status'] != '⏳']))}"]
        if lost:
            sl = _stats(lost)
            lines.append(f"Потерянные ({len(lost)}): в базе их результат ср. {_f(sl['avg'])}%, просадка ср. {_f(sl['mae_avg'])}%, "
                         f"TP {_f(sl['tp_pct'], '{:.1f}')}%")
        if new:
            sn = _stats(new)
            lines.append(f"Новые ({len(new)}): результат ср. {_f(sn['avg'])}%, просадка ср. {_f(sn['mae_avg'])}%")
        if shifted:
            lists.append((w, shifted))

    for w, shifted in lists:
        lines += ["", f"--- Сдвинувшиеся сделки при тени >= {w:g}% (первые {SHIFTED_LIST_LIMIT}) ---"]
        h = (f"{'монета':<10}{'было (время)':<18}{'вход':>12}{'стало (время)':<18}{'вход':>12}{'свечей':>8}"
             f"{'просадка было':>15}{'просадка стало':>16}{'рез. было':>11}{'рез. стало':>12}")
        lines += [h, "-" * len(h)]
        for b, v in sorted(shifted, key=lambda p: p[1]["time"])[:SHIFTED_LIST_LIMIT]:
            tb = datetime.datetime.fromtimestamp(b["time"], datetime.timezone.utc).strftime("%m-%d %H:%M")
            tv = datetime.datetime.fromtimestamp(v["time"], datetime.timezone.utc).strftime("%m-%d %H:%M")
            lines.append(f"{b['coin']:<10}{tb:<18}{b['entry']:>12.6g}{tv:<18}{v['entry']:>12.6g}"
                         f"{(v['time'] - b['time']) / 900.0:>8.0f}{_f(b.get('mae_pct')):>15}{_f(v.get('mae_pct')):>16}"
                         f"{_f(b.get('result_percent')):>11}{_f(v.get('result_percent')):>12}")
    lines += ["", "Как читать:",
              " • «Сдвинулись» — те же уровни, но вход на более поздней свече. Смотри, стала ли просадка меньше и не упал ли результат.",
              " • «Потеряны» — вход после пропуска так и не нашёлся (вотчер умер или данные кончились). Хорошо, если там результат слабый.",
              " • Если сделок стало заметно меньше, а итог и просадка не улучшились — правило не помогает."]
    return "\n".join(lines)


def main():
    coins, _cov = simulate_engine.list_coins_with_levels(START, END)
    if COINS is not None:
        coins = [c for c in coins if c in COINS]
    if not coins:
        print("❌ За период нет монет с уровнями (нужен levels_timeline_*.json за эти даты).")
        return
    print(f"Стратегия {STRATEGY}, монет: {len(coins)}. Докачиваю недостающие свечи в архив (уже скачанное не качается)...")
    simulate_engine.prefetch_archive(START, END)

    runs = {}
    for w in [None] + list(WICK_VARIANTS):
        label = "база" if w is None else f"тень >= {w:g}%"
        t0 = datetime.datetime.now()
        trades, errors = run_variant(w, coins, START, END, WORKERS)
        runs[w] = {"trades": trades, "errors": errors}
        secs = (datetime.datetime.now() - t0).total_seconds()
        print(f"[{label}] готово за {secs:.0f} с: LONG-сделок {len(trades)}, ошибок по монетам {len(errors)}")

    report = build_report(runs, START, END, len(coins))
    errs = {c: e for r in runs.values() for c, e in r["errors"].items()}
    if errs:
        report += "\n\nОшибки по монетам (в сводку не вошли): " + ", ".join(f"{c}: {e}" for c, e in sorted(errs.items())[:20])
    print("\n" + report)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\nОтчёт сохранён: {REPORT_FILE}")


if __name__ == "__main__":
    main()