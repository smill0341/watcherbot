import numpy as np

def analyze_context(closes, highs, lows, current_atr, trade_type, level_min, level_max):
    """
    Монолитный Макро-Аналитик Контекста
    Окно обзора: 192 свечи (2 суток на 15m)
    """
    if len(closes) < 192:
        return {"allowed": True, "reason": "Not enough data", "score": 0}

    current_close = closes[-1]
    
    # Разделяем историю на 3 блока по 16 часов для оценки макро-картины
    b1_lows, b2_lows, b3_lows = lows[-192:-128], lows[-128:-64], lows[-64:]
    b1_highs, b2_highs, b3_highs = highs[-192:-128], highs[-128:-64], highs[-64:]
    
    # =================================================
    # 1. МАКРО-СТРУКТУРА (Trend / Откуда пришли)
    # =================================================
    is_counter_trend = False
    
    if trade_type == 'LONG':
        # Если каждый следующий 16-часовой блок рисует максимумы ниже предыдущего - это жесткий дамп
        if max(b3_highs) < max(b2_highs) < max(b1_highs):
            is_counter_trend = True
    elif trade_type == 'SHORT':
        # Если каждый блок рисует минимумы выше предыдущего - это жесткий памп
        if min(b3_lows) > min(b2_lows) > min(b1_lows):
            is_counter_trend = True

    # =================================================
    # 2. ТИП ПОДХОДА (Momentum / Компрессия или Импульс)
    # Окно: последние 4 часа (16 свечей)
    # =================================================
    approach_status = "NORMAL"
    recent_low = min(lows[-16:])
    recent_high = max(highs[-16:])
    
    if trade_type == 'LONG':
        move_down = recent_high - current_close
        if move_down > (current_atr * 4.0):
            approach_status = "IMPULSE_DUMP"  # Падающий нож
        elif min(b3_lows) < min(b2_lows) < min(b1_lows) and move_down < (current_atr * 1.5):
             approach_status = "COMPRESSION"  # Медленное сползание (давление продавца)
             
    elif trade_type == 'SHORT':
        move_up = current_close - recent_low
        if move_up > (current_atr * 4.0):
            approach_status = "IMPULSE_PUMP"  # Взлетающая ракета
        elif max(b3_highs) > max(b2_highs) > max(b1_highs) and move_up < (current_atr * 1.5):
             approach_status = "COMPRESSION"  # Медленное поджатие вверх

    # =================================================
    # 3. ПРИМАНКА (Inducement / Накопление ликвидности перед зоной)
    # Ищем, была ли недавняя остановка ЦУТЬ-ЧУТЬ не доходя до уровня
    # =================================================
    has_inducement = False
    
    # Смотрим окно от 30 до 5 свечей назад (чтобы не цеплять текущий подход)
    window_lows = lows[-30:-5]
    window_highs = highs[-30:-5]
    
    if trade_type == 'LONG':
        # Если был минимум в диапазоне от 0.2 до 1.5 ATR выше зоны - там скопились стопы
        if level_max < min(window_lows) <= (level_max + current_atr * 1.5):
            has_inducement = True
    elif trade_type == 'SHORT':
        # Если был максимум чуть ниже зоны
        if (level_min - current_atr * 1.5) <= max(window_highs) < level_min:
            has_inducement = True

    # =================================================
    # 4. РАСПИЛ (Time at Price / Living in the zone)
    # =================================================
    zone_buffer = current_atr * 0.2
    candles_in_zone = sum(1 for c in closes[-62:-2] if (level_min - zone_buffer) <= c <= (level_max + zone_buffer))
    is_chopped = candles_in_zone > 8  # Больше 8 свечей в зоне за последние 15 часов

    # =================================================
    # ФОРМИРОВАНИЕ ВЕРДИКТА
    # =================================================
    trade_allowed = True
    reasons = []
    
    # Жесткие отказы (Красный свет)
    if is_chopped:
        trade_allowed = False
        reasons.append("KILLED: Level Chopped (No Liquidity)")
        
    if approach_status == "COMPRESSION":
        trade_allowed = False
        reasons.append("KILLED: Approach Compression (High Risk of Breakout)")
        
    if approach_status in ["IMPULSE_DUMP", "IMPULSE_PUMP"]:
        trade_allowed = False
        reasons.append("KILLED: Freight Train (Falling Knife / Rocket)")
        
    # Предупреждения (Влияют на вероятность, но могут быть отторгованы)
    if is_counter_trend:
        reasons.append("WARNING: Counter-trend trade")
        
    # Позитивные факторы
    if has_inducement:
        reasons.append("BONUS: Inducement found (Liquidity is primed)")

    if not reasons:
        reasons.append("Clear & Healthy Approach")

    return {
        "allowed": trade_allowed,
        "reason": " | ".join(reasons),
        "has_inducement": has_inducement,
        "is_counter_trend": is_counter_trend
    }