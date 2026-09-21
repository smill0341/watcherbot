import ccxt
import pandas as pd
import numpy as np

# Импортируем твой оригинальный метод!
from modules.cryptano.utils.levels_builder import build_levels

SYMBOL = 'RAYDIUM/USDT:USDT'
COIN = 'RAYDIUM'

def calculate_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    return np.max(ranges, axis=1).rolling(period).mean()

def run():
    print(f"📥 Качаем данные {SYMBOL} с Bybit...")
    exchange = ccxt.bybit()
    
    # Качаем данные точно как в твоем swing_hunter.py
    ohlcv_1M = exchange.fetch_ohlcv(SYMBOL, '1M', limit=60)
    ohlcv_1W = exchange.fetch_ohlcv(SYMBOL, '1W', limit=150)
    ohlcv_1d = exchange.fetch_ohlcv(SYMBOL, '1d', limit=365)
    ohlcv_4h = exchange.fetch_ohlcv(SYMBOL, '4h', limit=200)

    # Обязательно astype(float) чтобы не было ошибок
    df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"]).astype(float)
    df_1W = pd.DataFrame(ohlcv_1W, columns=["timestamp", "open", "high", "low", "close", "volume"]).astype(float)
    df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"]).astype(float)
    df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]).astype(float)

    # Считаем дневной ATR для динамического метода
    df_1d['atr'] = calculate_atr(df_1d)
    current_atr = df_1d['atr'].iloc[-1]
    
    # Добавляем даты строками, чтобы найти нужную свечу
    df_1M['date_str'] = pd.to_datetime(df_1M['timestamp'], unit='ms').dt.strftime('%Y-%m-%d')
    df_1W['date_str'] = pd.to_datetime(df_1W['timestamp'], unit='ms').dt.strftime('%Y-%m-%d')

    print("⚙️  Вызываем твой оригинальный build_levels()...")
    levels = build_levels(df_1M, df_1W, df_1d, df_4h, COIN)

    print(f"\nТекущий дневной ATR ({COIN}): {current_atr:.2f}")
    print("-" * 115)
    print(f"{'ДАТА':<12} | {'ТИП':<15} | {'ЦЕНА':<8} | {'ТВОЙ МЕТОД (1.5%)':<25} | {'ДИНАМИЧЕСКИЙ (ATR)':<25} | {'ТЕНЬ (SMC)':<20}")
    print("-" * 115)

    all_zones = levels.get('resistances', []) + levels.get('supports', [])
    
    found_macro = False
    for z in all_zones:
        # Берем только макро уровни
        if z.get('class') != 'MACRO':
            continue
            
        found_macro = True
        z_date = z.get('date')
        
        # Ищем эту свечу в месячном или недельном графике
        candle = None
        for df in [df_1M, df_1W]:
            match = df[df['date_str'] == z_date]
            if not match.empty:
                candle = match.iloc[0]
                break
                
        if candle is None:
            continue
            
        # Восстанавливаем цифры
        if 'support' in z.get('type', '').lower():
            peak_price = z['min']
            
            old_width = z['max'] - z['min']
            old_str = f"{z['min']:.2f} - {z['max']:.2f} (ш:{old_width:.2f})"
            
            atr_width = current_atr * 1.5
            atr_max = peak_price + atr_width
            atr_str = f"{peak_price:.2f} - {atr_max:.2f} (ш:{atr_width:.2f})"
            
            body_bottom = min(candle['open'], candle['close'])
            shadow_width = body_bottom - candle['low']
            smc_str = f"{candle['low']:.2f} - {body_bottom:.2f} (ш:{shadow_width:.2f})"
            
        else: # resistance
            peak_price = z['max']
            
            old_width = z['max'] - z['min']
            old_str = f"{z['min']:.2f} - {z['max']:.2f} (ш:{old_width:.2f})"
            
            atr_width = current_atr * 1.5
            atr_min = peak_price - atr_width
            atr_str = f"{atr_min:.2f} - {peak_price:.2f} (ш:{atr_width:.2f})"
            
            body_top = max(candle['open'], candle['close'])
            shadow_width = candle['high'] - body_top
            smc_str = f"{body_top:.2f} - {candle['high']:.2f} (ш:{shadow_width:.2f})"

        print(f"{z_date:<12} | {z.get('type'):<15} | {peak_price:<8.2f} | {old_str:<25} | {atr_str:<25} | {smc_str:<20}")

    if not found_macro:
        print("Твой алгоритм не нашел ни одного макро-уровня по DASH на данный момент.")

if __name__ == '__main__':
    run()