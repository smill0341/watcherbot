import time
import datetime
import os
import threading
import pandas as pd
import numpy as np
from scipy.signal import find_peaks
import schedule
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.common import resolve_symbol, KNOWN_TICKER_ALIASES
from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.utils.levels_builder import (
    build_levels, _is_mitigated, merge_overlapping_zones, _merge_nearby_macro_zones,
    compress_fat_zones, resolve_cross_overlaps,
)

# =========================================================
# ⚙️ НАСТРОЙКИ РАСПИСАНИЯ
# =========================================================
TIME_ASIAN_CLOSE = "03:05"
TIME_US_OPEN = "15:05"

# Сколько дней монета может отсутствовать в топ-70 по обороту, прежде чем
# её запись в macro_levels.json реально удаляется (мердж вместо перезаписи,
# см. build_macro_levels). Слишком мало — вернёмся к старому багу (вотчер
# осиротеет за один цикл), слишком много — файл будет копить мусор.
MACRO_STALE_DAYS = 14

# 🎛 НАСТРОЙКИ V2 ФИЛЬТРОВ
IMPULSE_ATR_MULTIPLIER = 2.5  # Цена должна улететь минимум на 2.5 ATR от зоны
IMPULSE_LOOKAHEAD_DAYS = 10   # Даем цене 10 дней на то, чтобы показать этот импульс

# BASE_DIR указывает на modules/cryptano/ (json-файлы теперь в jsonbank/ — см. utils/paths.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from modules.cryptano.utils.paths import MACRO_LEVELS_FILE, CUSTOM_LEVELS_FILE
from modules.cryptano.utils.levels_builder import MACRO_LAYER_STATS
from modules.cryptano.levels_history import save_levels_snapshot

# candle_store.py (локальная SQLite-база свечей) лежит в web/backend/, а не
# в modules/cryptano/ — раньше её использовал только дашборд (15m). Ничего
# в самом candle_store.py не меняем, просто тоже её читаем — тем же
# способом (has_data -> top_up_tail/backfill_symbol -> get_candles), каким
# её уже читает watcher_plan.py для 15m. Путь считаем от BASE_DIR (см. выше),
# как и остальные пути в этом файле, а не полагаемся на то, что web/backend
# уже пакет с __init__.py.
import sys as _sys
_WEB_BACKEND_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "web", "backend"))
if _WEB_BACKEND_DIR not in _sys.path:
    _sys.path.insert(0, _WEB_BACKEND_DIR)
import candle_store # type: ignore

# candle_store использует "1w" (маленькая w) для недельного таймфрейма,
# а здесь (и у биржи) он "1W" — единственное расхождение в написании между
# двумя местами. Ничего в candle_store.py не трогаем, просто переводим
# на его написание в момент обращения к нему.
_CANDLE_STORE_TF_MAP = {"1W": "1w"}

# Порог объёма и потолок числа монет — настраиваются в config.json (ключи
# crypto.min_volume_usd / crypto.max_coins), а не в коде: если ключа там
# нет, используется значение по умолчанию ниже. Единая точка правды —
# precalc_for_bot.py импортирует ИМЕННО эти константы отсюда, не держит
# своей копии (см. обсуждение бага с RAYDIUM — порог/потолок расходились
# молча, пока не свели в одно место).
_CONFIG_FILE = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "config.json"))
_config = load_json(_CONFIG_FILE, default={})
MIN_VOLUME_USD = _config.get("crypto", {}).get("min_volume_usd", 10_000_000)
MAX_COINS = _config.get("crypto", {}).get("max_coins", 80)

# Порог "это тот же уровень или новый" для паспорта уровня (см. обсуждение
# дрожания min/max и пропадания-появления зон из-за того, что ATR, от
# которого зависят и ширина зоны, и порог дистанции, пересчитывается заново
# на каждой сборке). То же значение, что уже используют LEVEL_DEDUP_
# TOLERANCE_PCT (bounce_manager.py) и MACRO_MERGE_DISTANCE_PCT (levels_
# builder.py) для той же по смыслу задачи — держим согласованно, а не
# заводим третью независимую константу.
LEVEL_PASSPORT_TOLERANCE_PCT = 4.0
# Паспорт держит старые границы зоны, только если её состав не менялся и
# ширина отличается не больше чем на столько процентов (обычное дрожание ATR).
# Больше — берутся свежие границы (см. _reconcile_levels_with_registry).
PASSPORT_WIDTH_CHANGE_PCT = 30.0



def _is_poc_zone(zone):
    """POC-зона (4h_poc_standalone) — не исторический свинг с точкой рождения,
    а живой, скользящий показатель "где концентрировался объём за последнее
    время" (см. levels_builder.py — у неё 'date' всегда ставится как сегодняшняя
    дата скана, current_idx_date, а не дата какой-то конкретной свечи). Её
    дрожание координат между сканами — не баг, а её прямая работа: она ДОЛЖНА
    двигаться вместе с недавним объёмом. Паспорт её не трогает вообще.

    Точное совпадение типа, а не подстрока: зона вида 'X+4h_poc' (структурный
    уровень, который POC просто подтвердил объёмом — см. confluence в
    levels_builder.py) — это НЕ чистый POC, это по-прежнему структурная зона
    со своей исторической датой, паспорт её замораживает как обычно."""
    return zone.get('type') == '4h_poc_standalone'


def _is_pmc_zone(zone):
    """PMC-зона (1M_close_PMC, ровно этот тип, БЕЗ confluence с чем-либо ещё)
    — цена ЗАКРЫТИЯ конкретного, уже закрытого месяца. В отличие от PMH/PWH
    (где сама цена — экстремум месяца/недели, а гуляет только ширина ATR-
    буфера вокруг неё), у PMC гулять почти нечему: закрытие того же самого
    месяца — исторический факт, не меняется скан от скана.

    Паспорт её тем не менее НЕ должен подхватывать по общей схеме "ближайшая
    в пределах 4% по цене" — на практике (см. обсуждение проекта, BTC) это
    приводило к тому, что свежий PMC-кандидат "прилипал" к СТАРОЙ, вообще не
    связанной с ним записи в реестре (например, старому PWH/find_peaks,
    оставшемуся с прошлых сканов ещё до появления PMC), если та случайно
    оказывалась в пределах допуска — и получал вместо своих точных координат
    чужие, замороженные. Раз координата и так стабильна сама по себе — прогон
    через паспорт только вредит, поэтому PMC, как и POC выше, просто
    добавляется в реестр как есть, без сверки со старыми записями.

    Точное совпадение типа — так же, как у _is_poc_zone: если PMC слился с
    чем-то ещё (confluence, "1M_close_PMC + 1W_low_PWL..."), это уже обычная
    структурная зона нескольких источников, её паспорт трогает как всегда."""
    return zone.get('type') == '1M_close_PMC'


def _reconcile_levels_with_registry(old_zones, fresh_zones, is_support, df_1d, current_idx=None, scan_time=None):
    """Паспорт уровня.

    min/max/date уже существующей записи ("паспорт") считаются правдой по
    геометрии и НЕ переписываются результатом свежего пересчёта — ATR, от
    которого зависит и ширина зоны, и общий фильтр дистанции в levels_
    builder.py, гуляет от скана к скану даже когда сам исторический уровень
    никуда не сдвинулся. Обновляются только те поля, которым и положено
    быть свежими каждый раз честно: type/score/reaction_count/mitigated/
    class.

    Свежая зона без пары в старом списке -> новый паспорт, как есть.

    Старая запись без пары в свежем списке -> НЕ удаляется автоматически
    (она могла просто не пройти сегодняшний фильтр дистанции ATR в levels_
    builder.py, а не быть пробитой) — удаляется только если отдельная,
    самостоятельная проверка _is_mitigated() (та же функция, что использует
    сам levels_builder.py, не копия) говорит, что уровень реально пробит
    закрытием с даты, когда он появился. levels_builder.py при этом не
    трогаем — это отдельная проверка того же факта, снаружи.

    ИСКЛЮЧЕНИЕ: POC-зоны (см. _is_poc_zone) и PMC-зоны (см. _is_pmc_zone) в
    паспорт не попадают вообще — ни заморозка геометрии, ни mitigated-
    проверка осиротевших записей. У POC координата по конструкции плавает
    (нет "исторической правды", с которой можно было бы сверяться), у PMC —
    наоборот, координата и так стабильна, а сверка по 4% только рискует
    подменить её чужой, случайно близкой старой записью (см. докстринг
    _is_pmc_zone). Разными путями к одному и тому же выводу: обеим не нужен
    паспорт, обе просто проходят как есть.

    scan_time — ISO-дата момента скана (для activated_at новых уровней)."""
    if current_idx is None:
        current_idx = len(df_1d) - 1
    if scan_time is None:
        scan_time = datetime.datetime.now().isoformat()

    def _mid(z):
        return (z['min'] + z['max']) / 2.0

    passthrough_fresh = [z for z in fresh_zones if _is_poc_zone(z) or _is_pmc_zone(z)]
    fresh_zones = [z for z in fresh_zones if not (_is_poc_zone(z) or _is_pmc_zone(z))]
    old_zones_for_match = [z for z in old_zones if not (_is_poc_zone(z) or _is_pmc_zone(z))]

    used_old = set()
    result = []  # POC/PMC не в этом списке вообще — добавляются отдельно, в
                 # самом конце, чтобы финальный merge-проход ниже их не трогал
                 # (см. return).

    for fresh in fresh_zones:
        fresh_mid = _mid(fresh)
        match_i = None
        for i, old in enumerate(old_zones_for_match):
            if i in used_old:
                continue
            old_mid = _mid(old)
            if old_mid == 0:
                continue
            if abs(fresh_mid - old_mid) / old_mid * 100.0 <= LEVEL_PASSPORT_TOLERANCE_PCT:
                match_i = i
                break
        if match_i is not None:
            used_old.add(match_i)
            old_z = old_zones_for_match[match_i]
            merged = dict(old_z)  # геометрия/дата/activated_at — от старой записи
            for key in ('type', 'score', 'reaction_count', 'mitigated', 'class'):
                if key in fresh:
                    merged[key] = fresh[key]
            # КОГДА ПАСПОРТ НЕ ДЕРЖИТ СТАРЫЕ ГРАНИЦЫ. Заморозка нужна против
            # дрожания ATR (та же зона, ширина чуть гуляет от скана к скану).
            # Но раньше она держала старые min/max ВСЕГДА — зона не сужалась,
            # когда умирал уровень внутри неё, и новые правила ширины (потолок
            # MACRO K*ATR) до старых зон не доходили вообще. Теперь свежие
            # границы берутся, если:
            #   - изменился СОСТАВ зоны (набор уровней в названии type), или
            #   - ширина отличается больше чем на PASSPORT_WIDTH_CHANGE_PCT.
            def _parts(t):
                return frozenset(p.strip() for p in str(t or '').split('+') if p.strip())
            old_w = float(old_z['max']) - float(old_z['min'])
            fresh_w = float(fresh['max']) - float(fresh['min'])
            width_changed = fresh_w > 0 and abs(old_w - fresh_w) / fresh_w * 100.0 > PASSPORT_WIDTH_CHANGE_PCT
            if _parts(old_z.get('type')) != _parts(fresh.get('type')) or width_changed:
                merged['min'] = fresh['min']
                merged['max'] = fresh['max']
                if fresh.get('date'):
                    merged['date'] = fresh['date']
            # activated_at — максимум старого и свежего. Обычно они совпадают
            # (то же множество компонентов), но если fresh['type'] "разросся"
            # новым confluence-компонентом (см. merge_overlapping_zones в
            # levels_builder.py) — свежий activated_at может быть позже: зона
            # готова не раньше момента, когда подтвердился ПОСЛЕДНИЙ из её
            # компонентов, а не первый исторический.
            fresh_act = fresh.get('activated_at')
            if fresh_act and (not merged.get('activated_at') or fresh_act > merged['activated_at']):
                merged['activated_at'] = fresh_act
            result.append(merged)
        else:
            # Новый уровень — levels_builder.py уже проставил activated_at
            # по своей формуле (задержка подтверждения по типу зоны). scan_time
            # тут только safety-fallback на случай, если по какой-то причине
            # оно не пришло (старый кэш модуля, ручной вызов в обход builder).
            fresh_with_activated = dict(fresh)
            if 'activated_at' not in fresh_with_activated:
                fresh_with_activated['activated_at'] = scan_time
            result.append(fresh_with_activated)

    for i, old in enumerate(old_zones_for_match):
        if i in used_old:
            continue
        keep = True
        date_str = old.get('date')
        if date_str:
            try:
                date_mask = pd.to_datetime(df_1d['timestamp'], unit='ms').dt.strftime('%Y-%m-%d') == date_str
                date_rows = df_1d.index[date_mask]
                if len(date_rows) > 0:
                    idx = int(date_rows[0])
                    price = old['min'] if is_support else old['max']
                    if _is_mitigated(df_1d, idx, price, is_support, current_idx=current_idx):
                        keep = False
            except Exception as e:
                print(f"[PASSPORT] Не смог проверить mitigated для старой записи ({date_str}): {e} — оставляю как есть")
        if keep:
            result.append(old)

    # Финальный проход слияния — ПОСЛЕ паспорта, не только до него. levels_
    # builder.py уже сливает пересекающиеся зоны, но видит только СЕГОДНЯШНИЙ
    # свежий пересчёт целиком. Паспорт выше подменяет геометрию КАЖДОЙ зоны
    # по отдельности на старую, замороженную — и две РАЗНЫЕ зоны, свежие
    # версии которых не пересекались, могут оказаться физически пересекающимися
    # уже здесь, после независимой подмены каждой на свою старую геометрию.
    # Тот же самый merge, что уже есть в levels_builder.py, не новый — MACRO
    # своим мягким правилом (по зазору между серединами), остальное — строгим
    # (только реальное физическое пересечение), в том же порядке, что и там.
    #
    # ВАЖНО: обёрнуто в try/except намеренно. Это ДОПОЛНИТЕЛЬНАЯ уборка
    # поверх уже готового, честного результата паспорта — если она сама
    # на каких-то данных (например, старых записях с форматом до этой
    # правки) споткнётся об исключение, монета не должна из-за этого
    # ТЕРЯТЬ ВСЕ уровни целиком (см. build_macro_levels — необработанное
    # исключение здесь пробросилось бы наверх и оставило бы coin вообще
    # без обновления на этот цикл). Деградация — вернуть результат ДО
    # этого прохода (дубли могут остаться), а не потерять всё.
    # Закидываем свежие POC/PMC в общий список ДО того, как алгоритм начнет склейку
    result = result + passthrough_fresh

    try:
        result = _merge_nearby_macro_zones(result)
        result = merge_overlapping_zones(result)
    except Exception as e:
        print(f"[PASSPORT MERGE] Не смог финально слить зоны: {e} — оставляю без этого прохода")

    # Возвращаем финальный склеенный массив
    return result


def _reconcile_coin_levels(coin, fresh_levels, macro_base, df_1d, scan_time=None):
    """Обёртка над _reconcile_levels_with_registry для одной монеты, отдельно
    для supports и resistances. Единственная точка, которую вызывают оба
    места сборки (build_macro_levels и build_levels_for_single_coin) — не
    дублируем логику дважды (см. историю с дублем export в background_
    tasks.py, тут та же ловушка).

    Количество зон на сторону НЕ ограничиваем (было — попробовали
    MAX_LEVELS_PER_SIDE=1, оказалось слишком жёстко, откатили по просьбе —
    возвращает столько зон, сколько реально прошло паспорт, как было
    исходно).

    compress_fat_zones/resolve_cross_overlaps — те же два шага, которыми
    build_levels() в levels_builder.py сам завершает сборку (merge ->
    compress -> resolve_cross_overlaps), но там они видят только СЕГОДНЯШНИЙ
    пересчёт. _reconcile_levels_with_registry() выше в конце тоже делает
    свой merge (склеивая старые "замороженные" зоны со свежими) — и этот
    повторный merge геометрию УЖЕ сжатых зон может снова раздуть (старая
    зона + свежая, слитые в одну, физически шире любой из них по отдельности).
    Раньше после этого повторного merge сжатия не было вообще — отсюда
    зоны в разы шире паспортного допуска (см. обсуждение проекта, скрины
    с огромными зелёными/красными зонами). Этот фикс остаётся в силе —
    речь только про лимит КОЛИЧЕСТВА зон, не про их ширину."""
    old_entry = macro_base.get(coin, {})
    old_supports = old_entry.get('supports', []) if isinstance(old_entry, dict) else []
    old_resistances = old_entry.get('resistances', []) if isinstance(old_entry, dict) else []
    reconciled_supports = _reconcile_levels_with_registry(old_supports, fresh_levels["supports"], True, df_1d, scan_time=scan_time)
    reconciled_resistances = _reconcile_levels_with_registry(old_resistances, fresh_levels["resistances"], False, df_1d, scan_time=scan_time)

    compress_fat_zones(reconciled_supports, coin)
    compress_fat_zones(reconciled_resistances, coin)
    reconciled_supports, reconciled_resistances = resolve_cross_overlaps(reconciled_supports, reconciled_resistances)

    return {
        "supports": reconciled_supports,
        "resistances": reconciled_resistances,
    }

_hunter_lock = threading.Lock()


# Счётчик источников данных и времени за текущий скан — для отчёта в конце
# build_macro_levels (см. _fetch_stats_reset/_fetch_stats_report): видно,
# сколько реально было запросов к бирже и на что ушло время.
#   db_fresh   — взято из базы, база свежая, К БИРЖЕ НЕ ХОДИЛИ;
#   db_topup   — в базе было, но устарело -> докачали хвост (1 запрос);
#   db_backfill— в базе не было совсем -> скачали полностью;
#   exchange_fallback — база не сработала, запрос напрямую к бирже.
_FETCH_STATS = {"db_fresh": 0, "db_topup": 0, "db_backfill": 0, "exchange_fallback": 0}
_FETCH_ERRORS = []  # короткие уникальные сообщения об ошибках БД за скан (без спама на каждую монету)
_FETCH_ERRORS_MAX = 5
_FETCH_TIME = {"candles": 0.0, "levels": 0.0, "pauses": 0.0}  # секунды за скан


def _fetch_stats_reset():
    for k in MACRO_LAYER_STATS:
        MACRO_LAYER_STATS[k] = 0
    for k in _FETCH_STATS:
        _FETCH_STATS[k] = 0
    for k in _FETCH_TIME:
        _FETCH_TIME[k] = 0.0
    _FETCH_ERRORS.clear()


def _network_calls():
    """Сколько раз за скан реально ходили на биржу за свечами."""
    return _FETCH_STATS["db_topup"] + _FETCH_STATS["db_backfill"] + _FETCH_STATS["exchange_fallback"]


def _fetch_stats_report(total_sec=None):
    total = sum(_FETCH_STATS.values()) or 1
    db_total = _FETCH_STATS["db_fresh"] + _FETCH_STATS["db_topup"] + _FETCH_STATS["db_backfill"]
    print(
        f"[CANDLE SOURCE] из базы: {db_total}/{total}  "
        f"(свежие, без биржи: {_FETCH_STATS['db_fresh']}, докачан хвост: {_FETCH_STATS['db_topup']}, "
        f"первая докачка: {_FETCH_STATS['db_backfill']})  |  "
        f"напрямую с биржи (БД не сработала): {_FETCH_STATS['exchange_fallback']}  |  "
        f"запросов к бирже всего: {_network_calls()}"
    )
    if total_sec is not None:
        print(
            f"[SWING HUNTER] время: всего {total_sec:.1f} сек  |  свечи {_FETCH_TIME['candles']:.1f}  |  "
            f"расчёт уровней {_FETCH_TIME['levels']:.1f}  |  паузы {_FETCH_TIME['pauses']:.1f}"
        )
    print(
        f"[MACRO] свингов найдено: 1M={MACRO_LAYER_STATS['1M_raw']}, 1W={MACRO_LAYER_STATS['1W_raw']}  |  "
        f"в итоговых зонах: 1M={MACRO_LAYER_STATS['1M']}, 1W={MACRO_LAYER_STATS['1W']}  |  "
        f"монет без недельных свечей: {MACRO_LAYER_STATS['1W_no_data']}"
    )
    if _FETCH_ERRORS:
        print(f"[CANDLE SOURCE] ошибки БД за скан (до {_FETCH_ERRORS_MAX}, первые): {_FETCH_ERRORS[:_FETCH_ERRORS_MAX]}")


def _is_fresh(rows, cs_tf):
    """База свежая, если последняя сохранённая свеча открылась не раньше,
    чем 2 периода таймфрейма назад. Фоновый воркер дашборда (app.py::
    _candle_backfill_worker) и так каждые ~3 минуты докачивает все
    таймфреймы для монет из watchlist — для уровней на 4h/1d/1W/1M такого
    отставания хватает с огромным запасом. Монета не из watchlist (воркер
    её не обновляет) окажется несвежей — тогда хвост докачивается как раньше."""
    if not rows:
        return False
    tf_ms = candle_store.TIMEFRAME_MS.get(cs_tf)
    if not tf_ms:
        return False
    last_open_sec = int(rows[-1]["time"])
    return (time.time() - last_open_sec) * 1000 < 2 * tf_ms


# Вынесено на уровень модуля (раньше жило только внутри build_macro_levels)
# — build_levels_for_single_coin теперь тоже качает 1M/1W и должен так же
# переживать rate-limit биржи, а не падать с первой ошибки.
def _safe_fetch_ohlcv(sym, tf, lim):
    """Свечи для расчёта уровней — сначала из локальной БД (candle_store):
      1. в базе есть и она свежая (см. _is_fresh) -> отдаём из базы, к бирже
         НЕ ходим вообще (раньше тут ВСЕГДА делался top_up_tail = запрос к
         бирже на каждый таймфрейм каждой монеты, ~400 запросов за скан,
         даже когда всё уже лежало в базе);
      2. в базе есть, но устарела -> докачиваем хвост (top_up_tail) и читаем;
      3. в базе нет совсем -> качаем полностью (backfill_symbol) и читаем.
    Если с базой что-то пошло не так — фолбэк: прямой запрос к бирже с
    retry на rate-limit, как было раньше.

    Сам candle_store.py не менялся."""
    cs_tf = _CANDLE_STORE_TF_MAP.get(tf, tf)
    try:
        if candle_store.has_data(sym, cs_tf):
            rows = candle_store.get_candles(sym, cs_tf, limit=lim)
            if _is_fresh(rows, cs_tf):
                kind = "db_fresh"
            else:
                candle_store.top_up_tail(exchange, sym, cs_tf)
                rows = candle_store.get_candles(sym, cs_tf, limit=lim)
                kind = "db_topup"
        else:
            candle_store.backfill_symbol(exchange, sym, cs_tf)
            rows = candle_store.get_candles(sym, cs_tf, limit=lim)
            kind = "db_backfill"
        if rows:
            _FETCH_STATS[kind] += 1
            # candle_store хранит время в секундах (unix), бирже/pandas
            # ниже по коду нужны миллисекунды — тот же формат, что уже
            # был в ohlcv-списке с биржи (timestamp, open, high, low,
            # close, volume).
            return [
                [int(r['time']) * 1000, r['open'], r['high'], r['low'], r['close'], r['volume']]
                for r in rows
            ]
    except Exception as e:
        msg = f"{tf}: {e}"
        if msg not in _FETCH_ERRORS and len(_FETCH_ERRORS) < _FETCH_ERRORS_MAX:
            _FETCH_ERRORS.append(msg)

    _FETCH_STATS["exchange_fallback"] += 1
    for attempt in range(5):  # 5 попыток пробить блок
        try:
            return exchange.fetch_ohlcv(sym, timeframe=tf, limit=lim)
        except Exception as e:
            if "10006" in str(e) or "Rate Limit" in str(e) or "Too many visits" in str(e):
                print(f"⚠️  Bybit Rate Limit. Ждем 4 сек (Попытка {attempt+1}/5)...")
                time.sleep(4.0)
            else:
                raise e
    raise Exception("Биржа заблокировала запросы после 5 попыток")


def get_top_symbols():
    """Единая точка правды для списка торговых символов, прошедших фильтр
    по объёму (:USDT свопы, объём >= MIN_VOLUME_USD, топ-MAX_COINS по
    убыванию объёма). Раньше эта логика была продублирована здесь и ОТДЕЛЬНО
    в precalc_for_bot.py — при расхождении разъезжались тихо, без единой
    ошибки (ровно так родился баг с RAYDIUM: подправили порог в одном месте,
    он не появился в другом). Теперь precalc_for_bot.py импортирует эту же
    функцию вместо своей копии — правишь порог/потолок ЗДЕСЬ, меняется и
    в бою, и в ручном пересчёте истории разом."""
    if not exchange.markets:
        exchange.load_markets(reload=True)
    else:
        exchange.load_markets(reload=False)
    tickers = exchange.fetch_tickers()

    # Только своп/фьючерс (та же логика, что resolve_symbol() в
    # utils/common.py) — если брать ещё и спот, одна и та же монета
    # может пройти порог объёма под ОБОИМИ символами и попасть в
    # список дважды под одинаковым coin (symbol.split("/")[0]),
    # непредсказуемо схлопываясь в один ключ ниже.
    symbols_with_volume = []
    for sym, tick in tickers.items():
        if sym.endswith(':USDT'):
            vol = float(tick.get('quoteVolume') or 0)
            if vol >= MIN_VOLUME_USD:
                symbols_with_volume.append((sym, vol))

    # Сортируем по убыванию объёма и берём топ-MAX_COINS самых
    # ликвидных (см. MAX_COINS выше, настраивается в config.json) —
    # без потолка список разросся настолько, что скан-цикл стал
    # занимать многие минуты на одном только чистом ожидании между
    # монетами.
    symbols_with_volume.sort(key=lambda x: x[1], reverse=True)
    valid_symbols = [sym for sym, vol in symbols_with_volume[:MAX_COINS]]

    print(f"🔥 Найдено {len(symbols_with_volume)} монет с объёмом ≥ ${MIN_VOLUME_USD:,.0f}. Берём топ-{MAX_COINS}.")

    # Монеты с ручными зонами (custom_levels.json, кнопка "+ добавить зону")
    # добавляем в скан ПОСТОЯННО, на каждом пересчёте — независимо от того,
    # попадают они в топ по объёму или нет. Раньше такая монета вообще не
    # получала авто-уровней, если сама по объёму в топ-MAX_COINS не проходила
    # (get_top_symbols её просто никогда не видел) — ручная зона оставалась
    # единственной, авто-уровни не считались и не обновлялись никогда.
    manual_added = _add_manual_zone_coins(valid_symbols)
    if manual_added:
        print(f"✋ Монет с ручными зонами добавлено в скан сверх топа: {manual_added}")

    return valid_symbols


def _add_manual_zone_coins(valid_symbols: list) -> int:
    """Мутирует valid_symbols на месте — дописывает символы монет, у которых
    в custom_levels.json есть хотя бы одна ручная зона (support/resistance),
    и которых ещё нет в списке (той же проверкой на дубли, что и остальной
    список — по символу, не по coin, чтобы не завести SPOT+FUTURES дубль).
    Возвращает, сколько монет реально добавлено (для отчёта в консоль)."""
    custom_db = load_json(CUSTOM_LEVELS_FILE, default={})
    if not custom_db:
        return 0

    manual_coins = [
        coin for coin, zones in custom_db.items()
        if coin != "_meta" and ((zones or {}).get("supports") or (zones or {}).get("resistances"))
    ]
    if not manual_coins:
        return 0

    if not exchange.markets:
        exchange.load_markets(reload=False)
    markets = exchange.markets or {}

    existing = set(valid_symbols)
    added = 0
    for coin in manual_coins:
        symbol = resolve_symbol(coin, markets) or resolve_symbol(KNOWN_TICKER_ALIASES.get(coin, ""), markets)
        if not symbol or symbol in existing:
            continue
        valid_symbols.append(symbol)
        existing.add(symbol)
        added += 1
    return added


def build_macro_levels(bot=None, admin_chat_id=None):
    print(f"[SWING HUNTER] Запуск генерации зон интереса...")
    _fetch_stats_reset()
    scan_started_at = time.time()
    try:
        valid_symbols = get_top_symbols()

        # МЕРДЖ вместо полной перезаписи: если монета в этот раз не попала
        # в топ-70 (или биржа моргнула ошибкой на fetch_tickers) — её старая
        # запись остаётся как есть, а не пропадает из файла. Иначе вотчер,
        # который на неё завязан, замирает (check_v_* видит coin_macro=None
        # и выходит рано) и на дашборде превращается в "?", хотя формально
        # ещё жив в памяти — см. cleanup_ghost_watchers.py и обсуждение бага.
        macro_base = load_json(MACRO_LEVELS_FILE, default={})
        seen_coins = set()

        for symbol in valid_symbols:
            coin = symbol.split("/")[0].replace(":USDT", "")

            # Пауза 0.3 сек — защита от rate-limit биржи. Нужна ТОЛЬКО если
            # по этой монете реально ходили на биржу (докачка хвоста/полная
            # докачка/фолбэк). Если всё взялось из свежей базы — пауза не
            # нужна, она раньше просто добавляла ~30 сек на 100 монет.
            net_before = _network_calls()
            try:
                _t0 = time.time()
                # === АНАЛИЗ через новый levels_builder ===
                # 1M/1W добавлены для нового макро-слоя (_extract_macro_swings)
                # в levels_builder.py — build_levels() теперь их требует первыми
                # двумя аргументами. limit=60/150 — тот же запас, что в тестовой
                # версии (60 месяцев / 150 недель истории).
                ohlcv_1M = _safe_fetch_ohlcv(symbol, "1M", 60)
                ohlcv_1W = _safe_fetch_ohlcv(symbol, "1W", 150)
                ohlcv_1d = _safe_fetch_ohlcv(symbol, "1d", 365)
                ohlcv_4h = _safe_fetch_ohlcv(symbol, "4h", 200)
                _FETCH_TIME["candles"] += time.time() - _t0

                if len(ohlcv_1d) < 50:
                    continue

                seen_coins.add(coin)  # реально дошли до расчёта — точка отсчёта для "протухания"

                df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1M) >= 5 else None
                df_1W = pd.DataFrame(ohlcv_1W, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1W) >= 5 else None
                df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_4h) >= 50 else None

                _t1 = time.time()
                levels = build_levels(df_1M, df_1W, df_1d, df_4h, coin)

                has_any = (levels["supports"] or levels["resistances"])
                if has_any:
                    scan_time = datetime.datetime.now().isoformat()
                    reconciled = _reconcile_coin_levels(coin, levels, macro_base, df_1d, scan_time=scan_time)
                    macro_base[coin] = {
                        "supports": reconciled["supports"],
                        "resistances": reconciled["resistances"],
                        "updated_at": scan_time
                    }
                else:
                    # Явно пересчитали и уровней не нашли — это не "выпал из
                    # топ-70", а честный пустой результат, тут можно смело убрать.
                    macro_base.pop(coin, None)
                _FETCH_TIME["levels"] += time.time() - _t1

            except Exception as e:
                print(f"[HUNTER ERROR] Ошибка для {coin}: {e}")
                continue
            finally:
                # finally срабатывает и на continue/ошибке — пауза после любой
                # монеты, по которой реально был запрос к бирже.
                if _network_calls() > net_before:
                    _tp = time.time()
                    time.sleep(0.3)
                    _FETCH_TIME["pauses"] += time.time() - _tp

        # Чистим только по-настоящему протухшие записи — монеты, которые
        # не сканировались (не в топ-70) уже дольше STALE_DAYS. Если монета
        # просто на пару циклов выпала и вернулась — её данные спокойно ждут.
        cutoff = datetime.datetime.now() - datetime.timedelta(days=MACRO_STALE_DAYS)
        stale_coins = []
        for coin, entry in macro_base.items():
            if coin == "_meta" or coin in seen_coins:
                continue
            updated_at = entry.get("updated_at") if isinstance(entry, dict) else None
            try:
                if not updated_at or datetime.datetime.fromisoformat(updated_at) < cutoff:
                    stale_coins.append(coin)
            except Exception:
                stale_coins.append(coin)  # битая/непонятная дата — тоже протухшая
        for coin in stale_coins:
            del macro_base[coin]
        if stale_coins:
            print(f"🧹 Убраны протухшие (>{MACRO_STALE_DAYS} дн. вне топ-70): {stale_coins}")

        # 🕒 Метаданные всего файла — чтобы одним взглядом видеть, когда последний раз обновлялось
        macro_base["_meta"] = {
            "last_build": datetime.datetime.now().isoformat(),
            "coins_count": len([k for k in macro_base if k != "_meta"]),
        }

        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        print(f"✅ [SWING HUNTER] Сбор завершен! Зоны сохранены: {MACRO_LEVELS_FILE}")
        _fetch_stats_report(total_sec=time.time() - scan_started_at)

        # История уровней — отдельно от живого macro_levels.json (см.
        # modules/cryptano/levels_history.py). Нужна реплею/рескану, чтобы
        # честно проверять прошлые свечи против уровней, актуальных В ТОТ
        # МОМЕНТ, а не против сегодняшних.
        try:
            save_levels_snapshot(macro_base)
        except Exception as e:
            print(f"⚠️ [SWING HUNTER] Не удалось сохранить снимок в историю уровней: {e}")

        return macro_base

    except Exception as e:
        print(f"❌ [SWING HUNTER] КРИТИЧЕСКАЯ ОШИБКА: {e}")
        _fetch_stats_report(total_sec=time.time() - scan_started_at)
        return {}

def build_levels_for_single_coin(coin):
    """Быстрая генерация уровней для одной монеты (для авто-добавлений)."""
    try:
        # Резолвим реальный символ на бирже (спот/футы + известные алиасы
        # тикеров типа TON->TONCOIN) — не все монеты торгуются как SPOT
        # COIN/USDT, часть только как COIN/USDT:USDT (перпы), либо не
        # торгуются на Bybit вообще (например токенизированные акции).
        if not exchange.markets:
            exchange.load_markets(reload=False)

        markets = exchange.markets or {}
        symbol = resolve_symbol(coin, markets)

        if not symbol:
            print(f"[HUNTER SKIP] {coin}: нет рынка ни SPOT, ни FUTURES на Bybit — пропускаю.")
            return None

        ohlcv_1M = _safe_fetch_ohlcv(symbol, "1M", 60)
        ohlcv_1W = _safe_fetch_ohlcv(symbol, "1W", 150)
        ohlcv_1d = _safe_fetch_ohlcv(symbol, "1d", 365)
        ohlcv_4h = _safe_fetch_ohlcv(symbol, "4h", 200)

        if len(ohlcv_1d) < 50:
            return None

        df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1M) >= 5 else None
        df_1W = pd.DataFrame(ohlcv_1W, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1W) >= 5 else None
        df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_4h) >= 50 else None

        levels = build_levels(df_1M, df_1W, df_1d, df_4h, coin)

        macro_base = load_json(MACRO_LEVELS_FILE, default={})
        scan_time = datetime.datetime.now().isoformat()
        reconciled = _reconcile_coin_levels(coin, levels, macro_base, df_1d, scan_time=scan_time)
        macro_base[coin] = {
            "supports": reconciled["supports"],
            "resistances": reconciled["resistances"],
            "updated_at": scan_time
        }
        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        try:
            save_levels_snapshot(macro_base)
        except Exception as e:
            print(f"⚠️ [SWING HUNTER] Не удалось сохранить снимок в историю уровней: {e}")

        return levels
    except Exception as e:
        print(f"[HUNTER ERROR] Ошибка при построении уровней для {coin}: {e}")
        return None

def run_heavy_generator(bot, admin_chat_id):
    """Фоновый поток для генерации уровней по расписанию."""
    schedule.every().day.at(TIME_ASIAN_CLOSE).do(build_macro_levels, bot, admin_chat_id)
    schedule.every().day.at(TIME_US_OPEN).do(build_macro_levels, bot, admin_chat_id)
    while True:
        schedule.run_pending()
        time.sleep(1)

def start_swing_hunter(bot, admin_chat_id):
    """Инициализация Swing Hunter: запускает фоновые потоки, не блокируя старт бота."""
    threading.Thread(target=run_heavy_generator, args=(bot, admin_chat_id), daemon=True).start()
    # minute_radar удалён — второй, независимый писатель в watchlist.json
    # (наравне с синхронизацией в background_tasks.py), не нужен: свою роль
    # ("добавить монету в watchlist") с запасом перекрывает синхронизация в
    # background_tasks.py (добавляет ВСЕ монеты из macro_levels.json, без
    # условия "уже коснулась зоны"), а "ушла в работу" и так проверяется
    # внутри самого 15-минутного скана, отдельно от watchlist.
    print("[SWING HUNTER] Инициализирован (heavy_generator запущен в фоне)")

# ТОЧКА ВХОДА ДЛЯ ПРЯМОГО ЗАПУСКА
if __name__ == "__main__":
    print("🚀 [SWING HUNTER] Начинаем принудительный сбор уровней...")
    build_macro_levels()
    print("🏁 [SWING HUNTER] Сбор завершен.")