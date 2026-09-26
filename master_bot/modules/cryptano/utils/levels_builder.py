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
MACRO_MERGE_DISTANCE_PCT = 8.0

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

            # Уровень официально подтверждается только в конце периода.
            # Сдвигаем дату рождения на момент закрытия недели/месяца,
            # чтобы зеленая полоса не рисовалась задним числом.
            period_end = (offset.rollforward(period_start)
                          if offset.rollforward(period_start) != period_start
                          else period_start + offset)
            date_str = period_end.strftime('%Y-%m-%d')

            # Определяем base_score из константы наверху файла (SCORE_PMH_PML/
            # SCORE_PWH_PWL) — раньше тут был захардкожен 0.0, из-за чего
            # правка констант (0 -> 5, по факту бэктестов) никогда не применялась
            # к реальным зонам. См. историю обсуждения проекта.
            base_score = SCORE_PMH_PML if freq == 'ME' else SCORE_PWH_PWL

            zone = _build_zone(price, atr_1d, base_score, label, date_str,
                                mitigated=mitigated, reaction_count=reactions,
                                volume_bonus=vol_bonus)
            zone['_is_support'] = is_support
            zones.append(zone)

    return zones


def _extract_monthly_close_levels(df_1d, months_back, current_price, atr_1d,
                                   max_distance, current_idx=None):
    """
    PMC (Prior Month Close) — граница МЕЖДУ месяцами: цена закрытия месяца
    (она же цена открытия следующего — месячные свечи идут встык, поэтому
    достаточно взять только close, брать ещё и open было бы задвоением
    одной и той же точки).

    Отличие от PMH/PML (см. _extract_period_extremes выше): там уровень —
    это ТЕНЬ месяца (high/low), а тут — сама граница между месяцами, часто
    как раз то место, где цена долго стояла до/после разворота (см.
    обсуждение проекта: тестировали через test_body_shelves.py на реальных
    данных BTC/ETH — весь размах ТЕЛА месяца как зону брать не стали,
    ширина скачет от долей % до 10% цены, это просто величина месячного
    хода, а не сила уровня; вместо этого берём саму границу и оборачиваем
    в стандартный ATR-буфер, как и все остальные зоны здесь).

    Ресемплим df_1d в месяцы, берём close последних months_back ЗАКРЫТЫХ
    месяцев (это окно намеренно БОЛЬШЕ, чем PERIODS_MONTHS_BACK у PMH/PML —
    см. комментарий у PERIODS_MONTHS_BACK_PMC наверху файла). is_support
    определяется на лету по текущей цене (граница уже находится по одну
    конкретную сторону от рынка, в отличие от high/low, где сторона известна
    заранее).
    """
    if current_idx is None:
        current_idx = len(df_1d) - 1

    work = df_1d.iloc[:current_idx + 1].copy()
    work['dt'] = pd.to_datetime(work['timestamp'], unit='ms')
    work = work.set_index('dt')

    resampled = work.resample('ME', label='left').agg({'close': 'last'})
    resampled = resampled.dropna()

    if len(resampled) > 0:
        offset = pd.tseries.frequencies.to_offset('ME')
        last_period_start = resampled.index[-1]
        last_period_end = (offset.rollforward(last_period_start)
                            if offset.rollforward(last_period_start) != last_period_start
                            else last_period_start + offset)
        now_ts = work.index[-1]
        if now_ts < last_period_end:
            resampled = resampled.iloc[:-1]  # текущий месяц ещё не закрыт

    if resampled.empty:
        return []

    recent = resampled.tail(months_back)
    zones = []

    for period_start, row in recent.iterrows():
        price = row['close']
        if pd.isna(price):
            continue
        if abs(price - current_price) > max_distance:
            continue

        period_end = (offset.rollforward(period_start)
                      if offset.rollforward(period_start) != period_start
                      else period_start + offset)
        date_str = period_end.strftime('%Y-%m-%d')

        # idx_ref — ПОСЛЕДНИЙ день периода (та самая свеча, чьё закрытие и
        # есть price), а не первый. Раньше брали первый день месяца — и
        # _is_mitigated/_count_reactions/_volume_bonus проверяли "пробита ли
        # зона" уже ВНУТРИ формирующего её месяца: если месяц открылся выше
        # итогового закрытия и просел к нему только к концу (обычное дело),
        # ранние дни того же месяца закрывались ВЫШЕ финального close —
        # алгоритм видел это как "цена уже закрылась за уровнем" и хоронил
        # зону, даже не дав ей родиться. У PMH/PML такой ошибки нет (там
        # уровень — это максимум/минимум самого месяца, выше/ниже физически
        # закрыться нельзя), у PMC (закрытие) — можно, отсюда и баг (см.
        # обсуждение проекта — на BTC декабрьский/январский уровень пропадал
        # именно так).
        period_dt = pd.to_datetime(df_1d['timestamp'], unit='ms')
        period_mask = (period_dt >= period_start) & (period_dt < period_end)
        candidates = df_1d[period_mask]
        if candidates.empty:
            continue
        idx_ref = candidates.index[-1]

        # bool(...) обязателен: price здесь numpy.float64 (из pandas), сравнение
        # numpy.float64 < float даёт numpy.bool_, а не обычный Python bool.
        # build_levels() ниже раскладывает зоны по supports/resistances через
        # "is True"/"is False" (сравнение по идентичности) — numpy.bool_(True)
        # is True даёт False в Python (другой тип объекта), поэтому БЕЗ bool()
        # зона не попадала НИ в support, ни в resistance и тихо исчезала —
        # именно так это и проявлялось на реальных данных BTC (см. debug_pmc.py).
        is_support = bool(price < current_price)
        # PMC НЕ проверяем на mitigated (в отличие от всех остальных типов
        # здесь) — намеренно, не забыли. mitigated="цена хоть раз закрылась
        # за уровнем -> уровень мёртв навсегда" хорошо работает для PMH/PWH/
        # find_peaks — это реальные экстремумы/отказы, цена туда просто так
        # не возвращается. Но PMC — это цена ЗАКРЫТИЯ месяца, обычная точка
        # посреди диапазона: после такого месяца цена почти всегда потом
        # хоть раз проходит её снова в ходе обычного движения — это не
        # "пробой уровня", это рынок ходит туда-сюда. На реальных данных
        # (BTC, см. обсуждение проекта, debug_pmc.py) это правило убивало
        # 6 из 6 кандидатов подряд, включая тот, что был в 2% от цены —
        # то есть на практике всегда убивало бы вообще всё. Актуальность
        # тут и так гарантирована двумя другими фильтрами (months_back —
        # только недавние месяцы, max_distance — только близкие к текущей
        # цене), доп. проверка "не пересекала ли её цена когда-либо между
        # тем" тут просто лишняя и обнуляет весь смысл слоя.
        mitigated = False
        reactions = _count_reactions(df_1d, idx_ref, price, atr_1d, is_support, current_idx)
        vol_bonus = _volume_bonus(df_1d, idx_ref)

        zone = _build_zone(price, atr_1d, SCORE_PMC, "1M_close_PMC", date_str,
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
        impulse_threshold = price + (local_atr * IMPULSE_ATR_MULTIPLIER)
        if lookahead.max() < impulse_threshold:
            continue

        ts = work['timestamp'].iloc[v]
        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')
        # Уровень доказан не в момент экстремума (v), а в момент, когда импульс
        # РЕАЛЬНО пробил порог — первый день в окне lookahead, где это случилось,
        # а не конец всего 10-дневного окна (см. докстринг activated_at в _build_zone).
        confirm_mask = lookahead >= impulse_threshold
        confirm_idx = confirm_mask.idxmax()
        activated_at = pd.to_datetime(work['timestamp'].loc[confirm_idx], unit='ms').strftime('%Y-%m-%d')

        mitigated = _is_mitigated(df_1d, v, price, True, current_idx)
        reactions = _count_reactions(df_1d, v, price, local_atr, True, current_idx)
        vol_bonus = _volume_bonus(df_1d, v)

        zone = _build_zone(price, local_atr, SCORE_FIND_PEAKS, "1d_extreme_peak", date_str,
                            mitigated=mitigated, reaction_count=reactions,
                            volume_bonus=vol_bonus, activated_at=activated_at)
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
        impulse_threshold = price - (local_atr * IMPULSE_ATR_MULTIPLIER)
        if lookahead.min() > impulse_threshold:
            continue

        ts = work['timestamp'].iloc[p]
        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')
        # См. комментарий в зеркальной ветке valleys выше — та же логика,
        # только импульс пробивает порог вниз, а не вверх.
        confirm_mask = lookahead <= impulse_threshold
        confirm_idx = confirm_mask.idxmax()
        activated_at = pd.to_datetime(work['timestamp'].loc[confirm_idx], unit='ms').strftime('%Y-%m-%d')

        mitigated = _is_mitigated(df_1d, p, price, False, current_idx)
        reactions = _count_reactions(df_1d, p, price, local_atr, False, current_idx)
        vol_bonus = _volume_bonus(df_1d, p)

        zone = _build_zone(price, local_atr, SCORE_FIND_PEAKS, "1d_extreme_peak", date_str,
                            mitigated=mitigated, reaction_count=reactions,
                            volume_bonus=vol_bonus, activated_at=activated_at)
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
            # POC пересчитывается на каждом скане (нет "исторической правды",
            # см. докстринг _is_poc_zone в swing_hunter.py) — activated_at = date,
            # без задержки подтверждения, как у остальных типов.
            "activated_at": current_idx_date or "",
            "mitigated": False,
            "reaction_count": 0,
        }
        zones.append(poc_zone)

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
        # PMC (месячные закрытия) — тот же класс "календарный уровень", что
        # PMH/PML/PWH/PWL, участвует в confluence-бонусах наравне с ними
        # (см. SCORE_PMC наверху файла и обсуждение проекта).
        z['_has_calendar'] = any(t in z.get('type', '') for t in ('PMH', 'PML', 'PWH', 'PWL', 'PMC'))
        z['_has_month_cal'] = any(t in z.get('type', '') for t in ('PMH', 'PML', 'PMC'))
        z['_has_week_cal'] = any(t in z.get('type', '') for t in ('PWH', 'PWL'))

    merged = [sorted_zones[0]]

    for current in sorted_zones[1:]:
        last = merged[-1]
        if current['min'] <= last['max']:
            # ГЕОМЕТРИЯ ПРИ СЛИЯНИИ:
            #   обычная + обычная  -> объединение границ (как раньше);
            #   MACRO + MACRO      -> объединение границ, зона остаётся MACRO;
            #   MACRO + обычная    -> границы ТОЧНОЙ (обычной) зоны, MACRO
            #                         остаётся только в названии как
            #                         подтверждение. Макро-свинг широкий по
            #                         природе (от тени до тела), он говорит
            #                         "в этом районе важное место", а точный
            #                         уровень внутри — где именно. Раньше тут
            #                         было объединение границ — MACRO
            #                         раздувал точную зону.
            last_macro = last.get('class') == 'MACRO'
            cur_macro = current.get('class') == 'MACRO'
            if last_macro and not cur_macro:
                last['min'] = current['min']
                last['max'] = current['max']
                last.pop('class', None)
            elif cur_macro and not last_macro:
                pass  # точная зона (last) уже задаёт границы
            else:
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

            if current.get('type') and last.get('type'):
                last['type'] = _merge_type_labels(last['type'], current['type'])
            # class='MACRO' остаётся, только если ОБЕ слитые зоны MACRO (см.
            # "ГЕОМЕТРИЯ ПРИ СЛИЯНИИ" выше). Смешанная зона — точная (обычная):
            # лимит ширины 3-5% в compress_fat_zones, в мягкое слияние близких
            # MACRO-зон (_merge_nearby_macro_zones) не попадает.
            if last_macro and cur_macro:
                last['class'] = 'MACRO'
            last['reaction_count'] = max(last.get('reaction_count', 0), current.get('reaction_count', 0))
            # Дата — самая РАННЯЯ среди слитых кандидатов, а не та, что
            # случайно оказалась первой по сортировке min. Уровень появился
            # тогда, когда сформировался первый из его компонентов — и это
            # же самая безопасная точка отсчёта для проверки на пробой
            # (_is_mitigated): если взять более позднюю дату, можно
            # пропустить более ранний пробой и ошибочно считать уровень
            # ещё живым.
            if current.get('date') and (not last.get('date') or current['date'] < last['date']):
                last['date'] = current['date']
            # activated_at — МАКСИМУМ среди слитых кандидатов (в отличие от date
            # выше). Зона считается технически готовой не раньше момента, когда
            # подтвердился САМЫЙ ПОЗДНИЙ из слитых в неё компонентов — если тип
            # "разросся" (напр. find_peaks пришёл через месяц после PWH), это и
            # есть момент, когда confluence реально сложилась.
            if current.get('activated_at') and (not last.get('activated_at') or current['activated_at'] > last['activated_at']):
                last['activated_at'] = current['activated_at']
        else:
            merged.append(current)

    for m in merged:
        m.pop('base_score', None)
        m.pop('_has_peak', None)
        m.pop('_has_calendar', None)
        m.pop('_has_month_cal', None)
        m.pop('_has_week_cal', None)

    return merged


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


def _extract_macro_swings(df, current_price, max_distance, base_score, label_prefix, atr_1d=None):
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
                    zone['min'] = price          # Низ зоны = абсолютный минимум (тень)
                    zone['max'] = body_bottom    # Верх зоны = низ тела свечи (обрезан потолком ATR)
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
                    zone['min'] = body_top       # Низ зоны = верх тела свечи (обрезан потолком ATR)
                    zone['max'] = price          # Верх зоны = абсолютный пик (тень)
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
        _m1 = _extract_macro_swings(df_1M, current_price, max_distance * 2.5, 5.0, "1M_MACRO", atr_1d=atr_1d)
        MACRO_LAYER_STATS['1M_raw'] += len(_m1)
        all_zones += _m1
    if df_1W is not None:
        _w1 = _extract_macro_swings(df_1W, current_price, max_distance * 1.5, 4.0, "1W_MACRO", atr_1d=atr_1d)
        MACRO_LAYER_STATS['1W_raw'] += len(_w1)
        all_zones += _w1
    else:
        MACRO_LAYER_STATS['1W_no_data'] += 1

    # 1. PMH/PML (месячные - старший масштаб, по теням)
    all_zones += _extract_period_extremes(
        df_1d, 'ME', PERIODS_MONTHS_BACK, current_price, atr_1d, max_distance,
        "1M_high_PMH", "1M_low_PML", current_idx
    )

    # 1b. PMC (границы между месяцами — close месяца, см. _extract_monthly_close_levels).
    # Окно больше, чем у PMH/PML выше (PERIODS_MONTHS_BACK_PMC, не PERIODS_
    # MONTHS_BACK) — см. комментарий у константы наверху файла.
    all_zones += _extract_monthly_close_levels(
        df_1d, PERIODS_MONTHS_BACK_PMC, current_price, atr_1d, max_distance, current_idx
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
    raw_zone_count = len(all_zones)  # сколько кандидатов вообще нашли все слои
                                       # ДО фильтра mitigated — для диагностики ниже:
                                       # если пусто и тут уже 0, дело в max_distance
                                       # (не дошли до этой строки живыми); если тут
                                       # много, а после mitigated пусто — дело в том,
                                       # что все кандидаты оказались "пробиты".
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

    # Второй, более мягкий проход — ТОЛЬКО для MACRO-зон (см. докстринг
    # _merge_nearby_macro_zones). Regular-зоны (find_peaks/PMH/PML/PWH/PWL/POC)
    # не трогаем: измерено на реальных данных (count_levels.py, HYPE/июль) -
    # там близких пар за месяц не нашлось ни разу, а трогать их скоринг
    # (confluence-бонусы) без такой же проверки рискованно.
    supports = _merge_nearby_macro_zones(supports)
    resistances = _merge_nearby_macro_zones(resistances)

    # POC: ПОСЛЕ merge. Если совпал со слитой зоной - бонус один раз, иначе своя зона.
    if df_4h is not None and len(df_4h) >= 50:
        poc_price, atr_4h = _get_poc(df_4h, current_price)
        poc_date_str = pd.to_datetime(df_4h['timestamp'].iloc[-1], unit='ms').strftime('%Y-%m-%d')

        # Помечаем исходную сторону КАЖДОЙ зоны перед объединением. Она уже
        # верно определена структурно (_is_support при построении, popped
        # чуть выше) и не должна меняться из-за того, где сейчас current_price.
        # РАНЬШЕ сторону "восстанавливали" по цене для ВСЕХ зон без
        # исключения — из-за этого сопротивление, мимо которого цена успела
        # пройти на момент скана, перекрашивалось в поддержку (и наоборот),
        # хотя структурно (тип, дата, откуда построено) оставалось прежним.
        # Единственная зона, у которой правда нет своей стороны — новая
        # самостоятельная POC-зона ('4h_poc_standalone'), она не проходит
        # через этот цикл и остаётся без метки.
        for z in supports:
            z['_orig_side'] = 'support'
        for z in resistances:
            z['_orig_side'] = 'resistance'

        # POC отдельно для supports и resistances (poc - это уровень, может попасть в любой)
        all_merged = supports + resistances
        all_merged = _apply_poc_confluence(all_merged, poc_price, atr_4h, poc_date_str)
        supports, resistances = [], []
        for z in all_merged:
            orig_side = z.pop('_orig_side', None)
            if orig_side == 'support':
                supports.append(z)
            elif orig_side == 'resistance':
                resistances.append(z)
            else:
                # Только самостоятельная POC-зона — своей стороны нет,
                # кладём по текущей цене (законно только для неё).
                if z['max'] < current_price:
                    supports.append(z)
                else:
                    resistances.append(z)
# === НОВЫЙ БЛОК: ФОЛБЭК ДЛЯ ПУСТЫХ ЗОН (ИЩЕМ МИНИМУМ 2 УРОВНЯ) ===
    def _run_fallback(is_support_target, needed):
        inf_dist = float('inf')  # Бесконечный радар
        fb_all = []
        if df_1M is not None: fb_all += _extract_macro_swings(df_1M, current_price, inf_dist, 5.0, "1M_MACRO", atr_1d=atr_1d)
        if df_1W is not None: fb_all += _extract_macro_swings(df_1W, current_price, inf_dist, 4.0, "1W_MACRO", atr_1d=atr_1d)
        deep_history = len(df_1d)  # Снимаем лимит в 3-4 месяца, сканируем вообще всё
        fb_all += _extract_period_extremes(df_1d, 'ME', deep_history, current_price, atr_1d, inf_dist, "1M_high_PMH", "1M_low_PML", current_idx)
        fb_all += _extract_period_extremes(df_1d, 'W', deep_history, current_price, atr_1d, inf_dist, "1W_high_PWH", "1W_low_PWL", current_idx)
        fb_all += _extract_find_peaks_layer(df_1d, current_price, atr_1d, inf_dist, current_idx)

        fb_filtered = []
        for z in fb_all:
            if z.get('mitigated'): continue
            is_sup = z.pop('_is_support', None)
            z.pop('mitigated', None)

            if is_sup == is_support_target:
                # Жесткая проверка: сопротивления должны быть строго ВЫШЕ цены, поддержки строго НИЖЕ
                if is_support_target and z['max'] >= current_price: continue
                if not is_support_target and z['min'] <= current_price: continue
                fb_filtered.append(z)

        merged = merge_overlapping_zones(fb_filtered)
        merged = _merge_nearby_macro_zones(merged)

        # Сортируем по удаленности от цены (чем ближе, тем лучше)
        merged.sort(key=lambda x: abs(((x['min'] + x['max']) / 2) - current_price))
        return merged[:needed]

    # Докидываем до 2 уровней, если не хватает
    if len(supports) < 2:
        missing = 2 - len(supports)
        for z in _run_fallback(True, missing):
            if not any(abs(z['min'] - s['min']) < 1e-9 for s in supports):
                supports.append(z)

    if len(resistances) < 2:
        missing = 2 - len(resistances)
        for z in _run_fallback(False, missing):
            if not any(abs(z['min'] - r['min']) < 1e-9 for r in resistances):
                resistances.append(z)
    # =================================================================
    compress_fat_zones(supports, coin)
    compress_fat_zones(resistances, coin)
    supports, resistances = resolve_cross_overlaps(supports, resistances)

    # Касания — по границам ИТОГОВЫХ зон (см. count_zone_touches), для всех
    # типов, включая MACRO. Заменяет максимум "касаний вокруг точки" исходных
    # уровней, который оставался после слияния.
    for z in supports:
        z['reaction_count'] = count_zone_touches(df_1d, z, True, current_idx)
    for z in resistances:
        z['reaction_count'] = count_zone_touches(df_1d, z, False, current_idx)

    # Счётчик MACRO-зон по таймфреймам — для отчёта пересчёта (см.
    # swing_hunter.py): видно, находит ли слой 1W_MACRO хоть что-то.
    for z in supports + resistances:
        t = str(z.get('type', ''))
        if '1W_MACRO' in t:
            MACRO_LAYER_STATS['1W'] += 1
        if '1M_MACRO' in t:
            MACRO_LAYER_STATS['1M'] += 1

    # Диагностика — только когда результат ПУСТ с обеих сторон. Не теория,
    # а числа с этого конкретного прогона: current_price/max_distance/atr —
    # если цена реально дальше max_distance от всего, будет видно здесь
    # прямо в консоли на следующем прогоне precalc_for_bot.py, а не в
    # виде моих рассуждений без цифр.
    if not supports and not resistances:
        print(
            f"[LEVELS DEBUG] {coin}: пусто и supports, и resistances | "
            f"current_price={current_price:.6g} | max_distance={max_distance:.6g} "
            f"({max_distance / current_price * 100:.1f}% от цены) | atr_1d={atr_1d:.6g} | "
            f"кандидатов до mitigated-фильтра={raw_zone_count}"
        )

    return {
        "supports": supports,
        "resistances": resistances,
    }