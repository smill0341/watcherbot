import time
import datetime
import pandas as pd

# =========================================================
# ⚙️ НАСТРОЙКИ МАШИНЫ ВРЕМЕНИ
# =========================================================
TEST_COIN = "KAT"               
TEST_DATE = "2026-06-10 22:00"  
TEST_MARKET = "swap"            
# =========================================================

from modules.cryptano.utils.crypto_utils import exchange, format_price as fmt_p
from modules.cryptano.market_overview import get_order_blocks

def fetch_historical_data_backwards(symbol, timeframe, limit, target_ts):
    try:
        tf_ms = exchange.parse_timeframe(timeframe) * 1000
        since_ts = int(target_ts - (limit * tf_ms))
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ts, limit=limit)
        filtered_ohlcv = [candle for candle in ohlcv if candle[0] <= target_ts]
        if not filtered_ohlcv: return None
        return pd.DataFrame(filtered_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    except Exception as e:
        print(f"Ошибка скачивания: {e}")
        return None

def clean_price(price_str):
    return str(price_str)

def fmt_z(z): 
    return clean_price(fmt_p((z['min'] + z['max']) / 2)) if z else "Нет"

def run_time_machine():
    try:
        target_dt = datetime.datetime.strptime(TEST_DATE, "%Y-%m-%d %H:%M")
        target_ts = int(target_dt.timestamp() * 1000)
    except Exception as e:
        print("❌ Ошибка формата даты!")
        return

    if exchange.options is None:
        exchange.options = {}
    dict(exchange.options)['defaultType'] = TEST_MARKET # type: ignore
    
    symbol = f"{TEST_COIN}/USDT:USDT" if TEST_MARKET == "swap" else f"{TEST_COIN}/USDT"
    
    print(f"\n⏳ Скачиваю историю для {symbol} строго ДО {target_dt}...")
    
    df_1h = fetch_historical_data_backwards(symbol, "1h", 336, target_ts)
    df_4h = fetch_historical_data_backwards(symbol, "4h", 360, target_ts)
    df_daily = fetch_historical_data_backwards(symbol, "1d", 365, target_ts)
    
    if df_1h is None or df_4h is None or df_daily is None:
        print("❌ Ошибка: Не удалось получить историю.")
        return
        
    current_price = df_1h.iloc[-1]['close']
    print(f"💵 Истинная цена {TEST_COIN} на указанную дату: {fmt_p(current_price)}\n")
    
    local_supports, local_resistances = get_order_blocks(df_1h, price_precision=6)
    swing_supports, swing_resistances = get_order_blocks(df_4h, price_precision=6)
    macro_supports, macro_resistances = get_order_blocks(df_daily, price_precision=6)
    
    def nearest_below(zones):
        return min((z for z in zones if z['max'] < current_price), key=lambda z: current_price - z['max'], default=None)

    def nearest_above(zones):
        return min((z for z in zones if z['min'] > current_price), key=lambda z: z['min'] - current_price, default=None)

    local_sup = nearest_below(local_supports)
    local_res = nearest_above(local_resistances)
    swing_sup = nearest_below(swing_supports)
    swing_res = nearest_above(swing_resistances)
    macro_sup = nearest_below(macro_supports)
    macro_res = nearest_above(macro_resistances)

    # === ТА САМАЯ СТРАХОВОЧНАЯ СЕТКА ИЗ ТВОЕГО market_overview.py ===
    window_min = float(df_4h['low'].min())
    window_max = float(df_4h['high'].max())

    if not local_sup:
        local_sup = {"min": window_min, "max": window_min, "score": 1}
    if not local_res:
        local_res = {"min": window_max, "max": window_max, "score": 1}
    if not macro_sup:
        macro_sup = {"min": float(df_daily['low'].min()), "max": float(df_daily['low'].min()), "score": 1}
    if not macro_res:
        macro_res = {"min": float(df_daily['high'].max()), "max": float(df_daily['high'].max()), "score": 1}

    # Сборка Macro-строки
    macro_zones = []
    if macro_sup: macro_zones.append(macro_sup)
    if macro_res: macro_zones.append(macro_res)
    macro_sorted = sorted(macro_zones, key=lambda x: x["max"])
    macro_text = " | ".join([f"{fmt_z(z)}" for z in macro_sorted]) if macro_sorted else "Формируется"
    # ==============================================================

    print("📌 КЛЮЧЕВЫЕ УРОВНИ")
    print(f"🔹 Local: {fmt_z(local_sup)} | {fmt_z(local_res)}")
    print(f"🔹 Swing: {fmt_z(swing_sup)} | {fmt_z(swing_res)}")
    print(f"🔹 Macro: {macro_text}")

if __name__ == "__main__":
    run_time_machine()