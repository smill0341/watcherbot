import time
import datetime
import os
import threading
import pandas as pd
import schedule
from backend.modules.cryptano.utils.bybit import exchange, resolve_symbol, KNOWN_TICKER_ALIASES
from backend.modules.cryptano.utils.storage import load_json, save_json_atomic
from backend.modules.cryptano.levels.levels_builder import build_levels, MACRO_LAYER_STATS

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

# BASE_DIR указывает на backend/, где лежит config.json.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.modules.cryptano.utils.paths import MACRO_LEVELS_FILE, CUSTOM_LEVELS_FILE

# Списки Потока А (боевой)/Потока Б (радар) — whitelist/blacklist/greylist.
# Путь строим от MACRO_LEVELS_FILE (тот же jsonbank), не трогая
# utils/paths.py лишний раз ради четырёх констант (тот же приём, что уже
# использован для MANUAL_WATCHERS_FILE в watcher_plan.py).
_JSONBANK_DIR = os.path.dirname(MACRO_LEVELS_FILE)
WHITELIST_FILE = os.path.join(_JSONBANK_DIR, "whitelist.json")
BLACKLIST_FILE = os.path.join(_JSONBANK_DIR, "blacklist.json")
GREYLIST_FILE = os.path.join(_JSONBANK_DIR, "greylist.json")
GREYLIST_LEVELS_FILE = os.path.join(_JSONBANK_DIR, "greylist_levels.json")

# Локальная SQLite-база свечей — общая для дашборда и сканера.
import backend.modules.cryptano.levels.candle_store as candle_store

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
_CONFIG_FILE = os.path.normpath(os.path.join(BASE_DIR, "config.json"))
_config = load_json(_CONFIG_FILE, default={})
MIN_VOLUME_USD = _config.get("crypto", {}).get("min_volume_usd", 10_000_000)
MAX_COINS = _config.get("crypto", {}).get("max_coins", 80)

# 🔭 Поток Б (радар) — свой потолок числа кандидатов (не путать с MAX_COINS,
# который теперь ни на что не влияет в этом файле — см. get_battle_symbols)
# и свой срок "протухания" (короче, чем MACRO_STALE_DAYS: кандидат, который
# перестал проходить порог объёма, должен исчезнуть из греylist быстрее,
# чем боевая монета из macro_levels.json — это просто витрина, не позиция).
GREYLIST_MAX_COINS = _config.get("crypto", {}).get("greylist_max_coins", 50)
GREYLIST_STALE_DAYS = _config.get("crypto", {}).get("greylist_stale_days", 4)

def _prev_registry(base, coin):
    """Реестр зон монеты с прошлого пересчёта (хранится в той же записи монеты, ключ "registry")."""
    entry = base.get(coin)
    return entry.get("registry") if isinstance(entry, dict) else None


def _coin_entry(levels, scan_time):
    """Запись монеты для macro_levels.json / greylist_levels.json: уровни + реестр для следующего пересчёта."""
    return {
        "supports": levels["supports"],
        "resistances": levels["resistances"],
        "registry": levels.get("registry"),
        "updated_at": scan_time,
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


def _fetch_and_build_levels(symbol, coin, registry=None):
    """Общий для боевого потока (build_macro_levels, build_levels_for_single_coin)
    и радара (build_greylist_candidates) кусок: скачать 1M/1W/1d/4h свечи
    (через _safe_fetch_ohlcv — сначала локальная БД, потом биржа) и
    построить зоны через build_levels() (одна точка для всех трёх мест).

    registry — реестр зон монеты с прошлого пересчёта (см. levels_builder.build_levels).
    Возвращает (levels, df_1d) — levels с ключом "registry" для следующего пересчёта —
    или None, если по монете меньше 50 дневных свечей. Сохранением не занимается —
    куда положить результат, решает вызывающий код (macro_levels.json или greylist_levels.json)."""
    _t0 = time.time()
    ohlcv_1M = _safe_fetch_ohlcv(symbol, "1M", 60)
    ohlcv_1W = _safe_fetch_ohlcv(symbol, "1W", 150)
    ohlcv_1d = _safe_fetch_ohlcv(symbol, "1d", 365)
    ohlcv_4h = _safe_fetch_ohlcv(symbol, "4h", 200)
    _FETCH_TIME["candles"] += time.time() - _t0

    if len(ohlcv_1d) < 50:
        return None

    df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1M) >= 5 else None
    df_1W = pd.DataFrame(ohlcv_1W, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1W) >= 5 else None
    df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_4h) >= 50 else None

    _t1 = time.time()
    levels = build_levels(df_1M, df_1W, df_1d, df_4h, coin, registry=registry, return_registry=True)
    _FETCH_TIME["levels"] += time.time() - _t1

    return levels, df_1d


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


def get_battle_symbols():
    """Поток А (боевой). ЗАМЕНЯЕТ прежний отбор "топ-N по объёму"
    (get_top_symbols() выше остаётся БЕЗ ИЗМЕНЕНИЙ — её отдельно
    использует precalc_for_bot.py/bulk-симулятор для СВОЕЙ, исторической
    задачи, ломать её нельзя). Источник теперь только whitelist.json +
    монеты с ручными зонами (custom_levels.json, через уже готовую
    _add_manual_zone_coins) — объём здесь больше вообще не участвует в
    отборе, дальше объём решает только Поток Б (радар, см.
    build_greylist_candidates), какие кандидаты вообще ПОКАЗАТЬ.

    Blacklist — абсолютный приоритет: даже если тикер оказался в
    whitelist.json ПО ОШИБКЕ (руками), а сам при этом в blacklist.json,
    блэклист побеждает — монета не попадёт в бой ни через whitelist, ни
    через ручную зону."""
    if not exchange.markets:
        exchange.load_markets(reload=False)
    markets = exchange.markets or {}

    whitelist = load_json(WHITELIST_FILE, default=[])
    if not isinstance(whitelist, list):
        whitelist = []

    valid_symbols = []
    seen = set()
    for coin in whitelist:
        coin = str(coin).upper().strip()
        if not coin:
            continue
        symbol = resolve_symbol(coin, markets) or resolve_symbol(KNOWN_TICKER_ALIASES.get(coin, ""), markets)
        if not symbol or symbol in seen:
            continue
        valid_symbols.append(symbol)
        seen.add(symbol)

    print(f"⚪ [БОЙ] Whitelist: {len(valid_symbols)}/{len(whitelist)} монет(ы) резолвнулись в реальный символ на бирже.")

    manual_added = _add_manual_zone_coins(valid_symbols)
    if manual_added:
        print(f"✋ [БОЙ] Монет с ручными зонами добавлено сверх whitelist: {manual_added}")

    blacklist = load_json(BLACKLIST_FILE, default=[])
    blacklist_set = {str(c).upper().strip() for c in blacklist} if isinstance(blacklist, list) else set()
    if blacklist_set:
        before = len(valid_symbols)
        valid_symbols = [s for s in valid_symbols if s.split("/")[0].replace(":USDT", "") not in blacklist_set]
        removed = before - len(valid_symbols)
        if removed:
            print(f"⛔ [БОЙ] Blacklist перекрыл {removed} монет(у) даже из whitelist/ручных зон.")

    return valid_symbols


def build_greylist_candidates(exclude_symbols):
    """Поток Б (радар). exclude_symbols — то, что уже целиком обработал
    Поток А (get_battle_symbols(), уже прогнан через blacklist) — эти
    монеты радару смотреть незачем, они и так боевые.

    Отбор кандидата такой же, как в get_top_symbols() (тикеры + объём >=
    MIN_VOLUME_USD), но БЕЗ отсечки "топ-N по объёму, дальше не смотрим" —
    вместо неё явный blacklist (которого в get_top_symbols() нет вообще —
    там просто топ по объёму без разбора) и свой, отдельный потолок
    GREYLIST_MAX_COINS.

    Кандидаты, честно прошедшие фильтр, получают полный расчёт уровней —
    ту же build_levels() с реестром, что и боевые —
    но результат уходит ТОЛЬКО в greylist_levels.json. macro_levels.json
    этот проход не трогает вообще — watcher_plan.py физически не видит
    ни один из этих уровней."""
    if not exchange.markets:
        exchange.load_markets(reload=False)
    tickers = exchange.fetch_tickers()

    blacklist = load_json(BLACKLIST_FILE, default=[])
    blacklist_set = {str(c).upper().strip() for c in blacklist} if isinstance(blacklist, list) else set()
    exclude_set = set(exclude_symbols)

    candidates = []
    for sym, tick in tickers.items():
        if not sym.endswith(':USDT') or sym in exclude_set:
            continue
        coin = sym.split("/")[0].replace(":USDT", "")
        if coin in blacklist_set:
            continue
        vol = float(tick.get('quoteVolume') or 0)
        if vol >= MIN_VOLUME_USD:
            candidates.append((sym, coin, vol))

    candidates.sort(key=lambda x: x[2], reverse=True)
    candidates = candidates[:GREYLIST_MAX_COINS]
    print(f"🔭 [РАДАР] Кандидатов сверх боевого списка и blacklist: {len(candidates)} (потолок {GREYLIST_MAX_COINS}).")

    greylist = load_json(GREYLIST_FILE, default={})
    if not isinstance(greylist, dict):
        greylist = {}
    greylist_levels = load_json(GREYLIST_LEVELS_FILE, default={})
    if not isinstance(greylist_levels, dict):
        greylist_levels = {}

    scan_time = datetime.datetime.now().isoformat()
    seen_coins = set()

    for sym, coin, vol in candidates:
        seen_coins.add(coin)  # реально дошли до расчёта — точка отсчёта для "протухания"
        net_before = _network_calls()
        try:
            result = _fetch_and_build_levels(sym, coin, _prev_registry(greylist_levels, coin))
            if result is None:
                continue
            levels, df_1d = result

            has_any = (levels["supports"] or levels["resistances"])
            if has_any:
                greylist_levels[coin] = _coin_entry(levels, scan_time)
            else:
                greylist_levels.pop(coin, None)

            existing = greylist.get(coin) or {}
            greylist[coin] = {
                "symbol": sym,
                "volume": vol,
                "discovered_at": existing.get("discovered_at", scan_time),
                "last_seen": scan_time,
            }
        except Exception as e:
            print(f"[RADAR ERROR] ❌ {coin}: {e}")
            continue
        finally:
            # Пауза ТОЛЬКО если реально ходили на биржу за этой монетой —
            # тот же приём, что и в основном боевом цикле (build_macro_levels).
            if _network_calls() > net_before:
                time.sleep(0.3)

    # Протухшие кандидаты — не встречены радаром GREYLIST_STALE_DAYS подряд
    # (объём упал ниже порога, монета попала в exclude_symbols раз уже стала
    # боевой/ручной, или вообще пропала с биржи). Порог короче, чем у
    # macro_levels.json (MACRO_STALE_DAYS) — это витрина, не позиция.
    cutoff = datetime.datetime.now() - datetime.timedelta(days=GREYLIST_STALE_DAYS)
    stale = []
    for coin, entry in list(greylist.items()):
        if coin == "_meta" or coin in seen_coins:
            continue
        last_seen = entry.get("last_seen") if isinstance(entry, dict) else None
        try:
            if not last_seen or datetime.datetime.fromisoformat(last_seen) < cutoff:
                stale.append(coin)
        except Exception:
            stale.append(coin)  # битая/непонятная дата — тоже протухшая
    for coin in stale:
        greylist.pop(coin, None)
        greylist_levels.pop(coin, None)
    if stale:
        print(f"🧹 [РАДАР] Убраны протухшие кандидаты (>{GREYLIST_STALE_DAYS} дн. не видели): {stale}")

    greylist_levels["_meta"] = {
        "last_build": scan_time,
        "coins_count": len([k for k in greylist_levels if k != "_meta"]),
    }

    save_json_atomic(GREYLIST_FILE, greylist)
    save_json_atomic(GREYLIST_LEVELS_FILE, greylist_levels)
    print(f"✅ [РАДАР] Готово. В сером списке: {len([k for k in greylist if k != '_meta'])} монет.")


def build_macro_levels():
    print(f"[SWING HUNTER] Запуск генерации зон интереса...")
    _fetch_stats_reset()
    scan_started_at = time.time()
    try:
        valid_symbols = get_battle_symbols()

        # МЕРДЖ вместо полной перезаписи: если по монете из whitelist в этот
        # раз не удалось сходить на биржу (сбой fetch_tickers и т.п.) — её
        # старая запись остаётся как есть, а не пропадает из файла. Иначе
        # вотчер, который на неё завязан, замирает (check_v_* видит
        # coin_macro=None и выходит рано) и на дашборде превращается в "?",
        # хотя формально ещё жив в памяти — см. cleanup_ghost_watchers.py и
        # обсуждение бага. Монеты, которых нет в whitelist ВООБЩЕ (явно
        # убрали руками), мердж не защищает — см. чистку ниже.
        macro_base = load_json(MACRO_LEVELS_FILE, default={})

        # Монеты, которые попали в valid_symbols НЕ через whitelist.json, а
        # только ручной зоной (custom_levels.json, см. _add_manual_zone_coins) —
        # например, серая монета, которой явно нарисовали зону, но в
        # whitelist так и не занесли. Для таких — никакого авто-анализа
        # радара: вотчер должен заходить строго по ручной зоне, а не по
        # тому, что тут насчитает технический анализ. Пустая заглушка
        # {"supports": [], "resistances": []} — реальные уровни возьмутся
        # из custom_levels.json через уже существующий мердж в
        # watcher_plan.py::get_merged_levels_for_coin. Заглушка нужна как
        # сам ключ в macro_levels.json — иначе монету не подхватит синк в
        # watchlist.json (background_tasks.py) и её снесёт чистка "не в
        # бою" ниже, как будто она вообще выпала из valid_symbols.
        whitelist_raw = load_json(WHITELIST_FILE, default=[])
        whitelist_coins = {str(c).upper().strip() for c in whitelist_raw} if isinstance(whitelist_raw, list) else set()

        for symbol in valid_symbols:
            coin = symbol.split("/")[0].replace(":USDT", "")

            if coin not in whitelist_coins:
                existing = macro_base.get(coin) or {}
                macro_base[coin] = {
                    "supports": [],
                    "resistances": [],
                    "updated_at": existing.get("updated_at") or datetime.datetime.now().isoformat(),
                }
                continue

            # Пауза 0.3 сек — защита от rate-limit биржи. Нужна ТОЛЬКО если
            # по этой монете реально ходили на биржу (докачка хвоста/полная
            # докачка/фолбэк). Если всё взялось из свежей базы — пауза не
            # нужна, она раньше просто добавляла ~30 сек на 100 монет.
            net_before = _network_calls()
            try:
                # === АНАЛИЗ через новый levels_builder ===
                # Скачивание свечей + build_levels() — общий кусок, вынесен
                # в _fetch_and_build_levels (см. её докстринг) — тот же он
                # переиспользуется build_levels_for_single_coin() и радаром
                # (build_greylist_candidates).
                result = _fetch_and_build_levels(symbol, coin, _prev_registry(macro_base, coin))
                if result is None:
                    continue
                levels, df_1d = result

                has_any = (levels["supports"] or levels["resistances"])
                if has_any:
                    macro_base[coin] = _coin_entry(levels, datetime.datetime.now().isoformat())
                else:
                    # Явно пересчитали и уровней не нашли — это не "выпал из
                    # топ-70", а честный пустой результат, тут можно смело убрать.
                    macro_base.pop(coin, None)

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

        # Чистим записи монет, которых больше нет в бою. Раньше (когда
        # источником был "топ-70 по объёму", менявшийся день ото дня сам по
        # себе) тут был отсчёт в днях (MACRO_STALE_DAYS) — монета не должна
        # была пропадать из-за одного случайного выпадения из топа. Теперь
        # источник — whitelist.json, который меняется только руками
        # (whitelist/demote), поэтому "выпадение из боя" — это не шум,
        # а явное решение, и ждать несколько дней, чтобы его отразить,
        # незачем: монета, которой нет в valid_symbols (whitelist,
        # прошедший resolve_symbol, + ручные зоны — см. get_battle_symbols/
        # _add_manual_zone_coins), убирается из macro_levels.json сразу же.
        #
        # Монеты ИЗ valid_symbols, у которых fetch просто не удался в этом
        # конкретном прогоне (сбой биржи и т.п.) — по-прежнему не трогаем,
        # их старые уровни остаются (см. merge выше, macro_base[coin]
        # перезаписывается только при успешном result).
        battle_coins = {sym.split("/")[0].replace(":USDT", "") for sym in valid_symbols}
        stale_coins = [
            coin for coin in macro_base
            if coin != "_meta" and coin not in battle_coins
        ]
        for coin in stale_coins:
            del macro_base[coin]
        if stale_coins:
            print(f"🧹 Убраны из боя (нет в whitelist/ручных зонах): {stale_coins}")

        # 🕒 Метаданные всего файла — чтобы одним взглядом видеть, когда последний раз обновлялось
        macro_base["_meta"] = {
            "last_build": datetime.datetime.now().isoformat(),
            "coins_count": len([k for k in macro_base if k != "_meta"]),
        }

        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        print(f"✅ [SWING HUNTER] Сбор завершен! Зоны сохранены: {MACRO_LEVELS_FILE}")
        _fetch_stats_report(total_sec=time.time() - scan_started_at)

        # ⚠️ Раньше тут писался снимок в историю уровней (levels_history.py::
        # save_levels_snapshot) "для рескана" — но check_bounce давно уже НЕ
        # читает историю вообще (см. комментарий в watcher_plan.py::
        # check_bounce: "боевой бот туда не смотрит", там же объяснение,
        # почему раньше это было даже вредно — бот часами работал по старым
        # уровням из 12-часового снимка). История уровней нужна ТОЛЬКО
        # симулятору, и теперь заполняется ТОЛЬКО вручную через
        # precalc_for_bot.py, когда сам решишь — бой в неё больше не пишет.

        # 🔭 Поток Б (радар) — тем же прогоном, сразу после боевого потока
        # (общий fetch_tickers дешевле, чем два отдельных прохода по
        # расписанию). Отдельный try/except: сбой радара не должен стоить
        # уже посчитанного и сохранённого боевого результата (macro_base
        # уже сохранён и заснапшочен строками выше).
        try:
            build_greylist_candidates(valid_symbols)
        except Exception as e:
            print(f"❌ [РАДАР] КРИТИЧЕСКАЯ ОШИБКА: {e}")

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

        macro_base = load_json(MACRO_LEVELS_FILE, default={})
        result = _fetch_and_build_levels(symbol, coin, _prev_registry(macro_base, coin))
        if result is None:
            return None
        levels, df_1d = result

        macro_base[coin] = _coin_entry(levels, datetime.datetime.now().isoformat())
        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        # ⚠️ Снимок в историю уровней больше не пишем — см. комментарий в
        # build_macro_levels() выше, история уровней теперь только через
        # precalc_for_bot.py вручную, бой её не читает и не пишет.

        return levels
    except Exception as e:
        print(f"[HUNTER ERROR] Ошибка при построении уровней для {coin}: {e}")
        return None

def run_heavy_generator():
    """Фоновый поток для генерации уровней по расписанию."""
    schedule.every().day.at(TIME_ASIAN_CLOSE).do(build_macro_levels)
    schedule.every().day.at(TIME_US_OPEN).do(build_macro_levels)
    while True:
        schedule.run_pending()
        time.sleep(1)

def start_swing_hunter():
    """Инициализация Swing Hunter: запускает фоновые потоки, не блокируя старт бота."""
    threading.Thread(target=run_heavy_generator, daemon=True).start()
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