import pandas as pd
from datetime import datetime
import sys

# Импорты твоих оригинальных функций
from modules.cryptano.utils.common import exchange, price_precision_from_market
from modules.cryptano.utils.indicators import get_market_state, get_cryptano_signal

if __name__ == "__main__":
    COIN = "bsb"
    START_TIME = "2026-06-05 00:00"
    END_TIME = "2026-06-08 23:59"
    
    symbol = f"{COIN.upper()}/USDT:USDT"
    print(f"🚀 ЧИСТЫЙ ТЕСТ CRITICAL (4H) | {symbol}")
    
    start_dt = datetime.strptime(START_TIME, "%Y-%m-%d %H:%M")
    end_dt = datetime.strptime(END_TIME, "%Y-%m-%d %H:%M")
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)
    
    try:
        # =========================================================
        # ИНИЦИАЛИЗАЦИЯ БИРЖИ И МАРКЕТОВ (Железобетонно)
        # =========================================================
        print("📥 Загружаю спецификации рынков Bybit...")
        markets_data = exchange.load_markets()
        if markets_data is not None:
            exchange.markets = markets_data
        else:
            exchange.markets = {}
            
        # Безопасно вытягиваем инфу по монете
        if isinstance(exchange.markets, dict) and symbol in exchange.markets:
            market_info = exchange.markets[symbol]
        else:
            market_info = {}
            
        if not market_info:
            print(f"⚠️ Предупреждение: Не нашли {symbol} в сетке. Ставим точность 4.")
            price_precision = 4
        else:
            price_precision = price_precision_from_market(market_info)
        
        # =========================================================
        # СКАНИРОВАНИЕ ИСТОРИИ (ТОЛЬКО 4H)
        # =========================================================
        print("\n" + "="*50)
        print(" 🕵️ НАЧАЛО ПОИСКА АНОМАЛИЙ CRITICAL")
        print("="*50)
        
        fetch_since_4h = start_ts - (250 * 4 * 60 * 60 * 1000) 
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", since=fetch_since_4h, limit=300)
        df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])
        
        found_signals = 0
        
        for i in range(200, len(df_4h) + 1):
            df_slice = df_4h.iloc[:i].copy()
            current_ts = df_slice["timestamp"].iloc[-1]
            
            if current_ts < start_ts: continue
            if current_ts > end_ts: break
                
            current_price = float(df_slice["close"].iloc[-1])
            time_str = datetime.fromtimestamp(current_ts / 1000).strftime('%Y-%m-%d %H:%M')
            
            market_data = get_market_state(df_slice, current_price)
            trend_code = market_data.get("trend_code", "RANGE")
            
            if trend_code == "BULL": crit_rsi_low, crit_rsi_high, crit_vol = 35, 85, 2.0
            elif trend_code == "BEAR": crit_rsi_low, crit_rsi_high, crit_vol = 20, 70, 2.5
            else: crit_rsi_low, crit_rsi_high, crit_vol = 25, 75, 3.0
                
            signal_data = get_cryptano_signal(
                df=df_slice, current_price=current_price, price_precision=price_precision,
                scan_type="auto", rsi_high=crit_rsi_high, rsi_low=crit_rsi_low, volume_multiplier=crit_vol
            )
            
            if signal_data and signal_data.get("type") == "SHORT_PUMP":
                found_signals += 1
                
                # Забираем чистые зоны
                zone_1 = signal_data.get("zone_1", 0)
                zone_2 = signal_data.get("zone_2", 0)
                
                print(f"🚨 [{time_str}] ПАМП! Цена на 4H свече: {current_price:.5f}")
                print(f"   🎯 Расчет: Зона 1 (Малая) = {zone_1:.5f} | Зона 2 (Пик) = {zone_2:.5f}")
                print("-" * 50)
                
        if found_signals == 0:
            print("❌ В указанном временном окне Критикал не выдал сигналов.")
            
    except Exception as e:
        print(f"💥 Ошибка выполнения теста: {e}")