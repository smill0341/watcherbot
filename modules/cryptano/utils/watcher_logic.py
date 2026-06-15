import pandas as pd
import numpy as np
from modules.cryptano.utils.indicators import calculate_atr

# --- ТУМБЛЕРЫ И НАСТРОЙКИ ---
TAKE_PROFIT = 10.0
SL_BUFFER = 0.5
RR_RATIO = 3.0
RANGE_FILTER_PCT = 0.30
USE_DYNAMIC_TP = True  # Включить динамический тейк по макро-уровням (NO TRADE zone)
MIN_PROFIT_PCT = 5.0   # Минимальный % хода до следующей зоны (если USE_DYNAMIC_TP = True)

# =========================================================================
# ФУНКЦИЯ ИСКЛЮЧИТЕЛЬНО ДЛЯ WATCHER (15M СНАЙПЕР С ОБЪЕМОМ И ТЕНЬЮ)
# =========================================================================
def analyze_extreme_pattern(df, direction, current_price, price_precision, source="Manual", macro_levels=None):
    if macro_levels is None: macro_levels = {}
    
    if len(df) < 20:
        return {"trigger_fired": False, "rsi_filter_passed": False, "volume_climax": False, "trigger_type": "НЕТ"}
        
    df = df.copy()
    df["atr"] = calculate_atr(df, 14)
    
    # ---------------------------------------------------------
    # НАСТРОЙКИ 1 В 1 ИЗ TEST_SIMULATOR_3.PY
    # ---------------------------------------------------------
    TAKE_PROFIT = 10.0
    SL_BUFFER = 0.5
    RR_RATIO = 3.0

    supports = macro_levels.get("supports", [])
    resistances = macro_levels.get("resistances", [])

    wait_for_choch = False
    choch_level = 0.0
    active_level = None

    trigger_fired = False
    trigger_type = "Ожидание CHoCH"
    
    # Симуляция последних 15 свечей (как в тестере)
    lookback = 15
    start_idx = max(1, len(df) - lookback)

    for i in range(start_idx, len(df)):
        c_close, c_open = float(df['close'].iloc[i]), float(df['open'].iloc[i])
        c_high, c_low = float(df['high'].iloc[i]), float(df['low'].iloc[i])
        c_vol = float(df['volume'].iloc[i])

        p_close, p_open = float(df['close'].iloc[i-1]), float(df['open'].iloc[i-1])
        p_vol = float(df['volume'].iloc[i-1])

        c_atr = float(df['atr'].iloc[i]) if not pd.isna(df['atr'].iloc[i]) else (c_high - c_low)

        # 1. АНТИ-НОЖ
        is_falling_knife = False
        is_flying_rocket = False

        if c_close < c_open and p_close < p_open:
            if (c_open - c_close) > (c_atr * 0.8) and c_vol >= p_vol:
                is_falling_knife = True
        if c_close > c_open and p_close > p_open:
            if (c_close - c_open) > (c_atr * 0.8) and c_vol >= p_vol:
                is_flying_rocket = True

        # 2. ПРОВЕРКА ОТМЕНЫ ИЛИ ПРОБОЯ (Если уже ждем CHoCH)
        if wait_for_choch and active_level is not None:
            # Сначала проверяем отмену сетапа (вылет за зону)
            if direction == "LONG" and c_low < active_level['min'] * 0.99:
                wait_for_choch = False
                active_level = None
            elif direction == "SHORT" and c_high > active_level['max'] * 1.01:
                wait_for_choch = False
                active_level = None

            # Проверка пробоя
            if wait_for_choch:
                if direction == "LONG" and c_close > choch_level:
                    if i == len(df) - 1: # Пробой произошел ПРЯМО СЕЙЧАС
                        trigger_fired = True
                    else: # Пробой был в прошлом, сбрасываем и ищем заново
                        wait_for_choch = False
                        active_level = None
                elif direction == "SHORT" and c_close < choch_level:
                    if i == len(df) - 1: # Пробой произошел ПРЯМО СЕЙЧАС
                        trigger_fired = True
                    else:
                        wait_for_choch = False
                        active_level = None

        # 3. ПОИСК КАСАНИЯ (Если не ждем CHoCH)
        if not wait_for_choch:
            if direction == "LONG":
                for sup in supports:
                    if c_low <= sup['max'] and c_close > sup['min']:
                        if is_falling_knife: break
                        wait_for_choch = True
                        choch_level = max(float(df['high'].iloc[i]), float(df['high'].iloc[i-1]))
                        active_level = sup
                        break
            elif direction == "SHORT":
                for res in resistances:
                    # Исправленная математика касания шорт-зоны
                    if c_high >= res['min'] and c_close < res['max']:
                        if is_flying_rocket: break
                        wait_for_choch = True
                        choch_level = min(float(df['low'].iloc[i]), float(df['low'].iloc[i-1]))
                        active_level = res
                        break

    # =========================================================
    # ФИЛЬТРЫ И МАТЕМАТИКА (Выполняется только при живом пробое)
    # =========================================================
    if trigger_fired and active_level is not None:
        c_close = float(df['close'].iloc[-1])
        
        # СТАРАЯ ЖЕСТКАЯ МАТЕМАТИКА (ФИКС 10%)
        if direction == "LONG":
            sl_price = active_level['min'] * (1 - SL_BUFFER / 100)
            tp_price = c_close * (1 + TAKE_PROFIT / 100)
            risk = c_close - sl_price
            reward = tp_price - c_close
        else:
            sl_price = active_level['max'] * (1 + SL_BUFFER / 100)
            tp_price = c_close * (1 - TAKE_PROFIT / 100)
            risk = sl_price - c_close
            reward = c_close - tp_price

        # ФИЛЬТР R/R
        if risk <= 0 or (reward / risk) < RR_RATIO:
            rr_val = (reward / risk) if risk > 0 else 0
            return {"trigger_fired": False, "rsi_filter_passed": False, "volume_climax": False, "trigger_type": f"ПРОПУСК: R/R < {RR_RATIO} (Факт: {rr_val:.2f})"}

        # УСПЕХ!
        return {
            "trigger_fired": True,
            "rsi_filter_passed": True,  
            "volume_climax": True,      
            "trigger_type": "CHoCH Пробой (Подтвержден)",
            "sl_price": round(sl_price, price_precision),
            "tp1_price": round(tp_price, price_precision),
            "tp2_price": round(tp_price, price_precision), 
            "tp3_price": round(tp_price, price_precision), 
            "rsi_value": round(float(df.get("rsi", pd.Series(50)).iloc[-1]), 1) if "rsi" in df else 50.0,
            "vol_ratio": 1.0 
        }

    return {"trigger_fired": False, "rsi_filter_passed": False, "volume_climax": False, "trigger_type": trigger_type}