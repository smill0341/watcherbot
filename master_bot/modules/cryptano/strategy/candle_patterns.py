# -*- coding: utf-8 -*-
"""
Единый файл свечных паттернов — 3 маршрута на SHORT + 3 зеркальных на LONG.

Извлечено из v_red_top_watcher.py (маршруты RED 1 / RED 2 / RED 3 —
самая чистая, уже проверенная 3-свечная система в проекте) и
зеркалировано на LONG. Пороги (проценты, объёмные множители) взяты
как есть, без изменений.

Это ТОЛЬКО распознавание паттерна по трём свечам (C1 якорь -> C2 реакция
-> C3 подтверждение). Никакого состояния, уровней, входа, TP/SL —
это будет отдельная сборка, когда возьмёмся подключать.

Как это используется (когда будем подключать):
  1. На каждой свече, пока паттерн не начат — check_c1(...).
     Если вернулись маршруты — свеча стала C1, ждём следующую как C2.
  2. Следующая свеча — check_c2(...). Отсеивает маршруты, которые не
     подтвердились. Если ничего не выжило — сброс, снова искать C1.
  3. Следующая свеча — check_c3(...). Если вернулся номер маршрута —
     паттерн сработал, это и есть сигнал входа.

direction: 'SHORT' (как в V_RED_TOP) или 'LONG' (зеркало).
"""

CONFIG = {
    # C1 — общий базовый порог, одинаковый для обеих сторон
    'C1_MIN_RANGE_PCT': 0.5,
    'C1_MIN_VOL_MULT': 1.0,

    # Маршрут 1: Пин-бар -> Реакция (RED 1 / зеркало для LONG)
    'R1_C1_MIN_VOL_MULT': 1.00,
    'R1_C1_MIN_BODY_PCT': 43.0,
    'R1_C1_MAX_SHADOW_PCT': 50.0,
    'R1_C2_MIN_SHADOW_MULT': 1.7,
    'R1_C2_MAX_BODY_PCT': 40.0,
    'R1_C3_MIN_BODY_PCT': 40.0,
    'R1_C3_MAX_SHADOW_PCT': 30.0,
    'R1_C3_VOL_VS_C1_PCT': 75.0,

    # Маршрут 2: Тень + объём-климакс (RED 2 / зеркало для LONG)
    'R2_C1_MIN_RANGE_PCT': 1.0,
    'R2_C1_MIN_VOL_MULT': 1.0,
    'R2_C1_MIN_BODY_PCT': 60.0,
    'R2_C1_MAX_SHADOW_PCT': 40.0,
    'R2_MIN_SHADOW_PCT': 7.0,
    'R2_VOL_OVERRIDE_MULT': 1.0,
    'R2_C3_MIN_BODY_PCT': 55.0,
    'R2_C3_MAX_SHADOW_PCT': 40.0,

    # Маршрут 3: Поглощение телом (RED 3 / зеркало для LONG)
    'R3_C1_MIN_VOL_MULT': 1.25,
    'R3_C1_MIN_BODY_PCT': 40.0,
    'R3_C1_MAX_SHADOW_PCT': 30.0,
    'R3_MIN_OVERLAP_PCT': 120.0,
    'R3_MAX_SHADOW_PCT': 20.0,
    'R3_MIN_BODY_PCT': 50.0,
    'R3_C3_MIN_BODY_PCT': 40.0,
    'R3_C3_MAX_SHADOW_PCT': 40.0,
}


def _metrics(o, h, l, c):
    """Базовые проценты свечи (геометрически, без привязки к направлению)."""
    hl = float(h - l)
    if hl <= 0:
        return {'body_pct': 0.0, 'top_shadow_pct': 0.0, 'bottom_shadow_pct': 0.0,
                'range_pct': 0.0, 'is_green': c > o, 'abs_body': 0.0}
    is_green = c > o
    abs_body = abs(c - o)
    top_shadow = h - max(o, c)
    bottom_shadow = min(o, c) - l
    return {
        'body_pct': abs_body / hl * 100.0,
        'top_shadow_pct': top_shadow / hl * 100.0,
        'bottom_shadow_pct': bottom_shadow / hl * 100.0,
        'range_pct': (hl / float(c) * 100.0) if c else 0.0,
        'is_green': is_green,
        'abs_body': abs_body,
    }


def check_c1(o, h, l, c, v, baseline_vol, direction):
    """C1 — якорь. SHORT: зелёная свеча (памп). LONG: красная (зеркало).
    Возвращает (список_маршрутов[1,2,3], данные_C1) или ([], None)."""
    m = _metrics(o, h, l, c)
    safe_baseline = float(baseline_vol) if baseline_vol and baseline_vol > 0 else 0.0001
    vol_mult = float(v) / safe_baseline

    anchor_ok = m['is_green'] if direction == 'SHORT' else (not m['is_green'])
    if not anchor_ok:
        return [], None
    if vol_mult < CONFIG['C1_MIN_VOL_MULT'] or m['range_pct'] < CONFIG['C1_MIN_RANGE_PCT']:
        return [], None

    # У SHORT-якоря мешает "верхняя" тень (памп должен быть чистым),
    # у LONG-якоря — зеркально "нижняя".
    shadow_pct = m['top_shadow_pct'] if direction == 'SHORT' else m['bottom_shadow_pct']

    routes = []
    if vol_mult >= CONFIG['R1_C1_MIN_VOL_MULT'] and m['body_pct'] >= CONFIG['R1_C1_MIN_BODY_PCT'] and shadow_pct <= CONFIG['R1_C1_MAX_SHADOW_PCT']:
        routes.append(1)
    req_range2 = CONFIG.get('R2_C1_MIN_RANGE_PCT', CONFIG['C1_MIN_RANGE_PCT'])
    if vol_mult >= CONFIG['R2_C1_MIN_VOL_MULT'] and m['body_pct'] >= CONFIG['R2_C1_MIN_BODY_PCT'] and shadow_pct <= CONFIG['R2_C1_MAX_SHADOW_PCT'] and m['range_pct'] >= req_range2:
        routes.append(2)
    if vol_mult >= CONFIG['R3_C1_MIN_VOL_MULT'] and m['body_pct'] >= CONFIG['R3_C1_MIN_BODY_PCT'] and shadow_pct <= CONFIG['R3_C1_MAX_SHADOW_PCT']:
        routes.append(3)

    if not routes:
        return [], None
    c1_data = {'o': float(o), 'h': float(h), 'l': float(l), 'c': float(c), 'v': float(v), 'abs_body': m['abs_body']}
    return routes, c1_data


def check_c2(c1_data, o, h, l, c, v, active_routes, direction):
    """C2 — реакция. Возвращает список маршрутов, переживших эту свечу."""
    m = _metrics(o, h, l, c)
    shadow_pct = m['top_shadow_pct'] if direction == 'SHORT' else m['bottom_shadow_pct']
    raw_shadow = (h - max(o, c)) if direction == 'SHORT' else (min(o, c) - l)
    # C2 "рабочего" цвета: красная для SHORT, зелёная для LONG (нужна для маршрутов 2 и 3)
    reaction_ok = (not m['is_green']) if direction == 'SHORT' else m['is_green']
    c1_body = c1_data['abs_body']

    survivors = []

    # Маршрут 3: поглощение телом C1
    if 3 in active_routes and reaction_ok:
        overlap_target = (c1_data['c'] - c1_body * (CONFIG['R3_MIN_OVERLAP_PCT'] / 100.0)) if direction == 'SHORT' \
            else (c1_data['c'] + c1_body * (CONFIG['R3_MIN_OVERLAP_PCT'] / 100.0))
        overlap_ok = (float(c) <= overlap_target) if direction == 'SHORT' else (float(c) >= overlap_target)
        if overlap_ok and m['body_pct'] >= CONFIG['R3_MIN_BODY_PCT'] and shadow_pct <= CONFIG['R3_MAX_SHADOW_PCT']:
            survivors.append(3)

    # Маршрут 2: климакс-объём + тень
    if 2 in active_routes and reaction_ok:
        req_vol = c1_data['v'] * CONFIG['R2_VOL_OVERRIDE_MULT']
        if shadow_pct >= CONFIG['R2_MIN_SHADOW_PCT'] and float(v) >= req_vol:
            survivors.append(2)

    # Маршрут 1: пин-бар (цвет C2 не важен)
    if 1 in active_routes:
        shadow_ratio = (raw_shadow / m['abs_body']) if m['abs_body'] > 0 else 999.0
        if shadow_ratio >= CONFIG['R1_C2_MIN_SHADOW_MULT'] and m['body_pct'] <= CONFIG['R1_C2_MAX_BODY_PCT']:
            survivors.append(1)

    return survivors


def check_c3(c1_data, active_routes, o, h, l, c, v, direction):
    """C3 — подтверждение. Возвращает сработавший маршрут (1/2/3) или None."""
    m = _metrics(o, h, l, c)
    shadow_pct = m['top_shadow_pct'] if direction == 'SHORT' else m['bottom_shadow_pct']
    confirm_ok = (not m['is_green']) if direction == 'SHORT' else m['is_green']
    if not confirm_ok:
        return None

    if 3 in active_routes:
        if m['body_pct'] >= CONFIG['R3_C3_MIN_BODY_PCT'] and shadow_pct <= CONFIG['R3_C3_MAX_SHADOW_PCT']:
            return 3
    if 2 in active_routes:
        if m['body_pct'] >= CONFIG['R2_C3_MIN_BODY_PCT'] and shadow_pct <= CONFIG['R2_C3_MAX_SHADOW_PCT']:
            return 2
    if 1 in active_routes:
        engulfing_ok = (float(c) <= c1_data['o']) if direction == 'SHORT' else (float(c) >= c1_data['o'])
        req_vol = c1_data['v'] * (CONFIG['R1_C3_VOL_VS_C1_PCT'] / 100.0)
        if engulfing_ok and m['body_pct'] >= CONFIG['R1_C3_MIN_BODY_PCT'] and shadow_pct <= CONFIG['R1_C3_MAX_SHADOW_PCT'] and float(v) >= req_vol:
            return 1
    return None
