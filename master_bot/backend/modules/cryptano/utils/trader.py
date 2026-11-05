"""
trader.py — исполнитель сигналов бота на Bybit.

Сам ничего не решает: получает готовый сигнал бота (монета, направление, вход, TP, SL)
и повторяет его на бирже — рыночный вход на заданную сумму и тейк-профит/стоп ровно по
цифрам сигнала. Фильтров, лимитов и своих расчётов TP/SL нет: что дал бот, то и уходит.

Настройки — в config.json бота, раздел crypto.trade:
    "trade": {
        "enabled": false,        # главный выключатель авто-торговли
        "mode": "dry",           # "dry" — только пишет в журнал, на биржу не шлёт
                                 # "testnet" — тестовая биржа Bybit, "live" — реальная
        "amount_usd": 5,         # сумма одной сделки в долларах (маржа; позиция = amount_usd * leverage)
        "leverage": 1            # плечо
    }
Ключи Bybit — в отдельном файле backend/trade_keys.json (не в config.json и не в коде):
    {"testnet": {"key": "...", "secret": "..."}, "live": {"key": "...", "secret": "..."}}
У ключей — только право «торговля», вывод средств не включать.

Журнал всего, что исполнитель сделал, — jsonbank/exchange_trades.json (отдельно от signals.json).
Расчёт бота в таблице сигналов и реальный результат на бирже — две независимые записи.
"""
import os
import time
import datetime
import threading

import ccxt

from backend.modules.cryptano.utils.storage import load_json, save_json_atomic
from backend.modules.cryptano.utils.paths import CRYPTANO_DIR, JSONBANK_DIR
from backend.modules.cryptano.utils.bybit import resolve_symbol

_BACKEND_DIR = os.path.dirname(os.path.dirname(CRYPTANO_DIR))  # .../backend
CONFIG_FILE = os.path.join(_BACKEND_DIR, "config.json")
KEYS_FILE = os.path.join(_BACKEND_DIR, "trade_keys.json")
TRADES_FILE = os.path.join(JSONBANK_DIR, "exchange_trades.json")

_lock = threading.Lock()
_exchanges = {}  # mode -> ccxt.bybit


def _settings():
    cfg = load_json(CONFIG_FILE, default={}) or {}
    t = (cfg.get("crypto") or {}).get("trade") or {}
    return {
        "enabled": bool(t.get("enabled", False)),
        "mode": str(t.get("mode", "dry")).lower(),
        "amount_usd": float(t.get("amount_usd", 5)),
        "leverage": int(t.get("leverage", 1)),
    }


def _exchange(mode):
    """Своя ccxt-биржа с ключами (общий exchange бота без ключей не трогаем)."""
    if mode not in ("testnet", "live"):
        raise ValueError(f"Неизвестный режим: {mode}")
    if mode not in _exchanges:
        keys = (load_json(KEYS_FILE, default={}) or {}).get(mode) or {}
        if not keys.get("key") or not keys.get("secret"):
            raise RuntimeError(f"Нет ключей '{mode}' в {KEYS_FILE}")
        ex = ccxt.bybit({
            "apiKey": keys["key"], "secret": keys["secret"],
            "enableRateLimit": True, "options": {"defaultType": "swap"},
        })
        if mode == "testnet":
            ex.set_sandbox_mode(True)
        ex.load_markets()
        _exchanges[mode] = ex
    return _exchanges[mode]


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_trades():
    data = load_json(TRADES_FILE, default=[])
    return data if isinstance(data, list) else []


def _signal_key(sig):
    return f"{sig.get('coin')}|{sig.get('time')}|{sig.get('level_id')}"


def _qty(ex, symbol, price, amount_usd, leverage):
    """Количество по сумме сделки. Возвращает (qty, причина_пропуска)."""
    market = ex.market(symbol)
    qty = float(ex.amount_to_precision(symbol, amount_usd * leverage / price))
    min_amount = ((market.get("limits") or {}).get("amount") or {}).get("min")
    min_cost = ((market.get("limits") or {}).get("cost") or {}).get("min")
    if qty <= 0 or (min_amount and qty < min_amount):
        return None, f"сумма {amount_usd}$ x{leverage} даёт количество {qty} < минимума биржи {min_amount}"
    if min_cost and qty * price < min_cost:
        return None, f"стоимость {qty * price:.2f}$ < минимума биржи {min_cost}$"
    return qty, None


def execute_signal(signal):
    """Повторяет сигнал бота на бирже. Никогда не бросает исключение в бота:
    любая ошибка пишется в журнал и в консоль."""
    try:
        st = _settings()
        if not st["enabled"]:
            return
        key = _signal_key(signal)
        long_side = signal.get("type") == "WATCHER_LONG"
        entry, tp, sl = signal.get("price"), signal.get("take_profit"), signal.get("stop_loss")
        record = {
            "key": key, "coin": signal.get("coin"), "direction": "LONG" if long_side else "SHORT",
            "mode": st["mode"], "signal_time": signal.get("time"), "level_id": signal.get("level_id"),
            "entry": entry, "tp": tp, "sl": sl, "amount_usd": st["amount_usd"], "leverage": st["leverage"],
            "created_at": _now_iso(), "status": "SENDING",
        }
        with _lock:
            trades = _load_trades()
            if any(t.get("key") == key for t in trades):
                return  # этот сигнал уже обработан (в т.ч. после перезапуска бота)
            trades.append(record)
            save_json_atomic(TRADES_FILE, trades, indent=2)

        try:
            if st["mode"] == "dry":
                record["status"] = "DRY"
                print(f"[TRADER] DRY {record['direction']} {record['coin']} на {st['amount_usd']}$ "
                      f"вход≈{entry} TP {tp} SL {sl} (на биржу не отправлено)")
            else:
                ex = _exchange(st["mode"])
                symbol = resolve_symbol(record["coin"], ex.markets)
                if not symbol:
                    raise RuntimeError(f"нет свопа USDT для {record['coin']}")
                qty, skip = _qty(ex, symbol, float(entry), st["amount_usd"], st["leverage"])
                if skip:
                    record["status"], record["error"] = "SKIP", skip
                    print(f"[TRADER] SKIP {record['coin']}: {skip}")
                else:
                    try:
                        ex.set_leverage(st["leverage"], symbol)
                    except Exception as e:
                        if "110043" not in str(e) and "not modified" not in str(e).lower():
                            raise
                    params = {}
                    if tp is not None:
                        params["takeProfit"] = tp
                    if sl is not None:
                        params["stopLoss"] = sl
                    order = ex.create_order(symbol, "market", "buy" if long_side else "sell", qty, None, params)
                    record.update({"status": "OPEN", "symbol": symbol, "qty": qty,
                                   "order_id": order.get("id"), "opened_at": _now_iso(),
                                   "opened_ms": int(time.time() * 1000)})
                    print(f"[TRADER] {st['mode'].upper()} {record['direction']} {record['coin']} qty {qty} "
                          f"TP {tp} SL {sl} ордер {order.get('id')}")
        except Exception as e:
            record["status"], record["error"] = "ERROR", str(e)
            print(f"[TRADER] ❌ {record['coin']}: {e}")

        with _lock:
            trades = _load_trades()
            for i, t in enumerate(trades):
                if t.get("key") == key:
                    trades[i] = record
            save_json_atomic(TRADES_FILE, trades, indent=2)
    except Exception as e:
        print(f"[TRADER] ❌ неожиданная ошибка: {e}")


def sync_results():
    """Для открытых позиций смотрит на бирже, закрылись ли они, и пишет реальный результат
    (цена выхода, прибыль в $ и %). Вызывать периодически (см. history.update_open_signals)."""
    try:
        with _lock:
            trades = _load_trades()
        open_ids = [i for i, t in enumerate(trades) if t.get("status") == "OPEN"]
        if not open_ids:
            return
        changed = False
        for i in open_ids:
            t = trades[i]
            try:
                ex = _exchange(t["mode"])
                pos = ex.fetch_positions([t["symbol"]])
                if any(abs(float(p.get("contracts") or 0)) > 0 for p in pos):
                    continue  # позиция ещё открыта
                market = ex.market(t["symbol"])
                resp = ex.privateGetV5PositionClosedPnl({"category": "linear", "symbol": market["id"], "limit": 20})
                rows = [r for r in ((resp.get("result") or {}).get("list") or [])
                        if int(r.get("updatedTime") or 0) >= int(t.get("opened_ms") or 0)]
                if not rows:
                    continue  # позиция закрыта, но запись о результате ещё не появилась — повторим в следующий раз
                r = max(rows, key=lambda x: int(x.get("updatedTime") or 0))
                avg_in, avg_out = float(r["avgEntryPrice"]), float(r["avgExitPrice"])
                pct = (avg_out - avg_in) / avg_in * 100.0 if t["direction"] == "LONG" else (avg_in - avg_out) / avg_in * 100.0
                t.update({"status": "CLOSED", "entry_fill": avg_in, "exit_price": avg_out,
                          "pnl_usd": float(r["closedPnl"]), "result_percent": round(pct, 2),
                          "closed_at": datetime.datetime.fromtimestamp(int(r["updatedTime"]) / 1000, datetime.timezone.utc).isoformat()})
                changed = True
                print(f"[TRADER] закрыта {t['direction']} {t['coin']}: выход {avg_out}, {t['pnl_usd']}$ ({t['result_percent']}%)")
            except Exception as e:
                print(f"[TRADER] ⚠️ не удалось проверить {t.get('coin')}: {e}")
        if changed:
            with _lock:
                fresh = _load_trades()
                by_key = {t["key"]: t for t in trades}
                for k, f in enumerate(fresh):
                    if f.get("key") in by_key and by_key[f["key"]].get("status") == "CLOSED":
                        fresh[k] = by_key[f["key"]]
                save_json_atomic(TRADES_FILE, fresh, indent=2)
    except Exception as e:
        print(f"[TRADER] ❌ sync_results: {e}")