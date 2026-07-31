"""
risk_calc.py
============
Чистый калькулятор TP/SL. Никаких стратегий, только математика.
Используется всеми стратегиями независимо от их источников.
"""


def calc_tp_and_rr(entry_price, sl, trade_type, all_opposite_levels, config):
    """
    Тупой калькулятор. Берет личный конфиг стратегии, считает Тейк-Профит, 
    применяет буфер к Стоп-Лоссу и проверяет, проходит ли сделка по Risk/Reward.
    Возвращает: ( {'sl': float, 'tp': float}, 'Причина ошибки если не прошел' )
    """
    sl_buffer_pct = config.get('SL_BUFFER', 0.0)
    
    if trade_type == 'LONG':
        sl_adj = sl * (1 - sl_buffer_pct / 100)
        sl_adj = sl_adj * 0.998 # Микро-запас от проскальзывания
        risk = entry_price - sl_adj
    else:
        sl_adj = sl * (1 + sl_buffer_pct / 100)
        sl_adj = sl_adj * 1.002
        risk = sl_adj - entry_price

    if risk <= 0:
        return None, "Invalid risk (SL >= entry)"

    tp_mode = config.get('TP_MODE', 'structural')
    min_rr = config.get('MIN_RR', 1.5)

    if tp_mode == 'fixed_pct':
        fixed_pct = config.get('FIXED_TP_PCT', 8.0)
        if trade_type == 'LONG':
            tp = entry_price * (1 + fixed_pct / 100)
            reward = tp - entry_price
        else:
            tp = entry_price * (1 - fixed_pct / 100)
            reward = entry_price - tp
    else: # structural
        tp_buffer_pct = config.get('TP_BUFFER_PCT', 0.3)
        fallback_tp_pct = config.get('TAKE_PROFIT', 8.0)

        if trade_type == 'LONG':
            candidates = [lvl['min'] for lvl in all_opposite_levels if lvl['min'] > entry_price]
            structural_level = min(candidates) if candidates else None
            tp = structural_level * (1 - tp_buffer_pct / 100) if structural_level else entry_price * (1 + fallback_tp_pct / 100)
            reward = tp - entry_price
        else:
            candidates = [lvl['max'] for lvl in all_opposite_levels if lvl['max'] < entry_price]
            structural_level = max(candidates) if candidates else None
            tp = structural_level * (1 + tp_buffer_pct / 100) if structural_level else entry_price * (1 - fallback_tp_pct / 100)
            reward = entry_price - tp

    if config.get('USE_RR_FILTER', True):
        rr = reward / risk if risk > 0 else 0
        if rr < min_rr:
            return None, f"Poor R/R: {rr:.2f} < {min_rr}"

    return {"sl": sl_adj, "tp": tp}, None
