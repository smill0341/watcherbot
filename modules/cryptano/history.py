import os
import datetime
from signal import signal
from modules.cryptano.common import exchange
from modules.cryptano.market_cache import load_markets_cached
from modules.storage import load_json, save_json_atomic

# Путь к файлу сигналов рядом с history.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNALS_FILE = os.path.join(BASE_DIR, "signals.json")

# ================= СОХРАНЕНИЕ СИГНАЛА =================

def save_signal(signal: dict):
    """Сохраняет новый сигнал в signals.json"""
    signals = _load_signals()

    # Определяем тип, цену входа, цель и стоп
    if signal.get("type") == "SHORT_PUMP":
        entry = signal.get("entry_market")
        target = signal.get("take_profit")
        stop = signal.get("stop_loss")
        signal_type = "SHORT"
    else:
        entry = signal.get("price")
        target = signal.get("r1")
        stop = signal.get("stop_loss")
        signal_type = "LONG"

    record = {
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "coin": signal.get("coin"),
        "type": signal_type,
        "entry": entry,
        "target": target,
        "stop": stop,
        "status": "⏳",
        "result_percent": None
    }

    signals.append(record)
    _save_signals(signals)
    print(f"[ИСТОРИЯ] Сохранён сигнал: {record['coin']} {record['type']} @ {record['entry']}")


# ================= ПРОВЕРКА РЕЗУЛЬТАТОВ =================

def check_and_update(bot, chat_id):
    """Проверяет открытые сигналы и обновляет статус"""
    signals = _load_signals()
    open_signals = [s for s in signals if s["status"] == "⏳"]

    if not open_signals:
        bot.send_message(chat_id, "📊 Нет открытых сигналов для проверки.")
        return

    bot.send_message(chat_id, f"⏳ Проверяю {len(open_signals)} открытых сигналов...")

    # Загружаем структуру рынков ОДИН РАЗ перед проверкой (для скорости)
    markets = load_markets_cached(exchange)

    updated = 0
    for signal in signals:
        if signal["status"] != "⏳":
            continue

        try:
            entry = float(signal["entry"])
            target = float(signal["target"])
            stop = float(signal["stop"])
        except (TypeError, ValueError):
            print(f"[ИСТОРИЯ] Пропускаю сигнал {signal.get('coin')} — некорректные данные")
            continue

        try:
            coin = signal['coin']
            symbol = f"{coin}/USDT"
            
            # --- УМНЫЙ ПОИСК СИМВОЛА (СПОТ ИЛИ ФЬЮЧЕРС) ---
            if symbol not in markets:
                symbol_fut = f"{coin}/USDT:USDT"
                if symbol_fut in markets:
                    symbol = symbol_fut  # Переключаемся на фьючерс
                else:
                    continue  # Если монеты нет ни там, ни там - тихо пропускаем

            # Запрашиваем цену по правильному тикеру (спот или фьючерс)
            ticker = exchange.fetch_ticker(symbol)
            last_price = ticker.get("last") or ticker.get("close")
            
            if last_price is None:
                print(f"[ИСТОРИЯ] Нет цены для {signal['coin']}, пропускаю")
                continue
            current_price = float(last_price)

            if signal["type"] == "LONG":
                if current_price >= target:
                    signal["status"] = "✅"
                    signal["result_percent"] = round(((target - entry) / entry) * 100, 2)
                    updated += 1
                elif current_price <= stop:
                    signal["status"] = "❌"
                    signal["result_percent"] = round(((stop - entry) / entry) * 100, 2)
                    updated += 1

            elif signal["type"] == "SHORT":
                if current_price <= target:
                    signal["status"] = "✅"
                    signal["result_percent"] = round(((entry - target) / entry) * 100, 2)
                    updated += 1
                elif current_price >= stop:
                    signal["status"] = "❌"
                    signal["result_percent"] = round(((entry - stop) / entry) * 100, 2)
                    updated += 1

        except Exception as e:
            print(f"[ИСТОРИЯ] Ошибка проверки {signal['coin']}: {e}")
            continue

    _save_signals(signals)
    print(f"[ИСТОРИЯ] Обновлено сигналов: {updated}")
# ================= ФОРМАТИРОВАНИЕ ИСТОРИИ =================

def format_history(period: str) -> str:
    """Форматирует историю сигналов по периоду"""
    signals = _load_signals()

    now = datetime.datetime.now()
    if period == "day":
        cutoff = now - datetime.timedelta(days=1)
        period_name = "последние 24 часа"
    elif period == "week":
        cutoff = now - datetime.timedelta(weeks=1)
        period_name = "последняя неделя"
    elif period == "month":
        cutoff = now - datetime.timedelta(days=30)
        period_name = "последний месяц"
    else:
        cutoff = None
        period_name = "всё время"

    # Фильтруем по периоду
    filtered = []
    for s in signals:
        try:
            sig_date = datetime.datetime.strptime(s["date"], "%Y-%m-%d %H:%M")
            if cutoff is None or sig_date >= cutoff:
                filtered.append(s)
        except Exception:
            continue

    if not filtered:
        return f"📊 *История сигналов — {period_name}*\n\nСигналов не найдено."

    # Считаем статистику
    profit_signals = [s for s in filtered if s["status"] == "✅"]
    stop_signals = [s for s in filtered if s["status"] == "❌"]
    open_signals = [s for s in filtered if s["status"] == "⏳"]

    total_closed = len(profit_signals) + len(stop_signals)
    winrate = round((len(profit_signals) / total_closed) * 100) if total_closed > 0 else 0

    avg_profit = 0
    if profit_signals:
        avg_profit = round(sum(s["result_percent"] for s in profit_signals) / len(profit_signals), 2)

    # Формируем сообщение
    msg = f"📊 *История сигналов — {period_name}*\n\n"

    # Сначала открытые
    for s in open_signals:
        msg += (
            f"⏳ *{s['coin']}* | {s['type']} | ожидает\n"
            f"   Вход: `{s['entry']}` | Цель: `{s['target']}` | Стоп: `{s['stop']}`\n"
            f"   📅 {s['date']}\n\n"
        )

    # Потом закрытые
    for s in profit_signals + stop_signals:
        pct = f"+{s['result_percent']}%" if s['result_percent'] and s['result_percent'] > 0 else f"{s['result_percent']}%"
        msg += (
            f"{s['status']} *{s['coin']}* | {s['type']} | `{pct}`\n"
            f"   Вход: `{s['entry']}` → Цель: `{s['target']}`\n"
            f"   📅 {s['date']}\n\n"
        )

    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"Всего сигналов: {len(filtered)}\n"
    msg += f"✅ Профит: {len(profit_signals)} | ❌ Стоп: {len(stop_signals)} | ⏳ Ждём: {len(open_signals)}\n"
    if total_closed > 0:
        msg += f"📈 Винрейт: {winrate}%\n"
    if profit_signals:
        msg += f"💰 Средний профит: {avg_profit}%"

    return msg


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================

def _load_signals() -> list:
    return load_json(SIGNALS_FILE, default=[]) or []


def _save_signals(signals: list):
    save_json_atomic(SIGNALS_FILE, signals, indent=2)
