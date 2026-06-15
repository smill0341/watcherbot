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
import numpy as np
from scipy.signal import find_peaks
import schedule  
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json, save_json_atomic

# =========================================================
# ⚙️ НАСТРОЙКИ РАСПИСАНИЯ И ТЕСТА
# =========================================================
TEST_TIME = "23:16"  

TIME_ASIAN_CLOSE = "03:05"
TIME_US_OPEN = "15:05"      

# 🧪 ТУМБЛЕР МАШИНЫ ВРЕМЕНИ (Укажи дату для теста, или None для лайва)
BACKTEST_DATE = "2026-05-01 16:00:00"  
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

def calculate_atr(df, period=14):
    """Быстрый расчет ATR для вычисления ширины Зоны ликвидности"""
    high_low = df["high"] - df["low"]
    high_cp = (df["high"] - df["close"].shift()).abs()
    low_cp = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def merge_overlapping_zones(zones):
    """
    Математическое слияние пересекающихся зон (Классический алгоритм Interval Merging).
    """
    if not zones:
        return []
        
    # Сортируем зоны снизу вверх по их минимальной границе
    sorted_zones = sorted(zones, key=lambda x: x['min'])
    merged = [sorted_zones[0]]
    
    for current in sorted_zones[1:]:
        last = merged[-1]
        
        # Если зоны пересекаются (текущая начинается до того, как закончилась предыдущая)
        if current['min'] <= last['max']:
            # Расширяем границы склеенной зоны до максимума
            last['max'] = max(last['max'], current['max'])
            
            # Если слились уровни разной силы (например 4H и 1D) - берем максимальный вес
            last['score'] = max(last['score'], current['score'])
            
            # Отмечаем, что зона усилена слиянием разных таймфреймов
            if current['type'] not in last['type']:
                last['type'] = f"{last['type']} + {current['type']}"
        else:
            merged.append(current)
            
    return merged

def build_levels_for_single_coin(coin):
    """Изолированный расчет макро-уровней для одной монеты."""
    print(f"[SWING HUNTER] Расчет институциональных зон для {coin}...")
    try:
        symbol = f"{coin}/USDT"
        supports = []
        resistances = []

        try:
            ohlcv_1d = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=365)
            if len(ohlcv_1d) >= 50:
                df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df_1d['atr'] = calculate_atr(df_1d, 14)
                atr_1d = df_1d['atr'].iloc[-1]
                if pd.isna(atr_1d) or atr_1d == 0: atr_1d = df_1d['close'].iloc[-1] * 0.05

                peaks, _ = find_peaks(df_1d['high'], distance=15, prominence=atr_1d * 1.5)
                valleys, _ = find_peaks(-df_1d['low'], distance=15, prominence=atr_1d * 1.5)

                current_price_1d = float(df_1d['close'].iloc[-1])

                for v in valleys:
                    price = float(df_1d['low'].iloc[v])
                    if abs(price - current_price_1d) / current_price_1d > 0.15:
                        continue

                    zone = {"min": price - (atr_1d * 0.5), "max": price + (atr_1d * 0.5), "score": 3.0, "type": "1d_extreme"}
                    if price < current_price_1d: supports.append(zone)
                    else: resistances.append(zone)

                for p in peaks:
                    price = float(df_1d['high'].iloc[p])
                    if abs(price - current_price_1d) / current_price_1d > 0.15:
                        continue

                    zone = {"min": price - (atr_1d * 0.5), "max": price + (atr_1d * 0.5), "score": 3.0, "type": "1d_extreme"}
                    if price > current_price_1d: resistances.append(zone)
                    else: supports.append(zone)
        except Exception as e:
            print(f"[SWING ERROR] Ошибка 1D для {coin}: {e}")

        try:
            ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=200)
            if len(ohlcv_4h) >= 50:
                df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df_4h['atr'] = calculate_atr(df_4h, 14)
                atr_4h = df_4h['atr'].iloc[-1]
                if pd.isna(atr_4h) or atr_4h == 0: atr_4h = df_4h['close'].iloc[-1] * 0.02

                df_4h['typical'] = (df_4h['high'] + df_4h['low'] + df_4h['close']) / 3
                min_val, max_val = df_4h['low'].min(), df_4h['high'].max()

                if max_val > min_val:
                    bins = np.linspace(min_val, max_val, 50)
                    df_4h['bin'] = pd.cut(df_4h['typical'], bins=bins)
                    vol_profile = df_4h.groupby('bin', observed=False)['volume'].sum()

                    poc_bin = vol_profile.idxmax()
                    if pd.notna(poc_bin) and hasattr(poc_bin, "mid"):
                        poc_price = float(getattr(poc_bin, "mid"))
                        current_price = float(df_4h['close'].iloc[-1])

                        zone = {
                                "min": poc_price - (atr_4h * 0.5),
                                "max": poc_price + (atr_4h * 0.5),
                                "score": 2.0,
                                "type": "4h_poc",
                                "date": "Volume Accumulation (200 candles)"
                            }

                        if current_price > poc_price: supports.append(zone)
                        else: resistances.append(zone)
        except Exception as e:
            print(f"[SWING ERROR] Ошибка 4H для {coin}: {e}")

        if supports or resistances:
            macro_base = load_json(MACRO_LEVELS_FILE, default={})
            macro_base[coin] = {
                "supports": merge_overlapping_zones(supports),
                "resistances": merge_overlapping_zones(resistances),
                "updated_at": datetime.datetime.now().isoformat()
            }
            save_json_atomic(MACRO_LEVELS_FILE, macro_base)
            print(f"✅ [SWING HUNTER] Уровни для {coin} обновлены.")
        else:
            print(f"⚠️ [SWING HUNTER] Нет зон для {coin}.")

    except Exception as e:
        print(f"❌ [SWING HUNTER] КРИТИЧЕСКАЯ ОШИБКА генерации {coin}: {e}")

def build_macro_levels(bot=None, admin_chat_id=None):
    print(f"[TEST HUNTER] Запуск генерации зон в папку {BASE_DIR}...")
    try:
        exchange.load_markets(reload=True)
        tickers = exchange.fetch_tickers()
        valid_symbols = []
        
        for sym, tick in tickers.items():
            if sym.endswith('/USDT') or sym.endswith(':USDT'):
                vol = float(tick.get('quoteVolume') or 0)
                if vol >= MIN_VOLUME_USD:
                    valid_symbols.append(sym)
                    
        macro_base = {}
        
        # Подготовка параметров для исторического среза данных
        fetch_params = {}
        if BACKTEST_DATE:
            dt_obj = datetime.datetime.strptime(BACKTEST_DATE, "%Y-%m-%d %H:%M:%S")
            fetch_params['endTime'] = int(dt_obj.timestamp() * 1000)
        
        for symbol in valid_symbols:
            time.sleep(1.5)
            coin = symbol.split("/")[0].replace(":USDT", "")
            
            try:
                supports = []
                resistances = []
                
                # === 1. АНАЛИЗ 1D ===
                ohlcv_1d = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=365, params=fetch_params)
                if len(ohlcv_1d) >= 50:
                    df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df_1d['atr'] = calculate_atr(df_1d, 14)
                    atr_1d = df_1d['atr'].iloc[-1]
                    if pd.isna(atr_1d) or atr_1d == 0: atr_1d = df_1d['close'].iloc[-1] * 0.05
                    
                    peaks, _ = find_peaks(df_1d['high'], distance=15, prominence=atr_1d * 1.5)
                    valleys, _ = find_peaks(-df_1d['low'], distance=15, prominence=atr_1d * 1.5)
                    
                    current_price_1d = float(df_1d['close'].iloc[-1])
                    
                    # === ФИЛЬТРАЦИЯ ПОДДЕРЖЕК (ВПАДИНЫ) ===
                    for v in valleys:
                        price = float(df_1d['low'].iloc[v])
                        if abs(price - current_price_1d) / current_price_1d > 0.15: continue
                        
                        # 🛡 НОВЫЙ ФИЛЬТР: Origin Move (ATR-based)
                        # Берем следующие X дней после касания
                        lookahead_slice = df_1d['high'].iloc[v+1 : v+1+IMPULSE_LOOKAHEAD_DAYS]
                        if lookahead_slice.empty: continue # Свежий уровень, еще нет истории для проверки
                        
                        max_move = lookahead_slice.max()
                        # Если цена не смогла уйти вверх хотя бы на 2.5 ATR -> это слабый уровень, удаляем
                        if max_move < price + (atr_1d * IMPULSE_ATR_MULTIPLIER):
                            continue 
                            
                        ts = df_1d['timestamp'].iloc[v]
                        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')
                        
                        zone = {"min": price - (atr_1d * 0.5), "max": price + (atr_1d * 0.5), "score": 3.0, "type": "1d_extreme", "date": date_str}
                        if price < current_price_1d: supports.append(zone)
                        else: resistances.append(zone)
                        
                    # === ФИЛЬТРАЦИЯ СОПРОТИВЛЕНИЙ (ПИКИ) ===
                    for p in peaks:
                        price = float(df_1d['high'].iloc[p])
                        if abs(price - current_price_1d) / current_price_1d > 0.15: continue
                        
                        # 🛡 НОВЫЙ ФИЛЬТР: Origin Move (ATR-based)
                        lookahead_slice = df_1d['low'].iloc[p+1 : p+1+IMPULSE_LOOKAHEAD_DAYS]
                        if lookahead_slice.empty: continue
                        
                        min_move = lookahead_slice.min()
                        # Если цена не рухнула вниз хотя бы на 2.5 ATR -> удаляем
                        if min_move > price - (atr_1d * IMPULSE_ATR_MULTIPLIER):
                            continue
                            
                        ts = df_1d['timestamp'].iloc[p]
                        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')
                        
                        zone = {"min": price - (atr_1d * 0.5), "max": price + (atr_1d * 0.5), "score": 3.0, "type": "1d_extreme", "date": date_str}
                        if price > current_price_1d: resistances.append(zone)
                        else: supports.append(zone)

                # === 2. АНАЛИЗ 4H ===
                ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=200, params=fetch_params)
                if len(ohlcv_4h) >= 50:
                    df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df_4h['atr'] = calculate_atr(df_4h, 14)
                    atr_4h = df_4h['atr'].iloc[-1]
                    if pd.isna(atr_4h) or atr_4h == 0: atr_4h = df_4h['close'].iloc[-1] * 0.02
                    
                    df_4h['typical'] = (df_4h['high'] + df_4h['low'] + df_4h['close']) / 3
                    min_val, max_val = df_4h['low'].min(), df_4h['high'].max()
                    
                    if max_val > min_val:
                        bins = np.linspace(min_val, max_val, 50)
                        df_4h['bin'] = pd.cut(df_4h['typical'], bins=bins)
                        vol_profile = df_4h.groupby('bin', observed=False)['volume'].sum()
                        
                        poc_bin = vol_profile.idxmax()
                        if pd.notna(poc_bin) and hasattr(poc_bin, "mid"):
                            poc_price = float(getattr(poc_bin, "mid"))
                            current_price = float(df_4h['close'].iloc[-1])
                            
                            zone = {"min": poc_price - (atr_4h * 0.5), "max": poc_price + (atr_4h * 0.5), "score": 2.0, "type": "4h_poc"}
                            if current_price > poc_price: supports.append(zone)
                            else: resistances.append(zone)

                if supports or resistances:
                    macro_base[coin] = {
                        "supports": merge_overlapping_zones(supports),
                        "resistances": merge_overlapping_zones(resistances),
                        "updated_at": datetime.datetime.now().isoformat()
                    }
                    
            except Exception as e:
                print(f"[TEST ERROR] Ошибка для {coin}: {e}")
                continue 
                
        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        print(f"✅ [TEST HUNTER] Сбор завершен! Создан файл: {MACRO_LEVELS_FILE}")
            
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
