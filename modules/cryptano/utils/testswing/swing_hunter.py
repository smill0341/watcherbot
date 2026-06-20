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
BACKTEST_DATE = "2026-02-01 16:00:00"  
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
    Математическое слияние зон.
    Логика: берем балл самого сильного уровня (база) + 1 балл за каждое дополнительное подтверждение (бонус).
    Это защищает от инфляции мусорных уровней (3+3+2 не станет 8).
    """
    if not zones:
        return []
        
    sorted_zones = sorted(zones, key=lambda x: x['min'])
    
    # Добавляем временные поля для подсчета базы и количества наслоений
    for z in sorted_zones:
        z['base_score'] = z.get('score', 1.0)
        z['components'] = 1
        
    merged = [sorted_zones[0]]
    
    for current in sorted_zones[1:]:
        last = merged[-1]
        
        # Если зоны пересекаются
        if current['min'] <= last['max']:
            last['max'] = max(last['max'], current['max'])
            
            # 1. Обновляем базовый балл (выбираем сильнейший источник из слипшихся)
            last['base_score'] = max(last['base_score'], current['base_score'])
            
            # 2. Увеличиваем счетчик слипшихся зон (для бонусов)
            last['components'] += 1
            
            # 3. Итоговый Score = Сильнейшая база + бонус за каждое наслоение
            last['score'] = last['base_score'] + (last['components'] - 1)
            
            if current.get('type') and last.get('type') and current['type'] not in last['type']:
                last['type'] = f"{last['type']} + {current['type']}"
        else:
            merged.append(current)
            
    # Подчищаем временные ключи перед сохранением в JSON
    for m in merged:
        m.pop('base_score', None)
        m.pop('components', None)
        
    return merged

MAJORS = ["BTC", "ETH", "SOL", "BNB"]

def compress_fat_zones(zones, coin):
    """Сжимает слишком широкие зоны к их центру"""
    max_width_pct = 0.03 if coin in MAJORS else 0.05
    for z in zones:
        center = (z['max'] + z['min']) / 2.0
        width = z['max'] - z['min']
        max_allowed_width = center * max_width_pct
        
        if width > max_allowed_width:
            new_half_width = max_allowed_width / 2.0
            z['min'] = center - new_half_width
            z['max'] = center + new_half_width
    return zones

def resolve_cross_overlaps(supports, resistances):
    """Удаляет слабые зоны при жестком пересечении Support и Resistance (>25%)"""
    to_remove_sup = set()
    to_remove_res = set()
    
    for i, s in enumerate(supports):
        for j, r in enumerate(resistances):
            if i in to_remove_sup or j in to_remove_res: continue
                
            overlap = min(r['max'], s['max']) - max(r['min'], s['min'])
            if overlap > 0:
                min_zone_width = min(r['max'] - r['min'], s['max'] - s['min'])
                if min_zone_width <= 0: continue 
                
                if (overlap / min_zone_width) > 0.25:
                    # Удаляем слабейшую
                    if s.get('score', 0) > r.get('score', 0):
                        to_remove_res.add(j)
                    elif r.get('score', 0) > s.get('score', 0):
                        to_remove_sup.add(i)
                    else:
                        # Если баллы равны, удаляем сопротивление (крипта чаще растет)
                        to_remove_res.add(j)
                        
    final_sup = [s for i, s in enumerate(supports) if i not in to_remove_sup]
    final_res = [r for j, r in enumerate(resistances) if j not in to_remove_res]
    return final_sup, final_res

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
            merged_sup = merge_overlapping_zones(supports)
            merged_res = merge_overlapping_zones(resistances)
            
            # 1. Сжимаем жирные зоны
            merged_sup = compress_fat_zones(merged_sup, coin)
            merged_res = compress_fat_zones(merged_res, coin)
            
            # 2. Удаляем конфликты (пересечения поддержки и сопротивления)
            final_sup, final_res = resolve_cross_overlaps(merged_sup, merged_res)

            macro_base = load_json(MACRO_LEVELS_FILE, default={})
            if final_sup or final_res:
                macro_base[coin] = {
                    "supports": final_sup,
                    "resistances": final_res,
                    "updated_at": datetime.datetime.now().isoformat()
                }
                save_json_atomic(MACRO_LEVELS_FILE, macro_base)
            print(f"✅ [SWING HUNTER] Уровни для {coin} обновлены.")
        else:
            print(f"⚠️ [SWING HUNTER] Нет зон для {coin}.")

    except Exception as e:
        print(f"❌ [SWING HUNTER] КРИТИЧЕСКАЯ ОШИБКА генерации {coin}: {e}")

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
                supports = []
                resistances = []
                
                # === 1. АНАЛИЗ 1D ===
                ohlcv_1d = safe_fetch_ohlcv(symbol, "1d", 365, fetch_params)
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
                        
                        # БЕРЕМ ATR ИМЕННО ТОГО ДНЯ, КОГДА БЫЛА ВПАДИНА
                        local_atr = df_1d['atr'].iloc[v]
                        if pd.isna(local_atr) or local_atr == 0: local_atr = atr_1d

                        lookahead_slice = df_1d['high'].iloc[v+1 : v+1+IMPULSE_LOOKAHEAD_DAYS]
                        if lookahead_slice.empty: continue 
                        
                        max_move = lookahead_slice.max()
                        # Сравниваем импульс с историческим ATR
                        if max_move < price + (local_atr * IMPULSE_ATR_MULTIPLIER):
                            continue 
                            
                        ts = df_1d['timestamp'].iloc[v]
                        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')
                        
                        zone = {"min": price - (local_atr * 0.5), "max": price + (local_atr * 0.5), "score": 3.0, "type": "1d_extreme", "date": date_str}
                        if price > current_price_1d: pass # ❌ ВЫКЛЮЧИЛИ СТАРЫЕ ШОРТЫ
                        else: supports.append(zone)       # ✅ ЛОНГИ НЕ ТРОГАЕМ
                        
                    # === ФИЛЬТРАЦИЯ СОПРОТИВЛЕНИЙ (ПИКИ) ===
                    for p in peaks:
                        price = float(df_1d['high'].iloc[p])
                        if abs(price - current_price_1d) / current_price_1d > 0.15: continue
                        
                        # БЕРЕМ ATR ИМЕННО ТОГО ДНЯ
                        local_atr = df_1d['atr'].iloc[p]
                        if pd.isna(local_atr) or local_atr == 0: local_atr = atr_1d

                        lookahead_slice = df_1d['low'].iloc[p+1 : p+1+IMPULSE_LOOKAHEAD_DAYS]
                        if lookahead_slice.empty: continue
                        
                        min_move = lookahead_slice.min()
                        if min_move > price - (local_atr * IMPULSE_ATR_MULTIPLIER):
                            continue
                            
                        ts = df_1d['timestamp'].iloc[p]
                        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')
                        
                        zone = {"min": price - (local_atr * 0.5), "max": price + (local_atr * 0.5), "score": 3.0, "type": "1d_extreme", "date": date_str}
                        if price > current_price_1d: resistances.append(zone)
                        else: supports.append(zone)
                    # =======================================================
                    # 🔥 НОВЫЙ АЛГОРИТМ SMC: SUPPLY ZONES (ТОЛЬКО ДЛЯ ШОРТОВ)
                    # =======================================================
                    df_1d['body'] = abs(df_1d['open'] - df_1d['close'])
                    df_1d['avg_body'] = df_1d['body'].rolling(14).mean()
                    
                    for i in range(15, len(df_1d) - 1):
                        # 1. Имбаланс (Красная свеча в 2+ раза больше средней и больше 0.8 ATR)
                        is_drop = df_1d['close'].iloc[i] < df_1d['open'].iloc[i]
                        is_huge = (df_1d['body'].iloc[i] > df_1d['avg_body'].iloc[i] * 2.0) and (df_1d['body'].iloc[i] > df_1d['atr'].iloc[i] * 0.8)
                        
                        if is_drop and is_huge:
                            # 2. База / Ордерблок (Предыдущая свеча была зеленой или дожи)
                            if df_1d['close'].iloc[i-1] >= df_1d['open'].iloc[i-1]:
                                zone_max = float(df_1d['high'].iloc[i-1])
                                zone_min = float(df_1d['low'].iloc[i-1])
                                
                                # Отсекаем мусор (ширина зоны не больше 5%)
                                if (zone_max - zone_min) / zone_min > 0.05: continue
                                
                                # 3. Фильтр свежести (Unmitigated)
                                # Цена не должна была пробивать эту зону вверх с момента создания
                                future_highs = df_1d['high'].iloc[i+1:]
                                if future_highs.empty or future_highs.max() < zone_max:
                                    
                                    # 4. Проверяем, что зона сейчас ВЫШЕ цены (чтобы было куда шортить)
                                    if zone_min > current_price_1d and abs(zone_min - current_price_1d) / current_price_1d <= 0.15:
                                        ts = df_1d['timestamp'].iloc[i-1]
                                        date_str = pd.to_datetime(ts, unit='ms').strftime('%Y-%m-%d')
                                        
                                        resistances.append({
                                            "min": zone_min, 
                                            "max": zone_max, 
                                            "score": 3.0, 
                                            "type": "1d_supply_ob", 
                                            "date": date_str
                                        })
                    # =======================================================
                # === 2. АНАЛИЗ 4H ===
                ohlcv_4h = safe_fetch_ohlcv(symbol, "4h", 200, fetch_params)
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
                            else: resistances.append(zone) # ✅ ВЕРНУЛИ 4H ШОРТЫ

                # Записываем в базу, пропустив через фильтры
                if supports or resistances:
                    merged_sup = merge_overlapping_zones(supports)
                    merged_res = merge_overlapping_zones(resistances)
                    
                    # 1. Сжимаем жирные зоны
                    merged_sup = compress_fat_zones(merged_sup, coin)
                    merged_res = compress_fat_zones(merged_res, coin)
                    
                    # 2. Удаляем конфликты (пересечения поддержки и сопротивления)
                    final_sup, final_res = resolve_cross_overlaps(merged_sup, merged_res)
                    
                    if final_sup or final_res:
                        macro_base[coin] = {
                            "supports": final_sup,
                            "resistances": final_res,
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
