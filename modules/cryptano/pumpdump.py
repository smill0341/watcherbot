# modules/cryptano/pumpdump.py

import time
import pandas as pd
from modules.cryptano.crypto_utils import calculate_rsi, exchange, price_precision_from_market
from modules.cryptano.market_cache import load_markets_cached

SCAN_COINS_LIMIT = 150

def check_manual_extreme(coin, direction):
    """
    Делает умный срез графика M15. Ищет пик/дно в последних свечах 
    и проверяет, не произошел ли разворот относительно этого пика.
    """
    try:
        start_time = time.time()
        api_queries = 2

        coin = coin.upper().replace("USDT", "").replace("/", "").strip()
        symbol = f"{coin}/USDT"
        
        markets = load_markets_cached(exchange)
        if symbol not in markets:
            symbol_fut = f"{coin}/USDT:USDT"
            if symbol_fut in markets:
                symbol = symbol_fut
            else:
                return f"❌ Монета *{coin}* не найдена на Bybit."

        market_info = markets[symbol]
        price_precision = price_precision_from_market(market_info)

        # Берем 30 свечей (7.5 часов истории)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=30)
        if len(ohlcv) < 10:
            return f"⚠️ Недостаточно данных по монете {coin} на M15."

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["rsi"] = calculate_rsi(df)

        current_candle = df.iloc[-1] # Самая последняя свеча (онлайн)
        current_price = round(float(current_candle["close"]), price_precision)
        
        from modules.cryptano.indicators import analyze_extreme_pattern
        result = analyze_extreme_pattern(df, direction, current_price, price_precision)

        score = result["score"]
        details = result["details"]
        sl_price = result["sl_price"]

        if direction == "SHORT":
            verdict = "🔥 СИГНАЛ АКТИВЕН (3+ балла)!" if score >= 3 else "⛔️ ВХОДИТЬ РАНО"
        elif direction == "LONG":
            verdict = "🔥 СИГНАЛ АКТИВЕН (4 из 4)!" if score == 4 else "⛔️ ОПАСНО (Требуется строго 4 из 4)"
        else:
            verdict = "❓ Направление неизвестно"

        # Собираем отчет
        report = (
            f"🌋 *СЛЕДОВАНИЕ ЗА ЭКСТРИМОМ: {coin}*\n"
            f"📈 Направление: *{direction}* | Цена онлайн: `{current_price}`\n"
            f"📊 Набрано: `{score} из 4 баллов` истощения\n"
            f"━━━━━━━━━━━━━━━\n"
            f" STATUS: *{verdict}*\n\n"
            f"🔍 *Анализ относительно пика/дна (M15):*\n"
        )
        
        for d in details:
            report += f" {d}\n"
        
        if (direction == "SHORT" and score >= 3) or (direction == "LONG" and score == 4):
            report += f"\n🎯 *ПЛАН ВХОДА С РЫНКА:*\n• Вход: `{current_price}`\n• Стоп-лосс: `{sl_price}`\n⚠️ _Рекомендуется заходить уменьшенным объёмом!_"
        else:
            report += f"\n⏳ Тренд еще силен или паттерн не сформирован."

        elapsed_time = time.time() - start_time
        print(f"\n[ЭКСТРЕМУМ-АНАЛИЗ] 📊 Проверка {coin} завершена за {elapsed_time:.2f} сек.")
        print(f"[ЭКСТРЕМУМ-АНАЛИЗ] 🌐 Запросов к API Bybit: {api_queries}\n")

        return report

    except Exception as e:
        return f"❌ Ошибка ручного экспресс-анализа {coin}: {e}"
