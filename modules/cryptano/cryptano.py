import datetime
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from telebot import types
import threading

from modules.cryptano.crypto_utils import calculate_rsi, exchange, get_top_coins, price_precision_for_value
from modules.cryptano.history import save_signal
from modules.cryptano.indicators import get_cryptano_signal
from modules.cryptano.market_cache import load_markets_cached
from modules.storage import load_json

SCAN_COINS_LIMIT = 200

_scan_lock = threading.Lock()

# ================= Настройки фильтров =================
RSI_HIGH = 70              
RSI_LOW = 30               
VOLUME_MULTIPLIER = 2.0    
TIMEFRAME = "4h"  
USE_FUTURES = True         
MAX_SCAN_WORKERS = 8

def scan_market(scan_type="auto"):
    if not _scan_lock.acquire(blocking=False):
        print("⚠️ [КРИПТА] Сканирование уже идёт, пропускаю.")
        return []
    try:
        coins = get_top_coins(limit=SCAN_COINS_LIMIT)
        start_time = time.time()
        api_queries = len(coins) + 1
        
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
                
                last_row = df.iloc[-1]
                current_price = float(last_row["close"])
                # --- УМНОЕ ОКРУГЛЕНИЕ ЦЕН (ТВОЙ АЛГОРИТМ) ---
                price_precision = price_precision_for_value(
                    current_price,
                    one_to_ten_decimals=2,
                    small_extra_decimals=2,
                )
                # ----------------------------------------------
                
                coin_name = symbol.split("/")[0]
                
                signal_data = get_cryptano_signal(
                    df=df,
                    current_price=current_price,
                    price_precision=price_precision,
                    scan_type=scan_type,
                    rsi_high=RSI_HIGH,
                    rsi_low=RSI_LOW,
                    volume_multiplier=VOLUME_MULTIPLIER
                )

                if signal_data:
                    signal_data["coin"] = coin_name
                    return signal_data
                        
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
        elapsed_time = time.time() - start_time
        found_signals = len(results)
        print(f"\n[Critical фильтр] 📊 Скан завершен за {elapsed_time:.1f} сек.")
        print(f"[Critical фильтр] 🪙 Монет обработано: {len(coins)} | Найдено сетапов: {found_signals}")
        print(f"[Critical фильтр] 🌐 Запросов к API Bybit: {api_queries}\n")
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
    bot.send_message(chat_id, "⏳ Сканирую ТОП монет, ждите пару секунд...", parse_mode="Markdown")
    
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
    print(f"[КРИПТА] ⏱ Планировщик запущен.")
    
    while True:
        schedule.run_pending()
        time.sleep(1)
