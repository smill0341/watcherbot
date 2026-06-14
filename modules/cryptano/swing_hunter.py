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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MACRO_LEVELS_FILE = os.path.join(BASE_DIR, "macro_levels.json")
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")

# =========================================================
# ⚙️ НАСТРОЙКИ РАСПИСАНИЯ
# =========================================================
TEST_TIME = "23:16"  

TIME_ASIAN_CLOSE = "03:05"
TIME_US_OPEN = "15:05"      
# =========================================================

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

def build_macro_levels(bot=None, admin_chat_id=None):
    print("[SWING HUNTER] Запуск генерации институциональных зон (1D Экстремумы + 4H POC)...")
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
        
        for symbol in valid_symbols:
            time.sleep(0.15)
            coin = symbol.split("/")[0].replace(":USDT", "")
            
            try:
                supports = []
                resistances = []
                
                # ==========================================================
                # === 1. АНАЛИЗ 1D (Исторические экстремумы через Scipy) ===
                # ==========================================================
                ohlcv_1d = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=365)
                if len(ohlcv_1d) >= 50:
                    df_1d = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df_1d['atr'] = calculate_atr(df_1d, 14)
                    atr_1d = df_1d['atr'].iloc[-1]
                    if pd.isna(atr_1d) or atr_1d == 0: atr_1d = df_1d['close'].iloc[-1] * 0.05
                    
                    # Ищем пики (Сопротивление)
                    peaks, _ = find_peaks(df_1d['high'], distance=15, prominence=atr_1d * 1.5)
                    # Ищем впадины (Поддержка) - инвертируем low для scipy
                    valleys, _ = find_peaks(-df_1d['low'], distance=15, prominence=atr_1d * 1.5)
                    
                    # Берем текущую цену для фильтрации
                    current_price_1d = float(df_1d['close'].iloc[-1])
                    
                    for v in valleys:
                        price = float(df_1d['low'].iloc[v])
                        # Игнорируем уровни, которые находятся дальше 15% от текущей цены
                        if abs(price - current_price_1d) / current_price_1d > 0.15: 
                            continue
                        
                        zone = {"min": price - (atr_1d * 0.25), "max": price + (atr_1d * 0.25), "score": 3.0, "type": "1d_extreme"}
                        # Если старая впадина сейчас ВЫШЕ цены — она стала сопротивлением
                        if price < current_price_1d: supports.append(zone)
                        else: resistances.append(zone)
                        
                    for p in peaks:
                        price = float(df_1d['high'].iloc[p])
                        # Игнорируем уровни, которые находятся дальше 15% от текущей цены
                        if abs(price - current_price_1d) / current_price_1d > 0.15: 
                            continue
                        
                        zone = {"min": price - (atr_1d * 0.25), "max": price + (atr_1d * 0.25), "score": 3.0, "type": "1d_extreme"}
                        # Если старый пик сейчас НИЖЕ цены — он стал поддержкой
                        if price > current_price_1d: resistances.append(zone)
                        else: supports.append(zone)

                # ==========================================================
                # === 2. АНАЛИЗ 4H (Volume Profile / Точка POC) ============
                # ==========================================================
                ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=200)
                if len(ohlcv_4h) >= 50:
                    df_4h = pd.DataFrame(ohlcv_4h, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df_4h['atr'] = calculate_atr(df_4h, 14)
                    atr_4h = df_4h['atr'].iloc[-1]
                    if pd.isna(atr_4h) or atr_4h == 0: atr_4h = df_4h['close'].iloc[-1] * 0.02
                    
                    # Жесткий и безотказный расчет Профиля Объема (50 уровней плотности)
                    df_4h['typical'] = (df_4h['high'] + df_4h['low'] + df_4h['close']) / 3
                    min_val, max_val = df_4h['low'].min(), df_4h['high'].max()
                    
                    if max_val > min_val:
                        bins = np.linspace(min_val, max_val, 50)
                        df_4h['bin'] = pd.cut(df_4h['typical'], bins=bins)
                        # Суммируем объем в каждом "блоке" цен
                        vol_profile = df_4h.groupby('bin', observed=False)['volume'].sum()
                        
                        # Находим бин с максимальным объемом (Point of Control)
                        poc_bin = vol_profile.idxmax()
                        if pd.notna(poc_bin) and hasattr(poc_bin, "mid"):
                            poc_price = float(getattr(poc_bin, "mid"))
                            current_price = float(df_4h['close'].iloc[-1])
                            
                            zone = {
                                "min": poc_price - (atr_4h * 0.25),
                                "max": poc_price + (atr_4h * 0.25),
                                "score": 2.0,
                                "type": "4h_poc"
                            }
                            
                            if current_price > poc_price:
                                supports.append(zone)
                            else:
                                resistances.append(zone)

                # Записываем в базу, пропустив через фильтр склейки
                if supports or resistances:
                    macro_base[coin] = {
                        "supports": merge_overlapping_zones(supports),
                        "resistances": merge_overlapping_zones(resistances),
                        "updated_at": datetime.datetime.now().isoformat()
                    }
                    
            except Exception as e:
                print(f"[SWING ERROR] Ошибка 4H/1D для {coin}: {e}")
                continue 
                
        save_json_atomic(MACRO_LEVELS_FILE, macro_base)
        print(f"✅ [SWING HUNTER] Сбор завершен! Сохранено {len(macro_base)} монет с зонами POC и 1D.")
            
    except Exception as e:
        print(f"❌ [SWING HUNTER] КРИТИЧЕСКАЯ ОШИБКА генерации: {e}")


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

def start_swing_hunter(bot, admin_chat_id):
    threading.Thread(target=run_heavy_generator, args=(bot, admin_chat_id), daemon=True).start()
    threading.Thread(target=minute_radar, args=(bot, admin_chat_id), daemon=True).start()