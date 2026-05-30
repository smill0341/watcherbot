import asyncio
import datetime
import pandas as pd
import os
import ccxt.async_support as ccxt_async
from modules.cryptano.crypto_utils import calculate_rsi, get_top_100_coins
from modules.cryptano.regime import detect_market_regime
from modules.storage import load_json

# ================= Настройки фильтров =================
TIMEFRAME = "4h"
LIGHT_RSI_HIGH = 65
LIGHT_RSI_LOW = 35
LIGHT_VOLUME_MULTIPLIER = 1.5
SCAN_INTERVAL = 1800
COOLDOWN_HOURS = 4

async_exchange = ccxt_async.bybit({
    'enableRateLimit': True,
    'options': {
        'defaultType': 'linear'
    }
})

cooldown_cache = {}
_scan_lock = asyncio.Lock()

async def analyze_coin(symbol):
    try:
        coin_name = symbol.split("/")[0]
        ohlcv = await async_exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=250)
        if len(ohlcv) < 35:
            return None

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["rsi"] = calculate_rsi(df)

        last_row = df.iloc[-1]
        current_price = float(last_row["close"])
        rsi = float(last_row["rsi"])

        recent_volume = df["volume"].iloc[-1]
        avg_volume = df["volume"].iloc[-25:-5].mean()
        vol_ratio = float(recent_volume / avg_volume if avg_volume > 0 else 1.0)

        # Блок 1 (LIGHT)
        if not ((rsi < LIGHT_RSI_LOW or rsi > LIGHT_RSI_HIGH) and vol_ratio >= LIGHT_VOLUME_MULTIPLIER):
            return None

        # Блок 2 (DEEP)
        df["ma7"] = df["close"].rolling(window=7).mean()
        df["ma30"] = df["close"].rolling(window=30).mean()
        df["ma200"] = df["close"].rolling(window=200).mean()

        last_row = df.iloc[-1]
        current_price = float(last_row["close"])
        ma7 = float(last_row["ma7"])
        ma30 = float(last_row["ma30"])
        ma200 = float(last_row["ma200"])

        recent_30 = df.tail(30)
        local_max = float(recent_30["high"].max())
        local_min = float(recent_30["low"].min())
        range_size = local_max - local_min
        if range_size <= 0:
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

        market_regime = detect_market_regime(current_price, rsi, vol_ratio, ma30)

        return {
            "coin": coin_name,
            "trend": trend,
            "pos_pct": pos_pct,
            "vol_ratio": vol_ratio,
            "rsi": rsi,
            "market_regime": market_regime,
        }
    except Exception as e:
        print(f"[LIGHT SCANNER] Ошибка парсинга пары {symbol}: {e}")
        return None

async def run_manual_light_scan(bot, admin_chat_id):
    print("[SCANNER LOG] Начало ручного экспресс-сканирования Light...")
    
    if _scan_lock.locked():
        print("[SCANNER WARNING] Сканер занят фоновым потоком!")
        bot.send_message(admin_chat_id, "⚠️ Сканер сейчас занят фоновым мониторингом. Подожди минуту.")
        return

    async with _scan_lock:
        try:
            found_something = False
            print("[SCANNER LOG] Начинаю перебор монет по условиям Light...")

            await async_exchange.load_markets()
            symbols = get_top_100_coins() 
            
            tasks = [analyze_coin(symbol) for symbol in symbols]
            results = await asyncio.gather(*tasks)
            setups = [res for res in results if res]

            for setup in setups:
                coin_name = setup["coin"]
                found_something = True
                print(f"[SCANNER LOG] Найдена монета {coin_name}! Отправляю в Telegram.")
                
                msg = (
                    f"🟡 *WATCH SIGNAL | {coin_name}*\n\n"
                    f"* [ LIGHT SCANNER ] *\n"
                    f"• RSI: `{setup['rsi']:.1f}`\n"
                    f"• Vol: `x{setup['vol_ratio']:.1f}`\n\n"
                    f"* [ DEEP ANALYZER ] *\n"
                    f"• Trend: `{setup['trend']}`\n"
                    f"• Regime: `{setup['market_regime']}`\n"
                    f"━━━━━━━━━━━━━━━"
                )
                bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

            if not found_something:
                print("[SCANNER LOG] Монет по фильтру Light не найдено.")
                bot.send_message(admin_chat_id, "ℹ️ По легким фильтрам интересных монет сейчас нет. Рынок спокойный.")
            else:
                print("[SCANNER LOG] Ручной сканер Light успешно завершил работу.")
                bot.send_message(admin_chat_id, "✅ Ручной экспресс-скан Light завершен!")
                
        except Exception as e:
            print(f"[SCANNER ERROR] Ошибка внутри ручного Light-скана: {e}")
            bot.send_message(admin_chat_id, f"❌ Ошибка при сканировании: {e}")

async def run_light_scanner(bot, admin_chat_id):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "config.json"))
 
    print("  🟡 Light скринер инициализирован!")

    while True:
        try:
            config = load_json(config_path, default={})
            if config.get("crypto", {}).get("status") != "RUNNING":
                await asyncio.sleep(30)
                continue
        except Exception as e:
            print(f"[LIGHT SCANNER] ❌ Ошибка чтения конфига: {e}")
            await asyncio.sleep(30)
            continue

        if _scan_lock.locked():
            await asyncio.sleep(30)
            continue

        async with _scan_lock:
            try:
                await async_exchange.load_markets()
                coins = get_top_100_coins()
                now = datetime.datetime.now()

                eligible_symbols = []
                for symbol in coins:
                    coin_name = symbol.split("/")[0]
                    if coin_name in cooldown_cache:
                        if (now - cooldown_cache[coin_name]).total_seconds() < (COOLDOWN_HOURS * 3600):
                            continue
                    eligible_symbols.append(symbol)

                tasks = [analyze_coin(symbol) for symbol in eligible_symbols]
                results = await asyncio.gather(*tasks)
                setups = [res for res in results if res]

                for setup in setups:
                    coin_name = setup["coin"]
                    market_regime = setup["market_regime"]

                    if market_regime == "EXTREME_PUMP":
                        msg = (
                            f"🚨 *WATCH SIGNAL | {coin_name} | EXTREME PUMP*\n\n"
                            f"* [ LIGHT SCANNER ] *\n"
                            f"• RSI: `{setup['rsi']:.1f}`\n"
                            f"• Vol: `x{setup['vol_ratio']:.1f}`\n\n"
                            f"* [ DEEP ANALYZER ] *\n"
                            f"• Trend: `{setup['trend']}`\n"
                            f"• Regime: `{setup['market_regime']}`\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"⚠️ Цена в космосе (Price Discovery). Лимитки запрещены!\n"
                            f"🎯 _Монета передана снайперу (Live M15) для поиска SHORT после разворота._"
                        )
                        bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

                    elif market_regime == "EXTREME_DUMP":
                        msg = (
                            f"🩸 *WATCH SIGNAL | {coin_name} | EXTREME DUMP*\n\n"
                            f"* [ LIGHT SCANNER ] *\n"
                            f"• RSI: `{setup['rsi']:.1f}`\n"
                            f"• Vol: `x{setup['vol_ratio']:.1f}`\n\n"
                            f"* [ DEEP ANALYZER ] *\n"
                            f"• Trend: `{setup['trend']}`\n"
                            f"• Regime: `{setup['market_regime']}`\n"
                            f"━━━━━━━━━━━━━━━\n"
                            f"⚠️ Свободное падение (Падающий нож). Покупки вслепую запрещены!\n"
                            f"🎯 _Монета передана снайперу (Live M15) для поиска LONG после разворота._"
                        )
                        bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

                    else:
                        msg = (
                            f"🟡 *WATCH SIGNAL | {coin_name}*\n\n"
                            f"* [ LIGHT SCANNER ] *\n"
                            f"• RSI: `{setup['rsi']:.1f}`\n"
                            f"• Vol: `x{setup['vol_ratio']:.1f}`\n\n"
                            f"* [ DEEP ANALYZER ] *\n"
                            f"• Trend: `{setup['trend']}`\n"
                            f"• Regime: `{setup['market_regime']}`\n"
                            f"━━━━━━━━━━━━━━━"
                        )
                        bot.send_message(admin_chat_id, msg, parse_mode="Markdown")

                    cooldown_cache[coin_name] = now
            except Exception as e:
                print(f"[LIGHT SCANNER ERROR] {e}")

        await asyncio.sleep(SCAN_INTERVAL)