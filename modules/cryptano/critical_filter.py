import datetime
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from telebot import types
import threading

from modules.cryptano.utils.common import format_price as fmt_p, price_precision_from_market
from modules.cryptano.utils.crypto_utils import calculate_rsi, exchange, get_top_coins
from modules.cryptano.history import save_signal
from modules.cryptano.utils.indicators import get_cryptano_signal
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.storage import load_json

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
        print("⚠️ [Critical фильтр] Сканирование уже идёт, пропускаю.")
        return []
    try:
        coins = get_top_coins(limit=SCAN_COINS_LIMIT)
        start_time = time.time()
        total_processed_coins = len(coins) if coins else 0
        api_queries = total_processed_coins + 1
        
        print(f"Начало сканирования рынка ({scan_type}). Всего ликвидных пар: {total_processed_coins}")
        
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
                market_info = markets[symbol]
                price_precision = price_precision_from_market(market_info)
                
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
        worker_count = min(MAX_SCAN_WORKERS, max(1, total_processed_coins))
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
        print(f"\n [Critical фильтр] 📊 Скан завершен за {elapsed_time:.1f} сек.")
        print(f"[Critical фильтр] 🪙 Монет обработано: {total_processed_coins} | Найдено сетапов: {found_signals}")
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
                f"💰 Цена: {fmt_p(r['price'])} | 📊 RSI: {r['rsi']:.1f} | Объем: x{r['vol_ratio']:.2f}\n"
                f"🔴 Вход 1 (Рынок): {fmt_p(r['entry_market'])}\n"
                f"🔴 Вход 2 (Лимитка): {fmt_p(r['entry_limit'])}\n"
                f"🟢 Тейк-профит (MA30): {fmt_p(r['take_profit'])}\n"
                f"⚠️ Защитный Стоп: {fmt_p(r['stop_loss'])}\n\n"
            )
        else:
            msg += (
                f"🦈 *{r['coin']}* | ЛОНГ НА ОТКАТЕ\n"
                f"💰 Цена: {fmt_p(r['price'])} | 📊 RSI: {r['rsi']:.1f} | Объем: x{r['vol_ratio']:.2f}\n"
                f"🟢 Вход на откат (S1): {fmt_p(r['s1'])}\n"
                f"🔴 Выход (Цель R1): {fmt_p(r['r1'])}\n"
                f"⚠️ Защитный Стоп-лосс: {fmt_p(r['stop_loss'])}\n\n"
            )
    
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
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [Critical фильтр] Статус STOPPED — пропускаю скан.")
                return
        except Exception as e:
            print(f"[Critical фильтр] ❌ Ошибка чтения конфига: {e} | Путь: {config_path}")
            return

        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [Critical фильтр] Фоновый запуск сканирования рынка...")
        res = scan_market(scan_type="auto")
        if res:
            msg = format_results(res, "⏰ Авто-находка: Сильный RSI + Аномальный объем!")
            try:
                bot.send_message(admin_chat_id, msg, parse_mode="Markdown")
            except Exception as e:
                print(f"[Critical фильтр] ❌ Ошибка автоотправки: {e}")
                try:
                    bot.send_message(admin_chat_id, f"❌ [Critical фильтр] Ошибка:\n`{e}`", parse_mode="Markdown")
                except:
                    pass
        else:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [Critical фильтр] Совпадений не найдено.")

    schedule.every(60).minutes.do(run_auto_scan)
    print(f"[Critical фильтр] ⏱ Планировщик запущен.")
    
    while True:
        schedule.run_pending()
        time.sleep(1)
