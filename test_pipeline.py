import pandas as pd
from datetime import datetime
import sys
import traceback

from modules.cryptano.utils.common import exchange, price_precision_from_market
from modules.cryptano.utils.indicators import get_market_state, get_cryptano_signal, analyze_extreme_pattern
from modules.cryptano.utils.crypto_utils import calculate_rsi

if __name__ == "__main__":
    COIN = "solayer"
    START_TIME = "2026-06-07 18:00"
    END_TIME = "2026-06-09 23:59"
    
    symbol = f"{COIN.upper()}/USDT:USDT"
    print(f"🚀 ЗАПУСК ПОЛНОГО КОНВЕЙЕРА: CRITICAL (4h) -> WATCHER (15m) | {symbol}")
    
    start_dt = datetime.strptime(START_TIME, "%Y-%m-%d %H:%M")
    end_dt = datetime.strptime(END_TIME, "%Y-%m-%d %H:%M")
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)
    
    try:
        print("📥 Загружаю спецификации рынков Bybit...")
        markets_data = exchange.load_markets()
        exchange.markets = markets_data 
        
        market_info = exchange.markets.get(symbol, {}) if exchange.markets else {}
        if not market_info:
            print(f"❌ Ошибка: Не нашли информацию по монете {symbol} в рынках биржа.")
            sys.exit()
            
        price_precision = price_precision_from_market(market_info)
        
        print("\n" + "="*50)
        print(" 🕵️ ФАЗА 1: СКАНИРОВАНИЕ CRITICAL (4h)")
        print("="*50)
        
        fetch_since_4h = start_ts - (250 * 4 * 60 * 60 * 1000) 
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", since=fetch_since_4h, limit=300)
        df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])
        
        critical_trigger_time = None
        target_min = 0.0
        
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
                print(f"🚨 [{time_str}] CRITICAL НАШЕЛ ПАМП! Цена: {current_price:.5f}")
                critical_trigger_time = current_ts  
                # Ватчер работает, пока цена держится на хаях (не упала ниже 5% от сигнала)
                target_min = current_price * 0.95 
                print(f"🎯 ВАТЧЕР АКТИВИРОВАН! Ищем разворот (SFP/Pinbar) на хаях.")
                break
                         
        if not critical_trigger_time:
            print("❌ Critical не нашел сигналов в этом временном окне.")
            sys.exit()

        print("\n" + "="*50)
        print(" 🎯 ФАЗА 2: РАБОТАЕТ WATCHER (15m)")
        print("="*50)
        
        fetch_since_15m = critical_trigger_time - (150 * 15 * 60 * 1000)
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe="15m", since=fetch_since_15m, limit=400)
        df_15m = pd.DataFrame(ohlcv_15m, columns=["timestamp", "open", "high", "low", "close", "volume"])
        
        COOLDOWN_HOURS = 4
        cooldown_ms = COOLDOWN_HOURS * 60 * 60 * 1000
        last_signal_time = 0
        
        price_reached_zone = False
        
        for i in range(120, len(df_15m) + 1):
            df_slice = df_15m.iloc[:i].copy()
            current_candle = df_slice.iloc[-1]
            current_ts = current_candle["timestamp"]
            
            if current_ts < critical_trigger_time: continue
            if current_ts > end_ts: break
                
            current_price = float(current_candle["close"])
            candle_time = datetime.fromtimestamp(current_ts / 1000).strftime('%m-%d %H:%M')
           
            if not price_reached_zone:
                if df_slice["high"].iloc[-1] >= target_min:
                    price_reached_zone = True
                    print(f"\n🚀 [{candle_time}] ВАТЧЕР НАЧИНАЕТ ОХОТУ ЗА ЛИКВИДНОСТЬЮ!\n")
                else:
                    continue 
            
            df_slice["rsi"] = calculate_rsi(df_slice)
            
            # Передаем срез без последней открытой свечи
            result = analyze_extreme_pattern(df_slice.iloc[:-1], "SHORT", current_price, price_precision)
            
            trigger_fired = result.get("trigger_fired", False)
            rsi_filter_passed = result.get("rsi_filter_passed", False)
            trigger_type = result.get("trigger_type", "НЕТ")
            rsi_value = result.get("rsi_value", 50.0)
            
            is_ready = trigger_fired and rsi_filter_passed
            is_cooling_down = (current_ts - last_signal_time) < cooldown_ms
            
            # Показываем лог, только если Watcher увидел триггерный паттерн
            if trigger_fired:
                if is_cooling_down:
                    status = "🧊 КУЛДАУН (Спит)"
                else:
                    status = "🔥 ГОТОВ! ВХОД!" if is_ready else "❌ ОТКАЗ (Не пройден фильтр RSI)"
                    
                print(f"[{candle_time}] Цена: {current_price:.5f} | RSI: {rsi_value:.1f} | Паттерн: {trigger_type} -> {status}")
                
                if "🔥" in status:
                    last_signal_time = current_ts
                    print(f"   🎯 WATCHER PLAN | Вход: {current_price:.5f} | SL: {result.get('sl_price', 0)} | TP1: {result.get('tp1_price', 0)} | TP2: {result.get('tp2_price', 0)}")
                    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                    
                    # 🆕 Имитируем авто-удаление из watchlist: нашли вход — завершаем тест!
                    print("🤖 Сигнал получен. Монета авто-удалена из Watchlist. Остановка симуляции.")
                    break
                    
    except Exception as e:
        print(f"💥 Критическая ошибка в конвейере: {e}")
        traceback.print_exc()