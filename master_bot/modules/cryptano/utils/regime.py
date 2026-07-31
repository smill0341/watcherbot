# modules/cryptano/regime.py

def detect_market_regime(current_price, rsi, vol_ratio, ma30):
    if not ma30 or ma30 == 0:
        return "NORMAL"

    dist_to_ma = (abs(current_price - ma30) / ma30) * 100

    # Мягкие условия пампа: RSI > 72, Объем > 1.5x, Отрыв > 15% 
    # ИЛИ жесткий отрыв: улетели на 30%+ от MA30 (даже без RSI)
    is_pump = (rsi >= 72 and vol_ratio >= 1.5 and dist_to_ma >= 15) or (dist_to_ma >= 30)
    if is_pump and current_price > ma30:
        return "EXTREME_PUMP"

    # Аналогично для дампа
    is_dump = (rsi <= 28 and vol_ratio >= 1.5 and dist_to_ma >= 15) or (dist_to_ma >= 30)
    if is_dump and current_price < ma30:
        return "EXTREME_DUMP"

    return "NORMAL"