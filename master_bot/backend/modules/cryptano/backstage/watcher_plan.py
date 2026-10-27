# modules/cryptano/watcher_plan.py

import time
import os
import datetime
import uuid
from backend.modules.cryptano.utils.storage import load_json, save_json_atomic
import pandas as pd
import traceback
from dataclasses import dataclass
from typing import Callable
from backend.modules.cryptano.utils.bybit import (
    exchange, resolve_symbol, KNOWN_TICKER_ALIASES, load_markets_cached,
)
from backend.modules.cryptano.utils.indicators import calculate_atr, calculate_adx, calculate_ema, calculate_rsi
from backend.modules.cryptano.utils.paths import MACRO_LEVELS_FILE, CUSTOM_LEVELS_FILE, BOUNCE_ERRORS_FILE, SIGNALS_FILE
from backend.modules.cryptano.levels.history import save_signal


def get_merged_levels_for_coin(coin):
    """Supports/resistances монеты — macro (swing_hunter) + ручные
    (custom_levels.json), слитые в один словарь. ОДНА точка правды для
    всех стратегий (check_v для VB/VGB/VRT и check_bounce) — раньше каждая из них независимо делала
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

    if coin_custom.get("ignore_auto"):
        # ❄️ "Заморожена" — галочка, поставленная при добавлении ручного
        # уровня/объёма (см. app.py::add_custom_level). Монета торгует
        # ТОЛЬКО по ручным зонам — авто-уровни из macro_levels.json (даже
        # если whitelist их честно посчитал) сюда не подмешиваются вообще.
        return {
            "supports": list(coin_custom.get("supports", [])),
            "resistances": list(coin_custom.get("resistances", [])),
        }

    merged = dict(coin_macro) if coin_macro else {}
    merged["supports"] = list(coin_macro.get("supports", []) if coin_macro else []) + list(coin_custom.get("supports", []))
    merged["resistances"] = list(coin_macro.get("resistances", []) if coin_macro else []) + list(coin_custom.get("resistances", []))
    return merged


# {coin: (date, coin_macro)} — ТОЛЬКО для V_BOTTOM (см. ниже). Все
# остальные стратегии (VGB, VRT, check_bounce)
# по-прежнему зовут get_merged_levels_for_coin() напрямую и продолжают
# видеть уровни сразу, как только swing_hunter их пересчитает (~раз в
# 12ч) — этот кэш их не касается вообще.
_vb_levels_cache = {}


def _get_levels_for_vbottom(coin):
    """То же самое, что get_merged_levels_for_coin(coin), но обновляется НЕ
    ЧАЩЕ раза в календарные сутки (UTC) — специально для V_BOTTOM, без
    изменения общего расписания пересчёта уровней (оно общее для всех
    стратегий, трогать его значило бы менять и VGB/VRT/BOUNCE тоже, а
    просили именно и только VB).

    Раз в сутки — тот же выбор, что в симуляторе (simulate_engine.py::
    _refresh_levels_if_needed), сделано это тут отдельным кэшем НАД
    get_merged_levels_for_coin(), а не правкой самого swing_hunter/
    расписания сканов."""
    today = datetime.datetime.utcnow().date()
    cached = _vb_levels_cache.get(coin)
    if cached is not None and cached[0] == today:
        return cached[1]
    coin_macro = get_merged_levels_for_coin(coin)
    _vb_levels_cache[coin] = (today, coin_macro)
    return coin_macro

# Сработавшие объёмные триггеры (custom_levels.json[coin]["volume_triggers"])
# кладём в СВОЙ файл, а не в custom_levels.json/active_watchers.json.
# Путь строим от CUSTOM_LEVELS_FILE (тот же jsonbank), не трогая
# utils/paths.py лишний раз ради одной константы. Читает и чистит этот
# файл только дашборд (app.py::get_active_watchers/delete_watchers) —
# сам сканер сюда только ПИШЕТ при срабатывании, назад не читает.
MANUAL_WATCHERS_FILE = os.path.join(os.path.dirname(CUSTOM_LEVELS_FILE), "manual_watchers.json")

try:
    import backend.modules.cryptano.levels.candle_store as candle_store
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


# Минимальный score уровня для V-стратегий — как в симуляторе (слабые уровни не рассматриваем).
MIN_LEVEL_SCORE = 1.0


def _v_signal_events(w):
    """Путь V-вотчера (event_log) — копией, в сам сигнал (поле events), как у
    BOUNCE (см. _signal_level_snapshot). Сайт рисует точки сигнала именно из
    s.events. Копия, а не ссылка: сигнал навсегда остаётся с тем путём,
    который был на момент сделки."""
    return [dict(ev) for ev in (getattr(w, "event_log", None) or [])]


def _val(series, ts):
    """Значение индикатора на свече ts или None, если нет/NaN."""
    v = series.get(ts)
    return float(v) if v is not None and v == v else None


def _indicators_vb(df):
    return lambda ts: {}  # V_BOTTOM индикаторов не использует


def _indicators_vgb(df):
    atr = calculate_atr(df, 14)
    return lambda ts: {"c_atr": _val(atr, ts)}


def _indicators_vrt(df):
    atr = calculate_atr(df, 14)
    atr_slow = atr.rolling(window=100).mean()
    ema = calculate_ema(df, period=50)
    rsi = pd.Series(calculate_rsi(df.reset_index(drop=True)).values, index=df.index)

    def at(ts):
        c_atr = _val(atr, ts)
        slow = _val(atr_slow, ts)
        return {"c_atr": c_atr, "c_atr_slow": c_atr if slow is None else slow,
                "c_ema": _val(ema, ts), "c_rsi": _val(rsi, ts)}
    return at


@dataclass(frozen=True)
class VSpec:
    """Чем отличаются V-стратегии друг от друга. Всё остальное (свечи,
    отслеживание уровня, сигнал, отчёт) у них общее — см. check_v."""
    source: str                   # имя в сигнале и логах
    title: str                    # имя в отчёте
    min_candles: int
    levels_for: Callable[[str], dict]
    indicators: Callable[[pd.DataFrame], Callable[[pd.Timestamp], dict]]


V_SPECS = {
    "VB": VSpec("V_BOTTOM", "V-BOTTOM", 52, _get_levels_for_vbottom, _indicators_vb),
    "VGB": VSpec("V_GREEN_BOTTOM", "V-GREEN-BOTTOM", 52, get_merged_levels_for_coin, _indicators_vgb),
    # 100 свечей, а не 52: ATR_slow (SMA100 от ATR) требует запаса истории
    "VRT": VSpec("V_RED_TOP", "V-RED-TOP", 100, get_merged_levels_for_coin, _indicators_vrt),
}


def check_v(coin, mgr):
    """
    Проверка одной V-стратегии (mgr — её VManager, параметры в V_SPECS) по одной
    монете. Как check_bounce: свечи от последней обработанной (mgr.last_processed_time)
    до текущей идут через вотчер ПО ОЧЕРЕДИ — пропущенное за время простоя бота
    досматривается, каждое событие ложится на свою свечу.

    Правила слежения за уровнем (какой взять, когда снять) — в VManager.step,
    их же использует симулятор.

    Возвращает (отчёты_по_сигналам, число_оценок_уровня).
    """
    spec = V_SPECS[mgr.tag]
    long_side = mgr.direction == "LONG"
    try:
        time.sleep(0.3)
        coin = coin.upper().replace("USDT", "").replace("/", "").strip()

        markets = load_markets_cached(exchange)
        symbol = resolve_symbol(coin, markets)  # учитывает алиасы тикеров типа TON/GRAM
        if not symbol:
            return [], 0

        df, _ = _fetch_candles_df(symbol, coin, spec.min_candles, spec.source)
        if df is None:
            return [], 0

        coin_macro = spec.levels_for(coin)
        if not coin_macro:
            return [], 0
        own_side, opposite_side = ("supports", "resistances") if long_side else ("resistances", "supports")
        # Слабые уровни (score) не рассматриваем — как в симуляторе
        levels = [z for z in coin_macro.get(own_side, []) if z.get('score', 0) >= MIN_LEVEL_SCORE]
        opposite = coin_macro.get(opposite_side, [])
        if not levels:
            return [], 0

        last_t = mgr.last_processed_time.get(coin)
        if last_t is None:
            replay_df = df.iloc[[-1]]  # первый скан монеты — только текущая свеча
        else:
            replay_df = df[df.index > pd.Timestamp(int(last_t), unit="s", tz="UTC")]
            if replay_df.empty:
                return [], 0

        indicators_at = spec.indicators(df)
        reports = []
        evaluated = 0
        for ts in replay_df.index:
            pos = df.index.searchsorted(ts, side="right")  # позиция сразу ПОСЛЕ этой свечи
            window = df.iloc[:pos]
            ts_naive = ts.tz_localize(None) if ts.tzinfo is not None else ts
            # Только уровни, уже активные на эту свечу (activated_at)
            active = [z for z in levels if (_level_active_from(z) or ts_naive) <= ts_naive]

            tracked, result, cancelled = mgr.step(coin, active, opposite, window, **indicators_at(ts))
            if tracked is None or cancelled:
                continue
            evaluated += 1

            if not result:
                continue

            entry_price, sl, tp = result['entry_price'], result['sl'], result['tp']
            level_id = result['level_id']
            # level_*/time/events нужны дашборду, чтобы по клику на сигнал
            # нарисовать зону уровня и путь вотчера
            save_signal({
                "type": "WATCHER_LONG" if long_side else "SHORT_PUMP",
                "coin": coin,
                "source": spec.source,
                "price": entry_price,
                "take_profit": tp,
                "stop_loss": sl,
                "level_type": tracked.get("type"),
                "level_id": level_id,
                "level_min": tracked.get("min"),
                "level_max": tracked.get("max"),
                "level_date": tracked.get("date"),
                "level_score": tracked.get("score"),
                "level_reaction_count": tracked.get("reaction_count"),
                "time": int(ts.timestamp()),
                "events": _v_signal_events(mgr._watchers.get(level_id)),
            })

            if long_side:
                rr = (tp - entry_price) / (entry_price - sl) if entry_price > sl else 0
            else:
                rr = (entry_price - tp) / (sl - entry_price) if sl > entry_price else 0
            reports.append(
                f"{'🟢' if long_side else '🔴'} *{spec.title} {mgr.direction}* _{coin}_\n\n"
                f"Entry: `{entry_price:.8f}`\n"
                f"SL: `{sl:.8f}`\n"
                f"TP: `{tp:.8f}`\n"
                f"R/R: `{rr:.2f}`\n\n"
                f"📊 {result['history_log']}\n"
                f"Level: `{level_id}`"
            )

        mgr.last_processed_time[coin] = int(replay_df.index[-1].timestamp())
        return reports, evaluated

    except Exception as e:
        print(f"\n[{spec.source} ERROR] ❌ ОШИБКА ПРИ ПРОВЕРКЕ {spec.title} ({coin}): {e}")
        traceback.print_exc()
        return [], 0


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


def check_bounce(coin, allow_long, allow_short, bounce_mgr, strategy="BOUNCE"):
    """BOUNCE сам ведёт свои вотчеры внутри bounce_mgr — внешний
    tracked_levels не нужен, process_candle() получает полный список
    текущих уровней и сам решает, что уже отслеживается.

    strategy — имя стратегии в сигнале и уведомлении: "BOUNCE" (основной
    менеджер) или "BUYBACK" (менеджер выкупа, background_tasks.buyback_mgr).

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
        adx_series = calculate_adx(df)
        # EMA200(4h): для отчёта (ema_dist_birth) и для фильтра уровней
        # (levels/level_filter.py — пороги из config.json: общие + свои у монеты)
        from backend.modules.cryptano.simulator.outcome import make_ema_dist_fn, load_candles_4h
        from backend.modules.cryptano.levels.level_filter import LevelFilter
        _ema_fn = make_ema_dist_fn(df, candles_4h=load_candles_4h(candle_store, symbol))
        bounce_mgr.set_ema_dist_fn(coin, _ema_fn)
        level_filter = LevelFilter(coin, _ema_fn)

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
            c_adx = float(adx_series.loc[ts]) if ts in adx_series.index else None

            # Только уровни, которые на момент ЭТОЙ свечи уже активны
            # (activated_at <= время свечи). Уже живые вотчеры кормятся свечой
            # в любом случае — process_candle подхватывает их сам, независимо
            # от этих списков (списки решают только, кто может РОДИТЬСЯ).
            ts_naive = ts.tz_localize(None) if ts.tzinfo is not None else ts
            # Фильтр уровней по EMA — ДО стратегии: BOUNCE получает уже
            # отфильтрованные зоны (уже живые вотчеры фильтр не трогает).
            allowed_supports, allowed_resistances = level_filter.apply(ts, supports, resistances)
            candle_supports = [z for z in allowed_supports
                               if level_active_from[id(z)] is None or level_active_from[id(z)] <= ts_naive]
            candle_resistances = [z for z in allowed_resistances
                                  if level_active_from[id(z)] is None or level_active_from[id(z)] <= ts_naive]

            orders, draw_events = bounce_mgr.process_candle(
                c_low, c_high, c_close, candle_supports, candle_resistances, df_slice,
                allow_long=allow_long, allow_short=allow_short, c_atr=c_atr, coin=coin, c_adx=c_adx
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
                source_label = f"{strategy}_{tt}" + (f"_{mode}" if tt == "SHORT" and mode else "")
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
                    "method": d.get("method") or ("volume" if d.get("volume") is not None else None),
                    "method_value": d.get("method_value", d.get("volume")),
                    "method_mult": d.get("method_mult", d.get("volume_mult")),
                    "method_ratio": d.get("method_ratio"),
                    "pattern_route": d.get("pattern_route"),
                    "level_type": d.get("level_type") or d.get("type"),
                    # Уровень + путь вотчера на момент сделки — в сам сигнал
                    # (см. _signal_level_snapshot / history.py::save_signal).
                    **_signal_level_snapshot(bounce_mgr._watchers.get(level_id_val)),
                })

                reports.append(
                    f"{emoji} *{strategy} {tt}*{' 🪦' if d.get('reborn') else ''} _{coin}_\n\n"
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


def check_volume_triggers(coin):
    """
    Ручные объёмные триггеры (custom_levels.json[coin]["volume_triggers"],
    добавляются через модалку ➕ -> "Объем (Volume)" на дашборде,
    см. app.py::add_custom_level).

    СОЗНАТЕЛЬНО не похоже на check_bounce/check_v и т.д. — тут нет
    ни зоны (min/max), ни стратегии, ни TP/SL, ни менеджеров.
    Просто: объём последней ЗАКРЫТОЙ 15m свечи >= target
    -> кладём "ручного вотчера" в MANUAL_WATCHERS_FILE (напоминание для
    дашборда, живёт, пока пользователь сам не нажмёт ❌ в панели "В работе",
    см. app.py::get_active_watchers/delete_watchers) и УДАЛЯЕМ сработавший
    триггер из custom_levels.json — капкан одноразовый, иначе плодил бы
    дубли на каждой следующей объёмной свече.
    """
    try:
        custom_db = load_json(CUSTOM_LEVELS_FILE, default={})
        coin_data = custom_db.get(coin) or {}
        triggers = coin_data.get("volume_triggers") or []
        if not triggers:
            return

        markets = load_markets_cached(exchange)
        symbol = resolve_symbol(coin, markets)
        if not symbol:
            return

        # min_candles=2 — нужна ровно одна закрытая свеча, вторая просто
        # чтобы было из чего выбрать (_fetch_candles_df сама подстрахуется
        # походом на биржу, если локальной базы ещё нет вообще).
        df, _fetch_err = _fetch_candles_df(symbol, coin, 2, "VOL_TRIGGER")
        if df is None or df.empty:
            return

        last_ts = df.index[-1]
        last_volume = float(df["volume"].iloc[-1])

        fired = []
        remaining = []
        for trig in triggers:
            try:
                target = float(trig.get("target"))
            except (TypeError, ValueError):
                remaining.append(trig)  # битая запись — не теряем молча, просто не трогаем
                continue
            if last_volume >= target:
                fired.append(target)
            else:
                remaining.append(trig)

        if not fired:
            return

        for target in fired:
            _record_manual_watcher(coin, target, last_volume, last_ts)

        # Перечитываем прямо перед записью (не переиспользуем custom_db из
        # начала функции) — между чтением в начале и этим моментом был
        # сетевой поход за свечами, за это время пользователь мог
        # добавить/удалить ДРУГОЙ триггер этой же монеты с дашборда.
        # Перезаписываем только volume_triggers этой монеты.
        fresh_db = load_json(CUSTOM_LEVELS_FILE, default={})
        if coin in fresh_db:
            fresh_db[coin]["volume_triggers"] = remaining
            if not fresh_db[coin].get("supports") and not fresh_db[coin].get("resistances") and not remaining:
                fresh_db.pop(coin, None)
            save_json_atomic(CUSTOM_LEVELS_FILE, fresh_db)

    except Exception as e:
        print(f"\n[VOL_TRIGGER ERROR] ❌ {coin}: {e}")
        traceback.print_exc()


def _record_manual_watcher(coin, target, fired_volume, fired_ts):
    """Добавляет одну запись в MANUAL_WATCHERS_FILE — см. check_volume_triggers
    выше. level_id в своём неймспейсе ("VOL_...") — не пересекается ни с
    "BC_"/"VB_"/"VGB_"/"VRT_" (см. _STRATEGY_BY_TAG в background_tasks.py),
    ни с чем в bounce_mgr/v_bottom_mgr."""
    entries = load_json(MANUAL_WATCHERS_FILE, default=[])
    if not isinstance(entries, list):
        entries = []
    entries.append({
        "level_id": f"VOL_{coin}_{uuid.uuid4().hex[:8]}",
        "coin": coin,
        "strategy": "VOL_SPIKE",
        "target_volume": target,
        "fired_volume": fired_volume,
        "fired_at": _utc_iso(fired_ts),
        "created_at": datetime.datetime.now().isoformat(),
    })
    save_json_atomic(MANUAL_WATCHERS_FILE, entries)


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
        "level_reaction_count": getattr(watcher, "level_reaction_count", None),
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


def rescan_single_bounce_watcher(coin, level_id, bounce_mgr, strategy="BOUNCE"):
    """Рескан ОДНОГО живого вотчера (кнопка 🎯 и "рескан всех").
    strategy — как в check_bounce: "BOUNCE" или "BUYBACK" (имя в сигнале).

    Зачем: пересчитать путь вотчера с нуля по свечам — если его состояние
    расходится с реальностью (после сбоя, остановки бота, смены правил).

    Как:
    1. Берём зону вотчера и её направление — НАПРАВЛЕНИЕ НЕ ПЕРЕСМАТРИВАЕТСЯ.
    2. Отматываем назад: от activated_at, но не дальше RESCAN_MAX_LOOKBACK_DAYS.
    3. Находим первое касание зоны с этой стороны (LONG — цена опустилась до
       верха зоны, SHORT — поднялась до низа). Касания не было -> NOT_TOUCHED,
       вотчер не трогаем.
    4. Сбрасываем вотчера и проигрываем свечи от касания обычными правилами
       BounceWatcher (пробой середины, вход, смерть...) — до сделки или смерти.
       Дальше не идём: следующая "жизнь" зоны — дело живого бота (кладбище).
    5. Сделка -> в сигналы (уже записанные не дублируются), статус считается
       по свечам.

    is_focus в реплее всегда True (соседей не реплеим параллельно).
    НЕ трогает bounce_mgr.last_processed_time[coin].

    Возвращает {"error": ...} либо {"level_id", "coin", "activated_at", "since",
    "final_state", "direction", "orders", "event_log", ...}.
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
    direction = orig.trade_type
    mode = getattr(orig, 'mode', None)
    if direction == 'SHORT' and not mode:
        mode = 'MIRROR'
    level = {
        'min': zmin, 'max': zmax,
        'type': getattr(orig, 'level_type', 'UNKNOWN'),
        'score': getattr(orig, 'level_score', 0),
        'reaction_count': getattr(orig, 'level_reaction_count', None),
        'date': getattr(orig, 'level_date', None),
        'activated_at': getattr(orig, 'activated_at', None),
    }

    # Первое касание зоны со стороны вотчера
    touched = candidate_df['low'] <= zmax if direction == 'LONG' else candidate_df['high'] >= zmin
    if not touched.any():
        return {
            "level_id": level_id, "coin": coin,
            "activated_at": since_dt.strftime('%Y-%m-%d %H:%M'),
            "since": since_dt.strftime('%Y-%m-%d %H:%M'),
            "clamped_to_available_history": clamped,
            "final_state": "NOT_TOUCHED", "direction": direction,
            "orders": [], "event_log": [],
            "note": "Касания зоны на отрезке нет.",
        }
    start_ts = touched[touched].index[0]

    from backend.modules.cryptano.strategy.bounce_watcher import BounceWatcher
    max_hold_days = BounceWatcher.CONFIG.get("MAX_HOLD_DAYS", 14)

    # Все зоны монеты (macro + custom) — "противоположная сторона" по ПОЛОЖЕНИЮ:
    # для LONG — зоны выше этой, для SHORT — ниже.
    _m = get_merged_levels_for_coin(coin) or {}
    all_zones = list(_m.get("supports", [])) + list(_m.get("resistances", []))

    existing_signal_keys = {
        (s.get("level_id"), s.get("time"))
        for s in load_json(SIGNALS_FILE, default=[])
        if isinstance(s, dict)
    }

    life_lid = bounce_mgr._level_id(level, direction)
    if direction == 'SHORT':
        life_lid = f"{life_lid}__{mode}"

    # Лимит сделок на уровень (bounce_mgr.level_trades): рескан проигрывает
    # историю зоны заново с since_ts — сделки этой зоны с этого момента
    # забываем, рескан найдёт их сам.
    _base_long = bounce_mgr._level_id(level, 'LONG')
    _base_short = bounce_mgr._level_id(level, 'SHORT')
    bounce_mgr.forget_level_trades_since(
        [_base_long, _base_short, f"{_base_short}__CLIMAX", f"{_base_short}__MIRROR"],
        int(since_ts.timestamp()),
    )

    atr_series = calculate_atr(df)
    adx_series = calculate_adx(df)
    from backend.modules.cryptano.simulator.outcome import make_ema_dist_fn, load_candles_4h
    bounce_mgr.set_ema_dist_fn(coin, make_ema_dist_fn(df, candles_4h=load_candles_4h(candle_store, symbol)))  # EMA для отчёта (ema_dist_birth); рескан уже существующего вотчера фильтром не режется

    w = bounce_mgr._watchers.get(life_lid)
    if w is not None:
        w.reset_for_rescan()

    WINDOW = 60
    df_index = df.index
    orders = []

    for ts, row in candidate_df[candidate_df.index >= start_ts].iterrows():
        ts_n = ts.tz_localize(None) if ts.tzinfo is not None else ts
        pos = df_index.searchsorted(ts, side="right")
        df_slice = df.iloc[max(0, pos - WINDOW):pos]
        if len(df_slice) < 52:
            continue
        c_atr = float(atr_series.loc[ts]) if ts in atr_series.index else 0.0
        c_adx = float(adx_series.loc[ts]) if ts in adx_series.index else None

        _active = lambda z: (lambda t: t is None or t <= ts_n)(_level_active_from(z))
        if direction == 'LONG':
            opp_side = [z for z in all_zones if z['min'] > zmax and _active(z)]
        else:
            opp_side = [z for z in all_zones if z['max'] < zmin and _active(z)]

        # evaluate_bounce сам создаёт вотчер этой стороны, если его ещё нет
        # (born_at = время этой свечи, activated_at — из уровня).
        decision = bounce_mgr.evaluate_bounce(
            level, df_slice, direction, opp_side,
            is_focus=True, c_atr=c_atr, c_adx=c_adx,
            mode=mode, coin=coin,
        )
        w = bounce_mgr._watchers.get(life_lid)

        if decision.get('allow'):
            entry, sl, tp = decision.get('entry_price', 0.0), decision.get('sl', 0.0), decision.get('tp', 0.0)
            orders.append({'time': ts_n.isoformat(), 'direction': direction,
                           'entry': entry, 'sl': sl, 'tp': tp, 'reason': decision.get('reason', '')})
            status, result_percent, closed_at = _resolve_historical_signal_status(
                df, ts, direction, entry, tp, sl, max_hold_days
            )
            sig_key = (life_lid, int(ts.timestamp()))
            if sig_key not in existing_signal_keys:
                existing_signal_keys.add(sig_key)
                source_label = f"{strategy}_{direction}" + (f"_{mode}" if direction == "SHORT" and mode else "")
                save_signal({
                    "type": "SHORT_PUMP" if direction == "SHORT" else "WATCHER_LONG",
                    "coin": coin,
                    "source": source_label,
                    "price": entry,
                    "take_profit": tp,
                    "stop_loss": sl,
                    "time": int(ts.timestamp()),
                    "level_id": life_lid,
                    "method": decision.get("method") or ("volume" if decision.get("volume") is not None else None),
                    "method_value": decision.get("method_value", decision.get("volume")),
                    "method_mult": decision.get("method_mult", decision.get("volume_mult")),
                    "method_ratio": decision.get("method_ratio"),
                    "pattern_route": decision.get("pattern_route"),
                    "level_type": decision.get("level_type") or level.get("type"),
                    "status": status,
                    "result_percent": result_percent,
                    "closed_at": closed_at,
                    **_signal_level_snapshot(w),
                })

        if w is not None and w.state in ("TRIGGERED", "DEAD"):
            break  # путь вотчера закончен

    final_w = bounce_mgr._watchers.get(life_lid)
    if final_w is not None:
        _ls = start_ts.tz_localize(None) if start_ts.tzinfo is not None else start_ts
        final_w.born_at = _ls.strftime('%Y-%m-%dT%H:%M:%S')

    return {
        "level_id": life_lid,
        "coin": coin,
        "activated_at": since_dt.strftime('%Y-%m-%d %H:%M'),
        "since": (start_ts.tz_localize(None) if start_ts.tzinfo is not None else start_ts).strftime('%Y-%m-%d %H:%M'),
        "clamped_to_available_history": clamped,
        "final_state": final_w.state if final_w is not None else orig.state,
        "direction": direction,
        "orders": orders,
        "event_log": list(getattr(final_w, 'event_log', []) if final_w is not None else []),
    }