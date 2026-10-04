import datetime
from backend.modules.cryptano.utils.bybit import exchange, resolve_symbol, load_markets_cached
from backend.modules.cryptano.utils.storage import load_json, save_json_atomic
from backend.modules.cryptano.utils.paths import SIGNALS_FILE


def _utc_now_iso():
    """closed_at всегда пишем в UTC С ЯВНОЙ пометкой зоны (+00:00).
    Раньше живое закрытие писало местное время без зоны, а рескан — UTC
    без зоны. Браузер читает время без зоны как МЕСТНОЕ (Киев, +3), поэтому
    у сделок из рескана закрытие уезжало на 3 часа раньше входа -> "0 дней"
    и пустая полоса уровня на графике. С пометкой зоны браузер читает верно."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save_signal(signal: dict):
    """Сохраняет новый сигнал в signals.json."""
    signals = _load_signals()

    if signal.get("type") == "SHORT_PUMP":
        entry = signal.get("entry_market", signal.get("price"))
        target = signal.get("take_profit")
        stop = signal.get("stop_loss")
        signal_type = "SHORT"
    else:
        entry = signal.get("entry_limit", signal.get("price"))
        target = signal.get("take_profit", signal.get("r1"))
        stop = signal.get("stop_loss")
        signal_type = "LONG"

    # Rescans can provide an outcome already calculated from historical
    # candles; otherwise this remains a newly-opened live signal.
    status = signal.get("status", "⏳")
    result_percent = signal.get("result_percent")
    closed_at = signal.get("closed_at")

    _t = signal.get("time")
    try:
        _date_dt = datetime.datetime.fromtimestamp(int(_t)) if _t else datetime.datetime.now()
    except (TypeError, ValueError, OSError):
        _date_dt = datetime.datetime.now()

    record = {
        "date": _date_dt.strftime("%Y-%m-%d %H:%M"),
        "coin": signal.get("coin"),
        "type": signal_type,
        "source": signal.get("source", "CRITICAL"),
        "entry": entry,
        "target": target,
        "target_2": signal.get("target_2"),
        "target_3": signal.get("target_3"),
        "stop": stop,
        "status": status,
        "result_percent": result_percent,
        "time": signal.get("time"),
        "level_id": signal.get("level_id"),
        "level_type": signal.get("level_type"),
        "method": signal.get("method"),
        "method_value": signal.get("method_value"),
        "method_mult": signal.get("method_mult"),
        "closed_at": closed_at,
        "level_min": signal.get("level_min"),
        "level_max": signal.get("level_max"),
        "level_date": signal.get("level_date"),
        "level_score": signal.get("level_score"),
        "level_reaction_count": signal.get("level_reaction_count"),
        "activated_at": signal.get("activated_at"),
        "mode": signal.get("mode"),
        "events": signal.get("events"),
    }

    signals.append(record)
    _save_signals(signals)


def update_open_signals():
    """
    Обновляет текущий результат ещё не закрытых сигналов (status == "⏳") —
    вызывать периодически (см. background_tasks.py, ~раз в 15 минут, на той
    же границе, что и watcher-скан).

    Закрытие сделки — ровно по тому, что реально тестировалось и давало
    профит (без настоящего стоп-лосса, сделки держались до ~2 недель):
      - TP (Target) достигнут — закрывает ✅ с реальным результатом;
      - MAX_HOLD_DAYS дней прошло с входа, TP так и не достигнут —
        закрывает по ТЕКУЩЕЙ цене на этот момент (не обязательно в плюс),
        статус ✅/❌ по знаку итогового результата.

    Честное ограничение: проверка раз в 15 минут, не по каждой свече — если
    цена пробила TP и вернулась обратно ДО следующей проверки, это не будет
    замечено (для точного отслеживания нужна была бы история свечей с
    момента входа, а не разовая текущая цена — этого тут нет).
    """
    from backend.modules.cryptano.strategy.bounce_watcher import BounceWatcher

    max_hold_days = BounceWatcher.CONFIG.get("MAX_HOLD_DAYS", 14)
    signals = _load_signals()
    if not signals:
        return

    changed = False
    try:
        markets = load_markets_cached(exchange)
    except Exception as e:
        print(f"[SIGNALS UPDATE] ⚠️ Не удалось загрузить markets: {e}")
        return

    now = datetime.datetime.now()

    for record in signals:
        if record.get("status") != "⏳":
            continue
        coin = record.get("coin")
        entry = record.get("entry")
        direction = record.get("type")
        if not coin or entry is None or direction not in ("LONG", "SHORT"):
            continue

        try:
            symbol = resolve_symbol(coin, markets)
            if not symbol:
                continue
            ticker = exchange.fetch_ticker(symbol)
            last_val = ticker.get("last") or ticker.get("close")
            if last_val is None:
                continue
            last_price = float(last_val)
        except Exception as e:
            print(f"[SIGNALS UPDATE] ⚠️ {coin}: не удалось получить текущую цену: {e}")
            continue

        entry = float(entry)
        target = record.get("target")
        stop = record.get("stop")

        if direction == "LONG":
            pct = (last_price - entry) / entry * 100.0
            hit_tp = target is not None and last_price >= float(target)
            hit_sl = stop is not None and last_price <= float(stop)
        else:
            pct = (entry - last_price) / entry * 100.0
            hit_tp = target is not None and last_price <= float(target)
            hit_sl = stop is not None and last_price >= float(stop)

        # Сколько дней сделка уже открыта — по unix-времени входа, если оно
        # есть (новые сигналы, см. watcher_plan.py::check_bounce), иначе по
        # текстовой дате входа (старые сигналы, грубее, но лучше, чем никак).
        entry_time = record.get("time")
        if entry_time:
            entry_dt = datetime.datetime.utcfromtimestamp(int(entry_time))
        else:
            try:
                entry_dt = datetime.datetime.strptime(record.get("date", ""), "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                entry_dt = None
        # entry_dt из unix-времени — это UTC, значит и "сейчас" берём в UTC
        # (раньше было местное now — сдвиг на 3 часа в подсчёте дней).
        days_open = ((datetime.datetime.utcnow() if entry_time else now) - entry_dt).days if entry_dt else 0
        time_expired = days_open >= max_hold_days

        record["result_percent"] = round(pct, 2)
        if hit_tp:
            record["status"] = "✅"
        elif hit_sl:
            record["status"] = "❌"
        elif time_expired:
            record["status"] = "✅" if pct > 0 else "❌"

        if record.get("status") in ("✅", "❌"):
            record["closed_at"] = _utc_now_iso()

        changed = True

    if changed:
        _save_signals(signals)


def _load_signals() -> list:
    data = load_json(SIGNALS_FILE, default=[])
    return data if isinstance(data, list) else []


def _save_signals(signals: list):
    save_json_atomic(SIGNALS_FILE, signals, indent=2)
