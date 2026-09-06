import os
import datetime
from signal import signal
from modules.cryptano.utils.common import exchange
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.storage import load_json, save_json_atomic

# report.txt остаётся рядом с history.py — только json переехали в jsonbank/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from modules.cryptano.utils.paths import SIGNALS_FILE
REPORT_FILE = os.path.join(BASE_DIR, "report.txt")

# ================= СОХРАНЕНИЕ СИГНАЛА =================
def save_signal(signal: dict):
    """Сохраняет новый сигнал в signals.json"""
    signals = _load_signals()

    # Берем новые ключи (учитываем и старый, и новый формат Фибоначчи)
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

    record = {
        "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "coin": signal.get("coin"),
        "type": signal_type,
        "source": signal.get("source", "CRITICAL"), 
        "entry": entry,
        "target": target,
        "target_2": signal.get("target_2"), # 👈 Сохраняем TP2
        "target_3": signal.get("target_3"), # 👈 Сохраняем TP3
        "stop": stop,
        "status": "⏳",
        "result_percent": None
    }

    signals.append(record)
    _save_signals(signals)

# ================= ПРОВЕРКА РЕЗУЛЬТАТОВ =================
def check_and_update(bot, chat_id):
    """Проверяет открытые сигналы и обновляет статус"""
    signals = _load_signals()
    open_signals = [s for s in signals if s["status"] == "⏳"]

    if not open_signals:
        bot.send_message(chat_id, "📊 Нет открытых сигналов для проверки.")
        return

    bot.send_message(chat_id, f"⏳ Проверяю {len(open_signals)} открытых сигналов...")

    markets = load_markets_cached(exchange)
    updated = 0
    
    for signal in signals:
        if signal["status"] != "⏳":
            continue

        try:
            entry = float(signal["entry"])
            target = float(signal["target"])
            stop = float(signal.get("stop", 0))
        except (TypeError, ValueError):
            print(f"[ИСТОРИЯ] Пропускаю сигнал {signal.get('coin')} — некорректные данные")
            continue

        try:
            coin = signal['coin']
            symbol = f"{coin}/USDT"
            
            if symbol not in markets:
                symbol_fut = f"{coin}/USDT:USDT"
                if symbol_fut in markets:
                    symbol = symbol_fut
                else:
                    continue 

            ticker = exchange.fetch_ticker(symbol)
            last_price = ticker.get("last") or ticker.get("close")
            
            if last_price is None:
                continue
            current_price = float(last_price)

            if signal["type"] == "LONG":
                if current_price >= target:
                    signal["status"] = "✅"
                    signal["result_percent"] = round(((target - entry) / entry) * 100, 2)
                    updated += 1
                elif stop > 0 and current_price <= stop:
                    signal["status"] = "❌"
                    signal["result_percent"] = round(((stop - entry) / entry) * 100, 2)
                    updated += 1

            elif signal["type"] == "SHORT":
                if current_price <= target:
                    signal["status"] = "✅"
                    signal["result_percent"] = round(((entry - target) / entry) * 100, 2)
                    updated += 1
                elif stop > 0 and current_price >= stop:
                    signal["status"] = "❌"
                    signal["result_percent"] = round(((entry - stop) / entry) * 100, 2)
                    updated += 1

        except Exception as e:
            continue

    _save_signals(signals)
    print(f"[ИСТОРИЯ] Обновлено сигналов: {updated}")


# ================= ФОРМАТИРОВАНИЕ ИСТОРИИ (С ФАЙЛОМ) =================
# ================= ФОРМАТИРОВАНИЕ ИСТОРИИ (ДЛЯ ЧАТА) =================
def format_history(period: str) -> str:
    """Возвращает только короткое текстовое сообщение для чата"""
    signals = _load_signals()
    now = datetime.datetime.now()
    
    if period == "day":
        cutoff, p_name = now - datetime.timedelta(days=1), "последние 24 часа"
    elif period == "week":
        cutoff, p_name = now - datetime.timedelta(weeks=1), "последняя неделя"
    elif period == "month":
        cutoff, p_name = now - datetime.timedelta(days=30), "последний месяц"
    else:
        cutoff, p_name = None, "всё время"

    filtered = []
    for s in signals:
        try:
            date_str = s.get("date", "")
            if not date_str: continue
            sig_date = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            if cutoff is None or sig_date >= cutoff:
                filtered.append(s)
        except Exception:
            continue

    if not filtered:
        return f"📊 *История сигналов — {p_name}*\n\nСигналов не найдено."

    def get_stats_block(title, block_signals):
        if not block_signals: return f"{title}\nНет сигналов\n\n"
        closed = [s for s in block_signals if s.get("status") in ["✅", "❌"]]
        if not closed: return f"{title}\nСделки еще в работе\n\n"

        raw_total = len(closed)
        raw_wins = sum(1 for s in closed if s.get("status") == "✅")
        raw_wr = round((raw_wins / raw_total) * 100) if raw_total > 0 else 0

        coins_dict = {}
        for s in closed:
            c = s.get("coin", "UNKNOWN")
            if c not in coins_dict: coins_dict[c] = []
            coins_dict[c].append(s.get("status"))

        gr_total = len(coins_dict)
        gr_wins = sum(1 for st in coins_dict.values() if "✅" in st)
        gr_wr = round((gr_wins / gr_total) * 100) if gr_total > 0 else 0

        text = f"{title}\n📈 Все: {raw_wins}/{raw_total} ({raw_wr}%)\n🪙 Монеты: {gr_wins}/{gr_total} ({gr_wr}%)\n\n"
        return text

    crit = [s for s in filtered if s.get("source", "CRITICAL") == "CRITICAL"]
    light = [s for s in filtered if s.get("source") == "LIGHT"]
    watcher = [s for s in filtered if s.get("source") == "WATCHER"]

    msg = f"📊 *Результаты — {p_name}*\n\n"
    msg += get_stats_block("🚨 *CRITICAL FILTER*", crit)
    msg += get_stats_block("🎯 *WATCHER PLAN*", watcher)
    msg += get_stats_block("⚡️ *LIGHT FILTER*", light)

    open_signals = [s for s in filtered if s.get("status") == "⏳"]
    msg += f"⏳ *Всего открытых сделок:* {len(open_signals)}\n"
    
    return msg

# ================= ГЕНЕРАЦИЯ TXT ФАЙЛА =================
def generate_report_file(period: str) -> str | None:
    """Создает отформатированный .txt файл с кратким отчетом и детализацией столбиком"""
    signals = _load_signals()
    now = datetime.datetime.now()
    
    if period == "day":
        cutoff, p_name = now - datetime.timedelta(days=1), "последние 24 часа"
    elif period == "week":
        cutoff, p_name = now - datetime.timedelta(weeks=1), "последняя неделя"
    elif period == "month":
        cutoff, p_name = now - datetime.timedelta(days=30), "последний месяц"
    else:
        cutoff, p_name = None, "всё время"

    filtered = []
    for s in signals:
        try:
            date_str = s.get("date", "")
            if not date_str: continue
            sig_date = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M")
            if cutoff is None or sig_date >= cutoff:
                filtered.append(s)
        except Exception:
            continue

    if not filtered: return None

    # Вспомогательная функция для краткой статистики
    def get_stats_block(title, block_signals):
        if not block_signals: return f"{title}\nНет сигналов\n\n"
        closed = [s for s in block_signals if s.get("status") in ["✅", "❌"]]
        if not closed: return f"{title}\nСделки еще в работе\n\n"

        raw_total = len(closed)
        raw_wins = sum(1 for s in closed if s.get("status") == "✅")
        raw_wr = round((raw_wins / raw_total) * 100) if raw_total > 0 else 0

        coins_dict = {}
        for s in closed:
            c = s.get("coin", "UNKNOWN")
            if c not in coins_dict: coins_dict[c] = []
            coins_dict[c].append(s.get("status"))

        gr_total = len(coins_dict)
        gr_wins = sum(1 for st in coins_dict.values() if "✅" in st)
        gr_wr = round((gr_wins / gr_total) * 100) if gr_total > 0 else 0

        text = f"{title}\nВсе: {raw_wins}/{raw_total} ({raw_wr}%)\nМонеты: {gr_wins}/{gr_total} ({gr_wr}%)\n\n"
        return text

    crit = [s for s in filtered if s.get("source", "CRITICAL") == "CRITICAL"]
    light = [s for s in filtered if s.get("source") == "LIGHT"]
    watcher = [s for s in filtered if s.get("source") == "WATCHER"]
    open_signals = [s for s in filtered if s.get("status") == "⏳"]

    # Собираем текст
    lines = []
    lines.append(f"================ ПОЛНЫЙ ОТЧЕТ ({p_name.upper()}) ================\n\n")
    
    lines.append(get_stats_block("--- [ CRITICAL СТАТИСТИКА ] ---", crit))
    lines.append(get_stats_block("--- [ WATCHER СТАТИСТИКА ] ---", watcher))
    lines.append(get_stats_block("--- [ LIGHT СТАТИСТИКА ] ---", light))
    
    lines.append(f"Всего открытых сделок: {len(open_signals)}\n")
    if open_signals:
        open_coins = list(set([s.get("coin", "UNKNOWN") for s in open_signals]))
        lines.append(f"В работе: {', '.join(open_coins)}\n")
        
    lines.append("\n================ ДЕТАЛИЗАЦИЯ ПО СИГНАЛАМ ================\n")

    # 2. Добавляем детализацию СТОЛБИКОМ с группировкой по дате
    def format_list(title, block_signals):
        if not block_signals: return ""
        res = f"\n--- [ {title} ] ---\n"
        
        # Группируем по дням
        grouped = {}
        for s in reversed(block_signals): # reversed чтобы новые были сверху
            full_date = s.get('date', '')
            # Отрезаем время, оставляем только YYYY-MM-DD
            day = full_date.split(' ')[0] if full_date else "Неизвестная дата"
            if day not in grouped:
                grouped[day] = []
            grouped[day].append(s)
            
        # Формируем карточки
        for day, sigs in grouped.items():
            res += f"\n📅 ДЕНЬ: {day}\n"
            res += "=" * 25 + "\n"
            for s in sigs:
                c = s.get('coin', 'UNK').ljust(6)
                stype = s.get('type', '-').ljust(5)
                status = s.get('status', '⏳')
                entry = str(s.get('entry', 0))
                
                # НОВЫЕ ПЕРЕМЕННЫЕ: Достаем все 3 тейка и метку источника
                tp1 = str(s.get('target', 0))
                tp2 = s.get('target_2')
                tp3 = s.get('target_3')
                full_date = s.get('date', '')
                source_tag = s.get('source', 'CRITICAL')
                
                # Склеиваем тейки для красивого вывода
                if tp2 and tp3:
                    target_str = f"{tp1} | {tp2} | {tp3}"
                else:
                    target_str = tp1
                
                # Собираем карточку без задвоений
                res += f"Время: {full_date}\n"
                res += f"[{status}] {c} | {stype} ({source_tag})\n"
                res += f"| Вход: {entry}\n"
                res += f"| Цели: {target_str}\n"
                
                if status in ["✅", "❌"]:
                    pct = s.get('result_percent', 0)
                    pct_str = f"+{pct}%" if pct > 0 else f"{pct}%"
                    res += f"| Итог: {pct_str}\n"
                else:
                    res += f"| Итог: ОЖИДАНИЕ\n"
                res += "-" * 25 + "\n"
        return res

    lines.append(format_list("CRITICAL FILTER", crit))
    lines.append(format_list("WATCHER PLAN", watcher))
    lines.append(format_list("LIGHT FILTER", light))

    full_text = "".join(lines)

    # Запись с принудительными переносами для Windows Блокнота
    with open(REPORT_FILE, "w", encoding="utf-8", newline='\r\n') as f:
        f.write(full_text)

    return REPORT_FILE


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def _load_signals() -> list:
    return load_json(SIGNALS_FILE, default=[]) or []

def _save_signals(signals: list):
    save_json_atomic(SIGNALS_FILE, signals, indent=2)