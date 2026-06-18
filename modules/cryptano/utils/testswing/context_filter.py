import numpy as np

def evaluate_context(closes, highs, lows, current_atr, trade_type, level_min, level_max):
    """
    Макро-Аналитик Контекста (Smart Money)
    Окно обзора: 60 свечей (15 часов)
    """
    # Если истории слишком мало, пропускаем оценку
    if len(closes) < 62:
        return {"allowed": True, "reason": "Not enough data"}

    current_close = closes[-1]
    
    # =================================================
    # 1. ЗАЩИТА ОТ РАСПИЛА (Living in the zone)
    # Считаем, сколько свечей закрылись прямо внутри или близко к зоне
    # =================================================
    zone_buffer = current_atr * 0.2
    candles_in_zone = 0
    
    # Смотрим на последние 60 свечей (исключая текущую)
    for i in range(2, 62):
        c = closes[-i]
        if (level_min - zone_buffer) <= c <= (level_max + zone_buffer):
            candles_in_zone += 1
            
    # Если цена провела в зоне суммарно больше 8 свечей (2 часа) за последние 15 часов - зона выжата
    is_chopped = candles_in_zone > 8

    # =================================================
    # 2. МАКРО-ПОДЖАТИЕ (Compression)
    # Делим 60 свечей на два блока по 30 свечей (по 7.5 часов)
    # =================================================
    is_compressed = False
    
    block_a_lows = lows[-60:-30]  # Старая половина дня
    block_b_lows = lows[-30:]     # Новая половина дня
    
    block_a_highs = highs[-60:-30]
    block_b_highs = highs[-30:]

    if trade_type == 'SHORT':
        # Восходящий треугольник: дно второго блока минимум на 0.5 ATR выше первого
        if min(block_b_lows) > min(block_a_lows) + (current_atr * 0.5):
            is_compressed = True
    else:
        # Нисходящий треугольник: потолок второго блока минимум на 0.5 ATR ниже первого
        if max(block_b_highs) < max(block_a_highs) - (current_atr * 0.5):
            is_compressed = True

    # =================================================
    # 3. ИСТОЩЕНИЕ (Exhaustion / Натянутая резинка)
    # Вертикальный безоткатный пролет (Окно 15 свечей)
    # =================================================
    is_exhausted = False
    recent_low = min(lows[-15:])
    recent_high = max(highs[-15:])

    if trade_type == 'SHORT':
        move_up = current_close - recent_low
        if move_up > (current_atr * 3.5):  # Пролет 3.5 ATR вверх
            is_exhausted = True
    else:
        move_down = recent_high - current_close
        if move_down > (current_atr * 3.5):  # Пролет 3.5 ATR вниз
            is_exhausted = True

    # =================================================
    # ВЕРДИКТ СИГНАЛЬЩИКА
    # =================================================
    trade_allowed = True
    reason = "Clear"

    if trade_type == 'SHORT':
        if is_exhausted:
            trade_allowed = True # Резинка натянута, берем шорт на отскок
            reason = "Exhaustion Bounce"
        elif is_chopped:
            trade_allowed = False
            reason = "Level Chopped (No Liquidity)"
        elif is_compressed:
            trade_allowed = False
            reason = "Macro Compression UP"
            
    elif trade_type == 'LONG':
        if is_exhausted:
            trade_allowed = True
            reason = "Exhaustion Bounce"
        elif is_chopped:
            trade_allowed = False
            reason = "Level Chopped (No Liquidity)"
        elif is_compressed:
            trade_allowed = False
            reason = "Macro Compression DOWN"

    return {
        "allowed": trade_allowed,
        "reason": reason
    }