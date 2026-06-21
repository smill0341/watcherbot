import os
import sys

# 1. СНАЧАЛА ЖЕСТКО УКАЗЫВАЕМ ПУТЬ К КОРНЮ ПРОЕКТА
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "../../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import time
import datetime
import os
import threading
import pandas as pd
import schedule  
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.utils.testswing.levels_builder import build_levels

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

def build_macro_levels(bot=None, admin_chat_id=None, target_time_str=None):
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
                ohlcv_1d = safe_fetch_ohlcv(symbol, "1d", 365, fetch_params)
                ohlcv_4h = safe_fetch_ohlcv(symbol, "4h", 200, fetch_params)

                if len(ohlcv_1d) < 50:
                    continue

                df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"]) if len(ohlcv_4h) >= 50 else None

                levels = build_levels(df_1d, df_4h, coin)

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
        print(f"✅ [TEST HUNTER] Сбор завершен! Создан файл: {MACRO_LEVELS_FILE}")
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