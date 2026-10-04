"""
levels_builder_new.py  (НОВАЯ сборка уровней; старый levels_builder.py не тронут)
==================
Чистый модуль расчёта уровней. Не знает про биржу и расписание.
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
# PMC (Prior Month Close) — граница между месяцами (close месяца N = open
# месяца N+1, свечи идут встык). Тот же тип источника, что PMH/PML (календарная
# метка старшего масштаба), поэтому тот же вес — см. обсуждение проекта и
# test_body_shelves.py: пробовали сначала брать весь размах ТЕЛА месяца
# (open..close) как зону — ширина скакала от долей % до 10% цены (это просто
# то, насколько месяц сдвинулся, а не сила уровня), непригодно как триггер.
# Вместо этого берём саму границу (close) и оборачиваем в стандартный
# ATR-буфер, как и все остальные уровни здесь — тогда это обычный календарный
# уровень, ничем принципиально не отличается от PMH/PML.
SCORE_PMC = 5.0
CONFLUENCE_BONUS = 2.0  # бонус за совпадение с find_peaks или POC
POC_BONUS = 1.0  # дополнительный бонус если POC совпал с существующей зоной
# Пол score для зон, где сошлись календарные уровни РАЗНЫХ таймфреймов
# (месяц + неделя, напр. 1M_low_PML + 1W_low_PWL) — временная мера, до
# полного пересмотра скоринга. См. merge_overlapping_zones().
MIN_SCORE_MONTH_WEEK_CONFLUENCE = 9.0

# Допуск для слияния БЛИЗКИХ (не обязательно физически пересекающихся)
# зон СРЕДИ MACRO-зон (1M_MACRO/1W_MACRO). MACRO-зоны искусственно узкие
# (см. _extract_macro_swings — 1.5% от цены, а не реальный ATR месяца),
# поэтому два РАЗНЫХ исторических свинга, оказавшихся близко по цене,
# физически не пересекаются и остаются двумя записями вместо одной.
# Проверено на реальных данных (HYPE, июль) через count_levels.py:
# у regular-зон (find_peaks/PMH/PML/PWH/PWL/POC) за месяц ни разу не
# нашлось близкой пары — там свой скоринг с confluence-бонусами, трогать
# не стали. Только для MACRO, где reaction_count всегда 0 (сигнала для
# выбора "кто сильнее" нет) и score фиксированный — тут безопасно просто
# СЛИТЬ геометрию (min/max), а не выбирать одну зону.
MACRO_MERGE_DISTANCE_PCT = 1.0   # было 8.0: склейка MACRO-зон «в 8% друг от друга» давала толстые зоны

# Потолок ширины ОДНОЙ MACRO-зоны: не более, чем MACRO_ATR_CAP_MULTIPLIER
# дневных ATR(1d) этой монеты (тот же atr_1d, что использует PMH/PML).
# Раньше ширина = сырой размах "тень-до-тела" одной свечи БЕЗ всякого
# ограничения - на реальных данных (AERO 2025-10, DASH 2024-12) одна
# волатильная месячная свеча давала зону 58-125% от цены. Это и была
# причина: (1) honest-слияние в merge_overlapping_zones "проглатывало"
# несколько других реальных зон целиком под одну гигантскую, (2) случайные
# касания костяком зоны с несвязанными историческими уровнями на исчезающе
# малую величину (см. DASH: 71.8 вплотную к чужой зоне 71.7-78.6).
# K=3.0 подобран эмпирически на реальных данных (test_macro_atr_cap.py,
# BTC/ETH/AERO/DASH): не трогает зоны, которые и так уже in-range по своей
# волатильности, режет только настоящие однокандловые выбросы.
MACRO_ATR_CAP_MULTIPLIER = 3.0
# Сколько торговых дней в периоде MACRO-свечи: ATR месячной/недельной свечи делим на корень из этого
# числа, чтобы получить дневной масштаб (зазор/глубина зоны те же, что у остальных слоёв).
MACRO_ATR_DAYS = {"1M": 21.0, "1W": 5.0}

# Счётчики MACRO-слоёв за пересчёт (сбрасывает и печатает swing_hunter.py):
#   *_raw — сколько свингов нашёл слой ДО слияния/фильтров,
#   1W/1M — сколько итоговых зон содержат уровень этого слоя,
#   1W_no_data — у скольких монет не пришли недельные свечи вообще.
MACRO_LAYER_STATS = {'1M_raw': 0, '1W_raw': 0, '1M': 0, '1W': 0, '1W_no_data': 0}

# find_peaks-слой (lookahead-фильтр - старый алгоритм)
IMPULSE_ATR_MULTIPLIER = 2.5
IMPULSE_LOOKAHEAD_DAYS = 10
FIND_PEAKS_DISTANCE = 15
FIND_PEAKS_PROMINENCE_MULT = 1.5

# Сколько последних закрытых недель/месяцев/дней проверяем на актуальность
PERIODS_MONTHS_BACK = 3   # последние 3 закрытых месяца (PMH/PML, по теням)
PERIODS_WEEKS_BACK = 4     # последние 4 закрытых недели
PERIODS_DAYS_BACK = 5       # последние 5 закрытых дней (PDH/PDL)
# PMC — отдельное, БОЛЬШЕЕ окно от PERIODS_MONTHS_BACK выше: PMH/PML ищут
# экстремум (важна свежесть, дальние тени почти всегда уже отфильтрованы
# дистанцией), а PMC ищет саму границу месяца рядом с текущей ценой — на
# консолидации цена может стоять у границы N-месячной давности (см. пример
# BTC/ZRO в обсуждении проекта, где нужный уровень был на ~9 месяцев назад).
# max_distance ниже всё равно отсекает то, что физически далеко от цены —
# это окно только даёт кандидатам ШАНС попасть в фильтр, а не показывает
# их все подряд.
PERIODS_MONTHS_BACK_PMC = 12

# Дистанция релевантности: max_distance = ATR(1W) * этот множитель.
# Заменяет старый жёсткий процент от цены - адаптируется к волатильности монеты.
ATR_DISTANCE_MULTIPLIER = 2.0

# Порог объёмного бонуса: во сколько раз объём формирующей свечи должен
# превышать средний объём периода, чтобы получить бонус
VOLUME_SPIKE_MULTIPLIER = 2.0

# Потолок бонуса score за слияние нескольких источников в одну зону
MAX_MERGE_BONUS = 3.0

MAJORS = ["BTC", "ETH", "SOL", "BNB"]

# =========================================================
# НОВАЯ СБОРКА (см. шапку функций ниже). Источники метода: общепринятая практика
# (pivot high/low, зона от тени до тела свечи, «naked» уровни живут до закрытия за ними,
# Chung & Bellotti 2021: сила уровня = число отскоков, а не число совпавших слоёв).
# =========================================================
LINE_HALF_ATR = 0.25          # линия-уровень (закрытие месяца): зона = цена +- 0.25 ATR
WICK_GAP_ATR = 0.10           # зазор за тенью (цена не всегда долетает до экстремума)
WICK_MIN_INWARD_ATR = 0.25    # минимальная глубина зоны от тени внутрь
WICK_MAX_INWARD_ATR = 1.00    # максимальная глубина зоны от тени внутрь (до тела свечи, но не глубже)
PIVOT_K_1D = 5                # пивот на дневках: 5 свечей слева и справа ниже/выше
PIVOT_K_4H = 6                # пивот на 4h: 6 свечей (сутки) слева и справа
SCORE_PIVOT_4H = 3.5
MAX_PER_SIDE = 2              # сколько ближайших непробитых зон оставляем на сторону (в методичках: 2-3 зоны, не 15)
MERGE_MIN_OVERLAP = 0.5       # зоны сливаются, только если пересекаются хотя бы на 50% более узкой
MERGE_MAX_UNION_FACTOR = 1.5  # и если объединение не шире 1.5 самой широкой из них
REJECTION_SCORE_STEP = 0.5    # score = база источника + 0.5 за каждый отбой от готовой зоны
REJECTION_SCORE_CAP = 4



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
    """Эпизоды касания зоны price+-0.5ATR, закончившиеся отбоем (без закрытия за дальним краем).
    Логика прежняя, цикл по numpy вместо iterrows (в десятки раз быстрее)."""
    if current_idx is None:
        current_idx = len(df_1d) - 1
    if idx >= current_idx:
        return 0
    zone_half = atr_value * 0.5
    zmin, zmax = price - zone_half, price + zone_half
    lo = df_1d['low'].to_numpy(dtype=float)[idx + 1: current_idx + 1]
    hi = df_1d['high'].to_numpy(dtype=float)[idx + 1: current_idx + 1]
    cl = df_1d['close'].to_numpy(dtype=float)[idx + 1: current_idx + 1]
    reactions = 0
    in_ep = False
    broke = False
    for l, h, c in zip(lo, hi, cl):
        if l <= zmax and h >= zmin:
            if not in_ep:
                in_ep, broke = True, False
            if (is_support and c < zmin) or ((not is_support) and c > zmax):
                broke = True
        elif in_ep:
            if not broke:
                reactions += 1
            in_ep = False
    if in_ep and not broke:
        reactions += 1
    return reactions


def count_zone_touches(df_1d, zone, is_support, current_idx=None):
    """КАСАНИЯ ИТОГОВОЙ ЗОНЫ — по её реальным границам (min/max), после
    всех слияний и сжатия, для ВСЕХ типов уровней, включая MACRO.

    Раньше касания считал каждый исходный уровень сам, вокруг своей точки
    ±0.5 дневного ATR (_count_reactions), а у слитой зоны брался максимум;
    у MACRO не считались вообще (всегда 0). Реальных касаний самой зоны
    никто не считал.

    Правило то же, что у _count_reactions: по дневным свечам, "эпизодами" —
    несколько дней подряд в зоне = одно касание; засчитывается, только если
    эпизод закончился ОТБОЕМ (ни одного закрытия за дальним краем зоны:
    для поддержки ниже min, для сопротивления выше max). Считаем со
    следующего дня после даты зоны (date — дата самого раннего её уровня);
    если дата старше доступной дневной истории — со начала истории.

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
    даты уровень рисуется на графике как "рабочий" — см. паспорт в
    swing_hunter.py (_reconcile_levels_with_registry), который копирует его
    неизменным при повторных сканах."""
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
    """ATR, у которого нет пустых первых свечей: в начале окна — среднее по тем свечам, что есть.
    Нужен, чтобы уровень у начала окна истории не брал «сегодняшний» ATR (иначе его границы дёргаются)."""
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


def _atr_at(atr_arr, idx, default):
    """ATR на конкретной свече (а не «сегодняшний»): зона зависит только от истории до своей свечи и
    не дёргается от скана к скану (ATR — простая средняя за 14 свечей, от окна выгрузки не зависит)."""
    try:
        a = float(atr_arr[idx])
    except (IndexError, TypeError, ValueError):
        return float(default)
    return float(default) if (np.isnan(a) or a == 0) else a


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
        pos = np.flatnonzero((dates >= np.datetime64(period_start)) & (dates < np.datetime64(period_end)))
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
            zone = _build_zone(price, atr_p, base_score, label, date_str, mitigated=mitigated,
                                reaction_count=0, volume_bonus=vol_bonus)
            zone['min'], zone['max'] = _wick_body_zone(o_arr[idx_ext], c_arr[idx_ext], l_arr[idx_ext],
                                                       h_arr[idx_ext], is_support, atr_p)
            zone['_is_support'] = is_support
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
        pos = np.flatnonzero((dates >= np.datetime64(period_start)) & (dates < np.datetime64(period_end)))
        pos = pos[pos <= current_idx]
        if len(pos) == 0:
            continue
        idx_ref = int(pos[-1])
        is_support = bool(price < current_price)
        mitigated = _is_mitigated(df_1d, idx_ref, float(price), is_support, current_idx)
        vol_bonus = _volume_bonus(df_1d, idx_ref)
        atr_p = _atr_at(atr_arr, idx_ref, atr_1d)
        zone = _build_zone(price, atr_p, SCORE_PMC, "1M_close_PMC", date_str, mitigated=mitigated,
                            reaction_count=0, volume_bonus=vol_bonus)
        zone['min'] = float(price - LINE_HALF_ATR * atr_p)
        zone['max'] = float(price + LINE_HALF_ATR * atr_p)
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
        # Свеча дня ещё не закрылась в момент своего экстремума — уровень
        # подтверждается только на следующий день (см. докстринг activated_at
        # в _build_zone).
        if idx + 1 < len(df_1d):
            activated_at = pd.to_datetime(df_1d['timestamp'].iloc[idx + 1], unit='ms').strftime('%Y-%m-%d')
        else:
            activated_at = date_str

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
                                volume_bonus=vol_bonus, activated_at=activated_at)
            zone['_is_support'] = is_support
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

    zones = []
    for i in range(k, n - k):
        for is_support, flag, price in ((True, is_pl[i], lo[i]), (False, is_ph[i], hi[i])):
            if not flag or abs(price - current_price) > max_distance:
                continue
            a = atr_arr[i]
            if np.isnan(a) or a == 0:
                a = default_atr
            mitigated = _is_mitigated(work, i, float(price), is_support, n - 1)
            date_str = pd.to_datetime(int(ts[i]), unit='ms').strftime('%Y-%m-%d')
            act_str = pd.to_datetime(int(ts[i + k]), unit='ms').strftime('%Y-%m-%d')
            zone = _build_zone(price, a, base_score, label, date_str, mitigated=mitigated,
                                reaction_count=0, volume_bonus=_volume_bonus(work, i), activated_at=act_str)
            zone['min'], zone['max'] = _wick_body_zone(o[i], c[i], lo[i], hi[i], is_support, float(a))
            zone['_is_support'] = is_support
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


def merge_overlapping_zones(zones):
    """Слияние зон одной стороны.
    НОВОЕ: сливаем только если зоны перекрываются хотя бы на MERGE_MIN_OVERLAP более узкой (один и тот же
    уровень, а не две коробки, едва касающиеся краями) и объединение не шире MERGE_MAX_UNION_FACTOR
    самой широкой из них — цепочка «зона растёт, пока есть пересечения» больше не строит толстых зон.
    Score = база сильнейшего источника (бонусов за наложение слоёв нет: наложение силы не добавляет)."""
    if not zones:
        return []

    sorted_zones = sorted(zones, key=lambda x: x['min'])
    for z in sorted_zones:
        z['base_score'] = z.get('score', 0.0)
        z['_w0'] = float(z['max'] - z['min'])

    merged = [sorted_zones[0]]
    for current in sorted_zones[1:]:
        last = merged[-1]
        overlap = min(last['max'], current['max']) - current['min']
        narrow = min(last['max'] - last['min'], current['max'] - current['min'])
        can_merge = current['min'] <= last['max'] and (narrow <= 0 or overlap / narrow >= MERGE_MIN_OVERLAP)
        last_macro = last.get('class') == 'MACRO'
        cur_macro = current.get('class') == 'MACRO'
        union_w = max(last['max'], current['max']) - last['min']
        if can_merge and not (last_macro != cur_macro) and union_w > MERGE_MAX_UNION_FACTOR * max(last['_w0'], current['_w0']):
            can_merge = False
        if not can_merge:
            merged.append(current)
            continue

        if last_macro and not cur_macro:
            last['min'], last['max'] = current['min'], current['max']
            last.pop('class', None)
            last['_w0'] = current['_w0']
        elif cur_macro and not last_macro:
            pass
        else:
            last['max'] = max(last['max'], current['max'])
            last['_w0'] = max(last['_w0'], current['_w0'])
        last['base_score'] = max(last['base_score'], current['base_score'])
        last['score'] = round(last['base_score'], 2)
        if current.get('type') and last.get('type'):
            last['type'] = _merge_type_labels(last['type'], current['type'])
        if last_macro and cur_macro:
            last['class'] = 'MACRO'
        last['reaction_count'] = max(last.get('reaction_count', 0), current.get('reaction_count', 0))
        if current.get('date') and (not last.get('date') or current['date'] < last['date']):
            last['date'] = current['date']
        if current.get('activated_at') and (not last.get('activated_at') or current['activated_at'] > last['activated_at']):
            last['activated_at'] = current['activated_at']

    for m in merged:
        m.pop('base_score', None)
        m.pop('_w0', None)
    return merged


TF_RANK = (("1M", 4), ("1W", 3), ("1d", 2), ("4h", 1))  # сила уровня по таймфрейму источника


def _anchor_type(z):
    return str(z.get('type') or '').split('+')[0].strip()


def _tf_rank(z):
    t = _anchor_type(z)
    for prefix, rank in TF_RANK:
        if t.startswith(prefix):
            return rank
    return 0


def merge_anchor_zones(zones):
    """Склейка БЕЗ объединения границ (замена паспорту).
    Зоны одной стороны, которые реально заходят друг на друга, становятся ОДНОЙ зоной с границами
    самого сильного уровня (таймфрейм 1M > 1W > 1d > 4h, при равенстве — более старый). Остальные
    уровни зоны добавляются только в название (type), score = максимум баз. Границы зоны = границы
    её якоря, а они считаются по свече рождения и не меняются, пока уровень жив."""
    order = sorted(zones, key=lambda z: (-_tf_rank(z), str(z.get('date') or '9999-99-99'), float(z['min'])))
    kept = []
    for z in order:
        host = None
        for k in kept:
            if min(k['max'], z['max']) - max(k['min'], z['min']) > 0:
                host = k
                break
        if host is None:
            z['anchor'] = _anchor_type(z)
            kept.append(z)
            continue
        if z.get('type') and host.get('type'):
            host['type'] = _merge_type_labels(host['type'], z['type'])
        host['score'] = round(max(float(host.get('score', 0.0)), float(z.get('score', 0.0))), 2)
    return sorted(kept, key=lambda q: q['min'])


def _merge_nearby_macro_zones(zones, tolerance_pct=MACRO_MERGE_DISTANCE_PCT):
    """Второй, более мягкий проход слияния — ТОЛЬКО для зон class == 'MACRO'.

    Обычный merge_overlapping_zones выше сливает зоны только при физическом
    пересечении. MACRO-зоны узкие (1.5% от цены, искусственно, см. коммент
    к MACRO_MERGE_DISTANCE_PCT) — два разных исторических свинга, которые
    по смыслу про один и тот же район графика, часто НЕ пересекаются чисто
    из-за этой узости и остаются двумя отдельными записями. Тут условие
    слияния ослаблено: не 'пересекается', а 'зазор между серединами меньше
    tolerance_pct% от цены'.

    Зоны других классов (regular) эта функция не трогает вообще — у них
    свой, более тонкий скоринг (confluence-бонусы, MIN_SCORE_MONTH_WEEK_
    CONFLUENCE), смешивать с этой упрощённой логикой не стали (см. NOTES).

    В отличие от merge_overlapping_zones, тут НЕ считаем find_peaks/calendar
    confluence-бонусы — они физически невозможны для MACRO-зон (тип всегда
    '1M_MACRO_...'/'1W_MACRO_...', ни PMH/PML/PWH/PWL, ни extreme_peak).
    Просто расширяем границы, score/reaction_count берём максимум из пары.
    Ширину результата потом при необходимости обрежет compress_fat_zones
    (эта функция вызывается ДО него в build_levels)."""
    macro = [z for z in zones if z.get('class') == 'MACRO']
    rest = [z for z in zones if z.get('class') != 'MACRO']

    if not macro:
        return zones

    macro_sorted = sorted(macro, key=lambda z: z['min'])
    merged = [macro_sorted[0]]

    for current in macro_sorted[1:]:
        last = merged[-1]
        last_mid = (last['min'] + last['max']) / 2
        gap_tolerance = last_mid * (tolerance_pct / 100.0)
        # Зазор между концом last и началом current меньше допуска —
        # считаем одной зоной (в т.ч. если пересекаются - gap отрицательный).
        if current['min'] - last['max'] <= gap_tolerance:
            last['max'] = max(last['max'], current['max'])
            last['min'] = min(last['min'], current['min'])
            last['score'] = max(last.get('score', 0), current.get('score', 0))
            last['reaction_count'] = max(last.get('reaction_count', 0), current.get('reaction_count', 0))
            if current.get('type') and last.get('type'):
                last['type'] = _merge_type_labels(last['type'], current['type'])
            # Та же логика, что в merge_overlapping_zones — самая ранняя дата.
            if current.get('date') and (not last.get('date') or current['date'] < last['date']):
                last['date'] = current['date']
            # activated_at — максимум (см. комментарий в merge_overlapping_zones).
            if current.get('activated_at') and (not last.get('activated_at') or current['activated_at'] > last['activated_at']):
                last['activated_at'] = current['activated_at']
        else:
            merged.append(current)

    return merged + rest


def compress_fat_zones(zones, coin, is_support=None):
    """Сжимает слишком широкие зоны с учетом класса уровня.

    is_support=True  — поддержка: НИЗ (лой, сам уровень) остаётся на месте, срезается верх.
    is_support=False — сопротивление: ВЕРХ (хай) остаётся на месте, срезается низ.
    Раньше сжимали к ЦЕНТРУ — кончик уровня выпадал из зоны, а вся зона съезжала к цене
    и могла оказаться по другую сторону от неё (лонг-зона "на пике"). В новой сборке слияние
    не объединяет границы (merge_anchor_zones), у зоны всегда есть свой край уровня.
    is_support=None — старое поведение (к центру), для вызовов, которые сторону не передают
    (паспорт в swing_hunter.py, USE_LEVEL_PASSPORT=True)."""
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
            if is_support is True:
                z['max'] = z['min'] + max_allowed_width
            elif is_support is False:
                z['min'] = z['max'] - max_allowed_width
            else:
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
    # месяц /sqrt(21), неделя /sqrt(5). Первые свечи окна (меньше 14 до них) — среднее по тем, что есть.
    _scale = MACRO_ATR_DAYS.get(label_prefix[:2], 1.0) ** 0.5
    _h, _l, _c = df['high'], df['low'], df['close']
    _tr = pd.concat([_h - _l, (_h - _c.shift()).abs(), (_l - _c.shift()).abs()], axis=1).max(axis=1)
    _own_atr = (_tr.rolling(14, min_periods=1).mean() / _scale).to_numpy(dtype=float)

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
        if _d is not None and i + 1 < len(_ts_arr):
            pos = np.flatnonzero((_d['ts'] >= _ts_arr[i]) & (_d['ts'] < _ts_arr[i + 1]))
            if len(pos):
                j = int(pos[np.argmin(_d['lo'][pos])] if is_support else pos[np.argmax(_d['hi'][pos])])
                ext = _d['lo'][j] if is_support else _d['hi'][j]
                if tip_price and abs(ext - tip_price) / tip_price <= 0.002:
                    a = _atr_at(_d['atr'], j, atr_fb)
                    return _wick_body_zone(_d['o'][j], _d['c'][j], _d['lo'][j], _d['hi'][j], is_support, a)
        a = atr_fb
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
                        zone['min'], zone['max'] = _macro_bounds(i, True, price)
                    else:
                        zone['min'] = price
                        zone['max'] = body_bottom
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
                    body_top = max(open_price, close_price)
                    # Якорь - сама тень (price), потолок обрезает только
                    # то, насколько зона тянется К ТЕЛУ (вниз от price).
                    if atr_cap is not None:
                        body_top = max(body_top, price - atr_cap)

                    # Передаем atr=0.0, границы пропишем вручную
                    zone = _build_zone(price, 0.0, base_score, f"{label_prefix}_Resistance", date_str, False,
                                        activated_at=activated_at)
                    if atr_1d:
                        zone['min'], zone['max'] = _macro_bounds(i, False, price)
                    else:
                        zone['min'] = body_top
                        zone['max'] = price
                    zone['_is_support'] = False
                    zone['class'] = 'MACRO'
                    zones.append(zone)

    return zones

def _collect_zones(df_1M, df_1W, df_1d, df_4h, current_price, atr_1d, max_distance, current_idx, deep=False, count_stats=True):
    """Все сырые точки всех слоёв. deep=True — фолбэк: вся история и бесконечный радар (max_distance=inf)."""
    zones = []
    if df_1M is not None:
        z = _extract_macro_swings(df_1M, current_price, max_distance if deep else max_distance * 2.5, 5.0, "1M_MACRO", atr_1d=atr_1d, df_1d=df_1d)
        if count_stats:
            MACRO_LAYER_STATS['1M_raw'] += len(z)
        zones += z
    if df_1W is not None:
        z = _extract_macro_swings(df_1W, current_price, max_distance if deep else max_distance * 1.5, 4.0, "1W_MACRO", atr_1d=atr_1d, df_1d=df_1d)
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


def build_levels(df_1M, df_1W, df_1d, df_4h, coin, current_idx=None):
    """
    Главная функция модуля (НОВАЯ сборка). Интерфейс и формат результата прежние:
    {"supports": [...], "resistances": [...]}.

    Порядок:
      1. Точки всех слоёв (MACRO 1M/1W, PMH/PML, PMC, PWH/PWL, пивоты 1d, пивоты 4h). Зона точки — от тени
         до тела свечи (+зазор) либо цена +-0.25 ATR для линии (PMC).
      2. Пробитые закрытием за уровнем выкидываются (теперь и PMC).
      3. Слияние по якорю (merge_anchor_zones): пересекающиеся зоны -> одна, с границами самого сильного
         уровня (1M > 1W > 1d > 4h, при равенстве более старый); границы не объединяются и не дрожат.
         У каждой зоны постоянный level_id = сторона|якорь|дата свечи рождения.
      4. POC — только бонус к score, самостоятельной POC-зоны нет.
      5. На сторону оставляем MAX_PER_SIDE ближайших к цене непробитых зон; если меньше 2 — фолбэк
         (вся история, бесконечный радар), как раньше.
      6. Итоговый score = база сильнейшего источника + 0.5 за каждый отбой от ГОТОВОЙ зоны (до 4).
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

    all_zones = _collect_zones(df_1M, df_1W, df_1d, df_4h, current_price, atr_1d, max_distance, current_idx)
    raw_zone_count = len(all_zones)

    supports, resistances = _split_alive(all_zones)
    supports = merge_anchor_zones(supports)
    resistances = merge_anchor_zones(resistances)

    # POC — только бонус к уже существующим зонам
    if df_4h is not None and len(df_4h) >= 50:
        poc_price, atr_4h = _get_poc(df_4h, current_price)
        poc_date_str = pd.to_datetime(df_4h['timestamp'].iloc[-1], unit='ms').strftime('%Y-%m-%d')
        supports = _apply_poc_confluence(supports, poc_price, atr_4h, poc_date_str)
        resistances = _apply_poc_confluence(resistances, poc_price, atr_4h, poc_date_str)

    # Оставляем ближайшие к цене
    supports = _pick_nearest(supports, current_price, MAX_PER_SIDE)
    resistances = _pick_nearest(resistances, current_price, MAX_PER_SIDE)

    # ФОЛБЭК: минимум 2 уровня на сторону (вся история, бесконечный радар)
    def _run_fallback(is_support_target, needed):
        fb_all = _collect_zones(df_1M, df_1W, df_1d, df_4h, current_price, atr_1d, float('inf'), current_idx,
                                deep=True, count_stats=False)
        fb_s, fb_r = _split_alive(fb_all)
        cand = fb_s if is_support_target else fb_r
        cand = [z for z in cand if (z['max'] < current_price if is_support_target else z['min'] > current_price)]
        merged = merge_anchor_zones(cand)
        merged.sort(key=lambda x: _dist_to_price(x, current_price))
        return merged[:needed]

    if len(supports) < 2:
        for z in _run_fallback(True, 2 - len(supports)):
            if not any(abs(z['min'] - s_['min']) < 1e-9 for s_ in supports):
                supports.append(z)
    if len(resistances) < 2:
        for z in _run_fallback(False, 2 - len(resistances)):
            if not any(abs(z['min'] - r_['min']) < 1e-9 for r_ in resistances):
                resistances.append(z)

    compress_fat_zones(supports, coin, is_support=True)
    compress_fat_zones(resistances, coin, is_support=False)
    supports, resistances = resolve_cross_overlaps(supports, resistances)

    # Отбои — по границам ИТОГОВЫХ зон; score = база + 0.5 за отбой (до REJECTION_SCORE_CAP)
    for z in supports:
        z['reaction_count'] = count_zone_touches(df_1d, z, True, current_idx)
        z['score'] = round(z.get('score', 0.0) + REJECTION_SCORE_STEP * min(z['reaction_count'], REJECTION_SCORE_CAP), 2)
    for z in resistances:
        z['reaction_count'] = count_zone_touches(df_1d, z, False, current_idx)
        z['score'] = round(z.get('score', 0.0) + REJECTION_SCORE_STEP * min(z['reaction_count'], REJECTION_SCORE_CAP), 2)

    for side, zs in (("S", supports), ("R", resistances)):
        for z in zs:
            z['level_id'] = f"{side}|{z.get('anchor') or _anchor_type(z)}|{z.get('date')}"
            z.pop('anchor', None)

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

    return {
        "supports": sorted(supports, key=lambda z: z['min']),
        "resistances": sorted(resistances, key=lambda z: z['min']),
    }