# modules/cryptano/pumpdump.py

import pandas as pd
from modules.cryptano.common import calculate_rsi, exchange, price_precision_from_market
from modules.cryptano.market_cache import load_markets_cached

def check_manual_extreme(coin, direction):
    """
    Делает умный срез графика M15. Ищет пик/дно в последних свечах 
    и проверяет, не произошел ли разворот относительно этого пика.
    """
    try:
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
        
        score = 0
        details = []
        sl_price = 0.0

        if direction == "SHORT":
            # ИЩЕМ ПИК (Абсолютный хай в последних 15 свечах)
            recent_df = df.tail(15)
            peak_idx = recent_df["high"].idxmax()
            peak_candle = df.loc[peak_idx]
            
            # 1. Свечной паттерн на пике (Был ли там Пин-бар или Поглощение после него?)
            body = abs(peak_candle["close"] - peak_candle["open"])
            upper_shadow = peak_candle["high"] - max(peak_candle["close"], peak_candle["open"])
            is_pin = upper_shadow > (body * 1.5)
            
            is_engulf = False
            if peak_idx + 1 < len(df): # Если после пика уже закрылась следующая свеча
                next_c = df.loc[peak_idx + 1]
                is_engulf = (peak_candle["close"] > peak_candle["open"]) and (next_c["close"] < next_c["open"]) and (next_c["close"] < peak_candle["open"])

            if is_pin or is_engulf:
                score += 1
                details.append("✅ Разворотный паттерн на самом пике пампа")
            else:
                details.append("❌ На пике не было явной разворотной свечи")

            # 2. Дивергенция объемов (Объем сейчас ниже, чем был на пике)
            if current_candle["volume"] < peak_candle["volume"]:
                score += 1
                details.append("✅ Объемы покупателей иссякли после пика")
            else:
                details.append("❌ Объемы все еще аномально высокие")

            # 3. Разворот RSI от пика
            if current_candle["rsi"] < peak_candle["rsi"] and peak_candle["rsi"] > 70:
                score += 1
                details.append(f"✅ RSI упал от пика ({peak_candle['rsi']:.1f} -> {current_candle['rsi']:.1f})")
            else:
                details.append(f"❌ RSI не показывает разворот ({current_candle['rsi']:.1f})")

            # 4. Слом структуры (Цена пробила минимум той свечи, которая сделала хай)
            if current_price < peak_candle["low"]:
                score += 1
                details.append("✅ Слом структуры (Пробит лой пиковой свечи)")
            else:
                details.append("❌ Слом локальной структуры отсутствует")

            verdict = "🔥 СИГНАЛ АКТИВЕН (3+ балла)!" if score >= 3 else "⛔️ ВХОДИТЬ РАНО"
            sl_price = round(float(peak_candle["high"]), price_precision)

        elif direction == "LONG":
            # ИЩЕМ ДНО (Абсолютный лой в последних 15 свечах)
            recent_df = df.tail(15)
            low_idx = recent_df["low"].idxmin()
            low_candle = df.loc[low_idx]
            
            # 1. Свечной паттерн на дне
            body = abs(low_candle["close"] - low_candle["open"])
            lower_shadow = min(low_candle["close"], low_candle["open"]) - low_candle["low"]
            is_hammer = lower_shadow > (body * 1.5)
            
            is_bull_engulf = False
            if low_idx + 1 < len(df):
                next_c = df.loc[low_idx + 1]
                is_bull_engulf = (low_candle["close"] < low_candle["open"]) and (next_c["close"] > next_c["open"]) and (next_c["close"] > low_candle["open"])

            if is_hammer or is_bull_engulf:
                score += 1
                details.append("✅ Разворотный паттерн (Молот/Поглощение) на дне")
            else:
                details.append("❌ На дне не было явной разворотной свечи")

            # 2. Volume Climax (Был ли огромный объем именно в момент падения на дно?)
            avg_vol = df["volume"].mean()
            if low_candle["volume"] > (avg_vol * 2.5):
                score += 1
                details.append("✅ Кульминация продаж (Кит выкупил дно на объеме)")
            else:
                details.append("❌ Нет кульминационного объема на дне")

            # 3. Загиб RSI вверх от дна
            if current_candle["rsi"] > low_candle["rsi"] and low_candle["rsi"] < 30:
                score += 1
                details.append(f"✅ RSI отскочил от дна ({low_candle['rsi']:.1f} -> {current_candle['rsi']:.1f})")
            else:
                details.append(f"❌ RSI все еще на дне ({current_candle['rsi']:.1f})")

            # 4. База (Текущая цена выше закрытия свечи, которая сделала дно)
            if current_price > low_candle["close"] and current_candle["low"] >= low_candle["low"]:
                score += 1
                details.append("✅ Формируется база (Дно больше не обновляется)")
            else:
                details.append("❌ Цена продолжает давить вниз")

            verdict = "🔥 СИГНАЛ АКТИВЕН (4 из 4)!" if score == 4 else "⛔️ ОПАСНО (Требуется строго 4 из 4)"
            sl_price = round(float(low_candle["low"]), price_precision)

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

        return report

    except Exception as e:
        return f"❌ Ошибка ручного экспресс-анализа {coin}: {e}"
