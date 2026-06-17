import pandas as pd
import numpy as np
import os
import warnings

from sympy import true
warnings.filterwarnings("ignore")
from backtesting import Backtest, Strategy
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json

GLOBAL_DEBUG_STATS = {
    "Touches": 0,           # Сколько раз цена зашла в зону
    "Killed_by_GAP": 0,     # Отсеял Gap (воздух)
    "Killed_by_EMA": 0,     # Отсеяла EMA 200
    "Killed_by_PUMP": 0,    # Отсеял фильтр подхода (не было 6% роста)
    "Killed_by_FREIGHT_TRAIN": 0,   # Отсеял Anti-Knife
    "Killed_by_CHOCH": 0,   # Не дождались CHoCH (цена ушла выше)
    "Killed_by_IMPULSE": 0, # Свеча входа слишком большая
    "Killed_by_DISTANCE": 0,# Цена далеко улетела на входе
    "Killed_by_RISK": 0,    # Слишком большой стоп-лосс
    "Passed_to_Trade": 0    # Дошло до реальной сделки
}

# =========================================================
# 1. ОСНОВНЫЕ НАСТРОЙКИ (ЕДИНЫЙ ПУЛЬТ УПРАВЛЕНИЯ)
# =========================================================
TARGET_COIN = "H"  # Впиши "ALL" для теста всего портфеля, или имя монеты для детального теста

TIMEFRAME = "15m"
LIMIT_CANDLES = 1000 # Оптимально: ~10 дней актуальной истории

# 🧪 ТУМБЛЕР МАШИНЫ ВРЕМЕНИ
TEST_START_DATE = "2026-05-01 16:00:00"

TAKE_PROFIT = 10.0   # Оригинальная цель в %
SL_BUFFER = 3.5      # Оригинальный отступ стоп-лосса в %

# --- ТУМБЛЕРЫ ФИЛЬТРОВ ---
USE_CHOCH        = False    # Слом структуры
USE_ANTI_KNIFE   = True    # Запрет входа против агрессивных свечей
USE_RR_FILTER    = False   # Математический фильтр R/R
RR_RATIO         = 3.0     # Минимальный R/R

USE_RANGE_FILTER = False   # Игнорировать входы, если закрытие ушло в средние 40% диапазона
USE_LEVEL_BURN   = True   # Сжигать уровень ТОЛЬКО ПОСЛЕ ПЛЮСА

ALLOW_LONG_TRADES  = False # ❌ ОТКЛЮЧАЕМ ЛОНГИ ДЛЯ ЧИСТОГО ТЕСТА ШОРТОВ
ALLOW_SHORT_TRADES = True  # ✅ ШОРТЫ ОСТАЮТСЯ ВКЛЮЧЕНЫ
MIN_SCORE = 4      # Минимальный балл зоны для входа (отсекает мусор)

# --- НОВЫЕ СНАЙПЕРСКИЕ ФИЛЬТРЫ ---
USE_ZONE_GAP      = True   # Правило 2: Включить проверку "воздуха" между зонами
MIN_ZONE_GAP_PCT  = 3.0    # Минимальный зазор между Поддержкой и ближайшим Сопротивлением в %
USE_RISK_CAP      = False   # Правило 3: Ограничение риска
MAX_RISK_PCT      = 5.0   # Макс. стоп-лосс в %. Сделка с большим стопом отменяется

# --- ФИЛЬТР ОТСТАВАНИЯ (ДОГОНЯЮЩИЙ ВХОД) ---
USE_DISTANCE_FILTER = True # Мягкий фильтр: отмена сделки, если цена улетела далеко от зоны
MAX_DISTANCE_PCT    = 1.0  # Максимально допустимый отрыв от зоны в % (тестируем 1.0)
USE_IMPULSE_FILTER  = True   # Включаем фильтр аномальных свечей в реальную работу

# Настройки для LONG (Мягкие, чтобы не убить рабочие 56% профита)
MAX_DISTANCE_PCT_LONG  = 1.5    # Разрешаем отрыв до 1.5%
MAX_IMPULSE_RATIO_LONG = 3.0    # Фильтруем только совсем дикие свечи (> 3х)

# Настройки для SHORT (Жесткие, чтобы полностью вырезать баг со скринов)
MAX_DISTANCE_PCT_SHORT = 1.0    # Отрыв строго не более 1.0%
MAX_IMPULSE_RATIO_SHORT = 2.5   # Фильтруем любые свечи, которые больше средних на 80% (>= 1.8х)
# ФИЛЬТРЫ ДЛЯ ШОРТОВ (SFP + Тренд) ---
USE_SHORT_EMA200_FILTER = False  # Разрешить шорты только если цена НИЖЕ EMA 200
USE_SHORT_SFP_LOGIC     = True  # Искать SFP (прокол верхней границы зоны), а не просто касание низа
SHORT_CHOCH_PERIOD      = 10    # ГЛУБОКИЙ CHoCH: ищем лой за 10 свечей (а не за 2)
SHORT_LOOKBACK          = 100    # Окно поиска дна перед шортом (48 свечей = 12 часов)
SHORT_MIN_PUMP_PCT      = 10.0   # Минимальный рост от дна до сопротивления в %
# =========================================================

# --- SHADOW METRICS (ТЕНЕВАЯ СТАТИСТИКА) ---
USE_DISTANCE_FILTER = False # Выключаем жесткую отмену сделок, чтобы собрать дату
SHADOW_MAX_DISTANCE = 1.0   # Порог отрыва цены (для лога)
SHADOW_MAX_IMPULSE  = 2.0   # Порог аномальной свечи: в 2 раза больше средних 10 свечей (для лога)

CURRENT_SUPPORTS = []
CURRENT_RESISTANCES = []

def SMA(arr, n):
    return pd.Series(arr).rolling(n).mean()

class SmartSniperUniversal(Strategy):
    def init(self):
        self.burned_levels = set()  
        self.wait_for_bullish_choch = False
        self.choch_bull_level = 0.0
        self.wait_for_bearish_choch = False
        self.choch_bear_level = 0.0
        self.active_level = None
        self.last_closed_trades = 0
        self.current_trade_level_id = None
        
        high_low = pd.Series(self.data.High) - pd.Series(self.data.Low)
        self.atr = self.I(SMA, high_low, 14)
        
        # Считаем 4H EMA 200 (эмуляция на 15-минутном графике: 200 * 16 = 3200 свечей)
        def EMA(values, n):
            return pd.Series(values).ewm(span=n, adjust=False).mean()
        
        self.ema_4h_200 = self.I(EMA, self.data.Close, 3200)

        # ==========================================
        # 🎨 ОТРИСОВКА МАКРО-УРОВНЕЙ НА ГРАФИКЕ
        # ==========================================
        def create_line(val):
            return pd.Series(val, index=self.data.index)

        for sup in CURRENT_SUPPORTS:
            self.I(create_line, sup['max'], name=f"Support Top {sup['max']:.4f}", overlay=True, color="green")
            self.I(create_line, sup['min'], name=f"Support Bottom {sup['min']:.4f}", overlay=True, color="lightgreen")

        for res in CURRENT_RESISTANCES:
            self.I(create_line, res['min'], name=f"Resist Bottom {res['min']:.4f}", overlay=True, color="red")
            self.I(create_line, res['max'], name=f"Resist Top {res['max']:.4f}", overlay=True, color="pink")

    def next(self):
        # ==========================================
        # 1. СЖИГАНИЕ УРОВНЕЙ (ТОЛЬКО ПОСЛЕ ПЛЮСА)
        # ==========================================
        if len(self.closed_trades) > self.last_closed_trades:
            last_trade = self.closed_trades[-1]
            if last_trade.pl > 0 and self.current_trade_level_id is not None:
                if USE_LEVEL_BURN:
                    self.burned_levels.add(self.current_trade_level_id)
            self.current_trade_level_id = None
            self.last_closed_trades = len(self.closed_trades)

        if len(self.data) < 15: return
        
        c_close, c_open = self.data.Close[-1], self.data.Open[-1]
        c_high, c_low = self.data.High[-1], self.data.Low[-1]
        c_vol = self.data.Volume[-1]
        p_close, p_open = self.data.Close[-2], self.data.Open[-2]
        p_vol = self.data.Volume[-2]

        # ==========================================
        # 2. ФИЛЬТР ANTI-KNIFE (С объемами и тенями)
        # ==========================================
        is_falling_knife = False
        is_flying_rocket = False
        
        if USE_ANTI_KNIFE:
            # Средний объем за последние 10 свечей (исключая текущую)
            avg_vol = self.data.Volume[-11:-1].mean() if len(self.data) >= 11 else p_vol
            c_atr = self.atr[-1] if not np.isnan(self.atr[-1]) else (c_high - c_low)
            
            c_body_red = c_open - c_close
            c_body_green = c_close - c_open
            
            # Для отмены LONG (Падающий нож): Красная, большая, нет тени снизу, объем > 1.5х
            if c_close < c_open and c_body_red > (c_atr * 0.8):
                lower_wick = c_close - c_low
                if lower_wick < (c_body_red * 0.3) and c_vol > (avg_vol * 1.5):
                    is_falling_knife = True
                    
            # Для отмены SHORT (Ракета): Зеленая, большая, нет тени сверху, объем > 1.5х
            if c_close > c_open and c_body_green > (c_atr * 0.8):
                upper_wick = c_high - c_close
                if upper_wick < (c_body_green * 0.3) and c_vol > (avg_vol * 1.5):
                    is_flying_rocket = True

        if self.position:
            self.wait_for_bullish_choch = False
            self.wait_for_bearish_choch = False
            return

        # 🔥 ОБЪЕДИНЯЕМ ВСЕ ЗОНЫ (Решение проблемы зеркальных уровней)
        all_zones = CURRENT_SUPPORTS + CURRENT_RESISTANCES

        # ==========================================
        # 🛡 ПРОВЕРКА КОНТЕКСТА (ПУСТЫЕ СТОРОНЫ)
        # ==========================================
        has_supports = len(CURRENT_SUPPORTS) > 0
        has_resistances = len(CURRENT_RESISTANCES) > 0

        # Если ничего нет: skip symbol (сразу выходим из свечи)
        if not has_supports and not has_resistances:
            return 

        # Жесткая блокировка направлений (Учитывает и пустые зоны, и рубильники)
        can_long = has_supports and ALLOW_LONG_TRADES
        can_short = has_resistances and ALLOW_SHORT_TRADES
        
        # ==========================================
        # 3. ЛОГИКА LONG (Только от зон Поддержки)
        # ==========================================
        for sup in CURRENT_SUPPORTS:
            if not can_long:
                break # ❌ LONG аппаратно запрещен (нет поддержек или отключен вручную)
                
            if sup.get('score', 0) < MIN_SCORE: 
                continue
                
            level_id = f"{sup['min']}_{sup['max']}"
            if USE_LEVEL_BURN and level_id in self.burned_levels: 
                continue 

            # 🛡 ПРАВИЛО 2: Проверка "воздуха" (Gap) до ближайшего сопротивления
            if USE_ZONE_GAP:
                closest_res = min([r['min'] for r in CURRENT_RESISTANCES if r['min'] > sup['max']], default=None)
                if closest_res:
                    gap_pct = ((closest_res - sup['max']) / sup['max']) * 100
                    if gap_pct < MIN_ZONE_GAP_PCT:
                        continue # Скипаем зону, если до потолка нет запаса хода

            # Условие: Цена коснулась поддержки, но не пробила её насквозь
            if c_low <= sup['max'] and c_close > sup['min']:
                if is_falling_knife: break
                
                if USE_RANGE_FILTER:
                    closest_res = min([r['min'] for r in all_zones if r['min'] > sup['max']], default=None)
                    if closest_res:
                        range_size = closest_res - sup['max']
                        if (c_close - sup['max']) > (range_size * 0.30):
                            break 

                if USE_CHOCH:
                    self.wait_for_bullish_choch = True
                    self.choch_bull_level = max(self.data.High[-1], self.data.High[-2])
                    self.active_level = sup
                else:
                    self._execute_long(sup, c_close)
                break

        if USE_CHOCH and self.wait_for_bullish_choch and self.active_level is not None:
            if c_close > self.choch_bull_level:
                self._execute_long(self.active_level, c_close)
                self.wait_for_bullish_choch = False
            elif c_low < self.active_level['min'] * 0.99:
                self.wait_for_bullish_choch = False

        # ==========================================
        # 4. ЛОГИКА SHORT (С диагностикой отмен и SMC)
        # ==========================================
        global GLOBAL_DEBUG_STATS

        for res in CURRENT_RESISTANCES:
            if not can_short: break 
            if res.get('score', 0) < MIN_SCORE: continue 
                
            level_id = f"{res['min']}_{res['max']}"
            if USE_LEVEL_BURN and level_id in self.burned_levels: continue 

            # 🎯 1. СНАЧАЛА ПРОВЕРЯЕМ КАСАНИЕ ЗОНЫ (Оптимизация)
            if c_high < res['min']:
                continue # Вообще не дошли до зоны, идем дальше
                
            # 🔥 МЫ В ЗОНЕ! Фиксируем попытку
            GLOBAL_DEBUG_STATS["Touches"] += 1

            # 🛡 2. Проверка "воздуха" (Gap)
            if USE_ZONE_GAP:
                closest_sup = max([s['max'] for s in CURRENT_SUPPORTS if s['max'] < res['min']], default=None)
                if closest_sup:
                    gap_pct = ((res['min'] - closest_sup) / closest_sup) * 100
                    if gap_pct < MIN_ZONE_GAP_PCT:
                        GLOBAL_DEBUG_STATS["Killed_by_GAP"] += 1
                        continue 

            # 🛡 3. ФИЛЬТР ТРЕНДА: 4H EMA 200
            if USE_SHORT_EMA200_FILTER:
                if c_close > self.ema_4h_200[-1]:
                    GLOBAL_DEBUG_STATS["Killed_by_EMA"] += 1
                    continue

            # 🛡 4. ФИЛЬТР ПОДХОДА (Убедись, что SHORT_MIN_PUMP_PCT снижен до ~8-10%)
            recent_low = min(self.data.Low[-SHORT_LOOKBACK:])
            pump_pct = ((res['min'] - recent_low) / recent_low) * 100
            if pump_pct < SHORT_MIN_PUMP_PCT:
                GLOBAL_DEBUG_STATS["Killed_by_PUMP"] += 1
                continue 

            # 🔥 5. БРОНЯ ОТ "ПОЕЗДА" (Аномальный пробой)
            # Если сработал новый фильтр с объемами - блокируем вход
            if is_flying_rocket:
                GLOBAL_DEBUG_STATS["Killed_by_FREIGHT_TRAIN"] += 1
                break 

            # 🔥 6. ИСТИННЫЙ SFP И КАСАНИЕ
            is_sfp = (c_high >= res['max']) and (c_close < res['max']) and (c_close < c_open)
            is_touch = (c_high >= res['min']) and (c_close < res['max']) and not is_sfp

            # === СЦЕНАРИЙ А: SFP ===
            if is_sfp:
                self._execute_short(res, c_close)
                self.wait_for_bearish_choch = False 
                GLOBAL_DEBUG_STATS["Passed_to_Trade"] += 1
                break

            # === СЦЕНАРИЙ Б: Касание ===
            elif is_touch:
                if USE_CHOCH:
                    self.wait_for_bearish_choch = True
                    self.choch_bear_level = min(self.data.Low[-3:])
                    self.active_level = res
                else:
                    self._execute_short(res, c_close)
                    GLOBAL_DEBUG_STATS["Passed_to_Trade"] += 1
                break

        # ОЖИДАНИЕ СЛОМА СТРУКТУРЫ
        if USE_CHOCH and self.wait_for_bearish_choch and self.active_level is not None:
            if c_close < self.choch_bear_level:
                self._execute_short(self.active_level, c_close)
                self.wait_for_bearish_choch = False
                GLOBAL_DEBUG_STATS["Passed_to_Trade"] += 1
            elif c_high > self.active_level['max'] * 1.01:
                # Цена улетела выше зоны без слома CHoCH
                GLOBAL_DEBUG_STATS["Killed_by_CHOCH"] += 1
                self.wait_for_bearish_choch = False
    ## ==========================================
    # 5. ИСПОЛНЕНИЕ ОРДЕРОВ (БОЕВАЯ СНАЙПЕРСКАЯ ФИЛЬТРАЦИЯ)
    # ==========================================
    def _execute_long(self, level, current_price):
        # 1. Считаем импульс свечи
        current_body = abs(self.data.Close[-1] - self.data.Open[-1])
        avg_body = sum([abs(self.data.Close[-i] - self.data.Open[-i]) for i in range(2, 12)]) / 10
        avg_body = avg_body if avg_body > 0 else 0.0001
        impulse_ratio = current_body / avg_body
        
        # 2. Боевой фильтр импульса для LONG
        if USE_IMPULSE_FILTER and impulse_ratio > MAX_IMPULSE_RATIO_LONG:
            return

        # 3. Боевой фильтр дистанции для LONG
        if USE_DISTANCE_FILTER and current_price > level['max']:
            distance_pct = ((current_price - level['max']) / level['max']) * 100
            if distance_pct > MAX_DISTANCE_PCT_LONG:
                return

        # Исполнение ордера
        sl = level['min'] * (1 - SL_BUFFER / 100)
        risk_pct = ((current_price - sl) / current_price) * 100
        if USE_RISK_CAP and risk_pct > MAX_RISK_PCT: return 
            
        tp = current_price * (1 + TAKE_PROFIT / 100)
        self.current_trade_level_id = f"{level['min']}_{level['max']}"
        self.buy(sl=sl, tp=tp)


    def _execute_short(self, level, current_price):
        global GLOBAL_DEBUG_STATS
        
        # 🕵️‍♂️ ЛОГ ВХОДА ДЛЯ АНАЛИЗА ТАЙМИНГА (Выводится строго при отправке ордера)
        recent_low_debug = min(self.data.Low[-SHORT_LOOKBACK:])
        pump_pct_debug = ((level['min'] - recent_low_debug) / recent_low_debug) * 100
        print(f"\n🎯 [SHORT TRIGGER LOG] Время: {self.data.index[-1]}")
        print(f"   🔹 Границы зоны: Min={level['min']:.4f} | Max={level['max']:.4f}")
        print(f"   🔹 Цена входа (Close): {current_price:.4f} | Хай свечи: {self.data.High[-1]:.4f}")
        print(f"   🔹 Найдено дно за 48 свечей: {recent_low_debug:.4f}")
        print(f"   🔹 Насчитанный памп до зоны: {pump_pct_debug:.2f}%")
        print(f"   🔹 Свеча: Open={self.data.Open[-1]:.4f} | Close={current_price:.4f}\n")

        current_body = abs(self.data.Close[-1] - self.data.Open[-1])
        avg_body = sum([abs(self.data.Close[-i] - self.data.Open[-i]) for i in range(2, 12)]) / 10
        avg_body = avg_body if avg_body > 0 else 0.0001
        impulse_ratio = current_body / avg_body
        
        if USE_IMPULSE_FILTER and impulse_ratio > MAX_IMPULSE_RATIO_SHORT:
            GLOBAL_DEBUG_STATS["Killed_by_IMPULSE"] += 1
            return

        if USE_DISTANCE_FILTER and current_price < level['min']:
            distance_pct = ((level['min'] - current_price) / level['min']) * 100
            if distance_pct > MAX_DISTANCE_PCT_SHORT:
                GLOBAL_DEBUG_STATS["Killed_by_DISTANCE"] += 1
                return

        sl = level['max'] * (1 + SL_BUFFER / 100)
        risk_pct = ((sl - current_price) / current_price) * 100
        if USE_RISK_CAP and risk_pct > MAX_RISK_PCT: 
            GLOBAL_DEBUG_STATS["Killed_by_RISK"] += 1
            return 
            
        # Если дошли сюда - значит сделка открылась!
        GLOBAL_DEBUG_STATS["Passed_to_Trade"] += 1
        tp = current_price * (1 - TAKE_PROFIT / 100)
        self.current_trade_level_id = f"{level['min']}_{level['max']}"
        self.sell(sl=sl, tp=tp)

# =========================================================
# ЗАПУСК ТЕСТОВ И ОТЧЕТЫ (ЧИСТЫЙ СТАНДАРТ)
# =========================================================
macro_path = os.path.join("modules", "cryptano", "macro_levels.json")
macro_db = load_json(macro_path, default={}) if os.path.exists(macro_path) else {}

def get_cached_data(coin):
    # УНИВЕРСАЛЬНЫЙ ПОИСК ТИКЕРА В БАЗЕ БИРЖИ
    try:
        exchange.load_markets() # Подтягиваем список всех торговых пар
    except:
        pass

    symbol_perp = f"{coin.upper()}/USDT:USDT" # Формат фьючерса
    symbol_spot = f"{coin.upper()}/USDT"      # Формат спота

    # Сначала ищем фьючерс, если его нет — берем спот
    if exchange.markets and symbol_perp in exchange.markets:
        symbol = symbol_perp
    else:
        symbol = symbol_spot
    
    date_suffix = TEST_START_DATE[:10] if TEST_START_DATE else "live"
    cache_file = f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}_{date_suffix}.csv"
    
    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)
    else:
        print(f"🌐 Скачиваю свечи для {symbol}...")
        try:
            since_ts = None
            if TEST_START_DATE:
                dt_obj = pd.to_datetime(TEST_START_DATE) - pd.Timedelta(days=1)
                since_ts = int(dt_obj.timestamp() * 1000)
            
            if since_ts:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES, since=since_ts)
            else:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
                
            df = pd.DataFrame(ohlcv, columns=["Open_time", "Open", "High", "Low", "Close", "Volume"])
            df.index = pd.to_datetime(df["Open_time"], unit="ms")
            df.to_csv(cache_file)
            return df
        except Exception as e:
            print(f"❌ Ошибка скачивания {symbol}: {e}")
            return pd.DataFrame()
        
if TEST_START_DATE:
    macro_path = os.path.join("modules", "cryptano", "utils", "testswing", f"macro_test_{TEST_START_DATE[:10]}.json")
else:
    macro_path = os.path.join("modules", "cryptano", "macro_levels.json")

macro_db = load_json(macro_path, default={}) if os.path.exists(macro_path) else {}

filters_summary = (f"CHoCH={USE_CHOCH} | KNIFE={USE_ANTI_KNIFE} | "
                   f"R/R={USE_RR_FILTER} | RANGE={USE_RANGE_FILTER} | BURN(Win)={USE_LEVEL_BURN}")

if TARGET_COIN.upper() == "ALL":
    print(f"🤖 Начинаю глобальный аудит портфеля на {LIMIT_CANDLES} свечей...")
    portfolio_results = []
    
    for coin, data in macro_db.items():
        if not isinstance(data, dict): continue
        CURRENT_SUPPORTS = data.get("supports", [])
        CURRENT_RESISTANCES = data.get("resistances", [])
        if not CURRENT_SUPPORTS and not CURRENT_RESISTANCES: continue
        
        df = get_cached_data(coin)
        if df.empty: continue
        
        bt = Backtest(df, SmartSniperUniversal, cash=10000, commission=.0006, hedging=False)
        stats = bt.run()
        
        if int(stats['# Trades']) > 0:
            portfolio_results.append({
                "Монета": coin.upper(),
                "Сделок": int(stats['# Trades']),
                "Win Rate %": round(stats['Win Rate [%]'], 2),
                "Profit Factor": round(stats['Profit Factor'], 2) if pd.notna(stats['Profit Factor']) else "Без убытка",
                "Просадка %": round(stats['Max. Drawdown [%]'], 2),
                "Чистый Профит %": round(stats['Return [%]'], 2)
            })

    print("\n" + "="*85)
    print(f"📊 СВОДНЫЙ СРЕЗ ГЛОБАЛЬНОГО ТЕСТА СТРАТЕГИИ")
    print(f"🛠 Фильтры: {filters_summary}")
    print("="*85)
    if portfolio_results:
        report_df = pd.DataFrame(portfolio_results).sort_values(by="Чистый Профит %", ascending=False)
        print(report_df.to_string(index=False))
        print("-" * 85)
        print(f"📈 Суммарный профит портфеля: {report_df['Чистый Профит %'].sum():.2f}%")
        print(f"🏆 Средний Win Rate:         {report_df['Win Rate %'].mean():.2f}%")
    else:
        print("❌ Нет сделок. Фильтры отсекли всё.")
    print("="*85 + "\n")

else:
    coin_data = macro_db.get(TARGET_COIN.upper(), {}) if isinstance(macro_db.get(TARGET_COIN.upper()), dict) else {}
    CURRENT_SUPPORTS = coin_data.get("supports", [])
    CURRENT_RESISTANCES = coin_data.get("resistances", [])
    
    # 🔥 ПРОВЕРКА КОНТЕКСТА: Если ничего нет - skip symbol
    if not CURRENT_SUPPORTS and not CURRENT_RESISTANCES:
        print(f"⏩ Скипаем {TARGET_COIN.upper()}: нет уровней (skip symbol).")
    else:
        print(f"📥 Запускаю детальный тест для {TARGET_COIN.upper()}...")
        df = get_cached_data(TARGET_COIN)
    if df.empty:
        print("❌ Ошибка загрузки данных.")
    else:
        bt = Backtest(df, SmartSniperUniversal, cash=10000, commission=.0006, hedging=False)
        stats = bt.run()
        
        print("\n" + "="*70)
        print(f"📊 ТЕСТ ДЛЯ {TARGET_COIN.upper()}")
        print(f"🛠 Фильтры: {filters_summary}")
        print("="*70)
        print(f"💵 Конечный баланс:   ${stats['Equity Final [$]']:,.2f}")
        print(f"📈 Чистый профит:     {stats['Return [%]']:.2f}%")
        print(f"📉 Макс. просадка:    {stats['Max. Drawdown [%]']:.2f}%")
        print(f"🤝 Всего сделок:      {int(stats['# Trades'])}")
        
        if int(stats['# Trades']) > 0:
            print(f"🏆 Процент плюсовых:  {stats['Win Rate [%]']:.2f}%")
            print("-" * 70)
            for idx, row in stats['_trades'].iterrows():
                pct_val = row['ReturnPct'] * 100
                sign = "+" if pct_val > 0 else ""
                status = "✅ ПЛЮС" if row['PnL'] > 0 else "❌ МИНУС"
                tr_type = "LONG " if row['Size'] > 0 else "SHORT"
                t_in = row['EntryTime'].strftime('%d.%m %H:%M')
                t_out = row['ExitTime'].strftime('%d.%m %H:%M')
                
                # Напрямую берем готовые значения из колонок строки сделки
                p_entry = row['EntryPrice']
                p_sl = row['SL']
                p_tp = row['TP']
                
                # Выводим полную информацию для ручного анализа
                print(f"  ▪️ Сделка №{idx+1} ({tr_type}): {t_in} -> {t_out} | {status} ({sign}{pct_val:.2f}%)")
                print(f"    👉 Вход (Entry): {p_entry:.4f} | Стоп (SL): {p_sl:.4f} | Тейк (TP 10%): {p_tp:.4f}")
                
        chart_path = os.path.abspath(f'chart_{TARGET_COIN.lower()}.html')
        bt.plot(filename=chart_path, open_browser=True)
        
# 🔥 ГЛОБАЛЬНЫЙ ВЫВОД ДИАГНОСТИКИ (ВНЕ ЗАВИСИМОСТИ ОТ РЕЖИМА ТЕСТА)
print("\n" + "="*70)
print("🕵️‍♂️ ОТЧЕТ УБИЙЦ СДЕЛОК (GLOBAL DEBUG СТАТИСТИКА)")
print("="*70)
print(f"🎯 Всего касаний зон:                    {GLOBAL_DEBUG_STATS['Touches']}")
print(f"🔪 Отсеяно по Gap (Нет воздуха):         {GLOBAL_DEBUG_STATS['Killed_by_GAP']}")
print(f"🔪 Отсеяно по EMA200 (Против макро):     {GLOBAL_DEBUG_STATS['Killed_by_EMA']}")
print(f"🔪 Отсеяно по Пампу (Не было роста):     {GLOBAL_DEBUG_STATS['Killed_by_PUMP']}")
print(f"🔪 Отсеяно поездом (Freight Train):      {GLOBAL_DEBUG_STATS['Killed_by_FREIGHT_TRAIN']}")
print(f"🔪 Отсеяно (не дождались CHoCH):         {GLOBAL_DEBUG_STATS['Killed_by_CHOCH']}")
print(f"✅ Допущено до сделки:                   {GLOBAL_DEBUG_STATS['Passed_to_Trade']}")
        