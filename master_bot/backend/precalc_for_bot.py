"""
precalc_for_bot.py
===================
Массовый пересчёт исторических уровней ДЛЯ СИМУЛЯТОРА — тот же принцип,
что test/precalc.py у тестера, только результат кладётся прямо в
modules/cryptano/database/levels_timeline_YYYY_MM.json — ровно туда и в
том же формате, откуда его читает levels_history.py::get_levels_snapshot()
(см. докстринг levels_history.py: "уже посчитанные тестером файлы можно
класть в database/ как есть — этот модуль читает их как свои").

⚠️ ТОЛЬКО ДЛЯ СИМУЛЯТОРА. Боевой бот (watcher_plan.py::check_bounce и
check_v_bottom/check_v_green_bottom/check_v_red_top) историю уровней НЕ
читает вообще — только свежий macro_levels.json/custom_levels.json/
greylist_levels.json напрямую (см. комментарий в swing_hunter.py::
build_macro_levels, где раньше стояла запись в эту же историю — убрана,
потому что бою она была не нужна, а держать её вредно: снимок пишется
раз в 12ч и НЕ обновляется до следующего окна, бой часами работал бы по
старым уровням). Значит: запускаешь этот файл САМ, когда хочешь свежую
историю для теста — бой её никогда не трогает и не зависит от неё.

ДВА СПИСКА МОНЕТ — переключаются константой COIN_SOURCE ниже, и ОБА теперь
ровно те же списки, что использует боевой бот (никакой самодеятельности
по объёму — это была моя ошибка в прошлой версии файла, см. ниже):
  "whitelist" — get_battle_symbols() из swing_hunter.py, ТА ЖЕ функция,
    которой бой строит macro_levels.json (whitelist.json + ручные зоны,
    минус blacklist) — то же самое число монет, что в счётчике на
    странице симулятора слева.
  "greylist"  — текущие кандидаты Потока Б (greylist_levels.json), те же,
    что видны в окне "🔭 Серый список" на дашборде.
  "both"      — оба списка разом, объединены (объединение, не дубли).

⚠️ Раньше "whitelist" здесь означал get_top_symbols() — совсем другой,
волатильный список (топ-N по СЕГОДНЯШНЕМУ объёму на бирже, вообще не
смотрит в whitelist.json). Из-за этого прогон "по whitelist" молча считал
историю для другого набора монет, чем реально в бою — отсюда расхождение
в счётчиках (боевых монет 74, а с историей после прогона — 61 и т.п.).
get_top_symbols() сама по себе никуда не делась (бой её по-прежнему
использует для Потока Б/радара), просто precalc её больше не путает с
"whitelist".

Месяц по умолчанию собирается ЗАНОВО и перезаписывает файл (MERGE_WITH_EXISTING = False) —
старые уровни из прежних прогонов в него не попадают. Слияние с существующим файлом включается
константой MERGE_WITH_EXISTING = True (нужно, только если гонять whitelist и greylist отдельными прогонами).

Ничего не дублирует: build_levels берётся из levels/levels_builder.py (новый метод).
ПАСПОРТА здесь НЕТ: границы зоны считаются по свече рождения уровня и между снимками не меняются
(см. levels_builder.py), поэтому каждый снимок — это просто свежий build_levels. Этот файл только
качает историю и гоняет даты, как test/precalc.py.

ВАЖНО: запускать из корня master_bot/ (python precalc_for_bot.py) — только
читает/качает данные и пишет в database/, ничего в bounce_state.json/
active_watchers.json/watchlist.json не трогает, живому боту не мешает
работать параллельно.
"""
import os
import sys
import time
import datetime
import pandas as pd
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from backend.modules.cryptano.utils.bybit import exchange, resolve_symbol, KNOWN_TICKER_ALIASES
from backend.modules.cryptano.utils.storage import load_json
from backend.modules.cryptano.levels.levels_builder import build_levels
from backend.modules.cryptano.utils.paths import DATABASE_DIR
# get_battle_symbols — ОДНА точка правды, из swing_hunter.py: та же функция,
# которой бой строит macro_levels.json. "whitelist" в этом файле теперь
# означает ровно то же самое, что и в бою — не отдельная копия логики,
# которая рано или поздно расходится молча (ровно так родился баг с
# RAYDIUM: правили порог в одном месте, он не подхватывался в другом).
# GREYLIST_LEVELS_FILE — те же ключи (тикеры), что видны в "🔭 Серый список".
from backend.modules.cryptano.levels.swing_hunter import (
    _reconcile_coin_levels,  # в precalc НЕ используется; оставлен, чтобы работали levels_ab.py / passport_ab.py
    _safe_fetch_ohlcv,  # тот же "сначала база, потом биржа", что уже стоит в бою
    get_battle_symbols,
    GREYLIST_LEVELS_FILE,
)

# --- КАКОЙ СПИСОК МОНЕТ СЧИТАТЬ ---
# "whitelist" | "greylist" | "both"
COIN_SOURCE = "whitelist"

# Сколько монет параллелить на фазе "сборка кэша" (build_full_history_cache,
# фаза 2/2) — это уже чистое чтение локальной базы, без сети, поэтому можно
# смело гонять в несколько потоков. Фаза 1 (докачка недостающего с биржи)
# потоки НЕ использует — там по-прежнему строго по одной монете с паузой,
# чтобы не словить лок/рейт-лимит биржи.
CACHE_BUILD_THREADS = 4

# --- ДИАПАЗОН ДАТ ДЛЯ ПЕРЕСЧЁТА ---
# Можно указать несколько периодов подряд, как в test/precalc.py.
MONTHS_TO_CALC = [
    {"start": "2026-09-01", "end": "2026-09-30"},
]


def _greylist_symbols():
    """Текущие кандидаты Потока Б (ключи greylist_levels.json), переведённые
    в биржевые символы — та же монета может быть в списке только один раз
    (сюда же попадают тикеры уже из whitelist, если COIN_SOURCE="both" —
    дубли убираются в get_valid_symbols())."""
    if not exchange.markets:
        exchange.load_markets(reload=False)
    markets = exchange.markets or {}

    greylist_levels = load_json(GREYLIST_LEVELS_FILE, default={})
    if not isinstance(greylist_levels, dict):
        greylist_levels = {}

    symbols = []
    for coin in greylist_levels.keys():
        if coin == "_meta":
            continue
        coin = str(coin).upper().strip()
        symbol = resolve_symbol(coin, markets) or resolve_symbol(KNOWN_TICKER_ALIASES.get(coin, ""), markets)
        if symbol:
            symbols.append(symbol)
    return symbols


def get_valid_symbols():
    """Список символов для пересчёта — по COIN_SOURCE выше. whitelist —
    get_battle_symbols(), ТА ЖЕ функция, что строит боевой macro_levels.json
    (whitelist.json + ручные зоны, минус blacklist) — те же монеты, что в
    счётчике на странице симулятора слева, один в один, без расхождений.
    greylist — текущие кандидаты Потока Б. both — объединение, без дублей
    (один и тот же символ мог попасть в оба списка сразу)."""
    if COIN_SOURCE == "whitelist":
        symbols = get_battle_symbols()
    elif COIN_SOURCE == "greylist":
        symbols = _greylist_symbols()
    elif COIN_SOURCE == "both":
        seen = set()
        symbols = []
        for symbol in list(get_battle_symbols()) + list(_greylist_symbols()):
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    else:
        raise ValueError(f"Неизвестный COIN_SOURCE={COIN_SOURCE!r} — ожидается 'whitelist'/'greylist'/'both'")

    print(f"📋 COIN_SOURCE={COIN_SOURCE!r} -> {len(symbols)} символов.")
    return symbols


def _ensure_fresh(valid_symbols, extra_candles):
    """ФАЗА 1/2 — докачка. Идём по монетам СТРОГО ПО ОДНОЙ, с паузой, как
    было всегда — здесь биржа ещё в игре, ей нужна очередь, а не толпа
    запросов разом (она и так иногда лочит на такое).

    Результат самих свечей тут не нужен — просто дёргаем _safe_fetch_ohlcv
    по всем 4 тф для монеты, он сам решает, брать из базы или докачивать
    (хвост/полностью). Побочный эффект — после этого прохода локальная
    база candle_store гарантированно свежая по всем монетам (кто вообще
    смог докачаться). Дальше в дело вступает фаза 2, которая уже с биржей
    не общается вообще."""
    total = len(valid_symbols)
    print(f"📡 Фаза 1/2: проверяю/докачиваю базу по {total} монетам (по одной, с паузой — бережём биржу)...")
    for i, symbol in enumerate(valid_symbols, 1):
        time.sleep(0.3)
        try:
            _safe_fetch_ohlcv(symbol, "1M", 60)
            _safe_fetch_ohlcv(symbol, "1W", 150)
            _safe_fetch_ohlcv(symbol, "1d", max(365, extra_candles // 4))
            _safe_fetch_ohlcv(symbol, "4h", extra_candles)
        except Exception as e:
            print(f"[FRESH ERROR] {symbol}: {e}")
        if i % 10 == 0 or i == total:
            print(f"   …{i}/{total}")
    print("✅ Фаза 1 готова — база свежая по всем монетам (кто докачался).")


def _build_cache_entry(symbol, extra_candles):
    """ФАЗА 2/2, одна монета. База уже свежая (см. _ensure_fresh выше),
    поэтому _safe_fetch_ohlcv тут читает чисто из локальной базы, сеть не
    трогает вообще — можно смело гонять параллельно в несколько потоков
    (см. CACHE_BUILD_THREADS)."""
    coin = symbol.split("/")[0].replace(":USDT", "")
    ohlcv_1M = _safe_fetch_ohlcv(symbol, "1M", 60)
    ohlcv_1W = _safe_fetch_ohlcv(symbol, "1W", 150)
    ohlcv_1d = _safe_fetch_ohlcv(symbol, "1d", max(365, extra_candles // 4))
    ohlcv_4h = _safe_fetch_ohlcv(symbol, "4h", extra_candles)

    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    entry = {
        "1M": pd.DataFrame(ohlcv_1M, columns=cols) if len(ohlcv_1M) >= 5 else None,
        "1W": pd.DataFrame(ohlcv_1W, columns=cols) if len(ohlcv_1W) >= 5 else None,
        "1d": pd.DataFrame(ohlcv_1d, columns=cols) if len(ohlcv_1d) >= 50 else None,
        "4h": pd.DataFrame(ohlcv_4h, columns=cols) if len(ohlcv_4h) >= 50 else None,
    }
    return coin, entry


def build_full_history_cache(valid_symbols, extra_candles=1500):
    """Качает всю нужную историю ОДИН раз — дальше все даты режутся из неё
    локально, без сети под каждую дату. Раньше это был один проход, где
    докачка и чтение базы были перемешаны по одной монете за раз — теперь
    чётко два прохода:
      1) _ensure_fresh — докачка недостающего/устаревшего, последовательно,
         с паузой (бирже нужна очередь);
      2) сборка кэша — база уже гарантированно свежая, читаем её в
         CACHE_BUILD_THREADS потоков (чистый локальный диск, сети нет)."""
    _ensure_fresh(valid_symbols, extra_candles)

    total = len(valid_symbols)
    print(f"📥 Фаза 2/2: собираю кэш из базы в {CACHE_BUILD_THREADS} поток(а/ов)...")
    cache = {}
    done = 0
    with ThreadPoolExecutor(max_workers=CACHE_BUILD_THREADS) as pool:
        futures = {pool.submit(_build_cache_entry, symbol, extra_candles): symbol for symbol in valid_symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            done += 1
            try:
                coin, entry = future.result()
                cache[coin] = entry
            except Exception as e:
                print(f"[CACHE ERROR] {symbol}: {e}")
            if done % 10 == 0 or done == total:
                print(f"   …{done}/{total}")
    print(f"✅ Кэш готов: {len(cache)} монет.")
    return cache


def _slice_by_time(df, target_ts_ms, tail_n):
    if df is None:
        return None
    sliced = df[df['timestamp'] <= target_ts_ms]
    if sliced.empty:
        return None
    return sliced.tail(tail_n).reset_index(drop=True)


# True  = дописывать в уже существующий levels_timeline_YYYY_MM.json (старые монеты/даты остаются).
# False = месяц считается заново и файл перезаписывается целиком (по умолчанию).
MERGE_WITH_EXISTING = False


def build_snapshot_for_date(target_time_str, cache):
    """Один снимок на одну дату: свежий build_levels по каждой монете, без паспорта."""
    dt_obj = datetime.datetime.strptime(target_time_str, "%Y-%m-%d %H:%M:%S")
    target_ts_ms = int(dt_obj.timestamp() * 1000)

    macro_base = {}
    for coin, tf_data in cache.items():
        try:
            df_1M = _slice_by_time(tf_data["1M"], target_ts_ms, 60)
            df_1W = _slice_by_time(tf_data["1W"], target_ts_ms, 150)
            df_1d = _slice_by_time(tf_data["1d"], target_ts_ms, 365)
            df_4h = _slice_by_time(tf_data["4h"], target_ts_ms, 200)

            if df_1d is None or len(df_1d) < 50:
                continue

            levels = build_levels(df_1M, df_1W, df_1d, df_4h, coin)
            has_any = (levels["supports"] or levels["resistances"])
            if has_any:
                macro_base[coin] = {
                    "supports": levels["supports"],
                    "resistances": levels["resistances"],
                    "updated_at": datetime.datetime.now().isoformat(),
                }
        except Exception as e:
            import traceback
            print(f"[PRECALC ERROR] {coin} на {target_time_str}: {e}")
            traceback.print_exc()
            continue

    return macro_base


def build_timeline_for_month(start_date, end_date, cache):
    month_label = pd.to_datetime(start_date).strftime("%Y_%m")
    dates = pd.date_range(start=start_date, end=end_date, freq='12h')

    output_path = os.path.join(DATABASE_DIR, f"levels_timeline_{month_label}.json")
    os.makedirs(DATABASE_DIR, exist_ok=True)

    # По умолчанию месяц считается с нуля и перезаписывает файл (MERGE_WITH_EXISTING = False).
    existing_timeline = {}
    if MERGE_WITH_EXISTING and os.path.exists(output_path):
        try:
            with open(output_path, 'r') as f:
                existing_timeline = json.load(f)
            print(f"ℹ️ Нашёл существующий {output_path} — дописываю в него.")
        except Exception as e:
            print(f"⚠️ Не смог прочитать существующий файл ({e}), начинаю с чистого листа.")

    timeline = dict(existing_timeline)

    for dt in dates:
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        print(f"⏳ Сбор уровней на момент: {time_str}")
        levels_dict = build_snapshot_for_date(time_str, cache)
        # СЛИЯНИЕ, не замена: valid_symbols фиксируется ОДИН раз на весь прогон
        # (по объёму ПРЯМО СЕЙЧАС, для выбранного COIN_SOURCE) и одинаков для
        # всех дат обоих месяцев — монета, которая сегодня не прошла отбор
        # (например, выпала из топа по объёму или из greylist), просто не
        # попадает в cache и, соответственно, в levels_dict ни на одну дату.
        # Раньше `timeline[time_str] = levels_dict` ЗАМЕНЯЛО весь снимок на
        # эту дату целиком — и стирало уже посчитанные ранее (в прошлом
        # прогоне, когда монета проходила порог) уровни для монет, которых
        # нет в ЭТОМ прогоне. Теперь — сохраняем старую запись для тех монет,
        # кого сегодня не пересчитывали, и обновляем только тех, кого реально
        # пересчитали. Это же слияние позволяет спокойно гонять сначала
        # COIN_SOURCE="whitelist", потом отдельным прогоном "greylist" — оба
        # результата уживаются в одном файле, не перетирая друг друга.
        old_snapshot = timeline.get(time_str, {})
        merged_snapshot = dict(old_snapshot)
        merged_snapshot.update(levels_dict)
        timeline[time_str] = merged_snapshot

    with open(output_path, 'w') as f:
        json.dump(timeline, f, indent=2)

    print(f"\n✅ МЕСЯЦ {month_label} СОХРАНЁН В: {output_path}\n")


if __name__ == "__main__":
    print(f"🚀 СТАРТ ПЕРЕСЧЁТА ИСТОРИИ УРОВНЕЙ ДЛЯ СИМУЛЯТОРА (COIN_SOURCE={COIN_SOURCE!r})...")

    valid_symbols = get_valid_symbols()

    earliest_start = min(pd.to_datetime(p["start"]) for p in MONTHS_TO_CALC)
    days_span = max((pd.Timestamp.now() - earliest_start).days, 0)
    extra_candles = max(1500, (days_span + 40) * 6)

    cache = build_full_history_cache(valid_symbols, extra_candles=extra_candles)

    for period in MONTHS_TO_CALC:
        print("==============================================")
        print(f"📅 ОБРАБОТКА ПЕРИОДА: {period['start']} -> {period['end']}")
        print("==============================================")
        build_timeline_for_month(period['start'], period['end'], cache)

    print("🎉 ГОТОВО! Симулятор (simulate_engine.py / bounce_simulate.py) увидит пересчитанную историю.")