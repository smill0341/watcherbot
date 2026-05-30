import time
import datetime
import threading
import pandas as pd
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from modules.cryptano.crypto_utils import calculate_rsi, exchange, get_top_100_coins
from modules.cryptano.market_cache import load_markets_cached
from modules.cryptano.regime import detect_market_regime
from modules.storage import load_json

# ================= Настройки фильтров =================
TIMEFRAME = "4h"
VOLUME_MULTIPLIER = 1.6  # Мягкий фильтр объема x1.2+
COOLDOWN_HOURS = 4       # Не спамить одной монетой 4 часа после сигнала
SCAN_INTERVAL = 1800      # Запуск сканирования каждые 15 минут (900 сек)
MAX_LIGHT_SCAN_WORKERS = 8

cooldown_cache = {}
_scan_lock = threading.Lock()


def _manual_light_setup(symbol):
    try:
        if not symbol.endswith('/USDT') or ':' in symbol:
            return None

        coin_name = symbol.split('/')[0]
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=40)
        if len(ohlcv) < 30:
            return None

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["rsi"] = calculate_rsi(df)

        last_candle = df.iloc[-1]
        current_price = last_candle["close"]
        rsi = last_candle["rsi"]

        recent_vol = last_candle["volume"]
        avg_vol = df["volume"].iloc[-25:-1].mean()
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

        if vol_ratio < VOLUME_MULTIPLIER:
            return None

        recent_high = df["high"].tail(30).max()
        recent_low = df["low"].tail(30).min()
        range_size = recent_high - recent_low
        if range_size <= 0:
            return None

        pos_pct = ((current_price - recent_low) / range_size) * 100
        trend, setup_info = "", ""

        if pos_pct > 80 and rsi > 55:
            trend, setup_info = "Weak/Strong Bear", "Near Resistance (Top 20%)"
        elif pos_pct < 20 and rsi < 45:
            trend, setup_info = "Weak/Strong Bull", "Near Support (Bottom 20%)"

        if not trend:
            return None

        return {
            "coin": coin_name,
            "trend": trend,
            "setup_info": setup_info,
            "pos_pct": pos_pct,
            "vol_ratio": vol_ratio,
            "rsi": rsi,
        }
    except Exception:
        return None


def _auto_light_setup(symbol):
    try:
        coin_name = symbol.split("/")[0]
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=250)
        if len(ohlcv) < 35:
            return None

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

        recent_volume = df["volume"].iloc[-1]
        avg_volume = df["volume"].iloc[-25:-5].mean()
        vol_ratio = float(recent_volume / avg_volume if avg_volume > 0 else 1.0)
        if vol_ratio < VOLUME_MULTIPLIER:
            return None

        recent_30 = df.tail(30)
        local_max = float(recent_30["high"].max())
        local_min = float(recent_30["low"].min())
        range_size = local_max - local_min
        if range_size == 0:
            return None

        pos_pct = ((current_price - local_min) / range_size) * 100

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
            return None

        is_long_setup = ("Bull" in trend) and (pos_pct < 20) and (rsi < 45)
        dist_to_support = ((current_price - local_min) / current_price) * 100

        is_short_setup = ("Bear" in trend) and (pos_pct > 80) and (rsi > 55)
        dist_to_res = ((local_max - current_price) / current_price) * 100

        if not ((is_long_setup and dist_to_support <= 2.0) or (is_short_setup and dist_to_res <= 2.0)):
            return None

        setup_info = f"Near support (+{dist_to_support:.1f}%)" if is_long_setup else f"Near resistance (-{dist_to_res:.1f}%)"
        market_regime = detect_market_regime(current_price, rsi, vol_ratio, ma30)

        return {
            "coin": coin_name,
            "trend": trend,
            "setup_info": setup_info,
            "pos_pct": pos_pct,
            "vol_ratio": vol_ratio,
            "rsi": rsi,
            "market_regime": market_regime,
        }
    except Exception as e:
        print(f"[LIGHT SCANNER] Ошибка парсинга пары {symbol}: {e}")
        return None

def run_manual_light_scan(bot, admin_chat_id):
    """
    ОДНОРАЗОВЫЙ ручной запуск легкого сканера по кнопке.
    """
    print("[SCANNER LOG] Начало ручного экспресс-сканирования Light...")
    
    # Захватываем лок, чтобы ручной поиск не подрался с фоновым автоматическим
    if not _scan_lock.acquire(blocking=False):
        print("[SCANNER WARNING] Сканер занят фоновым потоком!")
        bot.send_message(admin_chat_id, "⚠️ Сканер сейчас занят фоновым мониторингом. Подожди минуту.")
        return

    try:
        markets = load_markets_cached(exchange)
        found_something = False
        
        print("[SCANNER LOG] Начинаю перебор монет по условиям Light...")

        symbols = [symbol for symbol in markets if symbol.endswith('/USDT') and ':' not in symbol]
        worker_count = min(MAX_LIGHT_SCAN_WORKERS, max(1, len(symbols)))
        setups = []

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_manual_light_setup, symbol) for symbol in symbols]
            for future in as_completed(futures):
                setup = future.result()
                if setup:
                    setups.append(setup)

        for setup in setups:
            coin_name = setup["coin"]
            
            found_something = True
            print(f"[SCANNER LOG] Найдена монета {coin_name}! Отправляю в Telegram.")
            msg = (
                f"🟡 *WATCH SIGNAL | {coin_name}*\n"
                f"• Trend: `{setup['trend']}`\n"
                f"• Условие: `{setup['setup_info']}`\n"
                f"• Волатильность: `x{setup['vol_ratio']:.1f}` | RSI: `{setup['rsi']:.1f}`\n"
                f"━━━━━━━━━━━━━━━"
            )
            bot.send_message(admin_chat_id, msg, parse_mode="Markdown")
            continue
                
        if not found_something:
            print("[SCANNER LOG] Монет по фильтру Light не найдено.")
            bot.send_message(admin_chat_id, "ℹ️ По легким фильтрам интересных монет сейчас нет. Рынок спокойный.")
        else:
            print("[SCANNER LOG] Ручной сканер Light успешно завершил работу.")
            bot.send_message(admin_chat_id, "✅ Ручной экспресс-скан Light завершен!")
            
    except Exception as e:
        print(f"[SCANNER ERROR] Ошибка внутри ручного Light-скана: {e}")
        bot.send_message(admin_chat_id, f"❌ Ошибка при сканировании: {e}")
    finally:
        _scan_lock.release()

def run_light_scanner(bot, admin_chat_id):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "config.json"))
 
    print("  🟡 Light скринер инициализирован!")

    while True:
        try:
            # Читаем общий конфиг. Если автобот СТОП — этот скринер тоже засыпает
            config = load_json(config_path, default={})
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

            eligible_symbols = []
            for symbol in coins:
                coin_name = symbol.split("/")[0]
                if coin_name in cooldown_cache:
                    if (now - cooldown_cache[coin_name]).total_seconds() < (COOLDOWN_HOURS * 3600):
                        continue
                eligible_symbols.append(symbol)

            worker_count = min(MAX_LIGHT_SCAN_WORKERS, max(1, len(eligible_symbols)))
            setups = []

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(_auto_light_setup, symbol) for symbol in eligible_symbols]
                for future in as_completed(futures):
                    setup = future.result()
                    if setup:
                        setups.append(setup)

            for setup in setups:
                coin_name = setup["coin"]
                market_regime = setup["market_regime"]

                if market_regime == "EXTREME_PUMP":
                    msg = (
                        f"🚨 *WATCH SIGNAL | {coin_name} | EXTREME PUMP*\n"
                        f"• Trend: `{setup['trend']}` | Position: `{setup['pos_pct']:.0f}%`\n"
                        f"• Volume: `x{setup['vol_ratio']:.1f}` | RSI: `{setup['rsi']:.1f}`\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"⚠️ Цена в космосе (Price Discovery). Лимитки запрещены!\n"
                        f"🎯 _Монета передана снайперу (Live M15) для поиска SHORT после разворота._"
                    )
                    bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

                elif market_regime == "EXTREME_DUMP":
                    msg = (
                        f"🩸 *WATCH SIGNAL | {coin_name} | EXTREME DUMP*\n"
                        f"• Trend: `{setup['trend']}` | Position: `{setup['pos_pct']:.0f}%`\n"
                        f"• Volume: `x{setup['vol_ratio']:.1f}` | RSI: `{setup['rsi']:.1f}`\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"⚠️ Свободное падение (Падающий нож). Покупки вслепую запрещены!\n"
                        f"🎯 _Монета передана снайперу (Live M15) для поиска LONG после разворота._"
                    )
                    bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

                else:
                    msg = (
                        f"🟡 *WATCH SIGNAL | {coin_name}*\n"
                        f"• Trend: `{setup['trend']}`\n"
                        f"• Условие: `{setup['setup_info']}`\n"
                        f"• Волатильность: `x{setup['vol_ratio']:.1f}` | RSI: `{setup['rsi']:.1f}`\n"
                        f"━━━━━━━━━━━━━━━"
                    )
                    bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

                # Включаем кулдаун на 4 часа, чтобы сканер не мучил эту монету
                cooldown_cache[coin_name] = now
                continue
        finally:
            _scan_lock.release()

        # Пауза 15 минут до следующего полного прогона рынка
        time.sleep(SCAN_INTERVAL)
