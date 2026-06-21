import numpy as np

def analyze_context(closes, highs, lows, current_atr, trade_type, level_min, level_max):
    """
    Монолитный Макро-Аналитик Контекста
    Окно обзора: 192 свечи (2 суток на 15m)
    """
    # Защита от отсутствия данных (для первых часов работы тестера)
    if len(closes) < 192:
        return {"allowed": True, "reason": "Not enough data", "score": 0, "approach": "NORMAL"}

    current_close = closes[-1]
    
    # Разделяем историю на 3 блока по 16 часов для оценки макро-картины
    b1_lows, b2_lows, b3_lows = lows[-192:-128], lows[-128:-64], lows[-64:]
    b1_highs, b2_highs, b3_highs = highs[-192:-128], highs[-128:-64], highs[-64:]
    
    # =================================================
    # 1. МАКРО-СТРУКТУРА (Trend / Откуда пришли)
    # =================================================
    is_counter_trend = False
    
    if trade_type == 'LONG':
        if max(b3_highs) < max(b2_highs) < max(b1_highs):
            is_counter_trend = True
    elif trade_type == 'SHORT':
        if min(b3_lows) > min(b2_lows) > min(b1_lows):
            is_counter_trend = True

    # =================================================
    # 2. ТИП ПОДХОДА (Два окна: Локальное 16 и Основное 64)
    # =================================================
    approach_status = "NORMAL"
    
    # Подсчет направленности свечей (красные/зеленые)
    # Свеча "вниз" — это закрытие ниже предыдущего закрытия
    down_16 = sum(1 for i in range(-16, 0) if closes[i] < closes[i-1])
    up_16   = sum(1 for i in range(-16, 0) if closes[i] > closes[i-1])

    # Средний размер свечи за 16 часов (проверка на "мелкие свечи")
    avg_candle_size_64 = np.mean(highs[-64:] - lows[-64:])
    
    if trade_type == 'LONG':
        move_down_16 = max(highs[-16:]) - current_close
        
        # --- IMPULSE (Падающий нож / 4 часа) ---
        if move_down_16 > (current_atr * 3.5) and down_16 >= 12:
            approach_status = "IMPULSE_DUMP"
            
        # --- COMPRESSION (Макро-поджатие / 16 часов) ---
        else:
            h1, h2, h3, h4 = max(highs[-64:-48]), max(highs[-48:-32]), max(highs[-32:-16]), max(highs[-16:])
            
            # Считаем наклон максимумов через простейшую линейную регрессию numpy
            slope = np.polyfit([1, 2, 3, 4], [h1, h2, h3, h4], 1)[0]
            
            # Наклон отрицательный (давят вниз), свечи не огромные, нет панических распродаж
            if (slope < 0) and (down_16 <= 10) and (avg_candle_size_64 < current_atr * 1.5):
                approach_status = "COMPRESSION"
                
    elif trade_type == 'SHORT':
        move_up_16 = current_close - min(lows[-16:])
        
        # --- IMPULSE (Взлетающая ракета / 4 часа) ---
        if move_up_16 > (current_atr * 3.5) and up_16 >= 12:
            approach_status = "IMPULSE_PUMP"
            
        # --- COMPRESSION (Макро-поджатие вверх / 16 часов) ---
        else:
            l1, l2, l3, l4 = min(lows[-64:-48]), min(lows[-48:-32]), min(lows[-32:-16]), min(lows[-16:])
            
            # Считаем наклон минимумов через простейшую линейную регрессию numpy
            slope = np.polyfit([1, 2, 3, 4], [l1, l2, l3, l4], 1)[0]
            
            # Наклон положительный (давят вверх)
            if (slope > 0) and (up_16 <= 10) and (avg_candle_size_64 < current_atr * 1.5):
                approach_status = "COMPRESSION"

    # =================================================
    # 3. ПРИМАНКА (Inducement / Накопление ликвидности перед зоной)
    # =================================================
    has_inducement = False
    window_lows = lows[-30:-5]
    window_highs = highs[-30:-5]
    
    if trade_type == 'LONG':
        if level_max < min(window_lows) <= (level_max + current_atr * 1.5):
            has_inducement = True
    elif trade_type == 'SHORT':
        if (level_min - current_atr * 1.5) <= max(window_highs) < level_min:
            has_inducement = True

    # =================================================
    # 4. РАСПИЛ (Time at Price / Living in the zone)
    # =================================================
    zone_buffer = current_atr * 0.2
    candles_in_zone = sum(1 for c in closes[-62:-2] if (level_min - zone_buffer) <= c <= (level_max + zone_buffer))
    is_chopped = candles_in_zone > 8 

    # =================================================
    # ФОРМИРОВАНИЕ ВЕРДИКТА
    # =================================================
    trade_allowed = True
    reasons = []
    
    if is_chopped:
        trade_allowed = False
        reasons.append("KILLED: Level Chopped")
        
    if approach_status == "COMPRESSION":
        trade_allowed = False
        reasons.append("KILLED: Approach Compression")
        
    if approach_status in ["IMPULSE_DUMP", "IMPULSE_PUMP"]:
        trade_allowed = False
        reasons.append("KILLED: Freight Train")
        
    if is_counter_trend:
        reasons.append("WARNING: Counter-trend")
        
    if has_inducement:
        reasons.append("BONUS: Inducement found")

    if not reasons:
        reasons.append("Clear & Healthy Approach")

    return {
        "allowed": trade_allowed,
        "reason": " | ".join(reasons),
        "approach": approach_status,
        "has_inducement": has_inducement,
        "is_counter_trend": is_counter_trend
    }