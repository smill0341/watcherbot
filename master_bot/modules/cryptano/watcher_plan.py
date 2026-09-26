# modules/cryptano/watcher_plan.py

import time
import os
import sys
import datetime
from modules.cryptano.utils.storage import load_json, save_json_atomic
import pandas as pd
import gc
import traceback
from modules.cryptano.utils.common import calculate_rsi, exchange, format_price as fmt_p, price_precision_from_market, resolve_symbol, KNOWN_TICKER_ALIASES
from modules.cryptano.utils.market_cache import load_markets_cached
from modules.cryptano.utils.indicators import pandas_get_local_structure, calculate_atr, calculate_ema
from modules.cryptano.strategy.vbottom_manager import VBottomManager
from modules.cryptano.strategy.bounce_manager import BounceManager
from modules.cryptano.strategy.bounce_parent import BounceParent
from modules.cryptano.utils.paths import MACRO_LEVELS_FILE, CUSTOM_LEVELS_FILE, BOUNCE_ERRORS_FILE, SIGNALS_FILE
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

# Рескан вотчера (🎯 / "рескан всех") смотрит назад от activated_at уровня,
# но не дальше этого числа дней от сейчас — старая история зоны не нужна.
RESCAN_MAX_LOOKBACK_DAYS = 14


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
        df = _drop_forming_candle(df[["open", "high", "low", "close", "volume"]].astype(float))
        if len(df) >= min_candles:
            return df, None

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
    df = _drop_forming_candle(df[["open", "high", "low", "close", "volume"]].astype(float))
    if len(df) < min_candles:
        return None, f"⚠️ Недостаточно закрытых свечей {strategy_tag} для {coin} ({len(df)})."
    return df, None


_CANDLE_SEC = 15 * 60


def _drop_forming_candle(df: pd.DataFrame) -> pd.DataFrame:
    """Убирает ещё НЕ ЗАКРЫТУЮ свечу в конце (и candle_store, и биржа
    отдают текущую формирующуюся свечу последней строкой).

    БАГ, который это чинит: скан идёт на границе 15м + 60 сек, и последней
    строкой была свеча, открывшаяся минуту назад (почти без объёма, high/low
    ещё не сформированы). Вотчер обрабатывал ЕЁ, запоминал её время в
    last_processed_time — и на следующем скане она уже считалась
    "обработанной" и пропускалась (реплей берёт только свечи ПОЗЖЕ).
    Итог: живой бот видел только первую минуту каждой свечи, никогда её
    полный вид — объём не проходил порог, сделок не было. Рескан же идёт
    по уже закрытой истории — поэтому он находил сделки, а скан нет.
    Касается всех стратегий (все берут последнюю строку df)."""
    if df.empty:
        return df
    now = pd.Timestamp.now(tz="UTC")
    return df[df.index + pd.Timedelta(seconds=_CANDLE_SEC) <= now]


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
    
# Сколько последних ошибок check_bounce храним (bounce_errors.json) — чтобы
# файл не рос бесконечно, если проблема реально повторяется каждый цикл.
BOUNCE_ERRORS_MAX = 500


def _log_bounce_error(coin, exc, kind="exception"):
    """Пишет проблему в check_bounce() в отдельный файл (BOUNCE_ERRORS_FILE,
    см. paths.py), а не только в print() — печать видна лишь пока кто-то
    смотрит живую консоль сервера, теряется намертво после скролла/рестарта.

    kind:
      "exception" — упал сам процесс (см. except в конце check_bounce).
      "resolve_symbol" / "fetch_candles" — check_bounce НЕ упал, а тихо
      вышел через return ДО того, как накормил вотчеров этой монеты новой
      свечой (см. check_bounce, самые первые return 0, [], 0). Это не
      исключение, поэтому раньше нигде не логировалось вообще — именно
      такая тихая монета могла копить пропущенные циклы незаметно, пока
      её не найдёт ручной /api/rescan_all_watchers.

    Если тут снова и снова одна и та же монета — это и есть "застрявшая"
    монета, её BOUNCE-вотчеры не получают свежих свечей и поэтому не могут
    ни ожить, ни честно умереть сами.

    Сам лог не должен ронять check_bounce, если с записью что-то не так —
    поэтому обёрнут в свой try/except, print() ниже остаётся как фолбэк."""
    try:
        entries = load_json(BOUNCE_ERRORS_FILE, default=[])
        if not isinstance(entries, list):
            entries = []
        entries.append({
            "timestamp": datetime.datetime.now().isoformat(),
            "coin": coin,
            "kind": kind,
            "error": str(exc),
        })
        entries = entries[-BOUNCE_ERRORS_MAX:]
        save_json_atomic(BOUNCE_ERRORS_FILE, entries)
    except Exception as log_exc:
        print(f"[BOUNCE ERROR LOG] Не удалось записать в {BOUNCE_ERRORS_FILE}: {log_exc}")


def _coin_has_active_bounce_watcher(coin, bounce_mgr):
    """Тот же самый has_active_watcher-чек, что чуть ниже в check_bounce
    (line ~620), но нужен РАНЬШЕ — до похода за символом/свечами — чтобы
    решить, стоит ли логировать тихий пропуск монеты в _log_bounce_error.
    Логировать пропуск монеты, у которой и так нет ни одного живого
    вотчера — просто шум, там и терять нечего."""
    return any(
        getattr(w, "coin", None) == coin and getattr(w, "state", None) not in ("DEAD", "TRIGGERED")
        for w in bounce_mgr._watchers.values()
    )


def _level_active_from(level):
    """activated_at уровня -> pandas.Timestamp (наивный UTC) или None.

    activated_at — момент, с которого уровень МОЖНО брать в работу (см.
    комментарий "ДВЕ РАЗНЫЕ ДАТЫ" в bounce_manager.py::evaluate_bounce).
    Форматы: 'YYYY-MM-DD' (levels_builder.py — дата свечи подтверждения,
    считаем с 00:00 UTC этого дня) или полный ISO. None/пусто/нечитаемо —
    без ограничения (например, ручной уровень из custom_levels.json)."""
    raw = level.get('activated_at') if isinstance(level, dict) else None
    if not raw:
        return None
    try:
        t = pd.Timestamp(str(raw).replace('Z', '+00:00'))
        if t.tzinfo is not None:
            t = t.tz_convert('UTC').tz_localize(None)
        return t
    except Exception:
        return None


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
            if _coin_has_active_bounce_watcher(coin, bounce_mgr):
                _log_bounce_error(coin, "resolve_symbol не нашёл символ — вотчер(ы) этой монеты пропустили тик", kind="resolve_symbol")
            return 0, [], 0

        df, fetch_err = _fetch_candles_df(symbol, coin, 52, "BOUNCE")
        if df is None:
            if _coin_has_active_bounce_watcher(coin, bounce_mgr):
                _log_bounce_error(coin, f"_fetch_candles_df вернул None (fetch_err={fetch_err}) — вотчер(ы) этой монеты пропустили тик", kind="fetch_candles")
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

        # УРОВНИ ДЛЯ БОЕВОГО СКАНА — ТОЛЬКО macro_levels.json + custom_levels.json
        # (supports/resistances выше, get_merged_levels_for_coin). Исторические
        # снимки levels_timeline (levels_history.py) — ТОЛЬКО для симулятора,
        # боевой бот туда не смотрит. Раньше смотрел: снимок брался на каждую
        # свечу, а снимок пишется раз в 12ч и НЕ перезаписывается свежим
        # пересчётом в том же 12-часовом окне — бот часами работал по старым
        # уровням (отсюда был LONG от зоны, которая в свежем macro_levels.json
        # уже сопротивление).
        # Защита от "уровня из будущего" на прошлых свечах догона — через
        # activated_at уровня (см. _level_active_from): на свече раньше
        # activated_at уровень не участвует. Уровень без activated_at (ручной)
        # — активен всегда.
        level_active_from = {
            id(z): _level_active_from(z) for z in (supports + resistances)
        }

        # Кладбище этой монеты: забыть записи зон, которых больше нет среди
        # уровней, и давно "отпущенные" (см. bounce_manager.py::prune_graveyard).
        try:
            bounce_mgr.prune_graveyard(coin, supports + resistances, max_age_days=RESCAN_MAX_LOOKBACK_DAYS)
        except Exception as e:
            print(f"[BOUNCE] ⚠️ {coin}: не удалось почистить кладбище: {e}")

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

            # Только уровни, которые на момент ЭТОЙ свечи уже активны
            # (activated_at <= время свечи). Уже живые вотчеры кормятся свечой
            # в любом случае — process_candle подхватывает их сам, независимо
            # от этих списков (списки решают только, кто может РОДИТЬСЯ).
            ts_naive = ts.tz_localize(None) if ts.tzinfo is not None else ts
            candle_supports = [z for z in supports
                               if level_active_from[id(z)] is None or level_active_from[id(z)] <= ts_naive]
            candle_resistances = [z for z in resistances
                                  if level_active_from[id(z)] is None or level_active_from[id(z)] <= ts_naive]

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
                    # Уровень + путь вотчера на момент сделки — в сам сигнал
                    # (см. _signal_level_snapshot / history.py::save_signal).
                    **_signal_level_snapshot(bounce_mgr._watchers.get(level_id_val)),
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
        _log_bounce_error(coin, e)
        return 0, [], 0


def _signal_level_snapshot(watcher):
    """Снимок уровня и пути вотчера для записи ПРЯМО В СИГНАЛ (см.
    history.py::save_signal, поля level_min/level_max/.../events).
    Копия списка событий — не ссылка: вотчер продолжит жить (или будет
    сброшен рескан-ом через reset_for_rescan), а сигнал должен навсегда
    остаться с тем путём, который был на момент сделки."""
    if watcher is None:
        return {}
    return {
        "level_min": getattr(watcher, "min", None),
        "level_max": getattr(watcher, "max", None),
        "level_date": getattr(watcher, "level_date", None),
        "level_score": getattr(watcher, "level_score", None),
        "activated_at": getattr(watcher, "activated_at", None),
        "mode": getattr(watcher, "mode", None),
        "events": [dict(ev) for ev in (getattr(watcher, "event_log", None) or [])],
    }


def _utc_iso(ts):
    """Время свечи -> ISO в UTC С ЯВНОЙ пометкой зоны (+00:00), тот же формат,
    что history.py::_utc_now_iso. Без пометки браузер читал UTC как местное
    (Киев, +3) -> закрытие на 3 часа раньше входа."""
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.isoformat()


def _resolve_historical_signal_status(df, entry_ts, direction, entry, target, stop, max_hold_days):
    """Честно доигрывает историческую сделку по уже скачанным ЗАКРЫТЫМ
    свечам от входа (entry_ts) до сегодня — используется ТОЛЬКО рескан-путём
    (rescan_single_bounce_watcher), когда сделка вставляется в signals.json
    задним числом.

    Та же самая формула закрытия, что в history.py::update_open_signals()
    (TP / SL / MAX_HOLD_DAYS, сравнение процента по entry), но не по разовой
    текущей цене с тикера (тикер физически не знает, что было со свечами
    между историческим входом и сегодня), а по каждой реально прошедшей
    свече — как честный бэктест, не как снимок "сейчас".

    Возвращает (status, result_percent, closed_at_iso). status == "⏳"
    означает, что по-честному сделка была бы всё ещё открыта прямо
    сейчас — тогда её и оставляем открытой, дальше о ней позаботится
    обычная периодическая update_open_signals(), как о любой живой сделке.
    """
    # Со СЛЕДУЮЩЕЙ свечи после входа. Раньше (>=) проверка начиналась с
    # самой свечи входа — её high/low были ещё ДО входа, и сделка могла
    # "закрыться" на той же свече -> длительность 0.
    future = df[df.index > entry_ts]
    if future.empty:
        return "⏳", None, None

    for ts, row in future.iterrows():
        high, low = float(row['high']), float(row['low'])
        if direction == "LONG":
            hit_tp = target is not None and high >= target
            hit_sl = stop is not None and low <= stop
        else:
            hit_tp = target is not None and low <= target
            hit_sl = stop is not None and high >= stop

        if hit_tp or hit_sl:
            # Если в одной свече задело и TP, и SL — честно неизвестно, что
            # было первым (внутри свечи порядок движения не виден), берём
            # SL как более консервативный исход, как и везде в проекте.
            if hit_sl:
                exit_price = stop
                status = "❌"
            else:
                exit_price = target
                status = "✅"
            pct = ((exit_price - entry) / entry * 100.0) if direction == "LONG" \
                else ((entry - exit_price) / entry * 100.0)
            return status, round(pct, 2), _utc_iso(ts)

    # Ни TP, ни SL ни одна свеча не тронула — проверяем истечение MAX_HOLD_DAYS,
    # ровно как update_open_signals(): закрываем по факту на этот момент.
    entry_ts_naive = entry_ts.tz_localize(None) if entry_ts.tzinfo is not None else entry_ts
    last_ts = future.index[-1]
    last_ts_naive = last_ts.tz_localize(None) if last_ts.tzinfo is not None else last_ts
    days_open = (last_ts_naive - entry_ts_naive).days
    if days_open >= max_hold_days:
        last_close = float(future.iloc[-1]['close'])
        pct = ((last_close - entry) / entry * 100.0) if direction == "LONG" \
            else ((entry - last_close) / entry * 100.0)
        status = "✅" if pct > 0 else "❌"
        return status, round(pct, 2), _utc_iso(last_ts)

    # Свежая, ещё не истёкшая сделка — по-честному ещё открыта.
    return "⏳", None, None


def rescan_single_bounce_watcher(coin, level_id, bounce_mgr):
    """Рескан ЗОНЫ вотчера (кнопка 🎯 и "рескан всех").

    ================================================================
    КАК РАБОТАЕТ (простыми словами):
    1. Берём зону вотчера (min/max) и отматываем назад: от activated_at
       уровня, но не дальше RESCAN_MAX_LOOKBACK_DAYS (2 недели).
    2. Идём по свечам вперёд. РОЛЬ ЗОНЫ решает путь цены, а не ярлык и не
       направление старого вотчера:
         цена была НАД зоной и пришла к ней  -> поддержка     -> LONG;
         цена была ПОД зоной и пришла к ней  -> сопротивление -> SHORT.
       "Была над/под" — в начале отрезка это просто где стоит цена; дальше
       сторона меняется ТОЛЬКО когда цена закрылась дальше
       GRAVEYARD_ESCAPE_PCT (5%) от зоны — реально ушла, а не ткнула одной
       свечой.
    3. Касание -> "жизнь" вотчера в этом направлении (обычные правила
       BounceWatcher: пробой середины, вход, смерть...).
    4. Жизнь закончилась (сделка/смерть) -> новая жизнь только когда:
         сделка закрыта по свечам (TP/SL/время), И цена ушла от зоны
         дальше 5%, И потом снова касание. Направление новой жизни — с той
         стороны, куда цена ушла.
       Это ровно те же условия, что у живого бота (кладбище, см.
       bounce_manager.py::_update_graveyard).
    5. Сделки всех жизней -> в сигналы (уже записанные — не дублируются),
       статус сразу считается по свечам.
    6. Итог — последняя жизнь:
         ещё идёт   -> остаётся вотчер В ЕЁ НАПРАВЛЕНИИ (если направление
                       другое, чем у исходного вотчера — исходный закрывается,
                       на зоне ставится вотчер нужной стороны);
         закончилась и нового касания нет -> вотчера нет, кладбище не даст
                       живому боту сразу завести его снова.
    ================================================================

    is_focus в реплее всегда True (соседей не реплеим параллельно).
    НЕ трогает bounce_mgr.last_processed_time[coin].

    Возвращает {"error": ...} либо {"level_id" (итоговый), "coin", "since",
    "final_state", "direction", "lives", "orders", "event_log", ...}.
    """
    orig = bounce_mgr._watchers.get(level_id)
    if orig is None or getattr(orig, 'coin', None) != coin:
        return {"error": f"Вотчер {level_id} не найден для {coin}"}

    def _parse_dt(s):
        if not s:
            return None
        try:
            return datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00')).replace(tzinfo=None)
        except Exception:
            try:
                return datetime.datetime.strptime(str(s)[:10], '%Y-%m-%d')
            except Exception:
                return None

    activated_dt = _parse_dt(getattr(orig, 'activated_at', None) or getattr(orig, 'level_date', None))
    lookback_floor = datetime.datetime.utcnow() - datetime.timedelta(days=RESCAN_MAX_LOOKBACK_DAYS)
    since_dt = max(activated_dt, lookback_floor) if activated_dt else lookback_floor

    markets = load_markets_cached(exchange)
    symbol = resolve_symbol(coin, markets)
    if not symbol:
        return {"error": f"Монета {coin} не найдена на бирже"}

    df, fetch_err = _fetch_candles_df(symbol, coin, 52, "BOUNCE_SINGLE_RESCAN")
    if df is None:
        return {"error": fetch_err or "Нет доступных свечей"}

    earliest_available = df.index[0].tz_localize(None) if df.index[0].tzinfo is not None else df.index[0]
    clamped = False
    if since_dt < earliest_available.to_pydatetime():
        since_dt = earliest_available.to_pydatetime()
        clamped = True

    since_ts = pd.Timestamp(since_dt, tz="UTC")
    candidate_df = df[df.index >= since_ts]
    if candidate_df.empty:
        return {"error": "Нет свечей с момента activated_at этого вотчера"}

    zmin, zmax = float(orig.min), float(orig.max)
    level = {
        'min': zmin, 'max': zmax,
        'type': getattr(orig, 'level_type', 'UNKNOWN'),
        'score': getattr(orig, 'level_score', 0),
        'date': getattr(orig, 'level_date', None),
        'activated_at': getattr(orig, 'activated_at', None),
    }
    orig_dir = orig.trade_type
    orig_mode = getattr(orig, 'mode', None)

    escape_tol = float(getattr(bounce_mgr, 'CONFIG', {}).get('GRAVEYARD_ESCAPE_PCT', 5.0)) / 100.0
    escape_low = zmin * (1 - escape_tol)
    escape_high = zmax * (1 + escape_tol)

    # --- сторона цены относительно зоны и касания (см. п.2 докстринга) ---
    # side: 'above' / 'below' / None. Начало отрезка — просто где цена;
    # дальше меняется только уходом дальше escape (5%).
    first_close = float(candidate_df['close'].iloc[0])
    side = 'above' if first_close > zmax else ('below' if first_close < zmin else None)
    touches = {}          # ts -> 'LONG'/'SHORT' — касание с учётом стороны ДО этой свечи
    escape_side_at = {}   # ts -> 'above'/'below' — на этой свече цена ушла дальше 5%
    for ts, row in candidate_df.iterrows():
        lo, hi, cl = float(row['low']), float(row['high']), float(row['close'])
        if side == 'above' and lo <= zmax:
            touches[ts] = 'LONG'
        elif side == 'below' and hi >= zmin:
            touches[ts] = 'SHORT'
        if cl > escape_high:
            side = 'above'; escape_side_at[ts] = 'above'
        elif cl < escape_low:
            side = 'below'; escape_side_at[ts] = 'below'
        elif side is None:
            side = 'above' if cl > zmax else ('below' if cl < zmin else None)

    if not touches:
        # Касаний с понятной стороны нет. Если исходный вотчер РОДИЛСЯ
        # внутри отрезка — он родился неправильно (старая дыра) -> закрываем.
        born_raw = getattr(orig, 'born_at', None)
        born_dt = _parse_dt(born_raw) if born_raw and 'T' in str(born_raw) else None
        if born_dt is not None and born_dt >= since_dt:
            orig.state = "DEAD"
            orig.last_event_type = "RUNAWAY"
            orig.history_log = "Рескан: касания зоны с понятной стороны не было — вотчер родился неправильно. Закрыт."
            final_state = "DEAD"
        else:
            final_state = "NOT_TOUCHED"
        return {
            "level_id": level_id, "coin": coin,
            "since": since_dt.strftime('%Y-%m-%d %H:%M'),
            "clamped_to_available_history": clamped,
            "final_state": final_state, "direction": orig_dir, "lives": 0,
            "orders": [], "event_log": [],
            "note": "Касания зоны с понятной стороны на отрезке нет.",
        }

    from modules.cryptano.strategy.bounce_watcher import BounceWatcher
    max_hold_days = BounceWatcher.CONFIG.get("MAX_HOLD_DAYS", 14)

    # Все зоны монеты (macro + custom) — для "противоположной стороны" по
    # ПОЛОЖЕНИЮ, а не по ярлыку: для LONG — зоны выше этой, для SHORT — ниже.
    _m = get_merged_levels_for_coin(coin) or {}
    all_zones = list(_m.get("supports", [])) + list(_m.get("resistances", []))

    existing_signal_keys = {
        (s.get("level_id"), s.get("time"))
        for s in load_json(SIGNALS_FILE, default=[])
        if isinstance(s, dict)
    }

    def _life_id_and_mode(direction):
        """level_id и mode вотчера для этой стороны зоны. SHORT — режим
        исходного вотчера, если он сам SHORT, иначе MIRROR."""
        mode = None
        if direction == 'SHORT':
            mode = orig_mode if (orig_dir == 'SHORT' and orig_mode) else 'MIRROR'
        lid = bounce_mgr._level_id(level, direction)
        if mode:
            lid = f"{lid}__{mode}"
        return lid, mode

    # Лимит сделок на уровень (bounce_mgr.level_trades): рескан проигрывает
    # историю зоны заново с since_ts — сделки этой зоны с этого момента
    # забываем, рескан найдёт их сам. После первой сделки (при лимите 1)
    # evaluate_bounce не даст новой "жизни" войти второй раз.
    _base_long = bounce_mgr._level_id(level, 'LONG')
    _base_short = bounce_mgr._level_id(level, 'SHORT')
    bounce_mgr.forget_level_trades_since(
        [_base_long, _base_short, f"{_base_short}__CLIMAX", f"{_base_short}__MIRROR"],
        int(since_ts.timestamp()),
    )

    atr_series = calculate_atr(df)
    WINDOW = 60
    df_index = df.index
    orders = []
    lives = 0
    life_dir = life_mode = life_lid = None
    life_start_ts = None
    life_active = False
    life_trade_close = None     # None / pd.Timestamp / "OPEN"
    blocked_until = None
    escaped_after_end = True    # для первой жизни ухода не требуем
    used_ids = set()

    for ts, row in candidate_df.iterrows():
        ts_n = ts.tz_localize(None) if ts.tzinfo is not None else ts

        if not life_active:
            # Ждём закрытия сделки прошлой жизни и ухода цены на 5%.
            if blocked_until is not None and ts_n <= blocked_until:
                continue
            if not escaped_after_end:
                if ts in escape_side_at:
                    escaped_after_end = True
                continue
            if ts not in touches:
                continue
            # Новая жизнь: направление — по стороне, откуда пришла цена.
            life_dir = touches[ts]
            life_lid, life_mode = _life_id_and_mode(life_dir)
            w = bounce_mgr._watchers.get(life_lid)
            if w is not None:
                w.reset_for_rescan()
            life_active = True
            life_start_ts = ts
            life_trade_close = None
            lives += 1
            used_ids.add(life_lid)

        pos = df_index.searchsorted(ts, side="right")
        df_slice = df.iloc[max(0, pos - WINDOW):pos]
        if len(df_slice) < 52:
            continue
        c_atr = float(atr_series.loc[ts]) if ts in atr_series.index else 0.0

        # same_side (все зоны той же стороны, для paired_level) убран вместе
        # с самим paired_level — см. bounce_manager.py::
        # kill_short_watchers_behind_pierced_neighbor. Рескан одного вотчера
        # намеренно ведёт только ОДИН вотчер и соседей не реплеит параллельно
        # (см. докстринг функции выше, "is_focus в реплее всегда True"), так
        # что этой проверке тут и применяться не к чему.
        _active = lambda z: (lambda t: t is None or t <= ts_n)(_level_active_from(z))
        if life_dir == 'LONG':
            opp_side = [z for z in all_zones if z['min'] > zmax and _active(z)]
        else:
            opp_side = [z for z in all_zones if z['max'] < zmin and _active(z)]

        # evaluate_bounce сам создаёт вотчер этой стороны, если его ещё нет
        # (born_at = время этой свечи, activated_at — из уровня).
        decision = bounce_mgr.evaluate_bounce(
            level, df_slice, life_dir, opp_side,
            is_focus=True, c_atr=c_atr,
            mode=life_mode, coin=coin,
        )
        w = bounce_mgr._watchers.get(life_lid)

        if decision.get('allow'):
            entry, sl, tp = decision.get('entry_price', 0.0), decision.get('sl', 0.0), decision.get('tp', 0.0)
            orders.append({'time': ts_n.isoformat(), 'direction': life_dir,
                           'entry': entry, 'sl': sl, 'tp': tp, 'reason': decision.get('reason', '')})
            status, result_percent, closed_at = _resolve_historical_signal_status(
                df, ts, life_dir, entry, tp, sl, max_hold_days
            )
            # closed_at теперь с зоной (+00:00) — для сравнения со свечами
            # (ts_n — наивный UTC) убираем зону обратно.
            life_trade_close = "OPEN" if status == "⏳" or not closed_at \
                else pd.Timestamp(closed_at).tz_convert("UTC").tz_localize(None)
            sig_key = (life_lid, int(ts.timestamp()))
            if sig_key not in existing_signal_keys:
                existing_signal_keys.add(sig_key)
                source_label = f"BOUNCE_{life_dir}" + (f"_{life_mode}" if life_dir == "SHORT" and life_mode else "")
                save_signal({
                    "type": "SHORT_PUMP" if life_dir == "SHORT" else "WATCHER_LONG",
                    "coin": coin,
                    "source": source_label,
                    "price": entry,
                    "take_profit": tp,
                    "stop_loss": sl,
                    "time": int(ts.timestamp()),
                    "level_id": life_lid,
                    "method": "volume" if decision.get("volume") is not None else None,
                    "method_value": decision.get("volume"),
                    "method_mult": decision.get("volume_mult"),
                    "level_type": decision.get("level_type") or level.get("type"),
                    "status": status,
                    "result_percent": result_percent,
                    "closed_at": closed_at,
                    **_signal_level_snapshot(w),
                })

        if w is not None and w.state in ("TRIGGERED", "DEAD"):
            life_active = False
            escaped_after_end = False
            if life_trade_close == "OPEN":
                break  # сделка до сих пор открыта — дальше по этой зоне смотреть нечего
            blocked_until = life_trade_close if isinstance(life_trade_close, pd.Timestamp) else None

    final_w = bounce_mgr._watchers.get(life_lid) if life_lid else None
    if final_w is not None and life_start_ts is not None:
        _ls = life_start_ts.tz_localize(None) if life_start_ts.tzinfo is not None else life_start_ts
        final_w.born_at = _ls.strftime('%Y-%m-%dT%H:%M:%S')

    # Исходный вотчер: если итог — другая сторона/другой вотчер, а он сам в
    # рескане не участвовал — закрываем: роль зоны по пути цены другая.
    if level_id not in used_ids and orig.state not in ("TRIGGERED", "DEAD"):
        orig.state = "DEAD"
        orig.last_event_type = "RUNAWAY"
        orig.history_log = (f"Рескан: по пути цены зона сейчас работает как "
                            f"{'поддержка (LONG)' if life_dir == 'LONG' else 'сопротивление (SHORT)'} — "
                            f"этот вотчер ({orig_dir}) закрыт.")

    return {
        "level_id": life_lid or level_id,
        "original_level_id": level_id,
        "coin": coin,
        "activated_at": since_dt.strftime('%Y-%m-%d %H:%M'),
        "since": min(touches).tz_localize(None).strftime('%Y-%m-%d %H:%M') if min(touches).tzinfo is not None else min(touches).strftime('%Y-%m-%d %H:%M'),
        "clamped_to_available_history": clamped,
        "final_state": final_w.state if final_w is not None else orig.state,
        "direction": life_dir,
        "lives": lives,
        "orders": orders,
        "event_log": list(getattr(final_w, 'event_log', []) if final_w is not None else []),
    }