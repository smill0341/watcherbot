"""
levels_builder.py
==================
Чистый модуль расчёта уровней. Не знает про биржу, расписание, Telegram.
Принимает готовые DataFrame (1D и 4H), возвращает словарь зон,
АКТУАЛЬНЫХ на момент current_idx (на момент скана).

ИЕРАРХИЯ БАЗОВЫХ БАЛЛОВ (откуда пришёл уровень):
    PMH/PML (месяц, закрытый)        -> 5
    PWH/PWL (неделя, закрытая)       -> 5
    find_peaks + lookahead (1D)      -> 4
    4H POC (volume profile)          -> 3 самостоятельно, либо +1 confluence-бонус к существующей зоне
    PDH/PDL (день, закрытый)         -> слой не используется в build_levels (мёртвый код)
    Месяц + неделя сошлись в одной зоне -> пол 9 (см. MIN_SCORE_MONTH_WEEK_CONFLUENCE)

    Значения PMH/PML и PWH/PWL подняты с 0 по факту бэктестов: календарные
    уровни без find_peaks/POC confluence стабильно показывали положительный
    результат несколько месяцев подряд, тогда как "самодоказанный" POC —
    гораздо более волатильно. См. обсуждение в истории проекта.

МОДИФИКАТОРЫ SCORE:
    - Reaction count (сколько раз цена касалась зоны и отбивалась без закрытия
      за пределами) -> +0.5 за каждый эпизод, максимум +2
    - Объём на формирующей свече (если объём > 2x среднего за период) -> +0.5

MITIGATED:
    Если цена закрылась за пределами зоны после её формирования (зона пробита
    насквозь) - зона считается мёртвой и НЕ ПОПАДАЕТ в финальный результат.
    Без этого подтверждения слом структуры (CHoCH) не считается доказанным,
    поэтому role-reversal (старый support становится resistance) здесь не
    делается - это отдельная задача более высокого уровня (watcher), не этого
    модуля. См. NOTES_role_reversal.md.

ДИСТАНЦИЯ:
    Вместо жёсткого процента от цены используется динамический порог:
    max_distance = ATR(1W) * ATR_DISTANCE_MULTIPLIER.
    Волатильные монеты сами расширяют себе радар, спокойные - сужают.
    Это фильтр релевантности (зона физически достижима в обозримые дни),
    а не "архив" - архива в этой системе нет, выводятся только актуальные
    на момент current_idx уровни.

Merge пересекающихся зон (merge_overlapping_zones): база самого сильного
источника + бонус за каждое доп. наложение, с потолком MAX_MERGE_BONUS.
"""

import pandas as pd
import numpy as np
from scipy.signal import find_peaks

# =========================================================
# БАЗОВЫЕ ВЕСА ИСТОЧНИКОВ
# =========================================================
# Изначальная идея была: доказанный рынком сигнал (find_peaks/POC) > календарная
# метка (PWH/PMH). На практике за несколько месяцев бэктеста вышло НАОБОРОТ —
# одиночные PWH/PML стабильно прибыльны, а POC гораздо более волатилен
# (то сильно лучше, то сильно хуже месяц от месяца). Веса ниже подогнаны под
# фактический результат, а не под изначальную теорию — это сознательный выбор.
SCORE_FIND_PEAKS = 4.0
SCORE_POC = 3.0
SCORE_PWH_PWL = 5.0  # Раньше было 0 (без confluence = мусор) — по факту бэктестов
                       # одиночные PWL стабильно давали положительный результат
                       # каждый месяц, поднято до 5, чтобы не отсеивались фильтром MIN_SCORE
SCORE_PMH_PML = 5.0  # См. комментарий к SCORE_PWH_PWL — та же причина
SCORE_PDH_PDL = 0.0  # Без confluence — календарная метка = мусор (слой не используется в build_levels)
CONFLUENCE_BONUS = 2.0  # бонус за совпадение с find_peaks или POC
POC_BONUS = 1.0  # дополнительный бонус если POC совпал с существующей зоной
# Пол score для зон, где сошлись календарные уровни РАЗНЫХ таймфреймов
# (месяц + неделя, напр. 1M_low_PML + 1W_low_PWL) — временная мера, до
# полного пересмотра скоринга. См. merge_overlapping_zones().
MIN_SCORE_MONTH_WEEK_CONFLUENCE = 9.0

# find_peaks-слой (lookahead-фильтр - старый алгоритм)
IMPULSE_ATR_MULTIPLIER = 2.5
IMPULSE_LOOKAHEAD_DAYS = 10
FIND_PEAKS_DISTANCE = 15
FIND_PEAKS_PROMINENCE_MULT = 1.5

# Сколько последних закрытых недель/месяцев/дней проверяем на актуальность
PERIODS_MONTHS_BACK = 3   # последние 3 закрытых месяца
PERIODS_WEEKS_BACK = 4     # последние 4 закрытых недели
PERIODS_DAYS_BACK = 5       # последние 5 закрытых дней (PDH/PDL)

# Дистанция релевантности: max_distance = ATR(1W) * этот множитель.
# Заменяет старый жёсткий процент от цены - адаптируется к волатильности монеты.
ATR_DISTANCE_MULTIPLIER = 2.0

# Порог объёмного бонуса: во сколько раз объём формирующей свечи должен
# превышать средний объём периода, чтобы получить бонус
VOLUME_SPIKE_MULTIPLIER = 2.0

# Потолок бонуса score за слияние нескольких источников в одну зону
MAX_MERGE_BONUS = 3.0

MAJORS = ["BTC", "ETH", "SOL", "BNB"]


def calculate_atr(df, period=14):
    """Average True Range по стандартной формуле."""
    high_low = df["high"] - df["low"]
    high_cp = (df["high"] - df["close"].shift()).abs()
    low_cp = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def _calc_weekly_atr(df_1d, current_idx):
    """
    Приближённый недельный ATR из дневных данных: ATR(1D) * sqrt(7).
    Используется только для расчёта дистанции релевантности,
    не для ширины самих зон (там используется обычный дневной ATR).
    """
    daily_atr = calculate_atr(df_1d, 14).iloc[current_idx]
    if pd.isna(daily_atr) or daily_atr == 0:
        daily_atr = df_1d['close'].iloc[current_idx] * 0.05
    return float(daily_atr) * (7 ** 0.5)


def _is_mitigated(df_1d, idx, price, is_support, current_idx=None):
    """
    Проверяет, прошла ли цена через уровень с ЗАКРЫТИЕМ за его пределами
    после момента формирования уровня (idx). True = уровень мёртв.
    """
    if current_idx is None:
        current_idx = len(df_1d) - 1
    if idx >= current_idx:
        return False

    future_closes = df_1d['close'].iloc[idx + 1: current_idx + 1]
    if future_closes.empty:
        return False

    if is_support:
        return bool((future_closes < price).any())
    else:
        return bool((future_closes > price).any())


def _count_reactions(df_1d, idx, price, atr_value, is_support, current_idx=None):
    """
    Считает количество ЭПИЗОДОВ касания зоны (не отдельных свечей).
    Эпизод = последовательность подряд идущих свечей внутри зоны.
    Засчитывается реакцией, если эпизод завершился без закрытия за пределами
    (то есть был отбой, а не пробой).
    """
    if current_idx is None:
        current_idx = len(df_1d) - 1
    if idx >= current_idx:
        return 0

    zone_half = atr_value * 0.5
    zone_min, zone_max = price - zone_half, price + zone_half

    window = df_1d.iloc[idx + 1: current_idx + 1].reset_index(drop=True)
    if window.empty:
        return 0

    reactions = 0
    in_episode = False
    episode_broke_through = False

    for _, row in window.iterrows():
        touched = (row['low'] <= zone_max) and (row['high'] >= zone_min)

        if touched:
            if not in_episode:
                in_episode = True
                episode_broke_through = False
            if is_support and row['close'] < zone_min:
                episode_broke_through = True
            elif not is_support and row['close'] > zone_max:
                episode_broke_through = True
        else:
            if in_episode:
                if not episode_broke_through:
                    reactions += 1
                in_episode = False

    if in_episode and not episode_broke_through:
        reactions += 1

    return reactions


def _volume_bonus(df, idx, lookback=30):
    """+0.5 если объём формирующей свечи в VOLUME_SPIKE_MULTIPLIER раз больше
    среднего объёма за lookback свечей до неё."""
    if idx < lookback:
        return 0.0
    avg_vol = df['volume'].iloc[max(0, idx - lookback):idx].mean()
    if avg_vol <= 0 or pd.isna(avg_vol):
        return 0.0
    this_vol = df['volume'].iloc[idx]
    if this_vol >= avg_vol * VOLUME_SPIKE_MULTIPLIER:
        return 0.5
    return 0.0


def _calculate_zone_age(df, idx, current_idx=None):
    """Считает количество дней между формированием уровня (idx) и текущим моментом."""
    if current_idx is None:
        current_idx = len(df) - 1
    
    ts_zone = pd.to_datetime(df['timestamp'].iloc[idx], unit='ms')
    ts_current = pd.to_datetime(df['timestamp'].iloc[current_idx], unit='ms')
    age_days = (ts_current - ts_zone).days
    return max(0, age_days)


def _build_zone(price, atr_value, base_score, zone_type, date_str,
                 mitigated, reaction_count=0, volume_bonus=0.0):
    """
    Собирает зону. mitigated-зоны тоже строятся (нужно для фильтрации выше),
    но в финальный результат build_levels() они не попадают.
    
    Логика scoring:
    - base_score: find_peaks (4.0) или POC (3.0) или 0 (календарная метка без confluence)
    - reaction_count: до 3 касаний +0.5 за каждое (подтверждение уровня), 
                      после 4+ касаний -0.5 каждое (истощение ликвидности)
    - volume_bonus: +0.5 за спайк объёма на формирующей свече
    
    Freshness (возраст уровня) НЕ учитывается — по SMC теории уровень
    актуален до момента пробоя (mitigated), возраст вторичен.
    """
    score = base_score
    
    # Reaction count логика: до 3 бонус, после 4+ штраф (истощение ликвидности)
    if reaction_count <= 3:
        score += reaction_count * 0.5
    else:
        score += 1.5  # макс бонус за 3 касания
        score -= (reaction_count - 3) * 0.5  # штраф за каждое касание свыше 3
    
    score += volume_bonus
    
    score = max(score, 0.0)  # score не может быть отрицательным

    return {
        "min": float(price - atr_value * 0.5),
        "max": float(price + atr_value * 0.5),
        "score": round(float(score), 2),
        "type": zone_type,
        "date": date_str,
        "mitigated": bool(mitigated),
        "reaction_count": int(reaction_count),
    }


def _extract_period_extremes(df_1d, freq, n_periods_back, current_price, atr_1d,
                              max_distance, zone_label_high, zone_label_low,
                              current_idx=None):
    """
    Общая функция для PWH/PWL и PMH/PML.
    Ресемплит df_1d в недели/месяцы, берёт последние n_periods_back ЗАКРЫТЫХ
    периодов, для каждого создаёт зону по high и по low.
    Зоны дальше max_distance от текущей цены не создаются.
    
    ВАЖНО: PWH/PWL/PMH/PML БЕЗ confluence (совпадение с find_peaks или POC)
    получают base_score = 0 (мусор). С confluence — получают бонус.
    """
    if current_idx is None:
        current_idx = len(df_1d) - 1

    work = df_1d.iloc[:current_idx + 1].copy()
    work['dt'] = pd.to_datetime(work['timestamp'], unit='ms')
    work = work.set_index('dt')

    resampled = work.resample(freq, label='left').agg({'high': 'max', 'low': 'min', 'volume': 'sum'})
    resampled = resampled.dropna()

    if len(resampled) > 0:
        offset = pd.tseries.frequencies.to_offset(freq)
        last_period_start = resampled.index[-1]
        last_period_end = (offset.rollforward(last_period_start)
                            if offset.rollforward(last_period_start) != last_period_start
                            else last_period_start + offset)
        now_ts = work.index[-1]
        if now_ts < last_period_end:
            resampled = resampled.iloc[:-1]

    if resampled.empty:
        return []

    recent = resampled.tail(n_periods_back)
    zones = []

    for period_start, row in recent.iterrows():
        for price, is_support, label in [
            (row['high'], False, zone_label_high),
            (row['low'], True, zone_label_low),
        ]:
            if pd.isna(price):
                continue
            if abs(price - current_price) > max_distance:
                continue

            period_mask = (pd.to_datetime(df_1d['timestamp'], unit='ms') >= period_start)
            candidates = df_1d[period_mask]
            if candidates.empty:
                continue
            idx_ref = candidates.index[0]

            mitigated = _is_mitigated(df_1d, idx_ref, price, is_support, current_idx)
            reactions = _count_reactions(df_1d, idx_ref, price, atr_1d, is_support, current_idx)
            vol_bonus = _volume_bonus(df_1d, idx_ref)

            # Определяем base_score из константы наверху файла (SCORE_PMH_PML/
            # SCORE_PWH_PWL) — раньше тут был захардкожен 0.0, из-за чего
            # правка констант (0 -> 5, по факту бэктестов) никогда не применялась
            # к реальным зонам. См. историю обсуждения проекта.
            date_str = period_start.strftime('%Y-%m-%d')
            base_score = SCORE_PMH_PML if freq == 'ME' else SCORE_PWH_PWL

            # Ширина зоны — тот же принцип, что уже используется для MACRO-слоя
            # (см. _extract_macro_swings): фиксированный % от цены САМОЙ ТОЧКИ
            # (хай/лоу закрытого периода), а не от живого atr_1d. atr_1d каждый
            # 12ч-пересчёт немного другой (скользящее окно), из-за чего границы
            # PMH/PML/PWH/PWL слегка дрожали между снимками, даже когда сама
            # точка (price) уже давно зафиксирована. reactions/mitigated ниже
            # по-прежнему используют живой atr_1d — это другой расчёт (допуск
            # на касание), его не трогаем.
            zone_width_ref = price * 0.015

            zone = _build_zone(price, zone_width_ref, base_score, label, date_str,
                                mitigated=mitigated, reaction_count=reactions, 
                                volume_bonus=vol_bonus)
            zone['_is_support'] = is_support
            zones.append(zone)

    return zones


def _extract_daily_extremes(df_1d, n_days_back, current_price, atr_1d,
                             max_distance, current_idx=None):
    """PDH/PDL - последние n закрытых дней, в пределах max_distance от цены.
    БЕЗ confluence = score 0 (мусор), WITH confluence = бонус."""
    if current_idx is None:
        current_idx = len(df_1d) - 1

    zones = []
    start = max(0, current_idx - n_days_back)
    for idx in range(start, current_idx):
        row = df_1d.iloc[idx]
        ts = row['timestamp']
        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')

        for price, is_support in [(row['high'], False), (row['low'], True)]:
            if abs(price - current_price) > max_distance:
                continue

            mitigated = _is_mitigated(df_1d, idx, price, is_support, current_idx)
            reactions = _count_reactions(df_1d, idx, price, atr_1d, is_support, current_idx)
            vol_bonus = _volume_bonus(df_1d, idx)

            label = "1d_low_PDL" if is_support else "1d_high_PDH"
            base_score = 0.0  # PDH/PDL БЕЗ confluence = score 0
            
            zone = _build_zone(price, atr_1d, base_score, label, date_str,
                                mitigated=mitigated, reaction_count=reactions,
                                volume_bonus=vol_bonus)
            zone['_is_support'] = is_support
            zones.append(zone)

    return zones


def _extract_find_peaks_layer(df_1d, current_price, atr_1d, max_distance, current_idx=None):
    """
    find_peaks слой с lookahead-фильтром. Зоны дальше max_distance отсекаются -
    единственный фильтр по расстоянию, без понятия архива/давности.
    find_peaks имеет собственное доказательство (цена развернулась здесь) = base_score 4.0
    """
    if current_idx is None:
        current_idx = len(df_1d) - 1

    work = df_1d.iloc[:current_idx + 1]
    if len(work) < 50:
        return []

    atr_series = calculate_atr(work, 14)
    local_atr_default = atr_series.iloc[-1]
    if pd.isna(local_atr_default) or local_atr_default == 0:
        local_atr_default = work['close'].iloc[-1] * 0.05

    peaks, _ = find_peaks(work['high'], distance=FIND_PEAKS_DISTANCE,
                           prominence=local_atr_default * FIND_PEAKS_PROMINENCE_MULT)
    valleys, _ = find_peaks(-work['low'], distance=FIND_PEAKS_DISTANCE,
                             prominence=local_atr_default * FIND_PEAKS_PROMINENCE_MULT)

    zones = []

    for v in valleys:
        price = float(work['low'].iloc[v])
        if abs(price - current_price) > max_distance:
            continue

        local_atr = atr_series.iloc[v]
        if pd.isna(local_atr) or local_atr == 0:
            local_atr = local_atr_default

        lookahead = work['high'].iloc[v + 1: v + 1 + IMPULSE_LOOKAHEAD_DAYS]
        if lookahead.empty:
            continue
        if lookahead.max() < price + (local_atr * IMPULSE_ATR_MULTIPLIER):
            continue

        ts = work['timestamp'].iloc[v]
        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')

        mitigated = _is_mitigated(df_1d, v, price, True, current_idx)
        reactions = _count_reactions(df_1d, v, price, local_atr, True, current_idx)
        vol_bonus = _volume_bonus(df_1d, v)

        zone = _build_zone(price, local_atr, SCORE_FIND_PEAKS, "1d_extreme_peak", date_str,
                            mitigated=mitigated, reaction_count=reactions, 
                            volume_bonus=vol_bonus)
        zone['_is_support'] = True
        zones.append(zone)

    for p in peaks:
        price = float(work['high'].iloc[p])
        if abs(price - current_price) > max_distance:
            continue

        local_atr = atr_series.iloc[p]
        if pd.isna(local_atr) or local_atr == 0:
            local_atr = local_atr_default

        lookahead = work['low'].iloc[p + 1: p + 1 + IMPULSE_LOOKAHEAD_DAYS]
        if lookahead.empty:
            continue
        if lookahead.min() > price - (local_atr * IMPULSE_ATR_MULTIPLIER):
            continue

        ts = work['timestamp'].iloc[p]
        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')

        mitigated = _is_mitigated(df_1d, p, price, False, current_idx)
        reactions = _count_reactions(df_1d, p, price, local_atr, False, current_idx)
        vol_bonus = _volume_bonus(df_1d, p)

        zone = _build_zone(price, local_atr, SCORE_FIND_PEAKS, "1d_extreme_peak", date_str,
                            mitigated=mitigated, reaction_count=reactions, 
                            volume_bonus=vol_bonus)
        zone['_is_support'] = False
        zones.append(zone)

    return zones


def _get_poc(df_4h, current_price):
    """POC по 4H volume profile. Возвращает (poc_price, atr_4h) либо (None, None)."""
    if len(df_4h) < 50:
        return None, None

    atr_4h = calculate_atr(df_4h, 14).iloc[-1]
    if pd.isna(atr_4h) or atr_4h == 0:
        atr_4h = df_4h['close'].iloc[-1] * 0.02

    typical = (df_4h['high'] + df_4h['low'] + df_4h['close']) / 3
    min_val, max_val = df_4h['low'].min(), df_4h['high'].max()
    if max_val <= min_val:
        return None, None

    bins = np.linspace(min_val, max_val, 50)
    bin_idx = pd.cut(typical, bins=bins)
    vol_profile = df_4h.groupby(bin_idx, observed=False)['volume'].sum()

    poc_bin = vol_profile.idxmax()
    if pd.isna(poc_bin) or not hasattr(poc_bin, "mid"):
        return None, None

    return float(poc_bin.mid), float(atr_4h)


def _apply_poc_confluence(zones, poc_price, atr_4h, current_idx_date=None):
    """
    Если POC попадает в существующую зону (find_peaks/PWH/PMH/PDH) -
    добавляем бонус + confluence bonus за совпадение.
    Если POC НЕ совпал ни с одной зоной - создаёт СВОЮ самостоятельную зону
    со SCORE_POC, потому что объём - это собственное рыночное доказательство.
    
    При совпадении:
    - найти ЛЮБУЮ существующую зону = даём ей CONFLUENCE_BONUS (2.0)
    - плюс дополнительный POC_BONUS (1.0) к её score
    """
    if poc_price is None:
        return zones
    poc_half = atr_4h * 0.5
    poc_min, poc_max = poc_price - poc_half, poc_price + poc_half

    matched_any = False
    for z in zones:
        overlap = min(z['max'], poc_max) - max(z['min'], poc_min)
        if overlap > 0:
            # POC совпал с зоной -> только POC_BONUS (+1.0).
            # CONFLUENCE_BONUS тут НЕ добавляем - за "источники совпали" уже отвечает
            # merge_overlapping_zones. Иначе одно совпадение считается дважды.
            z['score'] = round(z['score'] + POC_BONUS, 2)
            existing_type = z.get('type', '')
            if '4h_poc' not in existing_type:
                z['type'] = f"{existing_type}+4h_poc"
            matched_any = True

    if not matched_any:
        # POC сам по себе - самостоятельная зона с собственным доказательством (объём)
        poc_zone = {
            "min": float(poc_min),
            "max": float(poc_max),
            "score": SCORE_POC,
            "type": "4h_poc_standalone",
            "date": current_idx_date or "",
            "mitigated": False,
            "reaction_count": 0,
        }
        zones.append(poc_zone)

    return zones


def _apply_confluence_bonus(zones):
    """
    Раздаёт confluence бонус календарным уровням (PWH/PMH/PDH с score=0)
    если они пересекаются с найденными пиками/впадинами (find_peaks с score=4).
    После этого календарный уровень получит score = 0 + CONFLUENCE_BONUS = 2.0
    """
    find_peaks_zones = [z for z in zones if 'extreme_peak' in z.get('type', '')]
    calendar_zones = [z for z in zones if 'PMH' in z.get('type', '') or 'PWH' in z.get('type', '') 
                      or 'PDH' in z.get('type', '')]
    
    for cal_zone in calendar_zones:
        for fp_zone in find_peaks_zones:
            # Проверяем пересечение
            overlap = min(cal_zone['max'], fp_zone['max']) - max(cal_zone['min'], fp_zone['min'])
            if overlap > 0:
                # Даём calendar уровню confluence бонус
                cal_zone['score'] = round(cal_zone['score'] + CONFLUENCE_BONUS, 2)
                cal_zone['type'] = f"{cal_zone['type']}+confluence"
                break  # достаточно одного совпадения с find_peaks
    
    return zones


def merge_overlapping_zones(zones):
    """Слияние пересекающихся зон.
    Score = база сильнейшего источника. БОНУС за количество наслоений УБРАН -
    раньше он задваивал confluence (один и тот же факт "уровни совпали" считался
    и тут, и в _apply_confluence_bonus). Теперь merge только объединяет геометрию,
    а за подтверждение find_peaks/POC отвечают отдельные функции ПОСЛЕ merge.

    Исключение: если слились РАЗНЫЕ по природе источники (календарный + find_peaks),
    это настоящая confluence -> +CONFLUENCE_BONUS один раз.

    Отдельный, более сильный случай: если в одной зоне сошлись календарные
    уровни РАЗНЫХ таймфреймов (месяц + неделя, например 1M_low_PML + 1W_low_PWL) -
    это структурная confluence сама по себе, без find_peaks/POC. Временно (до
    полного пересмотра скоринга) даём такой зоне пол MIN_SCORE_MONTH_WEEK_CONFLUENCE,
    ниже которого score упасть не может."""
    if not zones:
        return []

    sorted_zones = sorted(zones, key=lambda x: x['min'])

    for z in sorted_zones:
        z['base_score'] = z.get('score', 0.0)
        z['_has_peak'] = 'extreme_peak' in z.get('type', '')
        z['_has_calendar'] = any(t in z.get('type', '') for t in ('PMH', 'PML', 'PWH', 'PWL'))
        z['_has_month_cal'] = any(t in z.get('type', '') for t in ('PMH', 'PML'))
        z['_has_week_cal'] = any(t in z.get('type', '') for t in ('PWH', 'PWL'))

    merged = [sorted_zones[0]]

    for current in sorted_zones[1:]:
        last = merged[-1]
        if current['min'] <= last['max']:
            last['max'] = max(last['max'], current['max'])
            last['base_score'] = max(last['base_score'], current['base_score'])
            last['_has_peak'] = last['_has_peak'] or current['_has_peak']
            last['_has_calendar'] = last['_has_calendar'] or current['_has_calendar']
            last['_has_month_cal'] = last['_has_month_cal'] or current['_has_month_cal']
            last['_has_week_cal'] = last['_has_week_cal'] or current['_has_week_cal']

            # Настоящая confluence: календарный уровень совпал с find_peaks -> бонус ОДИН раз
            confluence = CONFLUENCE_BONUS if (last['_has_peak'] and last['_has_calendar']) else 0.0
            last['score'] = round(last['base_score'] + confluence, 2)

            # Пол для месяц+неделя confluence (см. докстринг)
            if last['_has_month_cal'] and last['_has_week_cal']:
                last['score'] = max(last['score'], MIN_SCORE_MONTH_WEEK_CONFLUENCE)

            if current.get('type') and last.get('type') and current['type'] not in last['type']:
                last['type'] = f"{last['type']} + {current['type']}"
            last['reaction_count'] = max(last.get('reaction_count', 0), current.get('reaction_count', 0))
        else:
            merged.append(current)

    for m in merged:
        m.pop('base_score', None)
        m.pop('_has_peak', None)
        m.pop('_has_calendar', None)
        m.pop('_has_month_cal', None)
        m.pop('_has_week_cal', None)

    return merged


def compress_fat_zones(zones, coin):
    """Сжимает слишком широкие зоны к их центру."""
    max_width_pct = 0.03 if coin in MAJORS else 0.05
    for z in zones:
        center = (z['max'] + z['min']) / 2.0
        width = z['max'] - z['min']
        max_allowed_width = center * max_width_pct
        if width > max_allowed_width:
            new_half_width = max_allowed_width / 2.0
            z['min'] = center - new_half_width
            z['max'] = center + new_half_width
    return zones


def resolve_cross_overlaps(supports, resistances):
    """Удаляет слабую зону при жёстком пересечении support/resistance (>25%)."""
    to_remove_sup = set()
    to_remove_res = set()

    for i, s in enumerate(supports):
        for j, r in enumerate(resistances):
            if i in to_remove_sup or j in to_remove_res:
                continue
            overlap = min(r['max'], s['max']) - max(r['min'], s['min'])
            if overlap > 0:
                min_zone_width = min(r['max'] - r['min'], s['max'] - s['min'])
                if min_zone_width <= 0:
                    continue
                if (overlap / min_zone_width) > 0.25:
                    if s.get('score', 0) > r.get('score', 0):
                        to_remove_res.add(j)
                    elif r.get('score', 0) > s.get('score', 0):
                        to_remove_sup.add(i)
                    else:
                        to_remove_res.add(j)

    final_sup = [s for i, s in enumerate(supports) if i not in to_remove_sup]
    final_res = [r for j, r in enumerate(resistances) if j not in to_remove_res]
    return final_sup, final_res


def _extract_macro_swings(df, current_price, max_distance, base_score, label_prefix):
    """Ищет структурные макро-свинги (по теням) и отсеивает пробитые."""
    if df is None or len(df) < 5:
        return []
        
    work = df.copy()
    # Фрактал: 2 свечи слева, 1 центр, 2 справа
    work['is_swing_low'] = (work['low'] == work['low'].rolling(window=5, center=True).min())
    work['is_swing_high'] = (work['high'] == work['high'].rolling(window=5, center=True).max())
    
    zones = []
    
    # Проходим по графику, отступив края
    for i in range(2, len(work) - 2):
        ts = work['timestamp'].iloc[i]
        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')
        
        # --- Поддержка (LONG) ---
        if work['is_swing_low'].iloc[i]:
            price = float(work['low'].iloc[i])
            if abs(price - current_price) <= max_distance:
                # Проверяем, пробит ли уровень закрытием (mitigated)
                mitigated = False
                future_closes = work['close'].iloc[i + 3:]
                if not future_closes.empty and (future_closes < price).any():
                    mitigated = True
                    
                if not mitigated:
                    # Узкая зона (1.5% от цены), так как ATR на макро слишком широкий
                    atr_fake = price * 0.015 
                    zone = _build_zone(price, atr_fake, base_score, f"{label_prefix}_Support", date_str, False)
                    zone['_is_support'] = True
                    zone['class'] = 'MACRO'
                    zones.append(zone)
                    
        # --- Сопротивление (SHORT) ---
        if work['is_swing_high'].iloc[i]:
            price = float(work['high'].iloc[i])
            if abs(price - current_price) <= max_distance:
                mitigated = False
                future_closes = work['close'].iloc[i + 3:]
                if not future_closes.empty and (future_closes > price).any():
                    mitigated = True
                    
                if not mitigated:
                    atr_fake = price * 0.015
                    zone = _build_zone(price, atr_fake, base_score, f"{label_prefix}_Resistance", date_str, False)
                    zone['_is_support'] = False
                    zone['class'] = 'MACRO'
                    zones.append(zone)
                    
    return zones

def build_levels(df_1M, df_1W, df_1d, df_4h, coin, current_idx=None):
    """
    Главная функция модуля.

    df_1d, df_4h: DataFrame с колонками timestamp, open, high, low, close, volume
    coin: тикер, нужен для compress_fat_zones (majors vs alts)
    current_idx: индекс "текущего момента" в df_1d (для бэктеста на срезе истории).
                 None = последняя свеча.

    Возвращает: {"supports": [...], "resistances": [...]}
    Только зоны, актуальные на момент current_idx:
      - не mitigated (цена не закрывалась за их пределами после формирования)
      - в пределах ATR(1W)*ATR_DISTANCE_MULTIPLIER от текущей цены
    Архива и понятия давности здесь нет.
    """
    if current_idx is None:
        current_idx = len(df_1d) - 1

    current_price = float(df_1d['close'].iloc[current_idx])
    df_1d = df_1d.copy()
    df_1d['atr'] = calculate_atr(df_1d, 14)
    atr_1d = df_1d['atr'].iloc[current_idx]
    if pd.isna(atr_1d) or atr_1d == 0:
        atr_1d = current_price * 0.05

    max_distance = _calc_weekly_atr(df_1d, current_idx) * ATR_DISTANCE_MULTIPLIER

    all_zones = []

    # --- НОВОЕ: НЕЗАВИСИМЫЕ МАКРО-УРОВНИ (MACRO) ---
    # max_distance умножаем, чтобы макро-радар видел дальше локального
    if df_1M is not None:
        all_zones += _extract_macro_swings(df_1M, current_price, max_distance * 2.5, 5.0, "1M_MACRO")
    if df_1W is not None:
        all_zones += _extract_macro_swings(df_1W, current_price, max_distance * 1.5, 4.0, "1W_MACRO")
    
    # 1. PMH/PML (месячные - старший масштаб)
    all_zones += _extract_period_extremes(
        df_1d, 'ME', PERIODS_MONTHS_BACK, current_price, atr_1d, max_distance,
        "1M_high_PMH", "1M_low_PML", current_idx
    )

    # 2. PWH/PWL (недельные)
    all_zones += _extract_period_extremes(
        df_1d, 'W', PERIODS_WEEKS_BACK, current_price, atr_1d, max_distance,
        "1W_high_PWH", "1W_low_PWL", current_idx
    )

    # 3. find_peaks слой (реальные развороты с импульсом)
    all_zones += _extract_find_peaks_layer(df_1d, current_price, atr_1d, max_distance, current_idx)

    # ПРИМЕЧАНИЕ: дневной слой (PDH/PDL) убран намеренно.
    # Проверено на данных: дневные зоны на 100% дублируют недельные/месячные/find_peaks
    # (0-2 уникальных из 10), не дают новой информации, только раздували score.

    # Убираем mitigated-зоны - они не актуальны без подтверждённого слома структуры.
    all_zones = [z for z in all_zones if not z['mitigated']]

    # Разделяем на supports/resistances и СНАЧАЛА сливаем дубли в одну зону.
    # Бонусы за confluence (find_peaks/POC) раздаём ТОЛЬКО ПОСЛЕ merge -
    # иначе одно совпадение считается несколько раз (на каждом дубле + merge_bonus).
    supports, resistances = [], []
    for z in all_zones:
        is_sup = z.pop('_is_support', None)
        z.pop('mitigated', None)
        if is_sup is True:
            supports.append(z)
        elif is_sup is False:
            resistances.append(z)

    supports = merge_overlapping_zones(supports)
    resistances = merge_overlapping_zones(resistances)

    # POC: ПОСЛЕ merge. Если совпал со слитой зоной - бонус один раз, иначе своя зона.
    if df_4h is not None and len(df_4h) >= 50:
        poc_price, atr_4h = _get_poc(df_4h, current_price)
        poc_date_str = pd.to_datetime(df_4h['timestamp'].iloc[-1], unit='ms').strftime('%Y-%m-%d')
        # POC отдельно для supports и resistances (poc - это уровень, может попасть в любой)
        all_merged = supports + resistances
        all_merged = _apply_poc_confluence(all_merged, poc_price, atr_4h, poc_date_str)
        # пересобираем: standalone POC-зона не имеет _is_support, кладём по стороне от цены
        supports, resistances = [], []
        for z in all_merged:
            if z.get('type') == '4h_poc_standalone':
                if z['max'] < current_price:
                    supports.append(z)
                else:
                    resistances.append(z)
            elif z['max'] <= current_price or z['min'] < current_price <= z['max']:
                # зона уже была среди supports/resistances, восстанавливаем сторону по цене
                if (z['min'] + z['max']) / 2 < current_price:
                    supports.append(z)
                else:
                    resistances.append(z)
            else:
                resistances.append(z)

    compress_fat_zones(supports, coin)
    compress_fat_zones(resistances, coin)
    supports, resistances = resolve_cross_overlaps(supports, resistances)

    return {
        "supports": supports,
        "resistances": resistances,
    }