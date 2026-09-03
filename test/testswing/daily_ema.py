"""
daily_ema.py
============
Отдельный, самостоятельный расчёт дневной EMA + недельного наклона —
НАМЕРЕННО отдельно от уровней (levels_builder/swing_hunter/precalc).

Почему отдельно, а не внутри build_macro_levels:
Уровни — дорогая по времени операция (1M/1W/1d/4h, вся логика levels_builder).
EMA — просто одно число на дневных свечах. Если захочешь поменять период EMA
или окно наклона — незачем пересчитывать уровни заново. Меняешь константы
внизу, перезапускаешь ЭТОТ файл — он лёгкий и быстрый (качает только "1d",
никакой другой таймфрейм ему не нужен).

Результат — тот же принцип файла, что levels_timeline_*.json у precalc.py,
только отдельно: ema_timeline_<месяц>.json вида
{
  "2026-07-01 00:00:00": {
    "XRP": {"ema_daily": 1.0234, "ema_slope_pct": -3.12},
    ...
  },
  ...
}

test_simulator.py читает его точно так же, как читает GLOBAL_TIMELINE —
отдельным словарём, по своему собственному имени файла.

Запуск: правите CONFIG ниже, затем python daily_ema.py
"""

import os
import sys
import time
import datetime
import json

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

MASTER_BOT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../master_bot"))
if MASTER_BOT_ROOT not in sys.path:
    sys.path.insert(0, MASTER_BOT_ROOT)
TEST_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../"))
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

import pandas as pd
from modules.cryptano.utils.crypto_utils import exchange
# Специально НЕ импортируем из swing_hunter.py — этот файл должен работать
# полностью самостоятельно, независимо от того, применены ли там какие-то
# другие правки или нет. Своя маленькая копия логики топ-70 ниже.

MIN_VOLUME_USD = 10_000_000  # тот же порог, что и в swing_hunter.py

# =====================================================================
# НАСТРОЙКИ — меняете тут, перезапускаете файл, уровни не трогаются
# =====================================================================
CONFIG = {
    'COINS': None,                        # None = взять топ-70 через get_top_symbols(); либо свой список ['XRP', 'DOGE', ...]
    'MONTHS_TO_CALC': [
        {"start": "2026-06-01", "end": "2026-07-30"},
        {"start": "2026-07-01", "end": "2026-07-31"},
        {"start": "2026-08-01", "end": "2026-08-31"},
    ],
    'STEP_HOURS': 12,                     # тот же ритм, что у пересчёта уровней
    'DAILY_EMA_PERIOD': 50,               # среднесрочный фильтр направления, ≈2.5 месяца
    'DAILY_EMA_SLOPE_LOOKBACK': 7,        # неделя дневных баров — % изменения EMA за неделю
    'HISTORY_DAYS_BACK': 400,             # сколько дней истории качать назад (запас на самую раннюю дату + сам период EMA)
}


def get_top_symbols():
    """Своя копия — та же логика, что в swing_hunter.py, но независимая
    (чтобы этот файл не требовал никаких других правок в проекте)."""
    if not exchange.markets:
        exchange.load_markets(reload=True)
    else:
        exchange.load_markets(reload=False)
    tickers = exchange.fetch_tickers()

    symbols_with_volume = []
    for sym, tick in tickers.items():
        if sym.endswith('/USDT') or sym.endswith(':USDT'):
            vol = float(tick.get('quoteVolume') or 0)
            if vol >= MIN_VOLUME_USD:
                symbols_with_volume.append((sym, vol))

    symbols_with_volume.sort(key=lambda x: x[1], reverse=True)
    valid_symbols = [sym for sym, vol in symbols_with_volume[:70]]
    print(f"🔥 Найдено {len(symbols_with_volume)} монет. Фильтруем до ТОП-70 самых ликвидных.")
    return valid_symbols


def _resolve_symbol(coin):
    if not exchange.markets:
        exchange.load_markets()
    markets = exchange.markets or {}
    symbol_perp = f"{coin.upper()}/USDT:USDT"
    symbol_spot = f"{coin.upper()}/USDT"
    return symbol_perp if symbol_perp in markets else symbol_spot


def _safe_fetch_ohlcv(sym, tf, lim):
    for attempt in range(5):
        try:
            return exchange.fetch_ohlcv(sym, timeframe=tf, limit=lim)
        except Exception as e:
            if "10006" in str(e) or "Rate Limit" in str(e) or "Too many visits" in str(e):
                print(f"   ⚠️ Rate limit — жду 4 сек (попытка {attempt + 1}/5)...")
                time.sleep(4.0)
            else:
                raise e
    raise Exception("Биржа заблокировала запросы после 5 попыток.")


def fetch_daily_cache(coins, days_back):
    """Качает ТОЛЬКО дневные свечи (не 1M/1W/4h — они тут не нужны вообще)
    с запасом days_back дней назад. Один раз на весь прогон, дальше всё
    считается в памяти."""
    print(f"📥 Качаю дневные свечи по {len(coins)} монетам (запас {days_back} дней)...")
    cache = {}
    skipped_short_history = []
    skipped_error = []
    for coin in coins:
        time.sleep(0.2)
        symbol = _resolve_symbol(coin)
        try:
            ohlcv = _safe_fetch_ohlcv(symbol, "1d", days_back)
            if len(ohlcv) < CONFIG['DAILY_EMA_PERIOD']:
                print(f"   [SKIP] {coin}: только {len(ohlcv)} дневных свечей на бирже "
                      f"(нужно минимум {CONFIG['DAILY_EMA_PERIOD']} — монета слишком молодая, "
                      f"EMA({CONFIG['DAILY_EMA_PERIOD']}) для неё физически не посчитать)")
                skipped_short_history.append(coin)
                continue
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            cache[coin] = df
        except Exception as e:
            print(f"   [ERROR] {coin}: {e}")
            skipped_error.append(coin)
            continue
    print(f"✅ Кэш готов: {len(cache)} из {len(coins)} монет.")
    if skipped_short_history:
        print(f"   ⚠️ Пропущено (не хватает истории): {', '.join(skipped_short_history)}")
    if skipped_error:
        print(f"   ⚠️ Пропущено (ошибка при скачивании): {', '.join(skipped_error)}")
    return cache


def calc_ema_at(df_1d, target_ts_ms):
    """EMA(DAILY_EMA_PERIOD) на дневных свечах ДО target_ts_ms включительно
    + недельный наклон в % (положительное = вверх, отрицательное = вниз,
    около нуля = плоско). (None, None), если данных не хватает."""
    period = CONFIG['DAILY_EMA_PERIOD']
    lookback = CONFIG['DAILY_EMA_SLOPE_LOOKBACK']

    sliced = df_1d[df_1d['timestamp'] <= target_ts_ms]
    if len(sliced) < period:
        return None, None

    ema_series = sliced['close'].ewm(span=period, adjust=False).mean()
    ema_val = float(ema_series.iloc[-1])

    if len(ema_series) > lookback and ema_series.iloc[-lookback] > 0:
        ema_week_ago = float(ema_series.iloc[-lookback])
        slope_pct = ((ema_val - ema_week_ago) / ema_week_ago) * 100
    else:
        slope_pct = 0.0

    return ema_val, slope_pct


def build_ema_timeline(start_date, end_date, cache):
    dates = pd.date_range(start=start_date, end=end_date, freq=f"{CONFIG['STEP_HOURS']}h")
    timeline = {}

    for dt in dates:
        target_ts_ms = int(dt.timestamp() * 1000)
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        entry = {}
        for coin, df_1d in cache.items():
            ema_val, slope_pct = calc_ema_at(df_1d, target_ts_ms)
            if ema_val is not None:
                entry[coin] = {"ema_daily": ema_val, "ema_slope_pct": slope_pct}
        timeline[time_str] = entry
        print(f"⏳ {time_str} — посчитано монет: {len(entry)}")

    return timeline


def _coins_from_levels_timeline(month_label):
    """Берём список монет НЕ из отдельного топ-70 по сегодняшнему объёму
    (это и была причина, почему BCH и другие монеты то есть, то нет — у
    levels_timeline и у отдельного вызова get_top_symbols() список монет
    мог не совпадать день ото дня), а прямо из уже готового
    levels_timeline_<месяц>.json — того же самого файла, который реально
    использует бот для уровней. Тогда покрытие монет ГАРАНТИРОВАННО
    совпадает 1-в-1, рассинхрон в принципе невозможен."""
    path = os.path.join(TEST_ROOT, f'levels_timeline_{month_label}.json')
    if not os.path.exists(path):
        print(f"⚠️ {path} не найден — не могу взять список монет оттуда, "
              f"откатываюсь на топ-70 по текущему объёму (риск рассинхрона).")
        return None
    with open(path, 'r', encoding='utf-8') as f:
        timeline = json.load(f)
    coins = set()
    for date_entry in timeline.values():
        coins.update(date_entry.keys())
    print(f"✅ Список монет взят из levels_timeline_{month_label}.json: {len(coins)} шт.")
    return sorted(coins)


if __name__ == "__main__":
    print("🚀 СТАРТ РАСЧЁТА ДНЕВНОЙ EMA...")

    for period in CONFIG['MONTHS_TO_CALC']:
        month_label = pd.to_datetime(period['start']).strftime("%Y_%m")

        if CONFIG['COINS']:
            coins = CONFIG['COINS']  # явно задан вручную — используем как есть
        else:
            coins = _coins_from_levels_timeline(month_label)
            if coins is None:
                coins = [s.split("/")[0].replace(":USDT", "") for s in get_top_symbols()]

        cache = fetch_daily_cache(coins, CONFIG['HISTORY_DAYS_BACK'])

        print(f"==============================================")
        print(f"📅 ПЕРИОД: {period['start']} -> {period['end']}")
        print(f"==============================================")
        timeline = build_ema_timeline(period['start'], period['end'], cache)

        filename = f"ema_timeline_{month_label}.json"
        output_path = os.path.join(TEST_ROOT, filename)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(timeline, f, indent=2)
        print(f"✅ СОХРАНЕНО: {output_path}")

    print("🎉 ГОТОВО.")