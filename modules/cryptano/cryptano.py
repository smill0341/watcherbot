import datetime
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from telebot import types
import threading

from modules.cryptano.crypto_utils import calculate_rsi, exchange, get_top_100_coins, price_precision_for_value
from modules.cryptano.history import save_signal
from modules.cryptano.market_cache import load_markets_cached
from modules.storage import load_json

_scan_lock = threading.Lock()

# ================= Настройки фильтров =================
RSI_HIGH = 70              
RSI_LOW = 30               
VOLUME_MULTIPLIER = 2.0    
TIMEFRAME = "4h"  
USE_FUTURES = True         
MAX_SCAN_WORKERS = 8

def calculate_atr(df, period=14):
    high_low = df["high"] - df["low"]
    high_cp = (df["high"] - df["close"].shift()).abs()
    low_cp = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calculate_pivot_points(df):
    last_row = df.iloc[-2]
    high = last_row["high"]
    low = last_row["low"]
    close = last_row["close"]
    
    pivot = (high + low + close) / 3
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    
    return pivot, r1, s1

def scan_market(scan_type="auto"):
    if not _scan_lock.acquire(blocking=False):
        print("⚠️ [КРИПТА] Сканирование уже идёт, пропускаю.")
        return []
    try:
        coins = get_top_100_coins()
        
        print(f"Начало сканирования рынка ({scan_type}). Всего ликвидных пар: {len(coins)}")
        
        # Сначала подгружаем структуру маркетов Bybit (один раз перед циклом, чтобы не спамить)
        markets = load_markets_cached(exchange)

        def analyze_symbol(symbol):
            try:
                # 1. Если монеты нет на бирже вообще - пропускаем
                if symbol not in markets:
                    return None
                
                # 2. РУБИЛЬНИК ФЬЮЧЕРСОВ
                # Если USE_FUTURES = False, то жестко отсекаем всё, что не спот
                if not USE_FUTURES:
                    if not markets[symbol].get('spot', False):
                        return None
                    if ':' in symbol:
                        return None
                    
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=250)
                if len(ohlcv) < 20:
                    return None
                    
                df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                
                df["rsi"] = calculate_rsi(df)
                df["atr"] = calculate_atr(df)
                df["ma30"] = df["close"].rolling(window=30).mean()
                df["ma200"] = df["close"].rolling(window=200).mean()
                
                last_row = df.iloc[-1]
                current_price = float(last_row["close"])
                # --- УМНОЕ ОКРУГЛЕНИЕ ЦЕН (ТВОЙ АЛГОРИТМ) ---
                price_precision = price_precision_for_value(
                    current_price,
                    one_to_ten_decimals=2,
                    small_extra_decimals=2,
                )
                # ----------------------------------------------
                rsi = float(last_row["rsi"])
                atr = float(last_row["atr"])
                ma30 = float(last_row["ma30"])
                
                recent_volume = df["volume"].iloc[-1]
                avg_volume = df["volume"].iloc[-25:-5].mean()
                vol_ratio = float(recent_volume / avg_volume if avg_volume > 0 else 1.0)
                
                coin_name = symbol.split("/")[0]
                
                is_rsi_high_trigger = (scan_type == "rsi_high" and rsi >= RSI_HIGH)
                is_short_pump_trigger = (
                    (scan_type in ["auto", "volume"] and rsi > 75.0 and vol_ratio >= 4.0)
                    or is_rsi_high_trigger
                )

                if is_short_pump_trigger:
                    entry_market = current_price
                    entry_limit = current_price + (atr * 1.5)
                    stop_loss = entry_limit + (atr * 0.5)
                    take_profit = ma30
                    
                    entry_market = round(entry_market, price_precision)
                    entry_limit = round(entry_limit, price_precision)
                    stop_loss = round(stop_loss, price_precision)
                    take_profit = round(take_profit, price_precision)
                    
                    return {
                        "coin": coin_name,
                        "type": "SHORT_PUMP",
                        "price": current_price,
                        "rsi": rsi,
                        "vol_ratio": vol_ratio,
                        "entry_market": entry_market,
                        "entry_limit": entry_limit,
                        "take_profit": take_profit,
                        "stop_loss": stop_loss
                    }
                    
                pivot, r1, s1 = calculate_pivot_points(df)
                
                s1 = round(float(s1), price_precision)
                r1 = round(float(r1), price_precision)
                stop_loss_long = round(s1 * 0.95, price_precision)
                
                is_rsi_trigger = (scan_type in ["rsi", "rsi_low"] and rsi <= RSI_LOW)
                is_vol_trigger = (scan_type == "volume" and vol_ratio >= VOLUME_MULTIPLIER)
                is_auto_trigger = (scan_type == "auto" and rsi <= 35.0 and vol_ratio >= VOLUME_MULTIPLIER)
                
                if is_rsi_trigger or is_vol_trigger or is_auto_trigger:
                    ma200 = float(last_row["ma200"]) if not pd.isna(last_row["ma200"]) else 0
                    if current_price > s1 and current_price > ma200:
                        return {
                            "coin": coin_name,
                            "type": "LONG_ROLLBACK",
                            "price": current_price,
                            "rsi": rsi,
                            "vol_ratio": vol_ratio,
                            "s1": s1,
                            "r1": r1,
                            "stop_loss": stop_loss_long
                        }
                        
            except Exception as e:
                print(f"Ошибка анализа {symbol}: {e}")
            return None

        results = []
        worker_count = min(MAX_SCAN_WORKERS, max(1, len(coins)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(analyze_symbol, symbol) for symbol in coins]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
                    save_signal(result)
                
        return results
    finally:
        _scan_lock.release()

def format_results(results, title):
    if not results:
        return f"📋 **{title}**\n\nВ данный момент подходящих монет не найдено."
    
    long_count = sum(1 for r in results if r.get("type") == "LONG_ROLLBACK")
    short_count = sum(1 for r in results if r.get("type") == "SHORT_PUMP")
    
    now = datetime.datetime.now().strftime("%d.%m.%y, %H:%M")
    msg = f"📋 **{title}**\n🕐 {now}\n\n"
    
    for r in results:
        if r["type"] == "SHORT_PUMP":
            msg += (
                f"🚨 *{r['coin']}* | КРИТИЧЕСКИЙ ПАМП (ШОРТ СЕТКОЙ)\n"
                f"💰 Цена: {r['price']} | 📊 RSI: {r['rsi']:.1f} | Объем: x{r['vol_ratio']:.2f}\n"
                f"🔴 Вход 1 (Рынок): {r['entry_market']}\n"
                f"🔴 Вход 2 (Лимитка): {r['entry_limit']}\n"
                f"🟢 Тейк-профит (MA30): {r['take_profit']}\n"
                f"⚠️ Защитный Стоп: {r['stop_loss']}\n\n"
            )
        else:
            msg += (
                f"🦈 *{r['coin']}* | ЛОНГ НА ОТКАТЕ\n"
                f"💰 Цена: {r['price']} | 📊 RSI: {r['rsi']:.1f} | Объем: x{r['vol_ratio']:.2f}\n"
                f"🟢 Вход на откат (S1): {r['s1']}\n"
                f"🔴 Выход (Цель R1): {r['r1']}\n"
                f"⚠️ Защитный Стоп-лосс: {r['stop_loss']}\n\n"
            )
    
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"📊 Итого: 🦈 Лонг: {long_count} | 🚨 Шорт: {short_count}"
    
    if long_count == 0:
        msg += "\n⚠️ Лонг-пар не найдено"
    if short_count == 0:
        msg += "\n⚠️ Шорт-пар не найдено"
        
    return msg

def get_crypto_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    btn_rsi = types.InlineKeyboardButton(text="📉 Фильтр по RSI (Перепроданность)", callback_data="crypto_rsi")
    btn_vol = types.InlineKeyboardButton(text="💰 Аномальный объем", callback_data="crypto_volume")
    btn_auto = types.InlineKeyboardButton(text="🔄 Полный фильтр (RSI + Vol)", callback_data="crypto_auto")
    keyboard.add(btn_rsi, btn_vol, btn_auto)
    return keyboard


# ================= ГЛАВНАЯ ТОЧКА ВХОДА ДЛЯ MAIN.PY =================
def process_crypto_command(text, bot, chat_id):
    print(f"[CRYPTANO LOG] Принята команда: '{text}'")
    bot.send_message(chat_id, "⏳ Сканирую ТОП-100 монет, подожди 30-60 секунд...")
    
    if text == "🔥 RSI > 70":
        print("[CRYPTANO] Запускаю сканирование перекупленности...")
        res = scan_market(scan_type="rsi_high")
        bot.send_message(chat_id, format_results(res, "Перекупленные активы"), parse_mode="Markdown")
    elif text == "🥶 RSI < 30":
        print("[CRYPTANO] Запускаю сканирование перепроданности...")
        res = scan_market(scan_type="rsi_low")
        bot.send_message(chat_id, format_results(res, "Перепроданные активы"), parse_mode="Markdown")
    elif text == "💰 Volume > x2":
        print("[CRYPTANO] Запускаю сканирование аномальных объемов...")
        res = scan_market(scan_type="volume")
        bot.send_message(chat_id, format_results(res, f"Аномальный объем (>{VOLUME_MULTIPLIER}x)"), parse_mode="Markdown")
    elif text == "⚡️ Critical фильтр":
        print("[CRYPTANO] Запускаю полный критический фильтр (RSI + Volume)...")
        res = scan_market(scan_type="auto")
        bot.send_message(chat_id, format_results(res, "Полный фильтр: RSI + Объемы"), parse_mode="Markdown")

    else:
        print(f"[CRYPTANO WARNING] Команда '{text}' пришла, но не распознана!")
        
def handle_crypto_callback(bot, call):
    chat_id = call.message.chat.id
    
    if call.data == "cryp_rsi":
        res = scan_market(scan_type="rsi")
    elif call.data == "cryp_vol":
        res = scan_market(scan_type="volume")
    elif call.data == "cryp_all":
        res = scan_market(scan_type="auto")
    else:
        return  # неизвестный callback — игнорируем
        
    bot.send_message(chat_id, format_results(res, "Результат сканирования"), parse_mode="Markdown")


def auto_scheduler(bot, admin_chat_id):
    import schedule
    import os

    # Правильный путь: cryptano.py лежит в modules/cryptano/, конфиг в корне
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "config.json"))

    print("  🪙  Critical скринер инициализирован!")

    def run_auto_scan():
        try:
            config = load_json(config_path, default={})
            if config.get("crypto", {}).get("status") != "RUNNING":
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [КРИПТА] Статус STOPPED — пропускаю скан.")
                return
        except Exception as e:
            print(f"[КРИПТА] ❌ Ошибка чтения конфига: {e} | Путь: {config_path}")
            return

        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [КРИПТА] Фоновый запуск сканирования рынка...")
        res = scan_market(scan_type="auto")
        if res:
            msg = format_results(res, "⏰ Авто-находка: Сильный RSI + Аномальный объем!")
            try:
                bot.send_message(admin_chat_id, msg, parse_mode="Markdown")
            except Exception as e:
                print(f"[КРИПТА] ❌ Ошибка автоотправки: {e}")
                try:
                    bot.send_message(admin_chat_id, f"❌ [КРИПТА] Ошибка:\n`{e}`", parse_mode="Markdown")
                except:
                    pass
        else:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [КРИПТА] Совпадений не найдено.")

    schedule.every(60).minutes.do(run_auto_scan)
    print(f"[КРИПТА] ⏱ Планировщик запущен. Следующий скан через 60 минут.")
    
    while True:
        schedule.run_pending()
        time.sleep(1)
