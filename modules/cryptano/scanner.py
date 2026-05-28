import time
import datetime
import threading
import pandas as pd
import ccxt
import json
import os

# Импортируем функцию анализа из твоего файла analyzer.py
from modules.cryptano.analyzer import analyze_coin

# ================= Настройки фильтров =================
TIMEFRAME = "4h"
VOLUME_MULTIPLIER = 1.2  # Мягкий фильтр объема x1.2+
COOLDOWN_HOURS = 4       # Не спамить одной монетой 4 часа после сигнала
SCAN_INTERVAL = 900      # Запуск сканирования каждые 15 минут (900 сек)

exchange = ccxt.bybit({'enableRateLimit': True})
cooldown_cache = {}
_scan_lock = threading.Lock()

def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ema_up = up.ewm(com=period - 1, adjust=False).mean()
    ema_down = down.ewm(com=period - 1, adjust=False).mean()
    rs = ema_up / ema_down
    return 100 - (100 / (1 + rs))

def get_top_100_coins():
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        usdt_pairs = []
        for symbol, ticker in tickers.items():
            if symbol.endswith("/USDT:USDT") or symbol.endswith("/USDT"):
                try:
                    quote_volume = float(ticker.get('quoteVolume', 0) or 0)
                except (ValueError, TypeError):
                    quote_volume = 0.0
                
                if quote_volume > 5000000.0: # Фильтр ликвидности > $5,000,000
                    usdt_pairs.append((symbol, quote_volume))
                    
        usdt_pairs.sort(key=lambda x: x[1], reverse=True)
        return [pair[0] for pair in usdt_pairs[:100]]
    except Exception as e:
        print(f"[LIGHT SCANNER] Ошибка получения ТОП монет: {e}")
        return []

def run_light_scanner(bot, admin_chat_id):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "config.json"))

    print("=" * 50)
    print("  🟡 Новый легкий скринер (H4) запущен в фоне!")
    print("=" * 50)

    while True:
        try:
            # Читаем общий конфиг. Если автобот СТОП — этот скринер тоже засыпает
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if config.get("crypto", {}).get("status") != "RUNNING":
                time.sleep(30)
                continue
        except Exception as e:
            print(f"[LIGHT SCANNER] ❌ Ошибка чтения конфига: {e}")
            time.sleep(30)
            continue

        if not _scan_lock.acquire(blocking=False):
            time.sleep(30)
            continue

        try:
            coins = get_top_100_coins()
            now = datetime.datetime.now()

            for symbol in coins:
                coin_name = symbol.split("/")[0] # Превращаем "ETH/USDT" в "ETH"

                # Проверка кулдауна (памяти), чтобы не слать алерты по кругу
                if coin_name in cooldown_cache:
                    if (now - cooldown_cache[coin_name]).total_seconds() < (COOLDOWN_HOURS * 3600):
                        continue

                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=250)
                    if len(ohlcv) < 35:
                        continue
                        
                    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    
                    df["rsi"] = calculate_rsi(df)
                    df["ma7"] = df["close"].rolling(window=7).mean()
                    df["ma30"] = df["close"].rolling(window=30).mean()
                    df["ma200"] = df["close"].rolling(window=200).mean()
                    
                    last_row = df.iloc[-1]
                    current_price = float(last_row["close"])
                    rsi = float(last_row["rsi"])
                    ma7 = float(last_row["ma7"])
                    ma30 = float(last_row["ma30"])
                    ma200 = float(last_row["ma200"])
                    
                    # 1. Фильтр по объёму х1.2+
                    recent_volume = df["volume"].iloc[-1]
                    avg_volume = df["volume"].iloc[-25:-5].mean()
                    vol_ratio = float(recent_volume / avg_volume if avg_volume > 0 else 1.0)
                    
                    if vol_ratio < VOLUME_MULTIPLIER:
                        continue
                        
                    # 2. Позиция цены в диапазоне 30 свечей
                    recent_30 = df.tail(30)
                    local_max = float(recent_30["high"].max())
                    local_min = float(recent_30["low"].min())
                    range_size = local_max - local_min
                    
                    if range_size == 0: 
                        continue
                    pos_pct = ((current_price - local_min) / range_size) * 100

                    # 3. Определение направления тренда по скользящим средним
                    if not pd.isna(ma7) and not pd.isna(ma30) and not pd.isna(ma200):
                        if current_price > ma7 and ma7 > ma30 and ma30 > ma200:
                            trend = "Strong Bull"
                        elif current_price > ma30 and current_price < ma200:
                            trend = "Weak Bull"
                        elif current_price < ma7 and ma7 < ma30 and ma30 < ma200:
                            trend = "Strong Bear"
                        elif current_price < ma30 and current_price > ma200:
                            trend = "Weak Bear"
                        else:
                            trend = "Range"
                    else:
                        continue

                    # 4. Проверка условий (Твои правила из ТЗ)
                    is_long_setup = ("Bull" in trend) and (pos_pct < 20) and (rsi < 45)
                    dist_to_support = ((current_price - local_min) / current_price) * 100

                    is_short_setup = ("Bear" in trend) and (pos_pct > 80) and (rsi > 55)
                    dist_to_res = ((local_max - current_price) / current_price) * 100

                    trigger_active = False
                    setup_info = ""

                    if is_long_setup and dist_to_support <= 2.0:
                        trigger_active = True
                        setup_info = f"Near support (+{dist_to_support:.1f}%)"
                    elif is_short_setup and dist_to_res <= 2.0:
                        trigger_active = True
                        setup_info = f"Near resistance (-{dist_to_res:.1f}%)"

                    # 5. СТЫКОВКА: Если нашли кандидата — запускаем тяжелый анализатор
                    if trigger_active:
                        # Собираем красивую стартовую карточку скринера
                        header = (
                            f"🟡 *WATCH SIGNAL | {coin_name}*\n"
                            f"• Trend: `{trend}`\n"
                            f"• Position: `{pos_pct:.0f}%` (Диапазон 30 свечей)\n"
                            f"• Условие: `{setup_info}`\n"
                            f"• Волатильность: `x{vol_ratio:.1f}` | RSI: `{rsi:.1f}`\n"
                            f"🤖 _Запущен глубокий анализ AI Снайпера..._\n"
                            f"━━━━━━━━━━━━━━━\n\n"
                        )
                        
                        # Вот ТУТ происходит магия: вызываем функцию из analyzer.py
                        # Передаем чистый тикер (например, "BTC"), как и требует анализатор
                        analyzer_report = analyze_coin(coin_name)
                        
                        # Склеиваем карточку скринера и подробный отчет анализатора в один пост
                        full_msg = header + analyzer_report
                        
                        bot.send_message(admin_chat_id, full_msg, parse_mode="Markdown")
                        
                        # Блокируем монету на 4 часа, чтобы не спамить в чат
                        cooldown_cache[coin_name] = now
                        
                except Exception as e:
                    print(f"[LIGHT SCANNER] Ошибка парсинга пары {symbol}: {e}")
                    continue
        finally:
            _scan_lock.release()

        # Пауза 15 минут до следующего полного прогона рынка
        time.sleep(SCAN_INTERVAL)