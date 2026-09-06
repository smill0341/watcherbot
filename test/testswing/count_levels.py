# -*- coding: utf-8 -*-
"""
count_levels.py
=================
Счётчик уровней за ПЕРИОД — без бэктеста, без вотчеров, без сделок.
Вызывает build_levels() на каждом 12-часовом шаге внутри заданного периода
(та же частота, с которой уровни обновляются у настоящего бота — см.
TIME_ASIAN_CLOSE/TIME_US_OPEN в swing_hunter.py) и печатает/сохраняет
реальные min/max каждой зоны на каждом шаге.

Цель — увидеть дрожание координат одного и того же реального уровня между
соседними пересчётами (тот самый источник дублей), не гоняя весь тестер и
не сверяя числа по логам вручную.

Результат:
  - в консоль: краткая сводка по каждой дате (сколько уровней, где именно)
  - в CSV (count_levels_out.csv): полная таблица — по строке на каждую
    зону на каждую дату, с реальными min/max. Открываете в Excel,
    сортируете по min — почти одинаковые зоны на соседних датах лягут
    рядом, дрожание видно сразу.

Запуск: правите CONFIG ниже, затем python count_levels.py
"""

import os
import sys
import time
import datetime
import csv

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

MASTER_BOT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../master_bot"))
if MASTER_BOT_ROOT not in sys.path:
    sys.path.insert(0, MASTER_BOT_ROOT)
TEST_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../"))
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

import pandas as pd
from modules.cryptano.utils.crypto_utils import exchange
from testswing.levels_builder import build_levels, compress_fat_zones, CONFLUENCE_BONUS, MIN_SCORE_MONTH_WEEK_CONFLUENCE

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
CONFIG = {
    'COINS': ['HYPE'],                     # список монет
    'DATE_START': "2026-07-01 00:00:00",  # начало периода
    'DATE_END': "2026-07-31 00:00:00",    # конец периода
    'STEP_HOURS': 12,                     # шаг пересчёта — 12ч = как у настоящего бота
    'OUTPUT_CSV': "count_levels_out.csv",
    # Допуск на слияние БЛИЗКИХ (не обязательно физически пересекающихся) зон,
    # % от цены. Обычный merge_overlapping_zones внутри build_levels сливает
    # зоны только если они пересекаются — если между ними есть зазор, они так
    # и остаются двумя отдельными записями, даже если это дрожание одного и
    # того же уровня между соседними 12ч-пересчётами. Этот скрипт считает
    # ВТОРОЙ, отдельный проход слияния поверх обычного результата — просто
    # чтобы на цифрах увидеть, насколько сильно это сократило бы число зон,
    # ничего не меняя в боевом levels_builder.py.
    'ZONE_MERGE_DISTANCE_PCT': 4.0,
    # Допуск для ВТОРОГО варианта борьбы со спамом — не сливать близкие зоны,
    # а оставлять только сильнейшую по reaction_count, остальные рядом с ней
    # выбрасывать целиком (см. keep_strongest_zones). Тот же % от цены, что
    # и ZONE_MERGE_DISTANCE_PCT, но смысл другой: тут не расширяем зону,
    # а именно удаляем "лишние" соседние.
    'ZONE_SUPPRESS_DISTANCE_PCT': 4.0,
}

# Иерархия таймфрейма-источника зоны, старше -> выше ранг. Используется только
# как тай-брейк при РАВНОМ reaction_count в keep_strongest_zones — сам score
# сюда сознательно не подмешиваем (см. обсуждение с пользователем: score
# подгонялся руками и не годится как критерий "что выбросить навсегда").
_TIMEFRAME_RANK = (
    ("1M_", 4),
    ("1W_", 3),
    ("1d_", 2),
    ("4h_", 1),
)


def _timeframe_rank(zone_type):
    """Берёт САМЫЙ старший таймфрейм, встречающийся в строке type (зона может
    быть составной после confluence, напр. '1M_low_PML + 1W_low_PWL')."""
    zone_type = zone_type or ''
    best = 0
    for prefix, rank in _TIMEFRAME_RANK:
        if prefix in zone_type:
            best = max(best, rank)
    return best


def keep_strongest_zones(zones, tolerance_pct, verbose_label=None):
    """Альтернатива merge_by_distance: НЕ сливает границы, а для каждой
    группы близких зон (в пределах tolerance_pct% по расстоянию между
    серединами) оставляет ровно одну — с наибольшим reaction_count
    (тай-брейк — старший таймфрейм источника, см. _timeframe_rank), все
    остальные рядом с ней выбрасывает целиком.

    Специально сравнение идёт "каждый кандидат vs уже выбранный победитель",
    а НЕ "сосед с соседом по цепочке" — иначе A и D могли бы попасть в одну
    группу транзитивно через B и C, даже если A и D сами по себе далеко друг
    от друга. Порядок: сортируем по силе (убывание), берём сильнейшую живую
    зону как победителя, гасим всё, что в допуске ИМЕННО от неё, повторяем
    для оставшихся.

    zones — список зон ОДНОЙ стороны (только supports либо только
    resistances). Возвращает новый список (не мутирует входные dict-ы).
    """
    if not zones:
        return []

    # Сортировка: reaction_count по убыванию, при равенстве — таймфрейм
    # по убыванию (месяц раньше недели раньше дня раньше 4h).
    remaining = sorted(
        zones,
        key=lambda z: (z.get('reaction_count', 0), _timeframe_rank(z.get('type', ''))),
        reverse=True,
    )

    winners = []
    suppressed_log = []  # [(winner, [проигравшие...])] — для отладочного вывода

    while remaining:
        winner = remaining.pop(0)
        winner_mid = (winner['min'] + winner['max']) / 2
        tolerance = winner_mid * (tolerance_pct / 100.0)

        losers = []
        still_remaining = []
        for candidate in remaining:
            cand_mid = (candidate['min'] + candidate['max']) / 2
            if abs(cand_mid - winner_mid) <= tolerance:
                losers.append(candidate)
            else:
                still_remaining.append(candidate)

        winners.append(winner)
        if losers:
            suppressed_log.append((winner, losers))
        remaining = still_remaining

    if verbose_label and suppressed_log:
        print(f"  [{verbose_label}] подавлено соседями:")
        for winner, losers in suppressed_log:
            w_desc = f"{winner['min']:.4f}-{winner['max']:.4f}[{winner.get('type','?')}, touches={winner.get('reaction_count',0)}]"
            for l in losers:
                l_desc = f"{l['min']:.4f}-{l['max']:.4f}[{l.get('type','?')}, touches={l.get('reaction_count',0)}]"
                print(f"      {w_desc}  вытеснил  {l_desc}")

    # Возвращаем в исходном порядке по цене — удобнее сравнивать с before/after.
    return sorted(winners, key=lambda z: z['min'])


def merge_by_distance(zones, tolerance_pct):
    """Второй проход слияния ПОВЕРХ уже готовых зон (после обычного
    merge_overlapping_zones внутри build_levels). Та же логика подсчёта
    score, что и в levels_builder.merge_overlapping_zones — но условие
    слияния ослаблено: не только 'пересекается', а 'зазор между зонами
    меньше tolerance_pct% от цены соседней зоны'."""
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
        gap_tolerance = last['max'] * (tolerance_pct / 100.0)
        if current['min'] <= last['max'] + gap_tolerance:
            last['max'] = max(last['max'], current['max'])
            last['base_score'] = max(last['base_score'], current['base_score'])
            last['_has_peak'] = last['_has_peak'] or current['_has_peak']
            last['_has_calendar'] = last['_has_calendar'] or current['_has_calendar']
            last['_has_month_cal'] = last['_has_month_cal'] or current['_has_month_cal']
            last['_has_week_cal'] = last['_has_week_cal'] or current['_has_week_cal']

            confluence = CONFLUENCE_BONUS if (last['_has_peak'] and last['_has_calendar']) else 0.0
            last['score'] = round(last['base_score'] + confluence, 2)

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


# =====================================================================
# СБОР ДАННЫХ (тот же паттерн, что в swing_hunter.py)
# =====================================================================
def safe_fetch_ohlcv(sym, tf, lim, params):
    for attempt in range(5):
        try:
            return exchange.fetch_ohlcv(sym, timeframe=tf, limit=lim, params=params)
        except Exception as e:
            if "10006" in str(e) or "Rate Limit" in str(e) or "Too many visits" in str(e):
                print(f"   ⚠️ Rate limit — жду 4 сек (попытка {attempt + 1}/5)...")
                time.sleep(4.0)
            else:
                raise e
    raise Exception("Биржа заблокировала запросы после 5 попыток.")


def _resolve_symbol(coin):
    if not exchange.markets:
        exchange.load_markets()
    symbol_perp = f"{coin.upper()}/USDT:USDT"
    symbol_spot = f"{coin.upper()}/USDT"
    return symbol_perp if symbol_perp in exchange.markets else symbol_spot # type: ignore


def _fetch_levels_at(symbol, coin, dt):
    fetch_params = {'endTime': int(dt.timestamp() * 1000)}

    ohlcv_1M = safe_fetch_ohlcv(symbol, "1M", 60, fetch_params)
    ohlcv_1W = safe_fetch_ohlcv(symbol, "1W", 150, fetch_params)
    ohlcv_1d = safe_fetch_ohlcv(symbol, "1d", 365, fetch_params)
    ohlcv_4h = safe_fetch_ohlcv(symbol, "4h", 200, fetch_params)

    if len(ohlcv_1d) < 50:
        return None

    df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1M) >= 5 else None
    df_1W = pd.DataFrame(ohlcv_1W, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1W) >= 5 else None
    df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_4h) >= 50 else None

    return build_levels(df_1M, df_1W, df_1d, df_4h, coin)


# =====================================================================
# ОСНОВНОЙ ПРОГОН
# =====================================================================
def run():
    date_start = datetime.datetime.strptime(CONFIG['DATE_START'], "%Y-%m-%d %H:%M:%S")
    date_end = datetime.datetime.strptime(CONFIG['DATE_END'], "%Y-%m-%d %H:%M:%S")
    step = datetime.timedelta(hours=CONFIG['STEP_HOURS'])

    steps = []
    dt = date_start
    while dt <= date_end:
        steps.append(dt)
        dt += step

    print(f"Период: {date_start} .. {date_end}, шаг {CONFIG['STEP_HOURS']}ч ({len(steps)} точек на монету)")

    rows = []  # для CSV: одна строка на (дата, монета, сторона, зона)

    for coin in CONFIG['COINS']:
        symbol = _resolve_symbol(coin)
        print(f"\n=== {coin} ({symbol}) ===")

        for dt in steps:
            try:
                levels = _fetch_levels_at(symbol, coin, dt)
            except Exception as e:
                print(f"  {dt}: ❌ Ошибка: {e}")
                continue

            if levels is None:
                print(f"  {dt}: недостаточно данных — пропуск")
                continue

            supports_before = levels.get("supports", [])
            resistances_before = levels.get("resistances", [])

            tolerance = CONFIG['ZONE_MERGE_DISTANCE_PCT']
            supports_after = compress_fat_zones(
                merge_by_distance([dict(z) for z in supports_before], tolerance), coin
            )
            resistances_after = compress_fat_zones(
                merge_by_distance([dict(z) for z in resistances_before], tolerance), coin
            )

            # Третий вариант: не сливаем границы, а оставляем сильнейшую по
            # reaction_count зону из каждой группы близких, остальные
            # выбрасываем целиком (см. keep_strongest_zones). compress_fat_zones
            # тут не нужен — победитель не расширялся, его ширина не менялась.
            suppress_tolerance = CONFIG['ZONE_SUPPRESS_DISTANCE_PCT']
            supports_strongest = keep_strongest_zones(
                [dict(z) for z in supports_before], suppress_tolerance,
                verbose_label=f"{coin} {dt} supports"
            )
            resistances_strongest = keep_strongest_zones(
                [dict(z) for z in resistances_before], suppress_tolerance,
                verbose_label=f"{coin} {dt} resistances"
            )

            # Краткая консольная сводка — только цифры, отсортировано по цене,
            # плюс было/стало после доп. слияния по расстоянию и после
            # "оставить сильнейшего".
            sup_str = ", ".join(f"{z['min']:.4f}-{z['max']:.4f}[{z.get('type','?')}]" for z in sorted(supports_before, key=lambda z: z['min']))
            res_str = ", ".join(f"{z['min']:.4f}-{z['max']:.4f}[{z.get('type','?')}]" for z in sorted(resistances_before, key=lambda z: z['min']))
            print(f"  {dt} | supports({len(supports_before)}->merge:{len(supports_after)}->strongest:{len(supports_strongest)}): {sup_str or '-'}")
            print(f"  {' ' * len(str(dt))} | resistances({len(resistances_before)}->merge:{len(resistances_after)}->strongest:{len(resistances_strongest)}): {res_str or '-'}")

            for stage, supports, resistances in (('before', supports_before, resistances_before),
                                                  ('after_merge', supports_after, resistances_after),
                                                  ('after_strongest', supports_strongest, resistances_strongest)):
                for z in supports:
                    rows.append({'date': dt, 'coin': coin, 'stage': stage, 'side': 'support',
                                 'min': z['min'], 'max': z['max'],
                                 'mid': (z['min'] + z['max']) / 2,
                                 'score': z.get('score', 0), 'type': z.get('type', '?'),
                                 'class': z.get('class', 'regular')})
                for z in resistances:
                    rows.append({'date': dt, 'coin': coin, 'stage': stage, 'side': 'resistance',
                                 'min': z['min'], 'max': z['max'],
                                 'mid': (z['min'] + z['max']) / 2,
                                 'score': z.get('score', 0), 'type': z.get('type', '?'),
                                 'class': z.get('class', 'regular')})

            time.sleep(0.3)  # чтобы не улететь в rate limit

    if rows:
        out_path = os.path.join(CURRENT_DIR, CONFIG['OUTPUT_CSV'])
        with open(out_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['date', 'coin', 'stage', 'side', 'min', 'max', 'mid', 'score', 'type', 'class'])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nСохранено: {out_path} ({len(rows)} строк)")
        print(f"Допуск слияния ZONE_MERGE_DISTANCE_PCT = {CONFIG['ZONE_MERGE_DISTANCE_PCT']}%")
        print(f"Допуск 'оставить сильнейшего' ZONE_SUPPRESS_DISTANCE_PCT = {CONFIG['ZONE_SUPPRESS_DISTANCE_PCT']}%")
        print("Колонка 'stage': 'before' — как сейчас у бота, 'after_merge' — с доп. слиянием близких зон "
              "(границы расширяются), 'after_strongest' — близкие зоны не сливаются, а лишние выбрасываются "
              "целиком, остаётся только зона с наибольшим reaction_count.")
        print("Откройте в Excel, отсортируйте по 'coin' -> 'side' -> 'stage' -> 'mid' — "
              "видно и дрожание между датами, и разницу между всеми тремя вариантами сразу.")

    print("\nГотово.")


if __name__ == "__main__":
    run()