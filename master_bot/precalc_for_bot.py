"""
precalc_for_bot.py
===================
Массовый пересчёт исторических уровней ДЛЯ БОЕВОГО БОТА — тот же принцип,
что test/precalc.py у тестера, только результат кладётся прямо в
modules/cryptano/database/levels_timeline_YYYY_MM.json — ровно туда и в
том же формате, откуда его читает levels_history.py::get_levels_snapshot()
(см. докстринг levels_history.py: "уже посчитанные тестером файлы можно
класть в database/ как есть — этот модуль читает их как свои").

Значит: bounce_simulate.py (боевой симулятор) и watcher_plan.py::check_bounce
(рескан) сразу начнут пользоваться СВЕЖЕ пересчитанной историей — с
паспортом (см. modules/cryptano/swing_hunter.py::_reconcile_coin_levels) и
с фиксом "самая старая дата побеждает при слиянии" (levels_builder.py) —
вместо старых снимков, накопленных ДО этих правок.

Ничего не дублирует: build_levels берётся из уже пропатченного
modules/cryptano/utils/levels_builder.py, паспорт — из уже пропатченного
modules/cryptano/swing_hunter.py. Этот файл только качает историю и гоняет
даты, как test/precalc.py.

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

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.levels_builder import build_levels
from modules.cryptano.utils.paths import DATABASE_DIR
# get_top_symbols/MIN_VOLUME_USD/MAX_COINS — ОДНА точка правды, из swing_hunter.py
# (тот же порог/потолок, что и в бою, а не отдельная копия, которая рано
# или поздно расходится молча — ровно так родился баг с RAYDIUM: правили
# порог в одном месте, он не подхватывался в другом).
from modules.cryptano.swing_hunter import (
    _reconcile_coin_levels,  # тот же паспорт, что уже стоит в бою
    get_top_symbols, MIN_VOLUME_USD, MAX_COINS,
)

# --- ДИАПАЗОН ДАТ ДЛЯ ПЕРЕСЧЁТА ---
# Можно указать несколько периодов подряд, как в test/precalc.py.
MONTHS_TO_CALC = [
    {"start": "2026-08-01", "end": "2026-08-31"},
    {"start": "2026-09-01", "end": "2026-09-19"},
]


def _safe_fetch_ohlcv(symbol, tf, lim):
    for attempt in range(5):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe=tf, limit=lim)
        except Exception as e:
            if "10006" in str(e) or "Rate Limit" in str(e) or "Too many visits" in str(e):
                print(f"⚠️ Rate limit, жду 4 сек (попытка {attempt+1}/5)...")
                time.sleep(4.0)
            else:
                raise e
    raise Exception("Биржа заблокировала запросы по Rate Limit после 5 попыток.")


def build_full_history_cache(valid_symbols, extra_candles=1500):
    """Качает всю нужную историю ОДИН раз — дальше все даты режутся из неё
    локально, без сети под каждую дату (тот же приём, что в test/swing_hunter.py)."""
    print(f"📥 Качаю историю по {len(valid_symbols)} монетам...")
    cache = {}
    for symbol in valid_symbols:
        time.sleep(0.3)
        coin = symbol.split("/")[0].replace(":USDT", "")
        try:
            ohlcv_1M = _safe_fetch_ohlcv(symbol, "1M", 60)
            ohlcv_1W = _safe_fetch_ohlcv(symbol, "1W", 150)
            ohlcv_1d = _safe_fetch_ohlcv(symbol, "1d", max(365, extra_candles // 4))
            ohlcv_4h = _safe_fetch_ohlcv(symbol, "4h", extra_candles)

            cols = ["timestamp", "open", "high", "low", "close", "volume"]
            cache[coin] = {
                "1M": pd.DataFrame(ohlcv_1M, columns=cols) if len(ohlcv_1M) >= 5 else None,
                "1W": pd.DataFrame(ohlcv_1W, columns=cols) if len(ohlcv_1W) >= 5 else None,
                "1d": pd.DataFrame(ohlcv_1d, columns=cols) if len(ohlcv_1d) >= 50 else None,
                "4h": pd.DataFrame(ohlcv_4h, columns=cols) if len(ohlcv_4h) >= 50 else None,
            }
        except Exception as e:
            print(f"[CACHE ERROR] {coin}: {e}")
            continue
    print(f"✅ Кэш готов: {len(cache)} монет.")
    return cache


def _slice_by_time(df, target_ts_ms, tail_n):
    if df is None:
        return None
    sliced = df[df['timestamp'] <= target_ts_ms]
    if sliced.empty:
        return None
    return sliced.tail(tail_n).reset_index(drop=True)


def build_snapshot_for_date(target_time_str, cache, old_macro_base):
    """Один снимок на одну дату — с паспортом (сверка с old_macro_base),
    той же функцией, что уже используется в бою (_reconcile_coin_levels)."""
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
                if old_macro_base is not None:
                    reconciled = _reconcile_coin_levels(coin, levels, old_macro_base, df_1d)
                    macro_base[coin] = {
                        "supports": reconciled["supports"],
                        "resistances": reconciled["resistances"],
                        "updated_at": datetime.datetime.now().isoformat(),
                    }
                else:
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

    # Если файл этого месяца уже существует в database/ (например, частично
    # накоплен боевым swing_hunter'ом) — начинаем паспорт с него, а не с
    # пустого места, чтобы не потерять уже правильно зафиксированные даты.
    existing_timeline = {}
    if os.path.exists(output_path):
        try:
            with open(output_path, 'r') as f:
                existing_timeline = json.load(f)
            print(f"ℹ️ Нашёл существующий {output_path} — начинаю паспорт с него.")
        except Exception as e:
            print(f"⚠️ Не смог прочитать существующий файл ({e}), начинаю с чистого листа.")

    timeline = dict(existing_timeline)
    running_macro_base = None
    if existing_timeline:
        last_key = sorted(existing_timeline.keys())[-1]
        running_macro_base = existing_timeline[last_key]

    for dt in dates:
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        print(f"⏳ Сбор уровней на момент: {time_str}")
        levels_dict = build_snapshot_for_date(time_str, cache, running_macro_base)
        # СЛИЯНИЕ, не замена: valid_symbols фиксируется ОДИН раз на весь прогон
        # (по объёму ПРЯМО СЕЙЧАС) и одинаков для всех дат обоих месяцев —
        # монета, которая сегодня не прошла порог объёма (например, RAYDIUM
        # на границе $10M), просто не попадает в cache и, соответственно, в
        # levels_dict ни на одну дату. Раньше `timeline[time_str] = levels_dict`
        # ЗАМЕНЯЛО весь снимок на эту дату целиком — и стирало уже посчитанные
        # ранее (в прошлом прогоне, когда монета проходила порог) уровни для
        # монет, которых нет в ЭТОМ прогоне. Теперь — сохраняем старую запись
        # для тех монет, кого сегодня не пересчитывали, и обновляем только тех,
        # кого реально пересчитали.
        old_snapshot = timeline.get(time_str, {})
        merged_snapshot = dict(old_snapshot)
        merged_snapshot.update(levels_dict)
        timeline[time_str] = merged_snapshot
        running_macro_base = merged_snapshot

    with open(output_path, 'w') as f:
        json.dump(timeline, f, indent=2)

    print(f"\n✅ МЕСЯЦ {month_label} СОХРАНЁН В: {output_path}\n")


if __name__ == "__main__":
    print("🚀 СТАРТ ПЕРЕСЧЁТА ИСТОРИИ УРОВНЕЙ ДЛЯ БОЕВОГО БОТА...")

    valid_symbols = get_top_symbols()

    earliest_start = min(pd.to_datetime(p["start"]) for p in MONTHS_TO_CALC)
    days_span = max((pd.Timestamp.now() - earliest_start).days, 0)
    extra_candles = max(1500, (days_span + 40) * 6)

    cache = build_full_history_cache(valid_symbols, extra_candles=extra_candles)

    for period in MONTHS_TO_CALC:
        print("==============================================")
        print(f"📅 ОБРАБОТКА ПЕРИОДА: {period['start']} -> {period['end']}")
        print("==============================================")
        build_timeline_for_month(period['start'], period['end'], cache)

    print("🎉 ГОТОВО! bounce_simulate.py и рескан теперь увидят пересчитанную историю.")