import pandas as pd

def check_live_confirmation(df, direction, zone_min, zone_max, vol_ratio):
    """
    Анализирует Price Action (последние свечи) для подтверждения входа.
    Возвращает: (is_confirmed, score, details_list)
    """
    is_rejection = False
    zone_held = False
    details = []
    score = 0

    # 1. Проверка Volume Spike (Объемный всплеск)
    if vol_ratio >= 1.5:
        score += 1
        details.append(f"✅ Volume Spike (объем x{vol_ratio})")
    else:
        details.append(f"❌ Нет всплеска объема (x{vol_ratio})")

    # Берем 3 последние свечи: 2 закрытые (-3, -2) и 1 текущую лайв (-1)
    last_3_candles = df.tail(3)
    
    # 2. Проверка Rejection Candle (Ищем Пинбар на двух последних ЗАКРЫТЫХ свечах)
    for i in [-3, -2]:
        row = df.iloc[i]
        body = abs(row['open'] - row['close'])
        if body == 0: body = 0.000001 # Защита от деления на ноль
        
        upper_shadow = row['high'] - max(row['open'], row['close'])
        lower_shadow = min(row['open'], row['close']) - row['low']
        
        if direction == "SHORT" and upper_shadow > (body * 2): # Тень сверху в 2 раза больше тела
            is_rejection = True
        elif direction == "LONG" and lower_shadow > (body * 2): # Тень снизу в 2 раза больше тела
            is_rejection = True

    if is_rejection:
        score += 1
        details.append("✅ Rejection Candle (длинная тень)")
    else:
        details.append("❌ Нет Rejection свечи")

    # 3. Проверка Zone Hold (Удержание зоны 3 последними свечами)
    if direction == "SHORT":
        # Касались ли зоны сопротивления?
        touches = last_3_candles[last_3_candles['high'] >= zone_min]
        # Пробили ли её закрытием тела свечи?
        breakouts = last_3_candles[last_3_candles['close'] > zone_max]
        
        if not touches.empty and breakouts.empty:
            zone_held = True

    elif direction == "LONG":
        # Касались ли зоны поддержки?
        touches = last_3_candles[last_3_candles['low'] <= zone_max]
        # Провалили ли её закрытием тела свечи?
        breakouts = last_3_candles[last_3_candles['close'] < zone_min]
        
        if not touches.empty and breakouts.empty:
            zone_held = True

    if zone_held:
        score += 1
        details.append("✅ Удержание уровня (Zone Hold)")
    else:
        details.append("❌ Нет удержания уровня")

    # Итог: если есть хотя бы 2 из 3 подтверждений — сетап активен
    is_confirmed = score >= 2
    
    return is_confirmed, score, details