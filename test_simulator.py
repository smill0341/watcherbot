import pandas as pd
from datetime import datetime
import sys

# Импорты твоих оригинальных функций
from modules.cryptano.utils.common import exchange, price_precision_from_market
from modules.cryptano.utils.indicators import get_market_state, get_cryptano_signal, analyze_extreme_pattern
from modules.cryptano.utils.crypto_utils import calculate_rsi

if __name__ == "__main__":
    COIN = "BSB"
    START_TIME = "2026-06-07 00:00"
    END_TIME = "2026-06-08 23:59"
    
    symbol = f"{COIN.upper()}/USDT:USDT"
    print(f"🚀 ЗАПУСК ПОЛНОГО КОНВЕЙЕРА: CRITICAL (4h) -> WATCHER (15m) | {symbol}")
    
    start_dt = datetime.strptime(START_TIME, "%Y-%m-%d %H:%M")
    end_dt = datetime.strptime(END_TIME, "%Y-%m-%d %H:%M")
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(end_dt.timestamp() * 1000)
    
    try:
        # =========================================================
        # 0. ИНИЦИАЛИЗАЦИЯ БИРЖИ И МАРКЕТОВ (Железобетонно)
        # =========================================================
        print("📥 Загружаю спецификации рынков Bybit...")
        markets_data = exchange.load_markets()
        exchange.markets = markets_data  # Принудительно пишем в объект биржи
        
        market_info = exchange.markets.get(symbol, {}) if exchange.markets else {}
        if not market_info:
            print(f"❌ Ошибка: Не нашли информацию по монете {symbol} в рынках биржа.")
            sys.exit()
            
        price_precision = price_precision_from_market(market_info)
        
        # =========================================================
        # ФАЗА 1: РАБОТАЕТ CRITICAL (ИЩЕМ ПЕРВЫЙ СИГНАЛ)
        # =========================================================
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
                target_min = signal_data.get("target_zone_min", current_price)
                print(f"🎯 ЖДЕМ ЗОНУ ФИБО: {target_min:.5f} и выше!")
                break
                
        if not critical_trigger_time:
            print("❌ Critical не нашел сигналов в этом временном окне.")
            sys.exit()

        # =========================================================
        # ФАЗА 2: РАБОТАЕТ WATCHER (СЛЕДИТ ЗА МОНЕТОЙ ПОСЛЕ СИГНАЛА)
        # =========================================================
        print("\n" + "="*50)
        print(" 🎯 ФАЗА 2: РАБОТАЕТ WATCHER (15m)")
        print("="*50)
        
        fetch_since_15m = critical_trigger_time - (150 * 15 * 60 * 1000)
        
        # Вычисляем точное количество свечей M15 до END_TIME, чтобы не обрывалось
        minutes_diff = (end_ts - fetch_since_15m) / (1000 * 60)
        limit_15m = int(minutes_diff / 15) + 10
        if limit_15m > 1000: limit_15m = 1000
        
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe="15m", since=fetch_since_15m, limit=limit_15m)
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
            
            # ЖЕСТКИЙ СТОП, КОГДА ДОШЛИ ДО END_TIME
            if current_ts > end_ts: 
                print("\n🛑 Тест достиг времени END_TIME. Остановка.")
                break
                
            current_price = float(current_candle["close"])
            candle_high = float(current_candle["high"])
            candle_time = datetime.fromtimestamp(current_ts / 1000).strftime('%m-%d %H:%M')
            
            # === ЖДЕМ ЗОНУ ФИБО ===
            if not price_reached_zone:
                if candle_high >= target_min:
                    price_reached_zone = True
                    print(f"\n🚀 [{candle_time}] ЦЕНА ДОШЛА ДО ФИБО ({target_min:.5f})! ВАТЧЕР ПРОСНУЛСЯ И ОТКРЫВАЕТ ОХОТУ!\n")
                else:
                    continue  # Ватчер спит, ничего не считает
            
            df_slice["rsi"] = calculate_rsi(df_slice)
            current_rsi = float(df_slice["rsi"].iloc[-1])
            
            result = analyze_extreme_pattern(df_slice.iloc[:-1], "SHORT", current_price, price_precision)
            score = result["score"]
            details = result["details"]
            
            retrace_present = any("Замена BOS" in d for d in details)
            trigger_msg = "✅ ОТКАТ" if retrace_present else "❌ ОТКАТ"
            is_cooling_down = (current_ts - last_signal_time) < cooldown_ms
            
            # ВЫВОД ТОЛЬКО ПОСЛЕ ТОГО, КАК ДОШЛИ ДО ЗОНЫ ФИБО
            if is_cooling_down:
                status = "🧊 КУЛДАУН (Спит)"
            else:
                status = "🔥 ГОТОВ! ВХОД!" if (score >= 3 and retrace_present) else "❌ ОТКАЗ / ЖДЕМ"
                
            print(f"[{candle_time}] Цена: {current_price:.5f} | RSI: {current_rsi:.1f} | Баллы: {score}/5 ({trigger_msg}) -> {status}")
            
            if not is_cooling_down:
                for d in details: print(f"      {d}")
                print("-" * 50)
                
            if "🔥" in status:
                last_signal_time = current_ts
                print(f"   🎯 WATCHER PLAN | Вход: {current_price:.5f} | TP1: {result.get('tp1_price', 0)}")
                print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                    
    except Exception as e:
        print(f"💥 Критическая ошибка в конвейере: {e}")