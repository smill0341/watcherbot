# ============================================================================
# ТЕСТОВАЯ КОПИЯ levels_builder.py — «РОВНО СТОЛЬКО, СКОЛЬКО НАДО» + «НА МЕСТЕ ГЛАВНАЯ ПЕРВАЯ ТОЧКА»
# (только для levels_test.py --warmup / --timeline).
# Боевой код этот файл не импортирует. Отличия от боевого помечены словом «ПРОГРЕВ».
# Билдер получает свечи с запасом (LOOKBACK) перед обычным окном (WINDOWS):
#   ATR(14) и средний объём (30) считаются по ВСЕМ полученным свечам, у каждой свечи — по её настоящим
#   предыдущим; свинги ищутся только в обычном окне; дневная свеча МАКРО-свинга ищется во всех дневных
#   свечах (≈ 60 месяцев), а не только в последних 365.
# ============================================================================
"""
levels_builder.py — расчёт уровней (зон поддержки/сопротивления).
Не знает про биржу и расписание: принимает готовые DataFrame, возвращает зоны.

Точки (сырые уровни):
    MACRO-свинги 1M/1W, PMH/PML (месяц), PMC (закрытие месяца), PWH/PWL (неделя),
    пивоты 1d (k=5), пивоты 4h (k=6). Зона точки — от тени до тела свечи рождения
    (с зазором), у PMC — цена +- LINE_HALF_ATR*ATR. Точка мертва после дневного закрытия
    за уровнем. Дистанция: не дальше ATR(1W)*ATR_DISTANCE_MULTIPLIER от цены.

Сборка (build_levels) — через РЕЕСТР зон:
    зона, однажды выданная, стоит с теми же границами, пока её не пробьют закрытием;
    новая точка, пересекающаяся со стоящей зоной, поглощается ей (только в название);
    иначе — новая зона со своей геометрией. Без коэффициентов и допусков.
    POC — только бонус к score и метка. На сторону MAX_PER_SIDE ближайших, фолбэк до 2.
"""

import copy
import pandas as pd
import numpy as np
from backend.modules.cryptano.utils.indicators import calculate_atr

# =========================================================
# ВЕСА ИСТОЧНИКОВ (подобраны по бэктестам: календарные уровни стабильнее POC)
# =========================================================
SCORE_FIND_PEAKS = 4.0   # пивоты 1d (метка 1d_extreme_peak)
SCORE_POC = 3.0
SCORE_PWH_PWL = 5.0
SCORE_PMH_PML = 5.0
SCORE_PMC = 5.0
POC_BONUS = 1.0          # бонус, если POC совпал с зоной

# Потолок ширины MACRO-зоны: не больше K дневных ATR (одна волатильная месячная свеча давала зону 58-125% цены).
MACRO_ATR_CAP_MULTIPLIER = 3.0
# ATR месячной/недельной свечи -> дневной масштаб: делим на корень из числа торговых дней в периоде.
MACRO_ATR_DAYS = {"1M": 21.0, "1W": 5.0}

# Счётчики MACRO-слоёв за пересчёт (сбрасывает и печатает swing_hunter.py).
MACRO_LAYER_STATS = {'1M_raw': 0, '1W_raw': 0, '1M': 0, '1W': 0, '1W_no_data': 0}

# Окна поиска календарных уровней
PERIODS_MONTHS_BACK = 3       # PMH/PML — последние 3 закрытых месяца
PERIODS_WEEKS_BACK = 4        # PWH/PWL — последние 4 закрытых недели
PERIODS_MONTHS_BACK_PMC = 12  # PMC — граница месяца может быть давней (консолидация у старой границы)

# Дистанция релевантности: max_distance = ATR(1W) * этот множитель.
ATR_DISTANCE_MULTIPLIER = 2.0

# Объёмный бонус: объём свечи рождения больше среднего в столько раз -> +0.5
VOLUME_SPIKE_MULTIPLIER = 2.0

MAJORS = ["BTC", "ETH", "SOL", "BNB"]

# Геометрия зоны и сборка
LINE_HALF_ATR = 0.25          # линия-уровень (закрытие месяца): зона = цена +- 0.25 ATR
WICK_GAP_ATR = 0.10           # зазор за тенью (цена не всегда долетает до экстремума)
WICK_MIN_INWARD_ATR = 0.25    # минимальная глубина зоны от тени внутрь
WICK_MAX_INWARD_ATR = 1.00    # максимальная глубина зоны от тени внутрь (до тела свечи, но не глубже)
PIVOT_K_1D = 5                # пивот на дневках: 5 свечей слева и справа ниже/выше
PIVOT_K_4H = 6                # пивот на 4h: 6 свечей (сутки) слева и справа
SCORE_PIVOT_4H = 3.5
MAX_PER_SIDE = 2              # сколько ближайших непробитых зон выдаём на сторону
REJECTION_SCORE_STEP = 0.5    # score = база источника + 0.5 за каждый отбой от готовой зоны
REJECTION_SCORE_CAP = 4


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


def count_zone_touches(df_1d, zone, is_support, current_idx=None):
    """Касания итоговой зоны по её границам (min/max), для всех типов уровней.
    По дневным свечам, эпизодами: несколько дней подряд в зоне = одно касание; засчитывается,
    только если эпизод закончился отбоем (нет закрытия за дальним краем). Считаем со следующего
    дня после даты зоны; если дата старше истории — с начала истории.
    Вес (score) этим НЕ пересчитывается — только reaction_count."""
    if current_idx is None:
        current_idx = len(df_1d) - 1
    zmin, zmax = float(zone['min']), float(zone['max'])
    start_idx = 0
    date_str = zone.get('date')
    if date_str:
        try:
            dates = pd.to_datetime(df_1d['timestamp'], unit='ms')
            pos = int((dates < pd.Timestamp(str(date_str)[:10]) + pd.Timedelta(days=1)).sum())
            start_idx = pos  # первый день ПОСЛЕ даты зоны
        except Exception:
            start_idx = 0
    if start_idx > current_idx:
        return 0

    window = df_1d.iloc[start_idx: current_idx + 1]
    reactions = 0
    in_episode = False
    broke = False
    for lo, hi, cl in zip(window['low'].values, window['high'].values, window['close'].values):
        touched = (lo <= zmax) and (hi >= zmin)
        if touched:
            if not in_episode:
                in_episode = True
                broke = False
            if is_support and cl < zmin:
                broke = True
            elif not is_support and cl > zmax:
                broke = True
        elif in_episode:
            if not broke:
                reactions += 1
            in_episode = False
    if in_episode and not broke:
        reactions += 1
    return reactions


def _volume_bonus(df, idx, lookback=30):
    """+0.5 если объём формирующей свечи в VOLUME_SPIKE_MULTIPLIER раз больше
    среднего объёма за lookback свечей до неё."""
    if lookback == 30 and "_vavg" in df.columns:   # ПРОГРЕВ: средний объём 30 свечей ДО этой, по всем полученным
        avg_vol = df['_vavg'].iloc[idx]
    else:
        if idx < lookback:
            return 0.0
        avg_vol = df['volume'].iloc[max(0, idx - lookback):idx].mean()
    if avg_vol <= 0 or pd.isna(avg_vol):
        return 0.0
    this_vol = df['volume'].iloc[idx]
    if this_vol >= avg_vol * VOLUME_SPIKE_MULTIPLIER:
        return 0.5
    return 0.0


def _build_zone(price, atr_value, base_score, zone_type, date_str,
                 mitigated, reaction_count=0, volume_bonus=0.0, activated_at=None):
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

    activated_at — дата, когда уровень технически ПОДТВЕРДИЛСЯ (а не когда
    сформировалась цена-экстремум, см. date_str). У большинства типов это
    позже date_str на фиксированную задержку (см. каждый _extract_* —
    например, MACRO-фрактал подтверждается только после 2 свечей справа
    от центра). Если экстрактор не передал свою — по умолчанию = date_str
    (безопасный fallback, ничего не сдвигает). Это поле определяет, с какой
    даты уровень рисуется на графике как "рабочий"."""
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
        "activated_at": activated_at or date_str,
        "mitigated": bool(mitigated),
        "reaction_count": int(reaction_count),
    }


def _atr_full(df, period=14):
    """ATR по свечам. У первых period-1 свечей окна позади меньше period свечей — ATR там NaN (честно «не
    посчитать»). Раньше там стояло среднее по тем свечам, что есть: оно зависело от начала окна выгрузки, и
    границы зоны на старых свечах сдвигались каждый день, пока окно ехало вперёд."""
    if period == 14 and "_atr" in df.columns:   # ПРОГРЕВ: посчитан по всем полученным свечам до обрезки окна
        return df["_atr"]
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()   # ПРОГРЕВ: у новой монеты — по свечам с листинга


def _atr_at(atr_arr, idx, default):
    """ATR на конкретной свече (а не «сегодняшний»): зона зависит только от истории до своей свечи.
    NaN — у этой свечи позади меньше 14 свечей (край окна), честного ATR нет: вызывающий решает сам."""
    try:
        a = float(atr_arr[idx])
    except (IndexError, TypeError, ValueError):
        return float(default)
    if np.isnan(a):
        return float('nan')
    return float(default) if a == 0 else a


def _wick_body_zone(o, c, low, high, is_support, atr):
    """Зона от ТЕНИ свечи внутрь до тела (как рисуют руками), с зазором за тенью.
    Поддержка: от low-зазор до min(тело) (глубина не меньше WICK_MIN_INWARD_ATR и не больше
    WICK_MAX_INWARD_ATR). Сопротивление — зеркально."""
    gap = WICK_GAP_ATR * atr
    lo_in, hi_in = WICK_MIN_INWARD_ATR * atr, WICK_MAX_INWARD_ATR * atr
    if is_support:
        inward = min(max(min(o, c) - low, lo_in), hi_in)
        return float(low - gap), float(low + inward)
    inward = min(max(high - max(o, c), lo_in), hi_in)
    return float(high - inward), float(high + gap)


def _extract_period_extremes(df_1d, freq, n_periods_back, current_price, atr_1d,
                              max_distance, zone_label_high, zone_label_low,
                              current_idx=None):
    """PWH/PWL (недели) и PMH/PML (месяцы): хай/лоу последних закрытых периодов.
    Новое: зона строится по ДНЮ, на котором был экстремум (тень -> тело этой свечи),
    а не ±0.5 ATR вокруг цены; пробой считается от этого дня (закрытие за экстремумом)."""
    if current_idx is None:
        current_idx = len(df_1d) - 1

    work = df_1d.iloc[:current_idx + 1].copy()
    work['dt'] = pd.to_datetime(work['timestamp'], unit='ms')
    work = work.set_index('dt')
    resampled = work.resample(freq, label='left').agg({'high': 'max', 'low': 'min', 'volume': 'sum'}).dropna()

    offset = pd.tseries.frequencies.to_offset(freq)
    if len(resampled) > 0:
        last_start = resampled.index[-1]
        last_end = offset.rollforward(last_start) if offset.rollforward(last_start) != last_start else last_start + offset
        if work.index[-1] < last_end:
            resampled = resampled.iloc[:-1]
    if resampled.empty:
        return []

    dates = pd.to_datetime(df_1d['timestamp'], unit='ms').to_numpy()
    o_arr = df_1d['open'].to_numpy(dtype=float)
    c_arr = df_1d['close'].to_numpy(dtype=float)
    l_arr = df_1d['low'].to_numpy(dtype=float)
    h_arr = df_1d['high'].to_numpy(dtype=float)
    atr_arr = _atr_full(df_1d, 14).to_numpy(dtype=float)

    zones = []
    for period_start, row in resampled.tail(n_periods_back).iterrows():
        period_end = offset.rollforward(period_start) if offset.rollforward(period_start) != period_start else period_start + offset
        date_str = period_end.strftime('%Y-%m-%d')
        # Дни периода — те же, что в resample: (period_start, period_end], т.е. неделя пн..вс, месяц 1..последнее
        # число. Раньше бралось [period_start, period_end) — со сдвигом на день назад, и если экстремум был
        # в последний день периода, зона строилась по чужой свече.
        pos = np.flatnonzero((dates > np.datetime64(period_start)) & (dates <= np.datetime64(period_end)))
        pos = pos[pos <= current_idx]
        if len(pos) == 0:
            continue
        for price, is_support, label in ((row['high'], False, zone_label_high), (row['low'], True, zone_label_low)):
            if pd.isna(price) or abs(price - current_price) > max_distance:
                continue
            idx_ext = int(pos[np.argmin(l_arr[pos])] if is_support else pos[np.argmax(h_arr[pos])])
            mitigated = _is_mitigated(df_1d, idx_ext, float(price), is_support, current_idx)
            vol_bonus = _volume_bonus(df_1d, idx_ext)
            base_score = SCORE_PMH_PML if freq == 'ME' else SCORE_PWH_PWL
            atr_p = _atr_at(atr_arr, idx_ext, atr_1d)
            if np.isnan(atr_p):   # свеча экстремума у самого края окна — ATR честно не посчитать
                continue
            zone = _build_zone(price, atr_p, base_score, label, date_str, mitigated=mitigated,
                                reaction_count=0, volume_bonus=vol_bonus)
            zone['min'], zone['max'] = _wick_body_zone(o_arr[idx_ext], c_arr[idx_ext], l_arr[idx_ext],
                                                       h_arr[idx_ext], is_support, atr_p)
            zone['_is_support'] = is_support
            zone['_known'] = int(pd.Timestamp(period_end).value // 10**6) + 86400000   # ИЗВЕСТНА: закрытие периода
            zones.append(zone)
    return zones


def _extract_monthly_close_levels(df_1d, months_back, current_price, atr_1d,
                                   max_distance, current_idx=None):
    """PMC — закрытие закрытого месяца (граница между месяцами).
    Новое: уровень живёт, пока ни одно дневное закрытие после конца месяца не оказалось по ДРУГУЮ
    сторону от него (как «naked» уровни). Раньше PMC никогда не считался пробитым и жил 12 месяцев.
    Сторона определяется по текущей цене. Зона = цена +- LINE_HALF_ATR*ATR."""
    if current_idx is None:
        current_idx = len(df_1d) - 1

    work = df_1d.iloc[:current_idx + 1].copy()
    work['dt'] = pd.to_datetime(work['timestamp'], unit='ms')
    work = work.set_index('dt')
    resampled = work.resample('ME', label='left').agg({'close': 'last'}).dropna()

    offset = pd.tseries.frequencies.to_offset('ME')
    if len(resampled) > 0:
        last_start = resampled.index[-1]
        last_end = offset.rollforward(last_start) if offset.rollforward(last_start) != last_start else last_start + offset
        if work.index[-1] < last_end:
            resampled = resampled.iloc[:-1]
    if resampled.empty:
        return []

    dates = pd.to_datetime(df_1d['timestamp'], unit='ms').to_numpy()
    atr_arr = _atr_full(df_1d, 14).to_numpy(dtype=float)
    zones = []
    for period_start, row in resampled.tail(months_back).iterrows():
        price = row['close']
        if pd.isna(price) or abs(price - current_price) > max_distance:
            continue
        period_end = offset.rollforward(period_start) if offset.rollforward(period_start) != period_start else period_start + offset
        date_str = period_end.strftime('%Y-%m-%d')
        # Дни периода — те же, что в resample: (period_start, period_end], т.е. неделя пн..вс, месяц 1..последнее
        # число. Раньше бралось [period_start, period_end) — со сдвигом на день назад, и если экстремум был
        # в последний день периода, зона строилась по чужой свече.
        pos = np.flatnonzero((dates > np.datetime64(period_start)) & (dates <= np.datetime64(period_end)))
        pos = pos[pos <= current_idx]
        if len(pos) == 0:
            continue
        idx_ref = int(pos[-1])
        is_support = bool(price < current_price)
        mitigated = _is_mitigated(df_1d, idx_ref, float(price), is_support, current_idx)
        vol_bonus = _volume_bonus(df_1d, idx_ref)
        atr_p = _atr_at(atr_arr, idx_ref, atr_1d)
        if np.isnan(atr_p):
            continue
        zone = _build_zone(price, atr_p, SCORE_PMC, "1M_close_PMC", date_str, mitigated=mitigated,
                            reaction_count=0, volume_bonus=vol_bonus)
        zone['min'] = float(price - LINE_HALF_ATR * atr_p)
        zone['max'] = float(price + LINE_HALF_ATR * atr_p)
        zone['_is_support'] = is_support
        zone['_known'] = int(pd.Timestamp(period_end).value // 10**6) + 86400000   # ИЗВЕСТНА: закрытие месяца
        zones.append(zone)
    return zones


def _extract_pivots(df, k, label, base_score, current_price, max_distance, current_idx=None):
    """Пивоты: свеча, у которой high (low) выше (ниже) всех k свечей слева и справа — общепринятое
    определение разворота (ta.pivothigh / pivotlow). Зона — от тени до тела этой свечи (с зазором).
    Живёт, пока нет закрытия за пивотом (в своём таймфрейме)."""
    if df is None:
        return []
    if current_idx is None:
        current_idx = len(df) - 1
    work = df.iloc[:current_idx + 1].reset_index(drop=True)
    n = len(work)
    if n < 2 * k + 15:
        return []
    atr_series = _atr_full(work, 14)
    default_atr = atr_series.iloc[-1]
    if pd.isna(default_atr) or default_atr == 0:
        default_atr = float(work['close'].iloc[-1]) * 0.05
    win = 2 * k + 1
    # При одинаковых экстремумах пивотом считается только ПЕРВАЯ свеча (слева строго выше/ниже),
    # иначе следующий снимок мог выбрать соседнюю свечу с тем же low/high — и границы уровня дёргались.
    is_ph = ((work['high'] == work['high'].rolling(win, center=True).max())
             & (work['high'] > work['high'].shift(1).rolling(k).max())).to_numpy()
    is_pl = ((work['low'] == work['low'].rolling(win, center=True).min())
             & (work['low'] < work['low'].shift(1).rolling(k).min())).to_numpy()
    o = work['open'].to_numpy(dtype=float)
    c = work['close'].to_numpy(dtype=float)
    lo = work['low'].to_numpy(dtype=float)
    hi = work['high'].to_numpy(dtype=float)
    atr_arr = atr_series.to_numpy(dtype=float)
    ts = work['timestamp'].to_numpy()
    step = int(ts[1] - ts[0]) if n > 1 else 0   # длина свечи этого ТФ, мс

    zones = []
    for i in range(k, n - k):
        for is_support, flag, price in ((True, is_pl[i], lo[i]), (False, is_ph[i], hi[i])):
            if not flag or abs(price - current_price) > max_distance:
                continue
            a = atr_arr[i]
            if np.isnan(a):   # свеча у края окна — ATR честно не посчитать
                continue
            if a == 0:
                a = default_atr
            mitigated = _is_mitigated(work, i, float(price), is_support, n - 1)
            date_str = pd.to_datetime(int(ts[i]), unit='ms').strftime('%Y-%m-%d')
            act_str = pd.to_datetime(int(ts[i + k]), unit='ms').strftime('%Y-%m-%d')
            zone = _build_zone(price, a, base_score, label, date_str, mitigated=mitigated,
                                reaction_count=0, volume_bonus=_volume_bonus(work, i), activated_at=act_str)
            zone['min'], zone['max'] = _wick_body_zone(o[i], c[i], lo[i], hi[i], is_support, float(a))
            zone['_is_support'] = is_support
            zone['_known'] = int(ts[i + k]) + step   # ИЗВЕСТНА: закрытие подтверждающей свечи
            zones.append(zone)
    return zones


def _extract_find_peaks_layer(df_1d, current_price, atr_1d, max_distance, current_idx=None):
    """Раньше: find_peaks с жёстким фильтром (пики не ближе 15 дней, 1.5 ATR, импульс 2.5 ATR за 10 дней) —
    находил только крупнейшие развороты. Теперь: обычные пивоты на дневках (имя функции и метка
    '1d_extreme_peak' оставлены, чтобы остальной код и графики не менялись)."""
    return _extract_pivots(df_1d, PIVOT_K_1D, "1d_extreme_peak", SCORE_FIND_PEAKS,
                           current_price, max_distance, current_idx)


def _extract_pivots_4h(df_4h, current_price, max_distance):
    """Пивоты на 4h — уровни внутри диапазона (например 82650 на графике BTC), которых нет на дневках."""
    if df_4h is None:
        return []
    return _extract_pivots(df_4h, PIVOT_K_4H, "4h_extreme_peak", SCORE_PIVOT_4H, current_price, max_distance)


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
    """POC совпал с зоной -> POC_BONUS и метка '+4h_poc'. НОВОЕ: самостоятельная POC-зона больше не
    создаётся (у неё нет разворота, нет своей стороны, она давала сделки без уровня)."""
    if poc_price is None:
        return zones
    poc_half = atr_4h * 0.5
    poc_min, poc_max = poc_price - poc_half, poc_price + poc_half
    for z in zones:
        overlap = min(z['max'], poc_max) - max(z['min'], poc_min)
        if overlap > 0:
            z['score'] = round(z['score'] + POC_BONUS, 2)
            existing_type = z.get('type', '')
            if '4h_poc' not in existing_type:
                z['type'] = f"{existing_type}+4h_poc"
    return zones


def _merge_type_labels(existing, incoming):
    """Склейка названий слитых зон ("Экстремум + Лоу месяца + ...") БЕЗ
    повторов.

    РАНЬШЕ в обоих слияниях стояло `if current['type'] not in last['type']`
    — проверка подстроки ЦЕЛОЙ строкой. Как только incoming сам был уже
    составным ("Экстремум + Лоу месяца"), а в existing те же части шли в
    другом порядке/с другими соседями — подстрока не находилась, и вся
    составная строка приклеивалась целиком, с дублями. Цепочка слияний
    раздувала название снежным комом: "Макро Поддержка + Экстремум + Лоу
    месяца + Лоу недели + Объём (POC) + Макро Поддержка + Экстремум + ..." —
    на графике не влезало в строку.

    Теперь разбираем обе строки на отдельные части по '+', добавляем только
    те, которых ещё нет, порядок первого появления сохраняется."""
    parts = []
    for label in (existing, incoming):
        if not label:
            continue
        for p in str(label).split('+'):
            p = p.strip()
            if p and p not in parts:
                parts.append(p)
    return " + ".join(parts)


TF_RANK = (("1M", 4), ("1W", 3), ("1d", 2), ("4h", 1))  # сила уровня по таймфрейму источника


def _anchor_type(z):
    return str(z.get('type') or '').split('+')[0].strip()


def _tf_rank(z):
    t = _anchor_type(z)
    for prefix, rank in TF_RANK:
        if t.startswith(prefix):
            return rank
    return 0


def compress_fat_zones(zones, coin):
    """Сжимает слишком широкие зоны к их центру с учетом класса уровня."""
    for z in zones:
        center = (z['max'] + z['min']) / 2.0
        width = z['max'] - z['min']

        # Разделяем лимиты: макро-зонам (по теням) даем больше пространства
        if z.get('class') == 'MACRO':
            max_width_pct = 0.15 if coin in MAJORS else 0.25
        else:
            max_width_pct = 0.03 if coin in MAJORS else 0.05

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


def _extract_macro_swings(df, current_price, max_distance, base_score, label_prefix, atr_1d=None, df_1d=None):
    """Ищет структурные макро-свинги (по теням SMC) и отсеивает пробитые.

    atr_1d: дневной ATR монеты (тот же, что использует PMH/PML) - нужен,
    чтобы ограничить размах зоны потолком MACRO_ATR_CAP_MULTIPLIER*atr_1d
    вместо сырого, ничем не ограниченного "тень-до-тела" одной свечи (см.
    комментарий у MACRO_ATR_CAP_MULTIPLIER выше). atr_1d=None (старое
    поведение, без обрезки) оставлен только для обратной совместимости -
    в проде всегда передаём реальный atr_1d.
    """
    if df is None or len(df) < 5:
        return []

    atr_cap = (atr_1d * MACRO_ATR_CAP_MULTIPLIER) if atr_1d else None
    atr_fb = float(atr_1d) if atr_1d else 0.0  # всегда число (для _atr_at и проверки типов)

    # ATR той же свечи, из которой сделана зона, в ЕЁ таймфрейме (месячной/недельной) — он есть всегда
    # (месяцев загружено 60, недель 150) и не меняется от скана к скану. Приводим к дневному масштабу
    # делением на корень из числа торговых дней в периоде (масштабирование волатильности по времени):
    # месяц /sqrt(21), неделя /sqrt(5). Первые свечи окна (меньше 14 до них) — NaN, зону там не строим.
    _scale = MACRO_ATR_DAYS.get(label_prefix[:2], 1.0) ** 0.5
    _h, _l, _c = df['high'], df['low'], df['close']
    _tr = pd.concat([_h - _l, (_h - _c.shift()).abs(), (_l - _c.shift()).abs()], axis=1).max(axis=1)
    _own_atr = (_atr_full(df, 14) / _scale).to_numpy(dtype=float)   # ПРОГРЕВ: тот же ATR, посчитан до обрезки

    def _macro_atr(i):
        return _atr_at(_own_atr, i, atr_fb)

    # ШИРИНА MACRO-ЗОНЫ — ПО ДНЕВНОЙ СВЕЧЕ, а не по месячной/недельной.
    # Раньше зона тянулась от тени месячной/недельной свечи вглубь на до 1.0 ATR
    # ЭТОЙ ЖЕ свечи (хоть и делённого на sqrt(дней)) — на альтах выходило 15-30% цены,
    # и такая зона при склейке (якорь = старший ТФ) побеждала узкие PML/PMH/PWL/PWH
    # на том же месте. Теперь: находим ДЕНЬ, на котором был этот экстремум, и строим зону
    # тем же правилом, что у PMH/PML (_wick_body_zone по дневной свече и дневному ATR на
    # её дату). Свежий MACRO тогда совпадает с PML/PMH один в один. Если дневной свечи
    # нет (свинг старше загруженной дневной истории) или её экстремум не совпал с ценой
    # свинга — кончик тени +- коридор в дневных ATR (WICK_GAP_ATR за кончиком,
    # WICK_MIN_INWARD_ATR внутрь).
    _d = None
    if df_1d is not None and len(df_1d) > 0 and atr_1d:
        _d = {
            'ts': df_1d['timestamp'].to_numpy(),
            'o': df_1d['open'].to_numpy(dtype=float), 'c': df_1d['close'].to_numpy(dtype=float),
            'lo': df_1d['low'].to_numpy(dtype=float), 'hi': df_1d['high'].to_numpy(dtype=float),
            'atr': _atr_full(df_1d, 14).to_numpy(dtype=float),
        }
    _ts_arr = df['timestamp'].to_numpy()

    def _macro_bounds(i, is_support, tip_price):
        reason = "nodaily"   # ПРОГРЕВ: счётчик причин, по которым зона строится НЕ по дневной свече
        if _d is not None and i + 1 < len(_ts_arr):
            pos = np.flatnonzero((_d['ts'] >= _ts_arr[i]) & (_d['ts'] < _ts_arr[i + 1]))
            if len(pos):
                reason = "mismatch"
                j = int(pos[np.argmin(_d['lo'][pos])] if is_support else pos[np.argmax(_d['hi'][pos])])
                ext = _d['lo'][j] if is_support else _d['hi'][j]
                if tip_price and abs(ext - tip_price) / tip_price <= 0.002:
                    a = _atr_at(_d['atr'], j, atr_fb)
                    if not np.isnan(a):
                        return _wick_body_zone(_d['o'][j], _d['c'][j], _d['lo'][j], _d['hi'][j], is_support, a)
        MACRO_FB[reason] = MACRO_FB.get(reason, 0) + 1
        MACRO_FB[label_prefix[:2]] = MACRO_FB.get(label_prefix[:2], 0) + 1
        # Дневной свечи свинга нет (старше окна дневок или у самого его края) — кончик тени и ATR
        # собственной свечи свинга (месяц/неделя, приведён к дневному масштабу): от «сегодняшнего» ATR не зависит.
        a = _macro_atr(i)
        if np.isnan(a):
            return None
        return _wick_body_zone(tip_price, tip_price, tip_price, tip_price, is_support, a)

    work = df.copy()
    # Фрактал: 2 свечи слева, 1 центр, 2 справа
    work['is_swing_low'] = (work['low'] == work['low'].rolling(window=5, center=True).min())
    work['is_swing_high'] = (work['high'] == work['high'].rolling(window=5, center=True).max())

    zones = []

    # Проходим по графику, отступив края
    for i in range(2, len(work) - 2):
        ts = work['timestamp'].iloc[i]
        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')
        confirm_ts = work['timestamp'].iloc[i + 2]
        activated_at = pd.to_datetime(confirm_ts, unit='ms').strftime('%Y-%m-%d')
        _ct = pd.to_datetime(int(confirm_ts), unit='ms')
        known_ms = int(((_ct + pd.offsets.MonthBegin(1)) if label_prefix.startswith('1M') else (_ct + pd.Timedelta(days=7))).value // 10**6)

        # Получаем данные текущей свечи для SMC
        open_price = float(work['open'].iloc[i])
        close_price = float(work['close'].iloc[i])

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
                    body_bottom = min(open_price, close_price)
                    # Якорь - сама тень (price), потолок обрезает только
                    # то, насколько зона тянется К ТЕЛУ (вверх от price).
                    if atr_cap is not None:
                        body_bottom = min(body_bottom, price + atr_cap)

                    # Передаем atr=0.0, границы пропишем вручную
                    zone = _build_zone(price, 0.0, base_score, f"{label_prefix}_Support", date_str, False,
                                        activated_at=activated_at)
                    if atr_1d:
                        bounds = _macro_bounds(i, True, price)
                    else:
                        bounds = (price, body_bottom)
                    if bounds is not None:
                        zone['min'], zone['max'] = bounds
                        zone['_is_support'] = True
                        zone['class'] = 'MACRO'
                        zone['_known'] = known_ms   # ИЗВЕСТНА: закрытие подтверждающей свечи
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
                    body_top = max(open_price, close_price)
                    # Якорь - сама тень (price), потолок обрезает только
                    # то, насколько зона тянется К ТЕЛУ (вниз от price).
                    if atr_cap is not None:
                        body_top = max(body_top, price - atr_cap)

                    # Передаем atr=0.0, границы пропишем вручную
                    zone = _build_zone(price, 0.0, base_score, f"{label_prefix}_Resistance", date_str, False,
                                        activated_at=activated_at)
                    if atr_1d:
                        bounds = _macro_bounds(i, False, price)
                    else:
                        bounds = (body_top, price)
                    if bounds is not None:
                        zone['min'], zone['max'] = bounds
                        zone['_is_support'] = False
                        zone['class'] = 'MACRO'
                        zone['_known'] = known_ms   # ИЗВЕСТНА: закрытие подтверждающей свечи
                        zones.append(zone)

    return zones

def _collect_zones(df_1M, df_1W, df_1d, df_4h, current_price, atr_1d, max_distance, current_idx, deep=False, count_stats=True,
                   df_1d_macro=None):
    """Все сырые точки всех слоёв. deep=True — фолбэк: вся история и бесконечный радар (max_distance=inf)."""
    zones = []
    if df_1M is not None:
        z = _extract_macro_swings(df_1M, current_price, max_distance if deep else max_distance * 2.5, 5.0, "1M_MACRO", atr_1d=atr_1d, df_1d=(df_1d_macro if df_1d_macro is not None else df_1d))
        if count_stats:
            MACRO_LAYER_STATS['1M_raw'] += len(z)
        zones += z
    if df_1W is not None:
        z = _extract_macro_swings(df_1W, current_price, max_distance if deep else max_distance * 1.5, 4.0, "1W_MACRO", atr_1d=atr_1d, df_1d=(df_1d_macro if df_1d_macro is not None else df_1d))
        if count_stats:
            MACRO_LAYER_STATS['1W_raw'] += len(z)
        zones += z
    elif count_stats:
        MACRO_LAYER_STATS['1W_no_data'] += 1

    months = len(df_1d) if deep else PERIODS_MONTHS_BACK
    weeks = len(df_1d) if deep else PERIODS_WEEKS_BACK
    zones += _extract_period_extremes(df_1d, 'ME', months, current_price, atr_1d, max_distance,
                                      "1M_high_PMH", "1M_low_PML", current_idx)
    zones += _extract_monthly_close_levels(df_1d, PERIODS_MONTHS_BACK_PMC, current_price, atr_1d, max_distance, current_idx)
    zones += _extract_period_extremes(df_1d, 'W', weeks, current_price, atr_1d, max_distance,
                                      "1W_high_PWH", "1W_low_PWL", current_idx)
    zones += _extract_find_peaks_layer(df_1d, current_price, atr_1d, max_distance, current_idx)
    if df_4h is not None and len(df_4h) >= 50:
        zones += _extract_pivots_4h(df_4h, current_price, max_distance)
    return zones


def _split_alive(zones):
    """Выкидываем пробитые, делим на поддержки/сопротивления."""
    sup, res = [], []
    for z in zones:
        if z.get('mitigated'):
            continue
        is_sup = z.pop('_is_support', None)
        z.pop('mitigated', None)
        if is_sup is True:
            sup.append(z)
        elif is_sup is False:
            res.append(z)
    return sup, res


def _dist_to_price(z, price):
    if z['min'] <= price <= z['max']:
        return 0.0
    return min(abs(z['min'] - price), abs(z['max'] - price))


def _pick_nearest(zones, price, n):
    """n ближайших к цене зон; зона, пересекающая уже выбранную, пропускается (один слот на одно место)."""
    chosen = []
    for z in sorted(zones, key=lambda q: _dist_to_price(q, price)):
        if any(min(z['max'], c['max']) - max(z['min'], c['min']) > 0 for c in chosen):
            continue
        chosen.append(z)
        if len(chosen) >= n:
            break
    return chosen


def _overlap(a, b):
    return min(a['max'], b['max']) - max(a['min'], b['min'])


def _zone_key(side, z):
    return (side, round(z['min'], 10), round(z['max'], 10))


def _still_alive(zone, is_support, df_1d):
    """Зона реестра жива, пока ни одно дневное закрытие после её первого появления не ушло за дальний край."""
    d = str(zone.get('_since') or zone.get('date') or '')[:10]
    dates = pd.to_datetime(df_1d['timestamp'], unit='ms').dt.strftime('%Y-%m-%d')
    closes = df_1d['close'][dates > d]
    if closes.empty:
        return True
    return not ((closes < zone['min']).any() if is_support else (closes > zone['max']).any())


def _without_poc(label):
    parts = [p.strip() for p in str(label or '').split('+')]
    return ' + '.join(p for p in parts if p and p != '4h_poc')


def _place(points, existing):
    """Расстановка точек относительно стоящих зон реестра (границы стоящих не двигаются).
    Точки идут от самой сильной (1M > 1W > 1d > 4h, при равенстве — старше). Точка, пересекающаяся с
    стоящей зоной, поглощается ей (в название добавляется тип). Иначе — отдельная зона со СВОЕЙ геометрией.
    Возвращает (все зоны, новые зоны)."""
    zones = list(existing)
    added = []
    # ПЕРВАЯ ИЗВЕСТНАЯ ТОЧКА: основа зоны — точка, которая стала известна на этом месте раньше всех (_known —
    # момент закрытия свечи, подтвердившей точку). Новая точка всегда известна позже уже стоящих,
    # поэтому основу у живой точки не забирает никогда.
    for p in sorted(points, key=lambda q: (int(q.get('_known', 2**62)), _tf_rank(q), str(q.get('type')), q['min'])):
        host = next((z for z in zones if _overlap(p, z) > 0), None)
        if host is None:
            q = dict(p)
            q['anchor'] = _anchor_type(q)
            q['_base'] = float(q.get('score', 0.0))
            zones.append(q)
            added.append(q)
            continue
        host['type'] = _merge_type_labels(host.get('type'), p.get('type'))
        host['_base'] = max(float(host.get('_base', host.get('score', 0.0))), float(p.get('score', 0.0)))
    return zones, added


def _public(z):
    """Копия зоны для выдачи: без служебных полей."""
    return {k: v for k, v in z.items() if not k.startswith('_') and k != 'anchor'}


# ПРОГРЕВ: обычное окно, в котором ищем свинги (как всегда), и сколько свечей перед ним нужно дополнительно.
WINDOWS = {"1M": 60, "1W": 150, "1d": 365, "4h": 200}
# 1M, 1W: +14 на ATR своей свечи. 4h: +30 (ATR 14, средний объём 30). 1d: дневная свеча КАЖДОГО макро-свинга окна
# 1M (60 мес. ≈ 1860 дней) + 30 на ATR/объём этой свечи.
LOOKBACK = {"1M": 14, "1W": 14, "1d": 60 * 31 + 30, "4h": 30}
MACRO_FB = {}   # счётчик: сколько макро-зон строилось не по дневной свече и почему


def _warm_df(df, tf, trim=True):
    """ATR(14) и средний объём 30 свечей до каждой свечи — по ВСЕМ полученным свечам; потом (trim) обрезка до окна."""
    d = df.copy()
    h, l, c = d["high"], d["low"], d["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["_atr"] = tr.rolling(14, min_periods=1).mean()
    d["_vavg"] = d["volume"].shift(1).rolling(30, min_periods=30).mean()
    return d.tail(WINDOWS[tf]).reset_index(drop=True) if trim else d.reset_index(drop=True)


def prepare(df_1M, df_1W, df_1d, df_4h):
    """Свечи с запасом -> (1M, 1W, 1d в окне 365, 4h, все дневные свечи для поиска дня макро-свинга)."""
    d1M = None if df_1M is None else _warm_df(df_1M, "1M")
    d1W = None if df_1W is None else _warm_df(df_1W, "1W")
    d4h = None if df_4h is None else _warm_df(df_4h, "4h")
    d1_full = _warm_df(df_1d, "1d", trim=False)
    d1 = d1_full.tail(WINDOWS["1d"]).reset_index(drop=True)
    return d1M, d1W, d1, d4h, d1_full


def build_levels(df_1M, df_1W, df_1d, df_4h, coin, current_idx=None, registry=None, snap_day=None,
                 return_registry=False):
    """
    Главная функция модуля. Формат результата прежний: {"supports": [...], "resistances": [...]}.

    registry = {"S": [...], "R": [...]} — все зоны, уже выданные раньше для этой монеты и ещё не
    пробитые (None = пусто, тогда это обычная сборка с нуля). return_registry=True добавляет в
    результат ключ "registry" — передать его в следующий вызов.

    Порядок:
      1. Точки всех слоёв (MACRO 1M/1W, PMH/PML, PMC, PWH/PWL, пивоты 1d, пивоты 4h); пробитые
         закрытием за уровнем выкидываются; слишком широкие сжимаются.
      2. Зоны реестра стоят как были, пока дневное закрытие не уйдёт за дальний край (отсчёт от
         первого появления зоны).
      3. Расстановка точек (_place): пересекается со стоящей зоной -> поглощается ей, иначе новая зона
         со своей геометрией. Без коэффициентов и допусков.
      4. POC — бонус к score и метка, самостоятельной POC-зоны нет.
      5. На сторону MAX_PER_SIDE ближайших; если меньше 2 — фолбэк (вся история, бесконечный радар).
      6. Сжатие, resolve_cross_overlaps, отбои: score = база + POC + 0.5 за отбой (до REJECTION_SCORE_CAP).
    """
    registry = registry or {'S': [], 'R': []}
    df_1M, df_1W, df_1d, df_4h, d1_full = prepare(df_1M, df_1W, df_1d, df_4h)
    if current_idx is None:
        current_idx = len(df_1d) - 1

    current_price = float(df_1d['close'].iloc[current_idx])
    df_1d = df_1d.copy()
    df_1d['atr'] = calculate_atr(df_1d, 14)
    atr_1d = df_1d['atr'].iloc[current_idx]
    if pd.isna(atr_1d) or atr_1d == 0:
        atr_1d = current_price * 0.05
    if snap_day is None:
        snap_day = pd.to_datetime(df_1d['timestamp'].iloc[current_idx], unit='ms').strftime('%Y-%m-%d')

    max_distance = _calc_weekly_atr(df_1d, current_idx) * ATR_DISTANCE_MULTIPLIER

    # Зоны строятся из ВСЕХ точек (без радиуса): состав зоны не зависит от того, где сейчас цена.
    # Какие зоны показать — решает отбор ближайших (_pick_nearest) ниже.
    all_zones = _collect_zones(df_1M, df_1W, df_1d, df_4h, current_price, atr_1d, float('inf'), current_idx,
                               df_1d_macro=d1_full)
    raw_zone_count = len(all_zones)
    raw_s, raw_r = _split_alive(all_zones)

    poc_price = atr_4h = None
    if df_4h is not None and len(df_4h) >= 50:
        poc_price, atr_4h = _get_poc(df_4h, current_price)

    chosen_by_side, alive_prev = {}, {}
    for side, is_sup, pts in (('S', True, raw_s), ('R', False, raw_r)):
        existing = [copy.deepcopy(z) for z in registry.get(side, []) if _still_alive(z, is_sup, df_1d)]
        for z in existing:   # POC считается заново на каждом снимке, а не копится
            z['score'] = z.get('_base', z.get('score', 0.0))
            z['type'] = _without_poc(z.get('type'))
        alive_prev[side] = existing
        compress_fat_zones(pts, coin)
        zones, added = _place(pts, existing)
        for z in added:
            z['_since'] = snap_day
        if poc_price is not None:
            zones = _apply_poc_confluence(zones, poc_price, atr_4h)
        chosen = _pick_nearest(zones, current_price, MAX_PER_SIDE)

        if len(chosen) < 2:   # ФОЛБЭК: минимум 2 уровня на сторону (вся история, бесконечный радар)
            fb_all = _collect_zones(df_1M, df_1W, df_1d, df_4h, current_price, atr_1d, float('inf'), current_idx,
                                    deep=True, count_stats=False, df_1d_macro=d1_full)
            fb_s, fb_r = _split_alive(fb_all)
            cand = fb_s if is_sup else fb_r
            cand = [z for z in cand if (z['max'] < current_price if is_sup else z['min'] > current_price)]
            compress_fat_zones(cand, coin)
            _, fb_added = _place(cand, [dict(z) for z in chosen])
            fb_added.sort(key=lambda x: _dist_to_price(x, current_price))
            for z in fb_added[: 2 - len(chosen)]:
                z['_since'] = snap_day
                chosen.append(z)
        chosen_by_side[side] = chosen

    # Повторно НЕ сжимаем: точки сжаты до расстановки, а зоны реестра трогать нельзя
    # (повторное сжатие иногда сдвигает границы в последнем знаке -> другой level_id у менеджера).
    supports, resistances = resolve_cross_overlaps(chosen_by_side['S'], chosen_by_side['R'])

    # Отбои — по границам ИТОГОВЫХ зон; score = база (+POC) + 0.5 за отбой (до REJECTION_SCORE_CAP)
    for side, zs, is_sup in (('S', supports, True), ('R', resistances, False)):
        for z in zs:
            z['reaction_count'] = count_zone_touches(df_1d, z, is_sup, current_idx)
            z['score'] = round(float(z.get('score', 0.0)) + REJECTION_SCORE_STEP * min(z['reaction_count'], REJECTION_SCORE_CAP), 2)
            z['level_id'] = f"{side}|{z.get('anchor') or _anchor_type(z)}|{z.get('date')}"

    for z in supports + resistances:
        t = str(z.get('type', ''))
        if '1W_MACRO' in t:
            MACRO_LAYER_STATS['1W'] += 1
        if '1M_MACRO' in t:
            MACRO_LAYER_STATS['1M'] += 1

    if not supports and not resistances:
        print(
            f"[LEVELS DEBUG] {coin}: пусто и supports, и resistances | "
            f"current_price={current_price:.6g} | max_distance={max_distance:.6g} "
            f"({max_distance / current_price * 100:.1f}% от цены) | atr_1d={atr_1d:.6g} | "
            f"кандидатов до mitigated-фильтра={raw_zone_count}"
        )

    result: dict = {
        "supports": sorted((_public(z) for z in supports), key=lambda q: q['min']),
        "resistances": sorted((_public(z) for z in resistances), key=lambda q: q['min']),
    }
    if return_registry:
        new_registry = {}
        for side, zs in (('S', supports), ('R', resistances)):
            seen, new_registry[side] = set(), []
            for z in alive_prev[side] + zs:     # реестр = живые прежние + выданные сейчас
                k = _zone_key(side, z)
                if k not in seen:
                    seen.add(k)
                    new_registry[side].append(z)
        result["registry"] = new_registry
    return result