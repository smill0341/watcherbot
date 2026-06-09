# modules/cryptano/watcher_plan.py

import time
import pandas as pd
import gc
import traceback
from modules.cryptano.utils.common import calculate_rsi, exchange, format_price as fmt_p, price_precision_from_market
from modules.cryptano.utils.market_cache import load_markets_cached

SCAN_COINS_LIMIT = 150

def check_manual_extreme(coin, direction):
    """
    Делает умный срез графика M15. Ищет пик/дно в последних свечах 
    и проверяет, не произошел ли разворот на основе SFP (ликвидности).
    """
    try:
        start_time = time.time()
        api_queries = 2

        coin = coin.upper().replace("USDT", "").replace("/", "").strip()
        symbol = f"{coin}/USDT"
        
        print(f"\n[WATCHER DEBUG] 🚀 Запуск ручного анализа: {symbol} | Направление: {direction}")
        
        markets = load_markets_cached(exchange)
        if symbol not in markets:
            symbol_fut = f"{coin}/USDT:USDT"
            if symbol_fut in markets:
                symbol = symbol_fut
            else:
                print(f"[WATCHER DEBUG] ❌ Ошибка: {symbol} не найдена на бирже.")
                return False, f"❌ Монета *{coin}* не найдена на Bybit."

        market_info = markets[symbol]
        price_precision = price_precision_from_market(market_info)

        print(f"[WATCHER DEBUG] 📥 Скачиваем M15 свечи для {symbol}...")
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=120)
        if len(ohlcv) < 16:
            print(f"[WATCHER DEBUG] ⚠️ Недостаточно данных ({len(ohlcv)} свечей)")
            return False, f"⚠️ Недостаточно данных по монете {coin} на M15."

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["rsi"] = calculate_rsi(df)

        current_candle = df.iloc[-1] # Самая последняя свеча (онлайн)
        current_price = float(current_candle["close"])
        
        # Отрезаем незакрытую (живую) свечу, работаем только с фактами
        df_closed = df.iloc[:-1]
        
        print("[WATCHER DEBUG] 🧠 Вызов индикаторов analyze_extreme_pattern...")
        from modules.cryptano.utils.indicators import analyze_extreme_pattern
        # Получаем новые ключи статусов
        result = analyze_extreme_pattern(df_closed, direction, current_price, price_precision)
        
        print(f"[WATCHER DEBUG] 📊 Результат от индикаторов: {result}")

        trigger_fired = result.get("trigger_fired", False)
        rsi_filter_passed = result.get("rsi_filter_passed", False)
        volume_climax = result.get("volume_climax", False)
        trigger_type = result.get("trigger_type", "НЕТ")
        rsi_value = result.get("rsi_value", 50.0)
        vol_ratio = result.get("vol_ratio", 1.0)

        sl_price = result.get("sl_price", 0.0)
        tp1_price = result.get("tp1_price", 0.0)
        tp2_price = result.get("tp2_price", 0.0)
        tp3_price = result.get("tp3_price", 0.0)

        # 🚀 ГЛАВНОЕ ПРАВИЛО ВХОДА: Есть триггер (SFP/Пинбар) И пройден фильтр RSI
        is_ready = trigger_fired and rsi_filter_passed
        
        icon = "🔴" if direction == "SHORT" else "🟢"
        status_text = "ВХОД! УСЛОВИЯ ВЫПОЛНЕНЫ" if is_ready else "ЖДЕМ (Слабость не подтверждена)"
        
        report = f"{icon} {coin} | WATCHER {direction}\n\n"
        report += f"💰 Текущая цена: {fmt_p(current_price)}\n"
        report += f"📊 Статус: {status_text}\n\n"
        
        report += f" 👀 Metrics:\n"
        
        # 1. Триггер
        trigger_icon = "⚡️" if trigger_fired else "⏳"
        report += f"{trigger_icon} Триггер: {trigger_type}\n"
        
        # 2. RSI Предохранитель
        rsi_icon = "🔥" if rsi_filter_passed else "🧊"
        rsi_status = "Пройден" if rsi_filter_passed else "Остыл/Не дошел"
        report += f"{rsi_icon} RSI: {rsi_value} ({rsi_status})\n"
        
        # 3. Объемный усилитель
        vol_icon = "💥" if volume_climax else "📊"
        vol_text = f"Аномалия x{vol_ratio}" if volume_climax else f"Обычный x{vol_ratio}"
        report += f"{vol_icon} Объем: {vol_text}\n\n"
        
        if is_ready:
            report += f"📝 WATCHER PLAN:\n"
            report += f"• Entry: ({fmt_p(current_price)})\n"
            report += f"• TP1: {fmt_p(tp1_price)}\n"
            report += f"• TP2: {fmt_p(tp2_price)}\n"
            report += f"• TP3: {fmt_p(tp3_price)}\n"
            report += f"• Sl: {fmt_p(sl_price)}\n"

        elapsed_time = time.time() - start_time
        print(f"[WATCHER DEBUG] ✅ Проверка {coin} успешно завершена за {elapsed_time:.2f} сек.\n")

        del df
        gc.collect()

        return is_ready, report

    except Exception as e:
        print(f"\n[WATCHER ERROR] ❌ КРИТИЧЕСКАЯ ОШИБКА В РУЧНОМ СКАНЕ ({coin}): {e}")
        traceback.print_exc()  # Печатает полный стек вызовов ошибки в терминал
        return False, f"❌ Ошибка ручного экспресс-анализа {coin}: {e}"