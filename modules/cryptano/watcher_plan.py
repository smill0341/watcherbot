# modules/cryptano/watcher_plan.py

import time
import pandas as pd
import gc
from modules.cryptano.utils.common import calculate_rsi, exchange, format_price as fmt_p, price_precision_from_market
from modules.cryptano.utils.market_cache import load_markets_cached

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
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=120)
        if len(ohlcv) < 10:
            return f"⚠️ Недостаточно данных по монете {coin} на M15."

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["rsi"] = calculate_rsi(df)

        current_candle = df.iloc[-1] # Самая последняя свеча (онлайн)
        current_price = float(current_candle["close"])
        
        from modules.cryptano.utils.indicators import analyze_extreme_pattern
        result = analyze_extreme_pattern(df, direction, current_price, price_precision)

        score = result["score"]
        details = result["details"]
        sl_price = result["sl_price"]
        tp1_price = result.get("tp1_price", 0.0)

        current_rsi = float(current_candle["rsi"])
        
        # 1. Слом структуры (BOS) дает +1 балл, а не блокирует
        structure_broken = any("✅ Есть слом структуры" in d for d in details)
        if structure_broken:
            score += 1
            
        # 2. Порог входа (MIN_SCORE = 3)
        MIN_SCORE = 3

        # 3. Мягкий RSI для M15
        rsi_ok = False
        if direction == "SHORT" and current_rsi >= 60:
            rsi_ok = True
        elif direction == "LONG" and current_rsi <= 40:
            rsi_ok = True

        is_ready = score >= MIN_SCORE and rsi_ok
        
        verdict = "🔥 СИГНАЛ АКТИВЕН" if is_ready else "⏳ Наблюдение (Ждем условия)"
        icon = "🔴" if direction == "SHORT" else "🟢"
        bos_str = "" if structure_broken else " (BOS missing)"
        
        report = f"{icon} *{coin} | WATCHER {direction}*\n\n"
        report += f"💰 Цена: `{fmt_p(current_price)}`\n"
        report += f"📊 Готовность: `{score}/5{bos_str}`\n\n"
        report += f"*{verdict}*\n\n"
        report += "👀 *Metrics:*\n"
        
        for d in details:
            report += f"{d}\n"
            
        if is_ready:
            report += f"\n🎯 *ПЛАН ВХОДА С РЫНКА:*\n• Вход: `{fmt_p(current_price)}`\n• Стоп-лосс: `{fmt_p(sl_price)}`\n• Тейк-профит 1: `{fmt_p(tp1_price)}` (Fibo 38.2%)"
        else:
            report += f"\n⏳ Пока наблюдаем. Не хватает баллов (нужно 3) или RSI не в зоне (нужно <40 для лонга, >60 для шорта)."

        elapsed_time = time.time() - start_time
        print(f"\n[WATCHER PLAN] 📊 Проверка {coin} завершена за {elapsed_time:.2f} сек.")
        print(f"[WATCHER PLAN] 🌐 Запросов к API Bybit: {api_queries}\n")

        del df
        gc.collect()

        return report

    except Exception as e:
        return f"❌ Ошибка ручного экспресс-анализа {coin}: {e}"
