"""
Тестовые правила для BOUNCE: общий прогон симулятора («All run») с новыми правилами входа.

Что делает: запускает настоящий общий прогон симулятора (simulate_engine.run_bulk_simulation, режим
Watcher — как в бою) по всем монетам периода. Все настройки — боевые: общие из config.json (TP, SL,
пороги EMA и т.д.) и свои настройки монет из coin_settings.json. Сверху добавлены два тестовых правила,
которые включаются в настройках ниже:

  1. SKIP_SINGLE_4H — не торговать одиночные 4H-уровни. Уровень с типом ровно 4h_extreme_peak
     (без слияния с уровнями 1W / 1M / 1d / POC) убирается из списка уровней монеты, как будто его нет.
     Слитые уровни («4h_extreme_peak + 1W_low_PWL» и т.п.) остаются.
  2. MAX_LOWER_WICK_PCT — если у свечи, на которой вотчер вошёл бы в LONG, нижняя тень >= этого порога
     (в % от размаха свечи: (min(open, close) - low) / (high - low) * 100), вход на ней пропускается:
     вотчер остаётся жить и ищет следующую подходящую свечу по тем же правилам. 0 = правило выключено.
     Только LONG.
  Пропущенные из-за тени свечи помечаются в симуляторе событием WICK_SKIP (на пути выбранной сделки).
  3. REQUIRE_TOUCH — свеча входа (LONG) должна касаться зоны: её лой не выше верха зоны (level_max), как у
     красной свечи перед входом в BUYBACK. Если лой выше — вход на ней пропускается (событие TOUCH_SKIP на
     пути сделки в симуляторе), вотчер остаётся жить и ищет дальше. Только LONG.
  4. MAX_OPEN — не больше этого числа одновременно открытых сделок по всем монетам вместе. Если открыто
     MAX_OPEN сделок, новый сигнал пропускается (берётся тот, что пришёл раньше). Сделка открыта от входа
     до закрытия, не закрытая к концу данных — до конца. Пропущенные по лимиту в «All run» не попадают.
     0 = без лимита.
  Шорты в прогон пока не берутся (ALLOW_SHORT = False); при True — как в бою.

Результат сохраняется туда же, куда его кладёт обычный общий прогон (last_all_run.json), и сразу виден
в симуляторе (выбрать BOUNCE и обновить страницу). Прежний last_all_run.json этим прогоном заменяется.
Файлы бота и симулятора не меняются: правила живут только в этом файле.

Запуск: просто запустить файл. Период и правила — в настройках ниже.
"""
import os
import sys
import json
import datetime
import importlib

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
START = "2026-08-01"
END = "2026-08-30"
SKIP_SINGLE_4H = True        # True = убрать одиночные 4H-уровни; False = как в бою
MAX_LOWER_WICK_PCT = 40      # порог нижней тени свечи входа в % от размаха свечи; 0 = выключено
REQUIRE_TOUCH = True         # True = свеча входа должна касаться зоны (её лой <= верх зоны); False = как в бою
MAX_OPEN = 0             # максимум одновременно открытых сделок по всем монетам; 0 = без лимита
ALLOW_SHORT = False          # False = только LONG (SHORT в прогон не берётся); True = как в бою
# ======================================================================

from backend.modules.cryptano.simulator import simulate_engine
from backend.modules.cryptano.utils.storage import save_json_atomic
from backend.modules.cryptano.utils.paths import JSONBANK_DIR

STRATEGY = "BOUNCE"
SINGLE_4H_TYPE = "4h_extreme_peak"

_manager_module = importlib.import_module("backend.modules.cryptano.strategy.bounce_manager")
_BaseManager = _manager_module.BounceManager
_BaseWatcher = importlib.import_module("backend.modules.cryptano.strategy.bounce_watcher").BounceWatcher


class RulesWatcher(_BaseWatcher):
    """Боевой вотчер + пропуск входа в LONG на свече с большой нижней тенью.

    Вотчер доходит до входа по всем своим условиям, и только в момент входа здесь проверяется тень:
    если она >= порога — входа нет, свеча помечается событием WICK_SKIP (видно в симуляторе на пути
    сделки), состояние вотчера остаётся таким же, как до этой свечи, и он ищет вход дальше."""
    _cur_candle = None
    _wick_skipped = False

    def _update_impl(self, c_open, c_high, c_low, c_close, *args, **kwargs):
        self._cur_candle = (float(c_open), float(c_high), float(c_low), float(c_close))
        self._wick_skipped = False
        stage, fired = getattr(self, "climax_stage", None), getattr(self, "_simple_entry_fired", False)
        res = super()._update_impl(c_open, c_high, c_low, c_close, *args, **kwargs)
        if self._wick_skipped:
            # вход не состоялся — блокировки «после входа» (ждать перезарядки и т.п.) не включаем
            self.climax_stage = stage
            self._simple_entry_fired = fired
        return res

    def _lower_wick_pct(self):
        if self._cur_candle is None:
            return 0.0
        o, h, l, c = self._cur_candle
        rng = h - l
        return (min(o, c) - l) / rng * 100.0 if rng > 0 else 0.0

    def _wick_blocks(self):
        return bool(MAX_LOWER_WICK_PCT) and self.trade_type == "LONG" and self._lower_wick_pct() >= MAX_LOWER_WICK_PCT

    def _enter(self, *args, **kwargs):
        candle = self._cur_candle
        if REQUIRE_TOUCH and self.trade_type == "LONG" and candle is not None and candle[2] > self.max:
            self._wick_skipped = True
            self.last_event_type = "TOUCH_SKIP"
            self._last_event_price = None
            self._dbg(f"🟡 ПРОПУСК | Свеча входа не касается зоны: лой {candle[2]:.4f} > верх зоны {self.max:.4f}")
            return None
        if self._wick_blocks():
            self._wick_skipped = True
            self.last_event_type = "WICK_SKIP"
            self._last_event_price = None   # None = цена закрытия свечи
            self._dbg(f"🟡 ПРОПУСК | Нижняя тень {self._lower_wick_pct():.0f}% (>= {MAX_LOWER_WICK_PCT:g}%)")
            return None
        return super()._enter(*args, **kwargs)


class RulesManager(_BaseManager):
    """Боевой менеджер + удаление одиночных 4H-уровней из списка уровней каждой свечи
    (так же, как это делает фильтр уровней по EMA: убранного уровня для вотчеров просто нет)."""

    def process_candle(self, c_low, c_high, c_close, supports, resistances, *args, **kwargs):
        if SKIP_SINGLE_4H:
            supports = [z for z in (supports or []) if z.get("type") != SINGLE_4H_TYPE]
            if ALLOW_SHORT:
                resistances = [z for z in (resistances or []) if z.get("type") != SINGLE_4H_TYPE]
        return super().process_candle(c_low, c_high, c_close, supports, resistances, *args, **kwargs)


def _apply_rules():
    """Подменяет менеджер и вотчер на время прогона (в текущем процессе)."""
    simulate_engine.BounceManager = RulesManager
    if MAX_LOWER_WICK_PCT or REQUIRE_TOUCH:
        setattr(_manager_module, "BounceWatcher", RulesWatcher)


def _worker(task):
    """Одна монета в отдельном процессе (то же, что делает simulate_engine._simulate_one_coin_worker,
    но с тестовыми правилами). Модульного уровня — на Windows процессы стартуют заново."""
    coin, start, end, strategy, opts = task
    try:
        _apply_rules()
        res = simulate_engine.run_simulation(coin, start, end, strategy=strategy,
                                             top_up=False, save_last_run=False, **opts)
        return coin, {"trades": res.get("trades", []), "history": res.get("history", [])}, None
    except Exception as e:
        return coin, None, str(e)


def _unix(iso):
    t = datetime.datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.timestamp()


def _limit_open(trades):
    """Оставляет сделки, которые успели бы открыться при лимите MAX_OPEN одновременно открытых
    (по всем монетам, в порядке времени входа). Возвращает (оставленные, число пропущенных)."""
    kept, open_until = [], []
    for t in sorted(trades, key=lambda x: (x["time"], x.get("coin", ""))):
        open_until = [c for c in open_until if c > t["time"]]
        if len(open_until) >= MAX_OPEN:
            continue
        closed = t.get("closed_at") if t.get("status") in ("✅", "🕐") else None
        open_until.append(max(_unix(closed), t["time"] + 1) if closed else float("inf"))
        kept.append(t)
    return kept, len(trades) - len(kept)


def _apply_open_limit(result):
    """Применяет лимит к результату общего прогона: сделки, сводка по монетам, эпизоды."""
    kept, skipped = _limit_open(result.get("trades", []))
    by_coin = {}
    for t in kept:
        by_coin.setdefault(t["coin"], []).append(t)
    for coin, r in result.get("results", {}).items():
        if r.get("error"):
            continue
        agg = simulate_engine._aggregate_trades(by_coin.get(coin, []))
        if "levels_coverage" in r:
            agg["levels_coverage"] = r["levels_coverage"]
        result["results"][coin] = agg
    result["trades"] = kept
    result["coins_with_trades"] = len(by_coin)
    result["history_by_coin"] = {c: h for c, h in result.get("history_by_coin", {}).items() if c in by_coin}
    return skipped


def _summary(trades):
    closed = [t for t in trades if t.get("status") in ("✅", "🕐") and t.get("result_percent") is not None]
    tp = [t for t in closed if t["status"] == "✅"]
    return (f"сделок {len(trades)} (закрыто {len(closed)}, ещё открыто {len(trades) - len(closed)}), "
            f"TP {100.0 * len(tp) / len(closed):.1f}%, сумма {sum(t['result_percent'] for t in closed):.0f}%"
            if closed else f"сделок {len(trades)}, закрытых нет")


def _blocked_coins():
    """Монеты, заблокированные (⛔) в списке симулятора — они не участвуют в общем прогоне.
    Читается тот же файл jsonbank/sim_config.json, что использует кнопка ▶▶ ALL."""
    try:
        with open(os.path.join(JSONBANK_DIR, "sim_config.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        return {str(c).upper().strip() for c in (raw.get("blocked") or []) if str(c).strip()}
    except Exception:
        return set()


def main():
    blocked = _blocked_coins()
    coins, _cov = simulate_engine.list_coins_with_levels(START, END)
    coins = [c for c in coins if c not in blocked]
    if not coins:
        print("❌ За период нет монет с уровнями (нужен levels_timeline_*.json за эти даты).")
        return
    rules = {"skip_single_4h": SKIP_SINGLE_4H, "max_lower_wick_pct": MAX_LOWER_WICK_PCT,
             "require_touch": REQUIRE_TOUCH, "max_open": MAX_OPEN, "allow_short": ALLOW_SHORT}
    print(f"{STRATEGY}: {START} .. {END}, монет {len(coins)} (заблокированных в симуляторе исключено: {len(blocked)}). Тестовые правила: {rules}")
    print("Докачиваю недостающие свечи в архив (уже скачанное не качается)...")
    simulate_engine.prefetch_archive(START, END)

    # общий прогон симулятора, но монеты считает наш воркер с правилами
    simulate_engine._simulate_one_coin_worker = _worker
    result = simulate_engine.run_bulk_simulation(START, END, strategy=STRATEGY, blocked=blocked, allow_short=ALLOW_SHORT)

    skipped = _apply_open_limit(result) if MAX_OPEN else 0

    # отметка в сохранённом результате, чтобы было видно, что это тестовый прогон
    result.setdefault("settings", {})["test_rules"] = rules
    save_json_atomic(simulate_engine._sim_path(STRATEGY, "last_all_run"), result, indent=2)

    trades = result.get("trades", [])
    print(f"\nГотово. {'Только LONG' if not ALLOW_SHORT else 'LONG и SHORT'}: {_summary(trades)}")
    if MAX_OPEN:
        print(f"Лимит одновременно открытых: {MAX_OPEN}, пропущено по лимиту: {skipped}")
    errs = {c: r["error"] for c, r in result.get("results", {}).items() if r.get("error")}
    if errs:
        print("Ошибки по монетам: " + ", ".join(f"{c}: {e}" for c, e in sorted(errs.items())[:20]))
    print("Результат сохранён в last_all_run — открой симулятор, выбери BOUNCE и обнови страницу.")


if __name__ == "__main__":
    main()