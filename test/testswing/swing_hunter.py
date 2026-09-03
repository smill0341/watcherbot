import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. ПУТЬ К ОСНОВНОМУ БОТУ (D:\bot\master_bot) - чтобы работал импорт из modules
MASTER_BOT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../master_bot"))
if MASTER_BOT_ROOT not in sys.path:
    sys.path.insert(0, MASTER_BOT_ROOT)

# 2. ПУТЬ К ПАПКЕ TEST (D:\bot\test) - чтобы работал импорт from testswing...
TEST_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../"))
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

import time
import datetime
import threading
import pandas as pd
import schedule  
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json, save_json_atomic
from testswing.levels_builder import build_levels

# =========================================================
# ⚙️ НАСТРОЙКИ РАСПИСАНИЯ И ТЕСТА
# =========================================================
TEST_TIME = "2026-06-21 22:00:00"  

TIME_ASIAN_CLOSE = "03:05"
TIME_US_OPEN = "15:05"      

# 🧪 ТУМБЛЕР МАШИНЫ ВРЕМЕНИ (Укажи дату для теста, или None для лайва)
BACKTEST_DATE = None  # Например: "2026-04-01 00:00:00" или None для текущего времени
# =========================================================

# 🎛 НАСТРОЙКИ V2 ФИЛЬТРОВ
IMPULSE_ATR_MULTIPLIER = 2.5  # Цена должна улететь минимум на 2.5 ATR от зоны
IMPULSE_LOOKAHEAD_DAYS = 10   # Даем цене 10 дней на то, чтобы показать этот импульс

# Теперь BASE_DIR указывает прямо на modules/cryptano/utils/testswing/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# JSON файлы бэктеста будут создаваться прямо в этой папке
if BACKTEST_DATE:
    MACRO_LEVELS_FILE = os.path.join(BASE_DIR, f"macro_test_{BACKTEST_DATE[:10]}.json")
else:
    MACRO_LEVELS_FILE = os.path.join(BASE_DIR, "macro_levels.json")

WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")

LIGHT_RADAR_INTERVAL_SEC = 60
MIN_VOLUME_USD = 10_000_000

_hunter_lock = threading.Lock()

def get_top_symbols():
    """Один раз считает топ-70 по объёму — вынесено отдельно, чтобы кэш-режим
    мог зафиксировать список монет один раз на весь прогон precalc.py."""
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


def _safe_fetch_ohlcv(symbol, tf, lim, params=None):
    """Тот же retry-механизм против рейт-лимитов, что был внутри build_macro_levels,
    вынесен наружу, чтобы им мог пользоваться и массовый загрузчик кэша."""
    params = params or {}
    for attempt in range(5):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe=tf, limit=lim, params=params)
        except Exception as e:
            if "10006" in str(e) or "Rate Limit" in str(e) or "Too many visits" in str(e):
                print(f"⚠️ Было слишком много запросов. Bybit ругается. Ждем 4 сек (Попытка {attempt+1}/5)...")
                time.sleep(4.0)
            else:
                raise e
    raise Exception("Биржа наглухо заблокировала запросы по Rate Limit после 5 попыток.")


def build_full_history_cache(valid_symbols, extra_candles=1500):
    """Качает ВСЮ доступную историю по каждой монете и таймфрейму ОДИН раз
    (без endTime — просто 'дай всё, что есть'), вместо того чтобы дёргать биржу
    заново под каждую дату в precalc.py. Дальше build_macro_levels(cache=...)
    просто отрезает нужный хвост из уже скачанных DataFrame локально, без сети.

    extra_candles — на сколько 4h-свечей вглубь качать (запас, чтобы у самой
    ранней даты из MONTHS_TO_CALC тоже хватило истории на построение уровней).
    """
    print(f"📥 Качаю полную историю по {len(valid_symbols)} монетам (один раз, без сети под каждую дату)...")
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
            print(f"[CACHE ERROR] Не удалось скачать {coin}: {e}")
            continue
    print(f"✅ Кэш готов: {len(cache)} монет.")
    return cache


def _slice_by_time(df, target_ts_ms, tail_n):
    """Локальный аналог того, что раньше делала биржа через endTime: берём из
    уже скачанного df только свечи ДО target_ts включительно, и последние tail_n
    из них — то есть ровно тот же кусок данных, который раньше присылала биржа
    под конкретный endTime, просто без сетевого запроса."""
    if df is None:
        return None
    sliced = df[df['timestamp'] <= target_ts_ms]
    if sliced.empty:
        return None
    return sliced.tail(tail_n).reset_index(drop=True)


def build_macro_levels(bot=None, admin_chat_id=None, target_time_str=None,
                        cache=None, valid_symbols=None):
    """Если передан cache (см. build_full_history_cache) — работает целиком
    в памяти, без единого сетевого запроса: и список монет, и данные уже готовы.
    Без cache — старое поведение, один в один (сеть под каждый вызов)."""
    if cache is not None:
        macro_base = {}
        use_date = target_time_str or BACKTEST_DATE
        target_ts_ms = None
        if use_date:
            dt_obj = datetime.datetime.strptime(use_date, "%Y-%m-%d %H:%M:%S")
            target_ts_ms = int(dt_obj.timestamp() * 1000)

        for coin, tf_data in cache.items():
            try:
                if target_ts_ms is not None:
                    df_1M = _slice_by_time(tf_data["1M"], target_ts_ms, 60)
                    df_1W = _slice_by_time(tf_data["1W"], target_ts_ms, 150)
                    df_1d = _slice_by_time(tf_data["1d"], target_ts_ms, 365)
                    df_4h = _slice_by_time(tf_data["4h"], target_ts_ms, 200)
                else:
                    # Без даты — используем весь скачанный кэш как есть (лайв-режим)
                    df_1M, df_1W, df_1d, df_4h = tf_data["1M"], tf_data["1W"], tf_data["1d"], tf_data["4h"]

                if df_1d is None or len(df_1d) < 50:
                    continue

                levels = build_levels(df_1M, df_1W, df_1d, df_4h, coin)

                has_any = (levels["supports"] or levels["resistances"])
                if has_any:
                    macro_base[coin] = {
                        "supports": levels["supports"],
                        "resistances": levels["resistances"],
                        "updated_at": datetime.datetime.now().isoformat()
                    }
            except Exception as e:
                print(f"[TEST ERROR] Ошибка для {coin}: {e}")
                continue

        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        return macro_base

    print(f"[TEST HUNTER] Запуск генерации зон в папку {BASE_DIR}...")
    try:
        # Загружаем рынки из сети только один раз, в остальные разы берем из кэша библиотеки
        if not exchange.markets:
            exchange.load_markets(reload=True)
        else:
            exchange.load_markets(reload=False)
        tickers = exchange.fetch_tickers()
        
        # Собираем пары с их объемами для последующей сортировки
        symbols_with_volume = []
        for sym, tick in tickers.items():
            if sym.endswith('/USDT') or sym.endswith(':USDT'):
                vol = float(tick.get('quoteVolume') or 0)
                if vol >= MIN_VOLUME_USD:
                    symbols_with_volume.append((sym, vol))
                    
        # Сортируем по убыванию объема торгов и берем строго ТОП-70
        symbols_with_volume.sort(key=lambda x: x[1], reverse=True)
        valid_symbols = [sym for sym, vol in symbols_with_volume[:70]]
        
        print(f"🔥 Найдено {len(symbols_with_volume)} монет. Фильтруем до ТОП-70 самых ликвидных.")
        macro_base = {}
        
        # Подготовка параметров для исторического среза данных
        fetch_params = {}
        use_date = target_time_str or BACKTEST_DATE 
        if use_date:
            dt_obj = datetime.datetime.strptime(use_date, "%Y-%m-%d %H:%M:%S")
            fetch_params['endTime'] = int(dt_obj.timestamp() * 1000)
        
        # Safe fetcher with retry logic against rate limits
        def safe_fetch_ohlcv(sym, tf, lim, params):
            for attempt in range(5): # 5 попыток пробить блок
                try:
                    return exchange.fetch_ohlcv(sym, timeframe=tf, limit=lim, params=params)
                except Exception as e:
                    if "10006" in str(e) or "Rate Limit" in str(e) or "Too many visits" in str(e):
                        print(f"⚠️ Было слишком много запросов. Bybit ругается. Ждем 4 сек (Попытка {attempt+1}/5)...")
                        time.sleep(4.0)
                    else:
                        raise e
            raise Exception("Биржа наглухо заблокировала запросы по Rate Limit после 5 попыток.")
        
        
        for symbol in valid_symbols:
            time.sleep(0.3)
            coin = symbol.split("/")[0].replace(":USDT", "")

            try:
                # === АНАЛИЗ через новый levels_builder ===
                ohlcv_1M = safe_fetch_ohlcv(symbol, "1M", 60, fetch_params)
                ohlcv_1W = safe_fetch_ohlcv(symbol, "1W", 150, fetch_params)
                ohlcv_1d = safe_fetch_ohlcv(symbol, "1d", 365, fetch_params)
                ohlcv_4h = safe_fetch_ohlcv(symbol, "4h", 200, fetch_params)

                if len(ohlcv_1d) < 50:
                    continue

                df_1M = pd.DataFrame(ohlcv_1M, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1M) >= 5 else None
                df_1W = pd.DataFrame(ohlcv_1W, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_1W) >= 5 else None
                df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_4h) >= 50 else None

                levels = build_levels(df_1M, df_1W, df_1d, df_4h, coin)

                has_any = (levels["supports"] or levels["resistances"])
                if has_any:
                    macro_base[coin] = {
                        "supports": levels["supports"],
                        "resistances": levels["resistances"],
                        "updated_at": datetime.datetime.now().isoformat()
                    }

            except Exception as e:
                print(f"[TEST ERROR] Ошибка для {coin}: {e}")
                continue
                
        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        #print(f"✅ [TEST HUNTER] Сбор завершен! Создан файл: {MACRO_LEVELS_FILE}")
        return macro_base
            
    except Exception as e:
        print(f"❌ [TEST HUNTER] КРИТИЧЕСКАЯ ОШИБКА: {e}")

def minute_radar(bot, admin_chat_id):
    """
    Радар, который сканирует рынок каждую минуту и ждет, 
    пока цена зайдет ВНУТРЬ созданной зоны ликвидности.
    """
    while True:
        time.sleep(LIGHT_RADAR_INTERVAL_SEC)
        try:
            macro_base = load_json(MACRO_LEVELS_FILE, default={})
            if not macro_base: continue 
                
            watchlist = load_json(WATCHLIST_FILE, default={})
            tickers = exchange.fetch_tickers()
            added_coins = []
            
            for symbol, tick in tickers.items():
                if not (symbol.endswith('/USDT') or symbol.endswith(':USDT')): continue
                coin = symbol.split("/")[0].replace(":USDT", "")
                if coin not in macro_base: continue
                    
                current_price = float(tick.get('last') or 0)
                if current_price == 0: continue
                if coin in watchlist: continue
                    
                levels = macro_base[coin]
                is_triggered = False
                trigger_direction = ""
                
                # Проверяем касание именно ЗОНЫ, а не точной линии
                for sup in levels.get("supports", []):
                    # Если цена внутри зоны или на 1% выше нее (на подходе)
                    if sup['min'] <= current_price <= (sup['max'] * 1.01): 
                        is_triggered = True
                        trigger_direction = "LONG"
                        break
                        
                if not is_triggered:
                    for res in levels.get("resistances", []):
                        # Если цена внутри зоны или на 1% ниже нее
                        if (res['min'] * 0.99) <= current_price <= res['max']: 
                            is_triggered = True
                            trigger_direction = "SHORT"
                            break
                            
                if is_triggered:
                    watchlist[coin] = {
                        "direction": trigger_direction,
                        "added_at": datetime.datetime.now().isoformat(),
                        "source": "Swing Hunter"
                    }
                    added_coins.append(coin)
            
            if added_coins:
                save_json_atomic(WATCHLIST_FILE, watchlist)
                print(f"🎯 [SWING HUNTER] Радар: В Watchlist залетело {len(added_coins)} монет. Ждем подтверждения на 15М.")

        except Exception as e:
            pass

def run_heavy_generator(bot, admin_chat_id):
    schedule.every().day.at(TIME_ASIAN_CLOSE).do(build_macro_levels, bot, admin_chat_id)
    schedule.every().day.at(TIME_US_OPEN).do(build_macro_levels, bot, admin_chat_id)
    if TEST_TIME:
        schedule.every().day.at(TEST_TIME).do(build_macro_levels, bot, admin_chat_id)
    while True:
        schedule.run_pending()
        time.sleep(1)

# Находим самый конец файла swing_hunter.py и заменяем код, начиная с def start_swing_hunter...

def start_swing_hunter(bot, admin_chat_id):
    # В режиме теста нам не нужно запускать потоки, просто выполняем расчет
    build_macro_levels()

# ТОЧКА ВХОДА ДЛЯ ПРЯМОГО ЗАПУСКА С КОРНЯ
if __name__ == "__main__":
    print("🚀 [TEST ENGINE] Начинаем принудительный сбор уровней...")
    build_macro_levels()
    print("🏁 [TEST ENGINE] Сбор завершен.")