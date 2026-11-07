"""
trader.py — исполнитель сигналов бота на Bybit.

Сам ничего не решает: получает готовый сигнал бота (монета, направление, вход, TP, SL)
и повторяет его на бирже — рыночный вход и тейк-профит/стоп ровно по цифрам сигнала.
Своих расчётов TP/SL нет: что дал бот, то и уходит. Пропуски (устарел, лимит открытых,
плечо без SL, цена уже за TP/SL, ниже минимума биржи) пишутся в журнал со статусом SKIP
и причиной, а не молча.

Настройки — в config.json бота, раздел crypto.trade:
    "trade": {
        "enabled": false,          # главный выключатель авто-торговли ("Сигналы" на сайте = false)
        "mode": "dry",             # "dry"     — только журнал, на биржу не шлёт
                                   # "demo"    — Bybit Demo Trading (реальные цены, виртуальные деньги)
                                   # "testnet" — тестовая биржа Bybit
                                   # "live"    — реальная
        "amount_usd": 5,           # маржа одной сделки в $ (позиция = маржа * плечо)
        "amount_pct": null,        # если задано — маржа = этот % от баланса USDT (главнее amount_usd)
        "leverage": 1,             # плечо; при плече > 1 сделка без SL не открывается
        "max_open": 2,             # максимум одновременно открытых позиций бота
        "strategies": ["BOUNCE", "BUYBACK"],   # источники сигналов, которые торгуем (начало поля source)
        "max_signal_age_min": 30,  # сигнал старше (от закрытия свечи входа) не торгуем
        "max_hold_days": 14        # позиция старше закрывается рыночным ордером
    }
Ключи Bybit — в jsonbank/trade_keys.json (не в config.json и не в коде):
    {"demo": {"key": "...", "secret": "..."}, "live": {"key": "...", "secret": "..."}}
У ключей — только право «торговля», вывод средств не включать.

Журнал всего, что исполнитель сделал, — jsonbank/exchange_trades.json (отдельно от signals.json).
Расчёт бота в таблице сигналов и реальный результат на бирже — две независимые записи.
"""
import os
import time
import hashlib
import datetime
import threading

import ccxt

from backend.modules.cryptano.utils.storage import load_json, save_json_atomic
from backend.modules.cryptano.utils.paths import CRYPTANO_DIR, JSONBANK_DIR
from backend.modules.cryptano.utils.bybit import resolve_symbol

_BACKEND_DIR = os.path.dirname(os.path.dirname(CRYPTANO_DIR))  # .../backend
CONFIG_FILE = os.path.join(_BACKEND_DIR, "config.json")
KEYS_FILE = os.path.join(JSONBANK_DIR, "trade_keys.json")
TRADES_FILE = os.path.join(JSONBANK_DIR, "exchange_trades.json")

MODES = ("dry", "demo", "testnet", "live")
CANDLE_SEC = 15 * 60

_lock = threading.Lock()          # журнал (чтение-изменение-запись)
_exchanges = {}                   # mode -> ccxt.bybit
_ex_lock = threading.Lock()       # создание клиентов
_reconciled = False


# ----------------------------------------------------------------------------
# Настройки и клиент биржи
# ----------------------------------------------------------------------------

def _settings():
    cfg = load_json(CONFIG_FILE, default={}) or {}
    t = (cfg.get("crypto") or {}).get("trade") or {}
    pct = t.get("amount_pct")
    mode = str(t.get("mode", "dry")).lower()
    return {
        "enabled": bool(t.get("enabled", False)),
        "mode": mode if mode in MODES else "dry",
        "amount_usd": float(t.get("amount_usd", 5)),
        "amount_pct": float(pct) if pct not in (None, "") else None,
        "leverage": int(t.get("leverage", 1)),
        "max_open": int(t.get("max_open", 2)),
        "strategies": [str(s).upper() for s in (t.get("strategies") or ["BOUNCE", "BUYBACK"])],
        "max_signal_age_min": float(t.get("max_signal_age_min", 30)),
        "max_hold_days": float(t.get("max_hold_days", 14)),
    }


def settings():
    """Текущие настройки торговли (для сайта)."""
    return _settings()


def _exchange(mode):
    """Своя ccxt-биржа с ключами (общий exchange бота без ключей не трогаем)."""
    if mode not in ("demo", "testnet", "live"):
        raise ValueError(f"Для режима '{mode}' биржа не нужна")
    with _ex_lock:
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
            elif mode == "demo":
                ex.enable_demo_trading(True)
            ex.load_markets()
            _exchanges[mode] = ex
        return _exchanges[mode]


# ----------------------------------------------------------------------------
# Журнал
# ----------------------------------------------------------------------------

def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_trades():
    data = load_json(TRADES_FILE, default=[])
    return data if isinstance(data, list) else []


def _signal_key(sig):
    return f"{sig.get('coin')}|{sig.get('time')}|{sig.get('level_id')}"


def _client_id(key):
    """orderLinkId биржи (до 36 символов): по нему один сигнал физически не откроется дважды."""
    return "wb" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def _save_record(rec):
    """Записывает одну запись журнала по ключу (читает файл заново — не затирает чужие изменения)."""
    with _lock:
        trades = _load_trades()
        for i, t in enumerate(trades):
            if t.get("key") == rec["key"]:
                trades[i] = rec
                break
        else:
            trades.append(rec)
        save_json_atomic(TRADES_FILE, trades, indent=2)


def list_trades(limit=100, offset=0):
    """Журнал, свежие сверху (для вкладки «На бирже»)."""
    with _lock:
        trades = _load_trades()
    trades = sorted(trades, key=lambda t: t.get("created_at") or "", reverse=True)
    return {"items": trades[offset:offset + limit], "total": len(trades)}


def _open_count(mode, exclude_key=None):
    with _lock:
        return sum(1 for t in _load_trades()
                   if t.get("mode") == mode and t.get("status") in ("OPEN", "SENDING") and t.get("key") != exclude_key)


# ----------------------------------------------------------------------------
# Вход
# ----------------------------------------------------------------------------

def _qty(ex, symbol, price, margin_usd, leverage):
    """Количество по марже. Возвращает (qty, причина_пропуска)."""
    market = ex.market(symbol)
    qty = float(ex.amount_to_precision(symbol, margin_usd * leverage / price))
    min_amount = ((market.get("limits") or {}).get("amount") or {}).get("min")
    min_cost = ((market.get("limits") or {}).get("cost") or {}).get("min")
    if qty <= 0 or (min_amount and qty < min_amount):
        return None, f"маржа {margin_usd:.2f}$ x{leverage} даёт количество {qty} < минимума биржи {min_amount}"
    if min_cost and qty * price < min_cost:
        return None, f"стоимость {qty * price:.2f}$ < минимума биржи {min_cost}$"
    return qty, None


def _balance_usdt(ex):
    bal = ex.fetch_balance()
    usdt = bal.get("USDT") or {}
    return float(usdt.get("total") or usdt.get("free") or 0.0)


def _prepare_account(ex, symbol, leverage):
    """Плечо и изолированная маржа. Ошибки «не изменено» — не ошибки."""
    try:
        ex.set_leverage(leverage, symbol)
    except Exception as e:
        if "110043" not in str(e) and "not modified" not in str(e).lower():
            raise
    try:
        ex.set_margin_mode("isolated", symbol, {"leverage": leverage})
    except Exception as e:
        msg = str(e).lower()
        if "not modified" not in msg and "110026" not in msg and "unified" not in msg and "100028" not in msg:
            print(f"[TRADER] ⚠️ изолированная маржа не включена ({symbol}): {e}")


def execute_signal(signal):
    """Повторяет сигнал бота на бирже. Никогда не бросает исключение в бота:
    любая ошибка пишется в журнал и в консоль."""
    try:
        st = _settings()
        if not st["enabled"]:
            return
        source = str(signal.get("source") or "").upper()
        if not any(source.startswith(s) for s in st["strategies"]):
            return  # эту стратегию не торгуем — в журнал не пишем
        key = _signal_key(signal)
        long_side = signal.get("type") == "WATCHER_LONG"
        entry, tp, sl = signal.get("price"), signal.get("take_profit"), signal.get("stop_loss")
        record = {
            "key": key, "coin": signal.get("coin"), "direction": "LONG" if long_side else "SHORT",
            "source": signal.get("source"), "mode": st["mode"], "signal_time": signal.get("time"),
            "level_id": signal.get("level_id"), "entry": entry, "tp": tp, "sl": sl,
            "leverage": st["leverage"], "created_at": _now_iso(), "status": "SENDING",
        }
        with _lock:
            trades = _load_trades()
            if any(t.get("key") == key for t in trades):
                return  # этот сигнал уже обработан (в т.ч. после перезапуска бота)
            trades.append(record)
            save_json_atomic(TRADES_FILE, trades, indent=2)

        def skip(reason):
            record["status"], record["error"] = "SKIP", reason
            print(f"[TRADER] SKIP {record['direction']} {record['coin']}: {reason}")

        try:
            # --- проверки, не требующие биржи ---
            sig_time = signal.get("time")
            age_min = (time.time() - (int(sig_time) + CANDLE_SEC)) / 60.0 if sig_time else None
            if entry is None:
                skip("в сигнале нет цены входа")
            elif age_min is not None and age_min > st["max_signal_age_min"]:
                skip(f"сигнал устарел: {age_min:.0f} мин после закрытия свечи (лимит {st['max_signal_age_min']:g})")
            elif st["leverage"] > 1 and sl is None:
                skip(f"плечо x{st['leverage']} без SL не разрешено")
            elif st["mode"] != "dry" and _open_count(st["mode"], exclude_key=key) >= st["max_open"]:
                skip(f"лимит открытых позиций ({st['max_open']})")
            elif st["mode"] == "dry":
                record["status"] = "DRY"
                print(f"[TRADER] DRY {record['direction']} {record['coin']} "
                      f"вход≈{entry} TP {tp} SL {sl} (на биржу не отправлено)")
            else:
                ex = _exchange(st["mode"])
                symbol = resolve_symbol(record["coin"], ex.markets)
                if not symbol:
                    raise RuntimeError(f"нет свопа USDT для {record['coin']}")
                last = float(ex.fetch_ticker(symbol).get("last") or 0)
                if last <= 0:
                    raise RuntimeError("не удалось получить текущую цену")
                if long_side and ((tp is not None and tp <= last) or (sl is not None and sl >= last)):
                    skip(f"цена {last} уже за TP/SL (TP {tp}, SL {sl})")
                elif not long_side and ((tp is not None and tp >= last) or (sl is not None and sl <= last)):
                    skip(f"цена {last} уже за TP/SL (TP {tp}, SL {sl})")
                else:
                    margin = st["amount_usd"]
                    if st["amount_pct"]:
                        margin = _balance_usdt(ex) * st["amount_pct"] / 100.0
                    qty, why = _qty(ex, symbol, last, margin, st["leverage"])
                    if why:
                        skip(why)
                    else:
                        _prepare_account(ex, symbol, st["leverage"])
                        params = {"clientOrderId": _client_id(key)}
                        if tp is not None:
                            params["takeProfit"] = tp
                        if sl is not None:
                            params["stopLoss"] = sl
                        order = ex.create_order(symbol, "market", "buy" if long_side else "sell", qty, None, params)
                        record.update({"status": "OPEN", "symbol": symbol, "qty": qty, "margin_usd": round(margin, 2),
                                       "order_id": order.get("id"), "opened_at": _now_iso(),
                                       "opened_ms": int(time.time() * 1000)})
                        print(f"[TRADER] {st['mode'].upper()} {record['direction']} {record['coin']} qty {qty} "
                              f"TP {tp} SL {sl} ордер {order.get('id')}")
        except Exception as e:
            record["status"], record["error"] = "ERROR", str(e)
            print(f"[TRADER] ❌ {record['coin']}: {e}")

        _save_record(record)
    except Exception as e:
        print(f"[TRADER] ❌ неожиданная ошибка: {e}")


# ----------------------------------------------------------------------------
# Сопровождение: результаты, срок жизни, сверка
# ----------------------------------------------------------------------------

def _has_position(ex, symbol):
    pos = ex.fetch_positions([symbol])
    return any(abs(float(p.get("contracts") or 0)) > 0 for p in pos)


def _closed_pnl_row(ex, symbol, since_ms):
    market = ex.market(symbol)
    resp = ex.privateGetV5PositionClosedPnl({"category": "linear", "symbol": market["id"], "limit": 20})
    rows = [r for r in ((resp.get("result") or {}).get("list") or []) if int(r.get("updatedTime") or 0) >= int(since_ms or 0)]
    return max(rows, key=lambda x: int(x.get("updatedTime") or 0)) if rows else None


def _reconcile_once():
    """Один раз за запуск: позиции на бирже, которых нет в журнале как OPEN, — просто в консоль
    (ручные позиции не трогаем)."""
    global _reconciled
    if _reconciled:
        return
    _reconciled = True
    st = _settings()
    if st["mode"] == "dry":
        return
    try:
        ex = _exchange(st["mode"])
        with _lock:
            known = {t.get("symbol") for t in _load_trades() if t.get("status") == "OPEN" and t.get("mode") == st["mode"]}
        for p in ex.fetch_positions():
            if abs(float(p.get("contracts") or 0)) > 0 and p.get("symbol") not in known:
                print(f"[TRADER] ⚠️ на бирже есть позиция вне журнала бота: {p.get('symbol')} "
                      f"{p.get('side')} {p.get('contracts')} — бот её не трогает")
    except Exception as e:
        print(f"[TRADER] ⚠️ сверка с биржей не удалась: {e}")


def _settle(t, ex):
    """Обновляет одну запись журнала. Возвращает True, если запись изменилась."""
    now_ms = int(time.time() * 1000)

    if t["status"] == "SENDING":
        # Процесс упал между записью в журнал и ответом биржи: смотрим, открылась ли позиция.
        if now_ms - int((datetime.datetime.fromisoformat(t["created_at"])).timestamp() * 1000) < 120_000:
            return False
        symbol = resolve_symbol(t["coin"], ex.markets)
        if symbol and _has_position(ex, symbol):
            t.update({"status": "OPEN", "symbol": symbol, "opened_ms": now_ms, "opened_at": _now_iso()})
        else:
            t.update({"status": "ERROR", "error": "ответ биржи не получен, позиции нет"})
        return True

    # status == OPEN
    if _has_position(ex, t["symbol"]):
        max_hold_ms = _settings()["max_hold_days"] * 86400_000
        if not t.get("hold_close_sent") and now_ms - int(t.get("opened_ms") or now_ms) >= max_hold_ms:
            ex.create_order(t["symbol"], "market", "sell" if t["direction"] == "LONG" else "buy",
                            t.get("qty"), None, {"reduceOnly": True})
            t["hold_close_sent"] = True
            print(f"[TRADER] ⏱ {t['coin']}: срок {_settings()['max_hold_days']:g} дн. — закрываю рыночным")
            return True
        return False

    r = _closed_pnl_row(ex, t["symbol"], t.get("opened_ms"))
    if r is None:
        return False  # позиция закрыта, но запись о результате ещё не появилась — повторим в следующий раз
    avg_in, avg_out = float(r["avgEntryPrice"]), float(r["avgExitPrice"])
    pct = (avg_out - avg_in) / avg_in * 100.0 if t["direction"] == "LONG" else (avg_in - avg_out) / avg_in * 100.0
    t.update({"status": "CLOSED", "entry_fill": avg_in, "exit_price": avg_out,
              "pnl_usd": float(r["closedPnl"]), "result_percent": round(pct, 2),
              "closed_by": "MAX_HOLD" if t.get("hold_close_sent") else None,
              "closed_at": datetime.datetime.fromtimestamp(int(r["updatedTime"]) / 1000, datetime.timezone.utc).isoformat()})
    print(f"[TRADER] закрыта {t['direction']} {t['coin']}: выход {avg_out}, {t['pnl_usd']}$ ({t['result_percent']}%)")
    return True


def sync_results():
    """Для открытых позиций смотрит на бирже: закрылись ли, пора ли закрыть по сроку; пишет реальный
    результат (цена выхода, прибыль в $ и %). Вызывать периодически (раз в 15 минут вместе со сканом)."""
    try:
        _reconcile_once()
        with _lock:
            todo = [dict(t) for t in _load_trades() if t.get("status") in ("OPEN", "SENDING") and t.get("mode") != "dry"]
        for t in todo:
            try:
                if _settle(t, _exchange(t["mode"])):
                    _save_record(t)
            except Exception as e:
                print(f"[TRADER] ⚠️ не удалось проверить {t.get('coin')}: {e}")
    except Exception as e:
        print(f"[TRADER] ❌ sync_results: {e}")


# ----------------------------------------------------------------------------
# Для сайта
# ----------------------------------------------------------------------------

def fetch_positions(mode=None):
    """Живые позиции бота на бирже (то, что видит биржа сейчас)."""
    mode = mode or _settings()["mode"]
    if mode == "dry":
        return []
    ex = _exchange(mode)
    out = []
    for p in ex.fetch_positions():
        contracts = abs(float(p.get("contracts") or 0))
        if contracts <= 0:
            continue
        info = p.get("info") or {}
        out.append({
            "coin": (p.get("symbol") or "").split("/")[0], "symbol": p.get("symbol"), "side": p.get("side"),
            "contracts": contracts, "entry": p.get("entryPrice"), "mark": p.get("markPrice"),
            "pnl_usd": p.get("unrealizedPnl"), "pnl_pct": p.get("percentage"), "leverage": p.get("leverage"),
            "tp": info.get("takeProfit") or None, "sl": info.get("stopLoss") or None,
            "liq": p.get("liquidationPrice"),
        })
    return out


def fetch_balance(mode=None):
    """Баланс USDT на бирже (всего, свободно)."""
    mode = mode or _settings()["mode"]
    if mode == "dry":
        return None
    usdt = _exchange(mode).fetch_balance().get("USDT") or {}
    return {"total": usdt.get("total"), "free": usdt.get("free"), "used": usdt.get("used")}