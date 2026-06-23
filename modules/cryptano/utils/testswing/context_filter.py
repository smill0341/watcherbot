"""
context_filter.py (переписан)
===============================
Правильный анализ макро-контекста на основе Price Action.

Определяет: TREND, IMPULSE vs CORRECTION, ENERGY (аномалии).

Логика:
1. TREND: определяем направление (UP/DOWN/RANGE) через Higher Highs/Lows + EMA
2. IMPULSE: если текущее движение импульсивное (большие свечи, одного цвета) → торговать
           если коррективное (мелкие, микс) → ждать следующего импульса
3. ENERGY: есть ли аномалии (панические скачки, распил в одной зоне) → избегать

Approach:
  - IMPULSE_UP/DOWN: сильное направленное движение → можно входить ДО уровня
  - COMPRESSION: боковик → избегать
  - CHOPPY: разноцветные свечи без тренда → избегать
  - COUNTER_TREND: цена против структуры → осторожно
"""

import numpy as np


def analyze_context(closes, highs, lows, current_atr, trade_type, level_min, level_max, opens=None):
    """
    Определяет макро-контекст рынка.
    
    Args:
        closes, highs, lows: полная история (растущее окно до текущего момента)
        current_atr: ATR на текущей свече
        trade_type: 'LONG' или 'SHORT'
        level_min, level_max: границы уровня (для проверки распила)
    
    Returns:
        {
            "allowed": bool,
            "reason": str,
            "approach": "IMPULSE_UP", "IMPULSE_DOWN", "COMPRESSION", "CHOPPY",
            "trend": "UP", "DOWN", "RANGE",
            "energy": "NORMAL", "PANIC", "CHOPPED",
        }
    """
    
    # Защита: нужно минимум 100 свечей для анализа
    if len(closes) < 100:
        return {
            "allowed": True,
            "reason": "Not enough data",
            "approach": "NORMAL",
            "trend": "UNKNOWN",
            "energy": "NORMAL",
        }
    
    current_close = closes[-1]
    
    # ===== 1. TREND ANALYSIS =====
    # Считаем структуру: Higher Highs/Lows за последние 64 свечи (4 часа на 15m)
    recent_highs = highs[-64:]
    recent_lows = lows[-64:]
    
    # Разбиваем на 4 блока по 16 свечей, считаем max/min каждого
    h1, h2, h3, h4 = (max(recent_highs[0:16]), max(recent_highs[16:32]),
                       max(recent_highs[32:48]), max(recent_highs[48:64]))
    l1, l2, l3, l4 = (min(recent_lows[0:16]), min(recent_lows[16:32]),
                       min(recent_lows[32:48]), min(recent_lows[48:64]))
    
    # Определяем тренд через последовательность
    is_uptrend = (h1 < h2 < h3 < h4) and (l1 < l2 < l3 < l4)  # Higher Highs & Higher Lows
    is_downtrend = (h1 > h2 > h3 > h4) and (l1 > l2 > l3 > l4)  # Lower Highs & Lower Lows
    
    trend = "UP" if is_uptrend else ("DOWN" if is_downtrend else "RANGE")
    
    # ===== 2. IMPULSE vs CORRECTION =====
    # Смотрим на последние 16 свечей (1 час на 15m)
    last_16_closes = closes[-16:]
    last_16_highs = highs[-16:]
    last_16_lows = lows[-16:]
    
    # Если нет opens (может не быть в некоторых данных), восстанавливаем
    if opens is None:
        last_16_opens = opens_from_highs_lows(last_16_highs, last_16_lows, last_16_closes)
    else:
        last_16_opens = opens[-16:]
    
    # Считаем величину свечи (тело)
    last_16_bodies = np.abs(np.array(last_16_closes) - np.array(last_16_opens))
    
    # Считаем зелёные (close > open) и красные (close < open) свечи
    green_count = sum(1 for i in range(len(last_16_closes)) if last_16_closes[i] > last_16_opens[i])
    red_count = len(last_16_closes) - green_count
    
    # Считаем среднюю величину свечи (тело)
    avg_body = np.mean(last_16_bodies)
    avg_hl_range = np.mean(last_16_highs) - np.mean(last_16_lows)
    
    # IMPULSE: большие свечи (>1.5*avg), большинство одного цвета (>=12 из 16)
    large_candles = sum(1 for b in last_16_bodies if b > (current_atr * 0.8))
    
    if trade_type == 'LONG':
        # Для LONG ищем зелёные импульсивные свечи
        if green_count >= 12 and large_candles >= 10:
            approach = "IMPULSE_UP"
        elif red_count >= 12 and large_candles >= 10:
            approach = "IMPULSE_DOWN"  # Падающий нож — ИЗБЕГАТЬ для LONG
        else:
            approach = "COMPRESSION"  # Много микс-свечей = боковик
    else:
        # Для SHORT ищем красные импульсивные свечи
        if red_count >= 12 and large_candles >= 10:
            approach = "IMPULSE_DOWN"
        elif green_count >= 12 and large_candles >= 10:
            approach = "IMPULSE_UP"  # Растущая ракета — ИЗБЕГАТЬ для SHORT
        else:
            approach = "COMPRESSION"
    
    # ===== 3. ENERGY CHECK =====
    # Ищем панические аномалии (одна свеча сильно больше остальных)
    max_body = max(last_16_bodies) if last_16_bodies.size > 0 else 0
    is_panic = max_body > (current_atr * 2.5)
    
    # Ищем "распил" (цена зацикливается в одной зоне)
    zone_buffer = current_atr * 0.3
    candles_in_zone = sum(
        1 for c in closes[-30:] if (level_min - zone_buffer) <= c <= (level_max + zone_buffer)
    )
    is_chopped = candles_in_zone > 10  # Более 10 свечей из 30 в зоне = распил
    
    energy = "PANIC" if is_panic else ("CHOPPED" if is_chopped else "NORMAL")
    
    # ===== 4. ЛОГИРОВАНИЕ (БЕЗ ФИЛЬТРАЦИИ) =====
    # Контекст ВСЕ логирует, но ничего не блокирует.
    # Решение о входе принимает только watcher.
    # Это позволит в отчётах увидеть: контекст правильно ли считается?
    
    reasons = []
    
    # Просто логируем всё, что видим
    if approach == "IMPULSE_UP":
        reasons.append("Impulse UP")
    elif approach == "IMPULSE_DOWN":
        reasons.append("Impulse DOWN")
    elif approach == "COMPRESSION":
        reasons.append("Compression (sideways)")
    
    if trend == "UP":
        reasons.append("Uptrend")
    elif trend == "DOWN":
        reasons.append("Downtrend")
    elif trend == "RANGE":
        reasons.append("Ranging")
    
    if energy == "PANIC":
        reasons.append("⚠️ Panic spike")
    elif energy == "CHOPPED":
        reasons.append("⚠️ Chopped zone")
    
    # Дополнительная информация про согласованность
    if (trade_type == 'LONG' and approach == "IMPULSE_UP"):
        reasons.append("✓ LONG aligned with UP impulse")
    elif (trade_type == 'LONG' and approach == "IMPULSE_DOWN"):
        reasons.append("✗ LONG vs DOWN impulse")
    elif (trade_type == 'SHORT' and approach == "IMPULSE_DOWN"):
        reasons.append("✓ SHORT aligned with DOWN impulse")
    elif (trade_type == 'SHORT' and approach == "IMPULSE_UP"):
        reasons.append("✗ SHORT vs UP impulse")
    
    # Вердикт: ВСЕГДА разрешено входить (контекст только логирует)
    trade_allowed = True
    
    return {
        "allowed": trade_allowed,
        "reason": " | ".join(reasons),
        "approach": approach,
        "trend": trend,
        "energy": energy,
    }


def opens_from_highs_lows(highs, lows, closes):
    """
    Восстанавливаем Open приблизительно (не имеем его в контексте).
    Предполагаем Open примерно в 30% от High-Low диапазона.
    """
    opens = []
    for h, l, c in zip(highs, lows, closes):
        # Если свеча зелёная (close > open), open ближе к low
        # Если свеча красная (close < open), open ближе к high
        # Для простоты: open = (high + low) / 2, потом настраиваем
        estimated_open = (h + l) / 2
        opens.append(estimated_open)
    return opens
