# modules/cryptano/watcher_plan.py

import time
import os
import sys
import datetime
from modules.cryptano.utils.storage import load_json
import pandas as pd
import gc
import traceback
from modules.cryptano.utils.common import calculate_rsi, exchange, format_price as fmt_p, price_precision_from_market, resolve_symbol, KNOWN_TICKER_ALIASES
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.indicators import pandas_get_local_structure, calculate_atr, calculate_ema
from modules.cryptano.strategy.vbottom_manager import VBottomManager
from modules.cryptano.strategy.bounce_manager import BounceManager
from modules.cryptano.strategy.bounce_parent import BounceParent
from modules.cryptano.utils.paths import MACRO_LEVELS_FILE, CUSTOM_LEVELS_FILE
from modules.cryptano.levels_history import get_levels_snapshot
from modules.cryptano.history import save_signal


def get_merged_levels_for_coin(coin):
    """Supports/resistances монеты — macro (swing_hunter) + ручные
    (custom_levels.json), слитые в один словарь. ОДНА точка правды для
    всех четырёх стратегий (check_v_bottom/check_v_green_bottom/
    check_v_red_top/check_bounce) — раньше каждая из них независимо делала
    те же 2 строки (load_json + .get с алиасом), custom_levels.json тогда
    пришлось бы добавлять в четырёх местах отдельно, с риском забыть одно
    (та же ловушка, что раньше была с MIN_VOLUME_USD/MAX_COINS в
    swing_hunter.py/precalc_for_bot.py — правили в одном месте, не
    подхватывалось в другом).

    Возвращает {} (не None), если по монете нет вообще ничего ни в одном
    источнике — вызывающий код как проверял `if not coin_macro:`, так и
    продолжает проверять, пустой словарь тоже falsy."""
    macro_db = load_json(MACRO_LEVELS_FILE, default={})
    coin_macro = macro_db.get(coin) or macro_db.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})

    custom_db = load_json(CUSTOM_LEVELS_FILE, default={})
    coin_custom = custom_db.get(coin) or custom_db.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})

    if not coin_custom:
        return coin_macro  # частый случай — без custom вообще ничего не копируем зря

    merged = dict(coin_macro) if coin_macro else {}
    merged["supports"] = list(coin_macro.get("supports", []) if coin_macro else []) + list(coin_custom.get("supports", []))
    merged["resistances"] = list(coin_macro.get("resistances", []) if coin_macro else []) + list(coin_custom.get("resistances", []))
    return merged

# candle_store.py лежит в web/backend/ и не является пакетом (нет __init__.py) —
# app.py подключает его тем же способом: добавляет свою папку в sys.path и
# делает обычный import. Повторяем тот же приём здесь, чтобы читать общую
# локальную базу свечей вместо похода на биржу на каждый скан.
# NOTE: статический анализатор (Pylance/Pyright) не умеет проследить импорт
# через динамически дополненный sys.path — отсюда "could not be resolved"
# в редакторе. Это не ошибка выполнения, реальный импорт по факту работает
# (candle_store.py гарантированно лежит по вычисленному пути), поэтому
# просто глушим предупреждение явно.
_CANDLE_STORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "web", "backend")
if _CANDLE_STORE_DIR not in sys.path:
    sys.path.insert(0, _CANDLE_STORE_DIR)
try:
    import candle_store  # type: ignore[import]
except Exception as e:
    candle_store = None
    print(f"[watcher_plan] ⚠️ candle_store недоступен, буду ходить на биржу напрямую: {e}")

SCAN_COINS_LIMIT = 150

# Сколько свечей просить из локальной базы дашборда — она хранит до ~60 дней
# на 15м (см. candle_store.BACKFILL_DAYS), берём с запасом, чтобы реплей мог
# найти реальный момент пробоя уровня, а не упираться в старое окно 120 свечей.
DEEP_LOOKBACK_LIMIT = 5760  # ~60 дней на 15м

# Потолок на реплей BOUNCE за один скан (см. check_bounce): раньше был
# урезан до ~200 свечей (2 дня) как защита от непредвиденного простоя, но
# рескан (см. bounce_mgr.last_processed_time + /api/rescan) — это НАМЕРЕННАЯ
# перемотка, часто дальше 2 дней (дефолт кнопки — неделя назад, можно и
# больше). С низким потолком рескан молча прыгал к последней свече вместо
# честного повтора всего пути. Поднимаем до глубины, которая и так уже
# качается (DEEP_LOOKBACK_LIMIT) — дальше реплеить всё равно нечем.
BOUNCE_REPLAY_MAX_CANDLES = DEEP_LOOKBACK_LIMIT


def _fetch_candles_df(symbol, coin, min_candles, strategy_tag):
    # type: (str, str, int, str) -> tuple[pd.DataFrame | None, str | None]
    """
    Тянет свечи для стратегии.
    1. Сперва — из локальной базы дашборда (candle_store.py). Она уже
       копится фоновым потоком в app.py, читать оттуда быстро и не тратит
       лимиты биржи, плюс там глубина до ~60 дней — этого достаточно, чтобы
       найти реальный момент пробоя уровня, а не только последние 30 часов.
    2. Если там пока пусто/мало (монета только что попала в вотчлист,
       фоновый поток дашборда ещё не успел докачать) — подстраховка: тянем
       последние 120 свечей напрямую с биржи, как раньше.

    Возвращает (df, error_msg) — df is None при ошибке, тогда error_msg заполнен.
    """
    candles = []
    if candle_store is not None:
        try:
            # ВАЖНО: дашборд (/api/ohlcv) на каждый запрос дотягивает хвост
            # свежих свечей через top_up_tail — иначе локальная база может
            # отставать от биржи, и вотчер будет сканить по устаревшей
            # "последней" свече, а точки на графике потом не совпадут с тем,
            # что реально видно в дашборде. Повторяем тот же приём здесь.
            if candle_store.has_data(symbol, "15m"):
                candle_store.top_up_tail(exchange, symbol, "15m")
            candles = candle_store.get_candles(symbol, "15m", limit=DEEP_LOOKBACK_LIMIT)
        except Exception as e:
            print(f"[{strategy_tag} WARNING] candle_store недоступен для {coin}: {e}")

    if len(candles) >= min_candles:
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.drop(columns=["time"]).set_index("timestamp")
        return df[["open", "high", "low", "close", "volume"]].astype(float), None

    # Подстраховка — локальной истории пока мало, идём напрямую на биржу
    ohlcv = None
    for attempt in range(3):
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=120)
            break
        except Exception as e:
            if "Rate Limit" in str(e) or "10006" in str(e):
                print(f"[{strategy_tag} WARNING] Bybit rate limit на {coin}. Пауза 1.5 сек...")
                time.sleep(1.5)
            else:
                raise e

    if not ohlcv or len(ohlcv) < min_candles:
        return None, f"⚠️ Недостаточно данных {strategy_tag} для {coin} ({len(ohlcv) if ohlcv else 0} свечей)."

    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]].astype(float), None


# --- Настройки origin-tracking для V_BOTTOM/V_GREEN_BOTTOM ---
# Должны совпадать с тем, что реально тестируется в test_simulator.py,
# иначе бой и симулятор снова разъедутся.
VBOTTOM_BREATH_BUFFER_PCT = 3.0  # см. test_simulator.py:VBOTTOM_BREATH_BUFFER_PCT
MIN_LEVEL_SCORE = 1.0            # см. test_simulator.py:MIN_LEVEL_SCORE


def _find_fresh_breach(levels, c_close, c_low, prev_close):
    """
    Ищет уровень, который цена только что пробила вниз — та же логика,
    что в test_simulator.py: сначала ищем "свежий" пробой (только что
    пересекли границу), если такого нет — берём любой уровень, под
    которым цена уже находится (fallback на случай гэпа/пропуска свечи).
    """
    for lvl in levels:
        if (c_close < lvl['min'] or c_low < lvl['min']) and prev_close >= lvl['min']:
            return lvl
    for lvl in levels:
        if c_close < lvl['min'] or c_low < lvl['min']:
            return lvl
    return None


def _find_fresh_breach_up(levels, c_close, c_high, prev_close):
    """
    Зеркало _find_fresh_breach для SHORT-стратегий от сопротивления:
    ищет уровень, который цена только что пробила ВВЕРХ (через lvl['max']).
    Сначала "свежий" пробой, если такого нет — fallback на любой уровень,
    над которым цена уже находится.
    """
    for lvl in levels:
        if (c_close > lvl['max'] or c_high > lvl['max']) and prev_close <= lvl['max']:
            return lvl
    for lvl in levels:
        if c_close > lvl['max'] or c_high > lvl['max']:
            return lvl
    return None


def check_v_bottom(coin, direction, vbottom_mgr=None, tracked_levels=None):
    """
    Проверяет V-BOTTOM паттерн — теперь по той же модели, что в симуляторе:
    отслеживаем ОДИН активный (пробитый) уровень за раз на монету, а не
    прогоняем все supports разом каждый скан. Как только уровень пробит —
    notify_breach(), дальше кормим свечами именно его, пока либо не
    сработает сигнал, либо цена не уйдёт выше буфера (force_reset_watcher).

    tracked_levels — персистентный словарь {f"{coin}_LONG": level_dict},
    должен жить между вызовами (передаётся из live_scan.py).

    Возвращает (is_ready, report_text, levels_checked).
    """
    # SHORT для V_BOTTOM не реализован в самом вотчере (всегда return None
    # внутри update()) — не тратим лишний поход на биржу впустую.
    if direction != "LONG":
        return False, None, 0

    if tracked_levels is None:
        tracked_levels = {}

    try:
        time.sleep(0.3)

        coin = coin.upper().replace("USDT", "").replace("/", "").strip()

        # Резолвим символ на бирже (учитывает алиасы тикеров типа TON/GRAM)
        markets = load_markets_cached(exchange)
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            return False, f"❌ Монета *{coin}* не найдена на Bybit.", 0

        # Тянем 15m-свечи (сначала из локальной базы дашборда, глубже и быстрее,
        # см. _fetch_candles_df; подстраховка на биржу — только если базы ещё нет)
        df, fetch_err = _fetch_candles_df(symbol, coin, 52, "V_BOTTOM")
        if df is None:
            return False, fetch_err, 0

        # Загружаем макро-уровни
        coin_macro = get_merged_levels_for_coin(coin)  # macro + custom (см. get_merged_levels_for_coin)

        if not coin_macro:
            return False, f"⚠️ Нет уровней для {coin} (ни macro, ни custom).", 0

        if vbottom_mgr is None:
            vbottom_mgr = VBottomManager()

        # Фильтр по score — как в симуляторе, слабые уровни вообще не рассматриваем
        supports = [s for s in coin_macro.get("supports", []) if s.get('score', 0) >= MIN_LEVEL_SCORE]
        resistances = coin_macro.get("resistances", [])

        if not supports:
            return False, f"⚠️ Нет поддержек для V-BOTTOM на {coin} (после фильтра score).", 0

        track_key = f"{coin}_LONG"
        c_close = float(df['close'].iloc[-1])
        c_low = float(df['low'].iloc[-1])
        prev_close = float(df['close'].iloc[-2]) if len(df) > 1 else c_close

        tracked = tracked_levels.get(track_key)

        if tracked is None:
            # Уровень пока не отслеживается — ищем свежий пробой вниз
            found = _find_fresh_breach(supports, c_close, c_low, prev_close)
            if found is None:
                return False, None, 0
            tracked = dict(found)
            tracked_levels[track_key] = tracked
            vbottom_mgr.notify_breach(tracked, 'LONG')
        else:
            # Уже отслеживаем — проверяем не ушла ли цена выше буфера отмены
            origin_max = tracked['max'] * (1 + VBOTTOM_BREATH_BUFFER_PCT / 100.0)
            if c_close > origin_max:
                vbottom_mgr.force_reset_watcher(tracked, 'LONG')
                del tracked_levels[track_key]
                return False, None, 0

        # Кормим свечой именно отслеживаемый уровень — не весь список
        result = vbottom_mgr.evaluate_v_bottom(tracked, df, "LONG", resistances, trend="UNKNOWN", c_atr=None, coin=coin)
        levels_checked = 1

        if not result.get('allow'):
            return False, None, levels_checked

        # Сигнал сработал — уровень отработал, снимаем со слежения
        del tracked_levels[track_key]

        entry_price = result.get('entry_price', 0.0)
        sl = result.get('sl', 0.0)
        tp = result.get('tp', 0.0)
        history_log = result.get('history_log', '')
        level_id = result.get('level_id', 'unknown')

        # save_signal() кладёт запись в signals.json (общий список "Результаты") —
        # раньше вотчерные входы туда вообще не попадали, только шли сообщением.
        save_signal({
            "type": "WATCHER_LONG",
            "coin": coin,
            "source": "V_BOTTOM",
            "price": entry_price,
            "take_profit": tp,
            "stop_loss": sl,
            "level_type": tracked.get("type"),
        })

        report = (
            f"🟢 *V-BOTTOM LONG* _{coin}_\n\n"
            f"Entry: `{entry_price:.8f}`\n"
            f"SL: `{sl:.8f}`\n"
            f"TP: `{tp:.8f}`\n"
            f"R/R: `{(tp-entry_price)/(entry_price-sl) if entry_price > sl else 0:.2f}`\n\n"
            f"📊 {history_log}\n"
            f"Level: `{level_id}`"
        )

        return True, report, levels_checked

    except Exception as e:
        print(f"\n[V_BOTTOM ERROR] ❌ ОШИБКА ПРИ ПРОВЕРКЕ V-BOTTOM ({coin}): {e}")
        traceback.print_exc()
        return False, f"❌ Ошибка V-BOTTOM анализа {coin}: {e}", 0

def check_v_green_bottom(coin, direction, vbottom_mgr=None, tracked_levels=None):
    """
    Проверяет V-GREEN-BOTTOM паттерн (лестница ям + режим кульминации)
    на 15-минутных свечах. Работает только для LONG. Та же модель
    origin-tracking, что и check_v_bottom — см. комментарий там.
    """
    if direction != "LONG":
        return False, None, 0

    if tracked_levels is None:
        tracked_levels = {}

    try:
        time.sleep(0.3)

        coin = coin.upper().replace("USDT", "").replace("/", "").strip()

        # Резолвим символ на бирже (учитывает алиасы тикеров типа TON/GRAM)
        markets = load_markets_cached(exchange)
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            return False, f"❌ Монета *{coin}* не найдена на Bybit.", 0

        # Тянем 15m-свечи (сначала из локальной базы дашборда, глубже и быстрее,
        # см. _fetch_candles_df; подстраховка на биржу — только если базы ещё нет)
        df, fetch_err = _fetch_candles_df(symbol, coin, 52, "V_GREEN_BOTTOM")
        if df is None:
            return False, fetch_err, 0

        # V-GREEN-BOTTOM реально использует ATR (в отличие от V-BOTTOM) — считаем по-настоящему
        atr_series = calculate_atr(df, 14)
        c_atr = float(atr_series.iloc[-1]) if not atr_series.empty and atr_series.iloc[-1] == atr_series.iloc[-1] else None

        # Загружаем макро-уровни
        coin_macro = get_merged_levels_for_coin(coin)  # macro + custom (см. get_merged_levels_for_coin)

        if not coin_macro:
            return False, f"⚠️ Нет уровней для {coin} (ни macro, ни custom).", 0

        if vbottom_mgr is None:
            vbottom_mgr = VBottomManager()

        # Фильтр по score — как в симуляторе
        supports = [s for s in coin_macro.get("supports", []) if s.get('score', 0) >= MIN_LEVEL_SCORE]
        resistances = coin_macro.get("resistances", [])

        if not supports:
            return False, f"⚠️ Нет поддержек для V-GREEN-BOTTOM на {coin} (после фильтра score).", 0

        track_key = f"{coin}_VGB_LONG"
        c_close = float(df['close'].iloc[-1])
        c_low = float(df['low'].iloc[-1])
        prev_close = float(df['close'].iloc[-2]) if len(df) > 1 else c_close

        tracked = tracked_levels.get(track_key)

        if tracked is None:
            found = _find_fresh_breach(supports, c_close, c_low, prev_close)
            if found is None:
                return False, None, 0
            tracked = dict(found)
            tracked_levels[track_key] = tracked
            vbottom_mgr.notify_breach(tracked, 'LONG')
        else:
            origin_max = tracked['max'] * (1 + VBOTTOM_BREATH_BUFFER_PCT / 100.0)
            if c_close > origin_max:
                vbottom_mgr.force_reset_watcher(tracked, 'LONG')
                del tracked_levels[track_key]
                return False, None, 0

        result = vbottom_mgr.evaluate_v_green_bottom(tracked, df, "LONG", resistances, trend="UNKNOWN", c_atr=c_atr, coin=coin)
        levels_checked = 1

        if not result.get('allow'):
            return False, None, levels_checked

        del tracked_levels[track_key]

        entry_price = result.get('entry_price', 0.0)
        sl = result.get('sl', 0.0)
        tp = result.get('tp', 0.0)
        history_log = result.get('history_log', '')
        level_id = result.get('level_id', 'unknown')

        save_signal({
            "type": "WATCHER_LONG",
            "coin": coin,
            "source": "V_GREEN_BOTTOM",
            "price": entry_price,
            "take_profit": tp,
            "stop_loss": sl,
            "level_type": tracked.get("type"),
        })

        report = (
            f"🟢 *V-GREEN-BOTTOM LONG* _{coin}_\n\n"
            f"Entry: `{entry_price:.8f}`\n"
            f"SL: `{sl:.8f}`\n"
            f"TP: `{tp:.8f}`\n"
            f"R/R: `{(tp-entry_price)/(entry_price-sl) if entry_price > sl else 0:.2f}`\n\n"
            f"📊 {history_log}\n"
            f"Level: `{level_id}`"
        )

        return True, report, levels_checked

    except Exception as e:
        print(f"\n[V_GREEN_BOTTOM ERROR] ❌ ОШИБКА ПРИ ПРОВЕРКЕ V-GREEN-BOTTOM ({coin}): {e}")
        traceback.print_exc()
        return False, f"❌ Ошибка V-GREEN-BOTTOM анализа {coin}: {e}", 0


def check_v_red_top(coin, direction, vbottom_mgr=None, tracked_levels=None):
    """
    Проверяет V-RED-TOP паттерн (шорт от сопротивления, "три индейца" +
    якорь/реакция/подтверждение). Работает только для SHORT.

    В отличие от check_v_bottom/check_v_green_bottom, ОДИН пробитый уровень
    может дать НЕСКОЛЬКО сигналов подряд (вотчер сам крутится обратно в
    WAIT_C1 после сделки, пока не упрётся в MAX_TRADES_PER_LEVEL) — поэтому
    уровень снимается со слежения только когда вотчер реально завершил
    работу (TRIGGERED/DEAD/IDLE), а не после первого же сигнала.

    tracked_levels — персистентный словарь {f"{coin}_VRT_SHORT": level_dict},
    должен жить между вызовами (передаётся из live_scan.py), отдельный от
    словаря V_BOTTOM/V_GREEN_BOTTOM.

    Возвращает (is_ready, report_text, levels_checked).
    """
    if direction != "SHORT":
        return False, None, 0

    if tracked_levels is None:
        tracked_levels = {}

    try:
        time.sleep(0.3)

        coin = coin.upper().replace("USDT", "").replace("/", "").strip()

        markets = load_markets_cached(exchange)
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            return False, f"❌ Монета *{coin}* не найдена на Bybit.", 0

        # Тянем 15m-свечи (сначала из локальной базы дашборда, глубже и быстрее,
        # см. _fetch_candles_df; подстраховка на биржу — только если базы ещё нет).
        # 100, а не 52 — ATR_slow (SMA100 от ATR) требует запаса истории.
        df, fetch_err = _fetch_candles_df(symbol, coin, 100, "V_RED_TOP")
        if df is None:
            return False, fetch_err, 0

        atr_series = calculate_atr(df, 14)
        c_atr = float(atr_series.iloc[-1]) if not atr_series.empty and atr_series.iloc[-1] == atr_series.iloc[-1] else None

        atr_slow_series = atr_series.rolling(window=100).mean()
        c_atr_slow = float(atr_slow_series.iloc[-1]) if not atr_slow_series.empty and atr_slow_series.iloc[-1] == atr_slow_series.iloc[-1] else c_atr

        ema_series = calculate_ema(df, period=50)
        c_ema = float(ema_series.iloc[-1]) if not ema_series.empty and ema_series.iloc[-1] == ema_series.iloc[-1] else None

        df_rsi = df.reset_index(drop=True)  # calculate_rsi не завязан на индекс, но не рискуем
        rsi_series = calculate_rsi(df_rsi)
        c_rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty and rsi_series.iloc[-1] == rsi_series.iloc[-1] else None

        coin_macro = get_merged_levels_for_coin(coin)  # macro + custom (см. get_merged_levels_for_coin)

        if not coin_macro:
            return False, f"⚠️ Нет уровней для {coin} (ни macro, ни custom).", 0

        if vbottom_mgr is None:
            vbottom_mgr = VBottomManager()

        resistances = [r for r in coin_macro.get("resistances", []) if r.get('score', 0) >= MIN_LEVEL_SCORE]
        supports = coin_macro.get("supports", [])

        if not resistances:
            return False, f"⚠️ Нет сопротивлений для V-RED-TOP на {coin} (после фильтра score).", 0

        track_key = f"{coin}_VRT_SHORT"
        c_close = float(df['close'].iloc[-1])
        c_high = float(df['high'].iloc[-1])
        prev_close = float(df['close'].iloc[-2]) if len(df) > 1 else c_close

        tracked = tracked_levels.get(track_key)

        if tracked is None:
            found = _find_fresh_breach_up(resistances, c_close, c_high, prev_close)
            if found is None:
                return False, None, 0
            tracked = dict(found)
            tracked_levels[track_key] = tracked
            vbottom_mgr.notify_breach(tracked, 'SHORT')

        result = vbottom_mgr.evaluate_v_red_top(
            tracked, df, "SHORT", supports, trend="UNKNOWN",
            c_atr=c_atr, c_atr_slow=c_atr_slow, c_ema=c_ema, c_rsi=c_rsi, coin=coin
        )
        levels_checked = 1

        # Уровень снимаем со слежения, только если вотчер реально завершился —
        # иначе следующий скан снова словит "fresh breach" и notify_breach()
        # дёрнет on_breach_start(), сбросив накопленные пики/сделки впустую.
        vrt_level_id = f"VRT_SHORT_{tracked['min']}_{tracked['max']}"
        watcher = vbottom_mgr._watchers.get(vrt_level_id)
        if watcher is not None and getattr(watcher, "state", None) in ("TRIGGERED", "DEAD", "IDLE"):
            del tracked_levels[track_key]

        if not result.get('allow'):
            return False, None, levels_checked

        entry_price = result.get('entry_price', 0.0)
        sl = result.get('sl', 0.0)
        tp = result.get('tp', 0.0)
        history_log = result.get('history_log', '')
        level_id = result.get('level_id', 'unknown')

        # "type": "SHORT_PUMP" -> save_signal() трактует именно как SHORT-запись
        save_signal({
            "type": "SHORT_PUMP",
            "coin": coin,
            "source": "V_RED_TOP",
            "price": entry_price,
            "take_profit": tp,
            "stop_loss": sl,
            "level_type": tracked.get("type"),
        })

        report = (
            f"🔴 *V-RED-TOP SHORT* _{coin}_\n\n"
            f"Entry: `{entry_price:.8f}`\n"
            f"SL: `{sl:.8f}`\n"
            f"TP: `{tp:.8f}`\n"
            f"R/R: `{(entry_price-tp)/(sl-entry_price) if sl > entry_price else 0:.2f}`\n\n"
            f"📊 {history_log}\n"
            f"Level: `{level_id}`"
        )

        return True, report, levels_checked

    except Exception as e:
        print(f"\n[V_RED_TOP ERROR] ❌ ОШИБКА ПРИ ПРОВЕРКЕ V-RED-TOP ({coin}): {e}")
        traceback.print_exc()
        return False, f"❌ Ошибка V-RED-TOP анализа {coin}: {e}", 0
    
def check_bounce(coin, allow_long, allow_short, bounce_mgr):
    """BOUNCE сам ведёт свои вотчеры внутри bounce_mgr — внешний
    tracked_levels не нужен, process_candle() получает полный список
    текущих уровней и сам решает, что уже отслеживается.

    РЕПЛЕЙ (см. bounce_mgr.last_processed_time, персистится в bounce_state.json):
    бот перезапускается много раз в день, и раньше сюда всегда шла только ОДНА
    последняя свеча (df.iloc[-1]) — все свечи, пропущенные за время простоя,
    вотчер вообще никогда не видел. Событие, реально случившееся во время
    простоя, при следующем скане приписывалось текущей (более поздней) свече —
    отсюда точки на графике "не на своей свече". Теперь вместо одной свечи
    прогоняем через process_candle ПО ОЧЕРЕДИ все свечи от последней
    обработанной до текущей — каждое событие ложится на ту свечу, где оно
    реально произошло, независимо от того, сколько раз бот успел упасть и
    подняться между сканами."""
    try:
        time.sleep(0.3)
        coin = coin.upper().replace("USDT", "").replace("/", "").strip()

        markets = load_markets_cached(exchange)
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            return 0, [], 0

        df, fetch_err = _fetch_candles_df(symbol, coin, 52, "BOUNCE")
        if df is None:
            return 0, [], 0

        coin_macro = get_merged_levels_for_coin(coin)  # macro + custom (см. get_merged_levels_for_coin)
        supports = coin_macro.get("supports", [])
        resistances = coin_macro.get("resistances", [])

        # Уровней сейчас может не быть (монета выпала из macro_levels.json —
        # например, ушла ниже порога объёма в swing_hunter). Это НЕ причина
        # переставать сканировать уже отслеживаемый вотчер — по правилам
        # проекта он остаётся "в работе", пока не случится сделка ИЛИ цена не
        # уйдёт далеко от зоны (см. RUNAWAY/climax hard-kill в bounce_watcher.py/
        # bounce_manager.py) — оба условия отслеживает сам вотчер, не список
        # текущих уровней. Поэтому только реально пустой случай (ни текущих
        # уровней, ни уже живого вотчера) даёт скипнуть монету целиком.
        has_active_watcher = any(
            getattr(w, "coin", None) == coin and getattr(w, "state", None) not in ("DEAD", "TRIGGERED")
            for w in bounce_mgr._watchers.values()
        )
        if not supports and not resistances and not has_active_watcher:
            return 0, [], 0

        atr_series = calculate_atr(df)

        last_t = bounce_mgr.last_processed_time.get(coin)
        if last_t is None:
            # Первый скан этой монеты с тех пор, как появилась персистентность
            # (или вообще первый скан монеты) — ведём себя как раньше: только
            # текущая свеча, не гоняем всю глубину истории задним числом.
            replay_df = df.iloc[[-1]]
        else:
            last_ts = pd.Timestamp(int(last_t), unit="s", tz="UTC")
            replay_df = df[df.index > last_ts]
            if replay_df.empty:
                # Новых свечей с прошлого раза не появилось — сканить нечего.
                return 0, [], len(supports) + len(resistances)
            if len(replay_df) > BOUNCE_REPLAY_MAX_CANDLES:
                # Аномально долгий простой — не реплеим каждую свечу отдельно,
                # догоняем сразу до текущей (см. константу выше).
                replay_df = replay_df.iloc[[-1]]

        reports = []
        total_orders = 0
        replay_started_at = time.time()
        # evaluate_bounce читает максимум последние 52 строки df_slice
        # (df['volume'].iloc[-52:-2] + df.iloc[-1]) — раньше тут был
        # df.loc[:ts] (ВСЯ история от начала до текущей точки), на поздних
        # свечах это тысячи строк, копируемых заново на каждой из ~1000+
        # свечей реплея — окно в 60 (с небольшим запасом) даёт тот же
        # результат без лишнего копирования.
        WINDOW = 60
        df_index = df.index
        for ts, row in replay_df.iterrows():
            pos = df_index.searchsorted(ts, side="right")  # позиция сразу ПОСЛЕ этой свечи
            df_slice = df.iloc[max(0, pos - WINDOW):pos]
            if len(df_slice) < 52:
                continue  # недостаточно истории ДО этой свечи для индикаторов — пропускаем честно

            c_high, c_low, c_close = float(row['high']), float(row['low']), float(row['close'])
            c_atr = float(atr_series.loc[ts]) if ts in atr_series.index else 0.0

            # Уровни НА МОМЕНТ ЭТОЙ СВЕЧИ, не сегодняшние (см. levels_history.py) —
            # иначе прошлая свеча проверяется против зоны, которая тогда могла
            # ещё не появиться / уже пропасть, и "найденная" реплеем сделка на
            # самом деле в тот момент была бы невозможна. ts уже в UTC (tz-aware,
            # см. _fetch_candles_df/candle_store) — снимки хранятся наивным UTC,
            # поэтому tzinfo снимаем перед сравнением.
            ts_naive = ts.tz_localize(None) if ts.tzinfo is not None else ts
            hist_snapshot = get_levels_snapshot(ts_naive.to_pydatetime())
            if hist_snapshot is not None:
                hist_coin_macro = hist_snapshot.get(coin) or hist_snapshot.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})
                candle_supports = hist_coin_macro.get("supports", [])
                candle_resistances = hist_coin_macro.get("resistances", [])
            else:
                # Снимка на этот момент нет вообще (ни у бота, ни подложенного
                # вручную) — честно НЕ подставляем сегодняшние уровни, значит
                # новый вотчер тут родиться не может. Уже живые вотчеры (после
                # reset_for_rescan) при этом всё равно кормятся этой свечой —
                # process_candle сам подхватывает их из levels_to_eval
                # независимо от переданного списка supports/resistances.
                candle_supports = []
                candle_resistances = []

            orders, draw_events = bounce_mgr.process_candle(
                c_low, c_high, c_close, candle_supports, candle_resistances, df_slice,
                allow_long=allow_long, allow_short=allow_short, c_atr=c_atr, coin=coin
            )
            total_orders += len(orders)

            for order in orders:
                d = order['decision']
                tt = order['trade_type']
                entry, sl, tp = d.get('entry_price', 0.0), d.get('sl', 0.0), d.get('tp', 0.0)
                rr = ((tp - entry) / (entry - sl)) if tt == 'LONG' and entry > sl else \
                     ((entry - tp) / (sl - entry)) if tt == 'SHORT' and sl > entry else 0
                emoji = "🟢" if tt == "LONG" else "🔴"

                # level_id для SHORT всегда вида "BC_SHORT_min_max__CLIMAX"/"...__MIRROR"
                # (см. evaluate_bounce() в bounce_manager.py) — берём режим прямо оттуда.
                level_id_val = d.get('level_id', '') or ''
                mode = level_id_val.split('__')[-1] if '__' in level_id_val else None
                source_label = f"BOUNCE_{tt}" + (f"_{mode}" if tt == "SHORT" and mode else "")
                save_signal({
                    "type": "SHORT_PUMP" if tt == "SHORT" else "WATCHER_LONG",
                    "coin": coin,
                    "source": source_label,
                    "price": entry,
                    "take_profit": tp,
                    "stop_loss": sl,
                    # unix-секунды РЕАЛЬНОЙ свечи входа (ts — свеча из реплей-цикла
                    # выше, не "сейчас") + level_id зоны — нужны дашборду для клика
                    # по сигналу (переход на график в этот момент + отрисовка зоны).
                    "time": int(ts.timestamp()),
                    "level_id": level_id_val or None,
                    # Столбец "Метод" на дашборде — пока единственный метод
                    # реакции на уровень это объём (см. bounce_watcher.py::_enter,
                    # поля volume/volume_mult). На будущее заложено как
                    # method/method_value/method_mult — появятся другие методы,
                    # просто передавай их сюда тем же способом.
                    "method": "volume" if d.get("volume") is not None else None,
                    "method_value": d.get("volume"),
                    "method_mult": d.get("volume_mult"),
                    "level_type": d.get("level_type") or d.get("type"),
                })

                reports.append(
                    f"{emoji} *BOUNCE {tt}*{' 🪦' if d.get('reborn') else ''} _{coin}_\n\n"
                    f"Entry: `{entry:.8f}`\nSL: `{sl:.8f}`\nTP: `{tp:.8f}`\nR/R: `{rr:.2f}`\n\n"
                    f"{d.get('reason', '')}\nLevel: `{d.get('level_id', 'unknown')}`"
                )

        bounce_mgr.last_processed_time[coin] = int(replay_df.index[-1].timestamp())

        replay_elapsed = time.time() - replay_started_at
        if len(replay_df) > 10:
            # Только для заметных реплеев (рескан/долгий простой) — на обычный
            # тик (1 свеча) печатать время незачем, шум в консоли.
            print(f"[BOUNCE REPLAY] {coin}: {len(replay_df)} свечей за {replay_elapsed:.1f} сек "
                  f"({replay_elapsed / len(replay_df) * 1000:.1f} мс/свеча)")

        return total_orders, reports, len(supports) + len(resistances)

    except Exception as e:
        print(f"\n[BOUNCE ERROR] ❌ {coin}: {e}")
        traceback.print_exc()
        return 0, [], 0


def rescan_single_bounce_watcher(coin, level_id, bounce_mgr):
    """Точечный рескан ОДНОГО уже существующего BOUNCE-вотчера — в отличие
    от check_bounce()/ручного рескана по монете (см. run_web.py::_run_rescan),
    НЕ трогает никаких других вотчеров этой же монеты. Отматывает вотчера от
    его собственного activated_at (см. bounce_manager.py::evaluate_bounce,
    строка activated_at копируется из level) и честно прогоняет весь его путь
    заново.

    Технически это возможно ТОЛЬКО потому, что evaluate_bounce() (в отличие
    от evaluate_bounce_side()/process_candle()) работает строго с
    self._watchers[level_id] — не перебирает и не видит остальных вотчеров
    монеты. Вызывая его напрямую в обход evaluate_bounce_side, мы кормим
    свечами ИСКЛЮЧИТЕЛЬНО этот один объект.

    ЧЕСТНОСТЬ РЕПЛЕЯ — единственная разница с боевым путём: is_focus здесь
    ВСЕГДА True. В бою is_focus решает _get_focus_level_id(), который
    смотрит на ВСЕХ конкурирующих вотчеров той же монеты/направления/mode
    (см. bounce_manager.py) — а мы намеренно их не трогаем и не реплеим
    параллельно, значит честно вычислить "кому в реальности достался бы
    фокус на каждый момент времени" здесь нечем. На практике это может
    показать вход, которого в реальности не было бы (если фокус тогда был
    у другого вотчера той же монеты) — держи это в уме, если у монеты
    исторически бывает больше одного live-кандидата одновременно.

    НЕ трогает bounce_mgr.last_processed_time[coin] — это общий курсор
    монеты для обычного боевого скана/рескана, точечный рескан отдельного
    вотчера не должен на него влиять.

    Возвращает {"error": ...} либо {"level_id", "coin", "since",
    "clamped_to_available_history", "final_state", "orders", "event_log"}.
    """
    watcher = bounce_mgr._watchers.get(level_id)
    if watcher is None or getattr(watcher, 'coin', None) != coin:
        return {"error": f"Вотчер {level_id} не найден для {coin}"}

    since_str = getattr(watcher, 'activated_at', None) or getattr(watcher, 'level_date', None)
    if not since_str:
        return {"error": "У вотчера нет ни activated_at, ни level_date — не от чего отматывать"}
    try:
        since_dt = datetime.datetime.fromisoformat(since_str.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        try:
            since_dt = datetime.datetime.strptime(since_str[:10], '%Y-%m-%d')
        except Exception:
            return {"error": f"Не смог разобрать дату activated_at='{since_str}'"}

    markets = load_markets_cached(exchange)
    symbol = resolve_symbol(coin, markets)
    if not symbol:
        return {"error": f"Монета {coin} не найдена на бирже"}

    df, fetch_err = _fetch_candles_df(symbol, coin, 52, "BOUNCE_SINGLE_RESCAN")
    if df is None:
        return {"error": fetch_err or "Нет доступных свечей"}

    # Клэмп на глубину локальной истории — ЯВНО, не тихим прыжком к последней
    # свече (та ловушка уже есть в check_bounce при BOUNCE_REPLAY_MAX_CANDLES,
    # здесь сообщаем об этом вызывающему коду честно).
    earliest_available = df.index[0].tz_localize(None) if df.index[0].tzinfo is not None else df.index[0]
    clamped = False
    if since_dt < earliest_available.to_pydatetime():
        since_dt = earliest_available.to_pydatetime()
        clamped = True

    since_ts = pd.Timestamp(since_dt, tz="UTC")
    candidate_df = df[df.index >= since_ts]
    if candidate_df.empty:
        return {"error": "Нет свечей с момента activated_at этого вотчера"}

    trade_type = watcher.trade_type
    mode = getattr(watcher, 'mode', None)
    level = {
        'min': watcher.min, 'max': watcher.max,
        'type': getattr(watcher, 'level_type', 'UNKNOWN'),
        'score': getattr(watcher, 'level_score', 0),
        'date': getattr(watcher, 'level_date', None),
        'activated_at': getattr(watcher, 'activated_at', None),
    }

    # activated_at — это дата, когда УРОВЕНЬ технически подтвердился, НЕ дата,
    # когда цена РЕАЛЬНО впервые коснулась зоны. В бою вотчер рождается строго
    # в момент touched (см. process_candle: touched_short = c_high >= r['min'],
    # touched_long = c_low <= s['max']) — на этот момент цена физически не
    # может быть "уже далеко снаружи". Если стартовать реплей прямо с
    # activated_at, первая свеча может застать цену уже за пределами зоны —
    # тогда сработает защита от телепорта (bounce_watcher.py, KILL_ON_TELEPORT_
    # OPEN) и вотчер умрёт мгновенно, ни разу честно не просканировав.
    # Поэтому ищем первую РЕАЛЬНО touched свечу начиная с activated_at и
    # начинаем реплей именно с неё — так же, как родился бы боевой вотчер.
    if trade_type == 'SHORT':
        touched_mask = candidate_df['high'] >= level['min']
    else:
        touched_mask = candidate_df['low'] <= level['max']

    if not touched_mask.any():
        return {
            "level_id": level_id, "coin": coin,
            "since": since_dt.strftime('%Y-%m-%d %H:%M'),
            "clamped_to_available_history": clamped,
            "final_state": "NOT_TOUCHED",
            "orders": [],
            "event_log": [],
            "note": "Цена ни разу не коснулась зоны с момента activated_at до сейчас — вотчеру физически неоткуда было родиться.",
        }

    first_touch_ts = candidate_df.index[touched_mask.argmax()]
    replay_df = df[df.index >= first_touch_ts]

    # Сброс ТОЛЬКО этого одного объекта — соседи в bounce_mgr._watchers не видят
    # этот вызов вообще (identity/min/max/trade_type/mode не трогаются).
    watcher.reset_for_rescan()

    atr_series = calculate_atr(df)
    WINDOW = 60
    df_index = df.index
    orders = []

    for ts, row in replay_df.iterrows():
        pos = df_index.searchsorted(ts, side="right")
        df_slice = df.iloc[max(0, pos - WINDOW):pos]
        if len(df_slice) < 52:
            continue
        c_atr = float(atr_series.loc[ts]) if ts in atr_series.index else 0.0

        ts_naive = ts.tz_localize(None) if ts.tzinfo is not None else ts
        hist_snapshot = get_levels_snapshot(ts_naive.to_pydatetime())
        if hist_snapshot is not None:
            hist_coin_macro = hist_snapshot.get(coin) or hist_snapshot.get(KNOWN_TICKER_ALIASES.get(coin, ""), {})
            opp_side = hist_coin_macro.get('resistances' if trade_type == 'LONG' else 'supports', [])
            same_side = hist_coin_macro.get('resistances', []) if trade_type == 'SHORT' else []
        else:
            opp_side, same_side = [], []

        decision = bounce_mgr.evaluate_bounce(
            level, df_slice, trade_type, opp_side,
            is_focus=True, c_atr=c_atr, same_side_levels=same_side,
            mode=mode, coin=coin,
        )
        if decision.get('allow'):
            orders.append({
                'time': ts_naive.isoformat(),
                'entry': decision.get('entry_price'),
                'sl': decision.get('sl'),
                'tp': decision.get('tp'),
                'reason': decision.get('reason', ''),
            })

    first_touch_naive = first_touch_ts.tz_localize(None) if first_touch_ts.tzinfo is not None else first_touch_ts
    return {
        "level_id": level_id,
        "coin": coin,
        "activated_at": since_dt.strftime('%Y-%m-%d %H:%M'),
        "since": first_touch_naive.strftime('%Y-%m-%d %H:%M'),  # реальный момент первого касания — отсюда шёл реплей
        "clamped_to_available_history": clamped,
        "final_state": watcher.state,
        "orders": orders,
        "event_log": list(getattr(watcher, 'event_log', [])),
    }