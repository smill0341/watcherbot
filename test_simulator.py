import pandas as pd
import numpy as np
import os
import warnings
import json

from sympy import true
warnings.filterwarnings("ignore")
from backtesting import Backtest, Strategy
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json
from modules.cryptano.utils.testswing.context_filter import evaluate_context, get_approach_type
from modules.cryptano.utils.testswing.context_filter import analyze_context


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
GLOBAL_REPORT = []      # 📊 ТРЕКЕР ДЛЯ ТАБЛИЦЫ
GLOBAL_LOSERS_LOG = []  # 📉 ТРЕКЕР ТОЛЬКО ДЛЯ УБЫТКОВ
GLOBAL_TRADE_CONTEXTS = {} # 🧠 ГЛОБАЛЬНЫЙ КЭШ ДЛЯ КОНТЕКСТА СДЕЛКИ
GLOBAL_WINNERS_LOG = []

# =========================================================
# 1. ОСНОВНЫЕ НАСТРОЙКИ (ЕДИНЫЙ ПУЛЬТ УПРАВЛЕНИЯ)
# =========================================================
TARGET_COIN = "ALL"  # Впиши "ALL" для теста всего портфеля, или имя монеты для детального теста

TIMEFRAME = "15m"
LIMIT_CANDLES = 1000 # Оптимально: ~10 дней актуальной истории

# 🧪 ТУМБЛЕР МАШИНЫ ВРЕМЕНИ
TEST_START_DATE = "2026-04-01 00:00:00"

TAKE_PROFIT = 10.0   # Оригинальная цель в %
SL_BUFFER = 1.0     # Оригинальный отступ стоп-лосса в %

# ---рабочие фильтра ---
USE_CONTEXT_FILTER  = False  # Вкл/Выкл продвинутый слой анализа тренда и поджатия
USE_ANTI_KNIFE   = True     # Запрет входа против агрессивных свечей

ALLOW_LONG_TRADES  = True  
ALLOW_SHORT_TRADES = False   
MIN_SCORE = 4      # Минимальный балл зоны для входа 
USE_ZONE_GAP      = True   # Правило 2: Включить проверку "воздуха" между зонами
MIN_ZONE_GAP_PCT  = 2.0   # Минимальный зазор между Поддержкой и ближайшим Сопротивлением в %
MIN_ZONE_GAP_PCT  = 2.0   # Минимальный зазор между Поддержкой и ближайшим Сопротивлением в %

# --- ФИЛЬТР ОТСТАВАНИЯ (ДОГОНЯЮЩИЙ ВХОД) ---
USE_DISTANCE_FILTER = True # Мягкий фильтр: отмена сделки, если цена улетела далеко от зоны
MAX_DISTANCE_PCT    = 1.0  # Максимально допустимый отрыв от зоны в % (тестируем 1.0)
USE_IMPULSE_FILTER  = False   # Включаем фильтр аномальных свечей в реальную работу



# --- ТУМБЛЕРЫ ФИЛЬТРОВ ---
USE_CHOCH        = False    # Слом структуры

USE_RR_FILTER    = False   # Математический фильтр R/R
RR_RATIO         = 2.0     # Минимальный R/R
# --- ФИЛЬТР ГЛУБИНЫ (Premium/Discount) ---
USE_DEPTH_FILTER    = False
MAX_NARROW_ZONE_PCT = 2.5   # Если зона уже 2.5%, заходим сразу от края
DEEP_ENTRY_MIN      = -10.0  # Для широких зон (5%) ждем погружения минимум на 40%
DEEP_ENTRY_MAX      = 70.0  # Блокируем вход, если цена легла на дно (>85%), там опасно

USE_RANGE_FILTER = False   # Игнорировать входы, если закрытие ушло в средние 40% диапазона
USE_LEVEL_BURN   = True   # Сжигать уровень ТОЛЬКО ПОСЛЕ ПЛЮСА

# ФИЛЬТРЫ SFP
USE_SHORT_SFP_LOGIC     = False  # Искать SFP (прокол верхней границы зоны)
USE_LONG_SFP_LOGIC      = False  # Искать SFP для лонгов (прокол нижней границы зоны)

USE_RISK_CAP      = False   # Правило 3: Ограничение риска
MAX_RISK_PCT      = 5.0   # Макс. стоп-лосс в %. Сделка с большим стопом отменяется

# Настройки для LONG (Мягкие, чтобы не убить рабочие 56% профита)
MAX_DISTANCE_PCT_LONG  = 1.5    # Разрешаем отрыв до 1.5%
MAX_IMPULSE_RATIO_LONG = 3.0    # Фильтруем только совсем дикие свечи (> 3х)

# Настройки для SHORT (Жесткие, чтобы полностью вырезать баг со скринов)
MAX_DISTANCE_PCT_SHORT = 1.0    # Отрыв строго не более 1.0%
MAX_IMPULSE_RATIO_SHORT = 2.5   # Фильтруем любые свечи, которые больше средних на 80% (>= 1.8х)
# ФИЛЬТРЫ ДЛЯ ШОРТОВ (SFP + Тренд) ---
USE_SHORT_EMA200_FILTER = False  # Разрешить шорты только если цена НИЖЕ EMA 200
SHORT_CHOCH_PERIOD      = 10    # ГЛУБОКИЙ CHoCH: ищем лой за 10 свечей (а не за 2)
SHORT_LOOKBACK          = 50    # Окно поиска дна перед шортом (48 свечей = 12 часов)
SHORT_MIN_PUMP_PCT      = 2.0   # Минимальный рост от дна до сопротивления в %
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
        self.level_states = {}
        self.wait_for_bullish_choch = False
        self.choch_bull_level = 0.0
        self.wait_for_bearish_choch = False
        self.choch_bear_level = 0.0
        self.active_level = None
        self.last_closed_trades = 0
        self.current_trade_level_id = None
        self.trade_contexts = []
        self.trade_counter = 0
        
        
        high_low = pd.Series(self.data.High) - pd.Series(self.data.Low)
        self.atr = self.I(SMA, high_low, 14)
        
        # Считаем 4H EMA 200 (эмуляция на 15-минутном графике: 200 * 16 = 3200 свечей)
        def EMA(values, n):
            return pd.Series(values).ewm(span=n, adjust=False).mean()
        
        self.ema_4h_200 = self.I(EMA, self.data.Close, 3200)

        # ==========================================
        # 🎨 ДИНАМИЧЕСКАЯ ОТРИСОВКА МАКРО-УРОВНЕЙ
        # ==========================================
        # Создаем индикаторы, которые будут динамически подтягивать значения из датафрейма
        self.draw_sup_max = self.I(lambda: self.data.df['sup_max'], name="Support Top")
        self.draw_res_min = self.I(lambda: self.data.df['res_min'], name="Resist Bottom")

    def next(self):
        global GLOBAL_DEBUG_STATS, CURRENT_SUPPORTS, CURRENT_RESISTANCES, GLOBAL_TIMELINE, TARGET_COIN_CURRENT
        
        # --- МАШИНА ВРЕМЕНИ ---
        current_time = pd.to_datetime(self.data.index[-1])
        period_key = current_time.floor('12h').strftime("%Y-%m-%d %H:%M:%S")
        
        if getattr(self, 'current_period_key', None) != period_key:
            if period_key in GLOBAL_TIMELINE:
                coin_data = GLOBAL_TIMELINE[period_key].get(TARGET_COIN_CURRENT.upper(), {})
                CURRENT_SUPPORTS = coin_data.get("supports", [])
                CURRENT_RESISTANCES = coin_data.get("resistances", [])
                
                self.current_period_key = period_key
                self.burned_levels.clear()
        
        # Записываем текущие уровни в датафрейм для отрисовки на графике
        # Берем верхнюю границу самой первой поддержки и нижнюю границу самого первого сопротивления
        c_price = self.data.Close[-1]
        active_sup = CURRENT_SUPPORTS[0]['max'] if CURRENT_SUPPORTS else np.nan
        active_res = CURRENT_RESISTANCES[0]['min'] if CURRENT_RESISTANCES else np.nan
        
        # Магия backtesting.py: обновляем значения в pandas df напрямую
        self.data.df.loc[self.data.index[-1], 'sup_max'] = active_sup
        self.data.df.loc[self.data.index[-1], 'res_min'] = active_res
   

      
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

        # 🔥 ОБНОВЛЯЕМ СТАТУСЫ УРОВНЕЙ (СВЕЖИЙ ИЛИ ЗОМБИ)
        for sup in CURRENT_SUPPORTS:
            l_id = f"{sup['min']}_{sup['max']}"
            if l_id not in self.level_states:
                self.level_states[l_id] = 'FRESH'
            if c_close < sup['min']:
                self.level_states[l_id] = 'ZOMBIE (Broken Down)'

        for res in CURRENT_RESISTANCES:
            l_id = f"{res['min']}_{res['max']}"
            if l_id not in self.level_states:
                self.level_states[l_id] = 'FRESH'
            if c_close > res['max']:
                self.level_states[l_id] = 'ZOMBIE (Broken Up)'

        # ==========================================
        # 2. ФИЛЬТР ANTI-KNIFE (Остановка Локомотива - 3 свечи)
        # ==========================================
        
        is_falling_knife = False
        is_flying_rocket = False
        
        if USE_ANTI_KNIFE:
            c_atr = self.atr[-1] if not np.isnan(self.atr[-1]) else (c_high - c_low)
            c_body_abs = abs(c_close - c_open)
            
            # Замеряем безоткатную дистанцию за последние 3 свечи (45 минут)
            if len(self.data) >= 3:
                drop_3 = max(self.data.High[-3:]) - c_close  # Сколько пролетели вниз
                pump_3 = c_close - min(self.data.Low[-3:])   # Сколько пролетели вверх
            else:
                drop_3 = 0
                pump_3 = 0
                
            # Падающий нож (Запрет Лонга): 
            # Либо текущая красная свеча огромная (>1.5 ATR), 
            # Либо мы безоткатно рухнули за 3 свечи (>2.5 ATR)
            if (c_close < c_open and c_body_abs > (c_atr * 1.5)) or (drop_3 > (c_atr * 2.5)):
                is_falling_knife = True
                
            # Ракета (Запрет Шорта): 
            # Либо текущая зеленая свеча огромная (>1.5 ATR), 
            # Либо мы безоткатно взлетели за 3 свечи (>2.5 ATR)
            if (c_close > c_open and c_body_abs > (c_atr * 1.5)) or (pump_3 > (c_atr * 2.5)):
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
        # 3. LОГИКА LONG (Снайперский SFP и Касание)
        # ==========================================
        for sup in CURRENT_SUPPORTS:
            if not can_long: break 
            if sup.get('score', 0) < MIN_SCORE: continue
                
            level_id = f"{sup['min']}_{sup['max']}"
            if USE_LEVEL_BURN and level_id in self.burned_levels: continue 

            if USE_ZONE_GAP:
                closest_res = min([r['min'] for r in CURRENT_RESISTANCES if r['min'] > sup['max']], default=None)
                if closest_res:
                    gap_pct = ((closest_res - sup['max']) / sup['max']) * 100
                    if gap_pct < MIN_ZONE_GAP_PCT: continue

            # Условие пересечения зоны
            # Касание зоны
            if c_low <= sup['max'] and c_close > sup['min']:
                if is_falling_knife: continue # 🔥 Ждем следующую свечу, не отменяем зону полностью
                
                # 🔥 НОВЫЙ БЛОК: Замер глубины и отсев плохих входов
                zone_range = sup['max'] - sup['min']
                zone_width_pct = (zone_range / sup['min']) * 100 if sup['min'] > 0 else 0
                depth_pct = ((sup['max'] - c_close) / zone_range) * 100 if zone_range > 0 else 0

                if USE_DEPTH_FILTER:
                    # 🔥 УМНАЯ АДАПТАЦИЯ ГЛУБИНЫ ПО SCORE ЗОНЫ
                    # Для слабых зон (Score 3) жестко ограничиваем глубину входа
                    # Для сильных зон (Score 4+) разрешаем заходить чуть глубже
                    max_allowed_depth = 65.0 if sup.get('score', 0) < 4.0 else 85.0
                    
                    if depth_pct < 0.0 or depth_pct > max_allowed_depth:
                        continue 
                    
                    # Если зона широкая (5%+), не лезем, пока цена не даст скидку
                    if zone_width_pct >= MAX_NARROW_ZONE_PCT and depth_pct < DEEP_ENTRY_MIN:
                        continue
                
                # 🔥 НОВЫЙ БЛОК: Слой Контекста (Smart Money)
                if USE_CONTEXT_FILTER:
                    ctx_eval = evaluate_context(
                        closes=self.data.Close,
                        highs=self.data.High,
                        lows=self.data.Low,
                        current_atr=c_atr,
                        trade_type='LONG',
                        level_min=sup['min'],  # Передаем дно зоны
                        level_max=sup['max']   # Передаем потолок зоны
                    )
                    if not ctx_eval['allowed']:
                        # Записываем причину отмены в лог, чтобы видеть глазами!
                        GLOBAL_DEBUG_STATS["Killed_by_CONTEXT"] = GLOBAL_DEBUG_STATS.get("Killed_by_CONTEXT", 0) + 1
                        continue
                
                if USE_RANGE_FILTER:
                    closest_res = min([r['min'] for r in all_zones if r['min'] > sup['max']], default=None)
                    if closest_res:
                        range_size = closest_res - sup['max']
                        if (c_close - sup['max']) > (range_size * 0.30): break 

                # Вычисляем истинный бычий SFP (жесткая проверка тени отторжения)
                lower_wick = (c_close - c_low) if (c_close > c_open) else (c_open - c_low)
                c_body_abs = abs(c_close - c_open)
                # SFP засчитывается только если нижняя тень минимум в 2 раза больше тела свечи
                is_long_sfp = (c_low <= sup['min']) and (c_close > sup['min']) and (lower_wick > c_body_abs * 2.0)
                is_long_touch = not is_long_sfp # Обычное касание без ложного пробоя низа
                
            

                # Если тумблер LONG SFP включен
                if USE_LONG_SFP_LOGIC:
                    if is_long_sfp:
                        self._execute_long(sup, c_close, is_sfp=True, is_choch=False)
                        self.wait_for_bullish_choch = False
                        break
                    elif is_long_touch:
                        if USE_CHOCH:
                            self.wait_for_bullish_choch = True
                            self.choch_bull_level = max(self.data.High[-1], self.data.High[-2])
                            self.active_level = sup
                        break

                # Если тумблер LONG SFP выключен — берем всё подряд (как было раньше)
                else:
                    if USE_CHOCH:
                        self.wait_for_bullish_choch = True
                        self.choch_bull_level = max(self.data.High[-1], self.data.High[-2])
                        self.active_level = sup
                    else:
                        self._execute_long(sup, c_close, is_sfp=False, is_choch=False)
                    break

        # ==========================================
        # 4. ЛОГИКА SHORT (С диагностикой отмен и SMC)
        # ==========================================

        for res in CURRENT_RESISTANCES:
            if not can_short: break 
            if res.get('score', 0) < MIN_SCORE: continue 
                
            level_id = f"{res['min']}_{res['max']}"
            if USE_LEVEL_BURN and level_id in self.burned_levels: continue 

            # 🎯 1. СНАЧАЛА ПРОВЕРЯЕМ КАСАНИЕ ЗОНЫ (Оптимизация)
            if c_high < res['min']: continue 
                
            # 🔥 НОВЫЙ БЛОК: Замер глубины для шортов
            zone_range = res['max'] - res['min']
            zone_width_pct = (zone_range / res['min']) * 100 if res['min'] > 0 else 0
            depth_pct = ((c_close - res['min']) / zone_range) * 100 if zone_range > 0 else 0


            # 🔥 НОВЫЙ БЛОК: Слой Контекста (Smart Money)
            if USE_CONTEXT_FILTER:
                ctx_eval = evaluate_context(
                    closes=self.data.Close,
                    highs=self.data.High,
                    lows=self.data.Low,
                    current_atr=c_atr,
                    trade_type='SHORT',
                    level_min=res['min'],  # Передаем дно зоны
                    level_max=res['max']   # Передаем потолок зоны
                )
                if not ctx_eval['allowed']:
                    GLOBAL_DEBUG_STATS["Killed_by_CONTEXT"] = GLOBAL_DEBUG_STATS.get("Killed_by_CONTEXT", 0) + 1
                    continue
                if not ctx_eval['allowed']:
                    GLOBAL_DEBUG_STATS["Killed_by_CONTEXT"] = GLOBAL_DEBUG_STATS.get("Killed_by_CONTEXT", 0) + 1
                    continue

                if USE_DEPTH_FILTER:
                    # 🔥 УМНАЯ АДАПТАЦИЯ ГЛУБИНЫ ПО SCORE ЗОНЫ
                    # Для слабых зон (Score 3) жестко ограничиваем глубину входа
                    # Для сильных зон (Score 4+) разрешаем заходить чуть глубже
                    max_allowed_depth = 65.0 if res.get('score', 0) < 4.0 else 85.0

                    if depth_pct < 0.0 or depth_pct > max_allowed_depth:
                        continue
                    
                    # Если зона широкая (5%+), не лезем, пока цена не даст скидку
                    if zone_width_pct >= MAX_NARROW_ZONE_PCT and depth_pct < DEEP_ENTRY_MIN:
                        continue

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
                continue # 🔥 Ждем следующую свечу

            # 🔥 6. ИСТИННЫЙ SFP И КАСАНИЕ (С учетом тумблера USE_SHORT_SFP_LOGIC)
            upper_wick = (c_high - c_close) if (c_close < c_open) else (c_high - c_open)
            c_body_abs = abs(c_close - c_open)
            # SFP засчитывается только если верхняя тень минимум в 2 раза больше тела свечи
            is_sfp = (c_high >= res['max']) and (c_close < res['max']) and (upper_wick > c_body_abs * 2.0)
            is_touch = (c_high >= res['min']) and (c_close < res['max']) and not is_sfp
            
            

            # === СЦЕНАРИЙ А: SFP ===
            if is_sfp:
                self._execute_short(res, c_close, is_sfp=True, is_choch=False)
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
                    self._execute_short(res, c_close, is_sfp=False, is_choch=False)
                    GLOBAL_DEBUG_STATS["Passed_to_Trade"] += 1
                break

        # ОЖИДАНИЕ СЛОМА СТРУКТУРЫ
        if USE_CHOCH and self.wait_for_bearish_choch and self.active_level is not None:
            if c_close < self.choch_bear_level:
                self._execute_short(self.active_level, c_close, is_sfp=False, is_choch=True)
                self.wait_for_bearish_choch = False
                GLOBAL_DEBUG_STATS["Passed_to_Trade"] += 1
            elif c_high > self.active_level['max'] * 1.01:
                # Цена улетела выше зоны без слома CHoCH
                GLOBAL_DEBUG_STATS["Killed_by_CHOCH"] += 1
                self.wait_for_bearish_choch = False
    # ==========================================
    # 5. ИСПОЛНЕНИЕ ОРДЕРОВ (БОЕВАЯ СНАЙПЕРСКАЯ ФИЛЬТРАЦИЯ)
    # ==========================================
    def _execute_long(self, level, current_price, is_sfp=False, is_choch=False):
        current_body = abs(self.data.Close[-1] - self.data.Open[-1])
        avg_body = sum([abs(self.data.Close[-i] - self.data.Open[-i]) for i in range(2, 12)]) / 10
        avg_body = avg_body if avg_body > 0 else 0.0001
        impulse_ratio = current_body / avg_body
        
        if USE_IMPULSE_FILTER and impulse_ratio > MAX_IMPULSE_RATIO_LONG: return
        if USE_DISTANCE_FILTER and current_price > level['max']:
            distance_pct = ((current_price - level['max']) / level['max']) * 100
            if distance_pct > MAX_DISTANCE_PCT_LONG: return

        sl = level['min'] * (1 - SL_BUFFER / 100)
        risk_pct = ((current_price - sl) / current_price) * 100
        if USE_RISK_CAP and risk_pct > MAX_RISK_PCT: return 
        
        global GLOBAL_TRADE_CONTEXTS
        closest_res = min([r['min'] for r in CURRENT_RESISTANCES if r['min'] > level['max']], default=None)
        gap_pct = ((closest_res - level['max']) / level['max']) * 100 if closest_res else 999.0
        
        # Считаем насколько глубоко в зону зашла цена на момент входа
        zone_range = level['max'] - level['min']
        entry_depth = ((level['max'] - current_price) / zone_range) * 100 if zone_range > 0 else 0.0
        
        # 🔥 Вызываем внешний анализатор контекста
        c_atr = self.atr[-1] if not np.isnan(self.atr[-1]) else (self.data.High[-1] - self.data.Low[-1])
        approach_type = get_approach_type(
            closes=self.data.Close,
            highs=self.data.High,
            lows=self.data.Low,
            trade_type='LONG',
            current_atr=c_atr
        )
        
        level_id = f"{level['min']}_{level['max']}"
        lvl_state = self.level_states.get(level_id, 'UNKNOWN')
        
        # 🛑 БЛОКИРОВКА МЕРТВЫХ ЗОН
        if lvl_state != 'FRESH':
            return
            
        # 🛑 ГИБРИДНЫЙ ФИЛЬТР ДЛЯ СЛАБЫХ ЗОН (Score 3)
        if level.get('score', 0) <= 3.0 and approach_type == "COMPRESSION":
            return
        
        GLOBAL_TRADE_CONTEXTS[current_price] = {
            "state": lvl_state,
            "score": level.get('score', 0),
            "type": level.get('type', 'unknown'),
            "width": round(((level['max'] - level['min']) / level['min']) * 100, 2),
            "gap": round(gap_pct, 2),
            "sfp": is_sfp,
            "choch": is_choch,
            "depth": round(entry_depth, 1),
            "approach": approach_type  # 🔥 Сохраняем ярлык
        }
            
        tp = current_price * (1 + TAKE_PROFIT / 100)
        self.current_trade_level_id = f"{level['min']}_{level['max']}"
        self.buy(sl=sl, tp=tp)

    def _execute_short(self, level, current_price, is_sfp=False, is_choch=False):
        global GLOBAL_DEBUG_STATS
        
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
            
        # Сохраняем контекст сделки для логов
        closest_sup = max([s['max'] for s in CURRENT_SUPPORTS if s['max'] < level['min']], default=None)
        gap_pct = ((level['min'] - closest_sup) / closest_sup) * 100 if closest_sup else 999.0
        zone_width = ((level['max'] - level['min']) / level['min']) * 100
        
        global GLOBAL_TRADE_CONTEXTS
        closest_sup = max([s['max'] for s in CURRENT_SUPPORTS if s['max'] < level['min']], default=None)
        gap_pct = ((level['min'] - closest_sup) / closest_sup) * 100 if closest_sup else 999.0
        
        # Считаем насколько глубоко в зону зашла цена на момент входа
        zone_range = level['max'] - level['min']
        entry_depth = ((current_price - level['min']) / zone_range) * 100 if zone_range > 0 else 0.0
        
        # 🔥 Вызываем внешний анализатор контекста
        c_atr = self.atr[-1] if not np.isnan(self.atr[-1]) else (self.data.High[-1] - self.data.Low[-1])
        approach_type = get_approach_type(
            closes=self.data.Close,
            highs=self.data.High,
            lows=self.data.Low,
            trade_type='SHORT',
            current_atr=c_atr
        )
        
        level_id = f"{level['min']}_{level['max']}"
        lvl_state = self.level_states.get(level_id, 'UNKNOWN')
        
        # 🛑 БЛОКИРОВКА МЕРТВЫХ ЗОН
        if lvl_state != 'FRESH':
            return
            
        # 🛑 ГИБРИДНЫЙ ФИЛЬТР ДЛЯ СЛАБЫХ ЗОН (Score 3)
        if level.get('score', 0) <= 3.0 and approach_type == "COMPRESSION":
            return
        
        GLOBAL_TRADE_CONTEXTS[current_price] = {
            "state": lvl_state,
            "score": level.get('score', 0),
            "type": level.get('type', 'unknown'),
            "width": round(((level['max'] - level['min']) / level['min']) * 100, 2),
            "gap": round(gap_pct, 2),
            "sfp": is_sfp,
            "choch": is_choch,
            "depth": round(entry_depth, 1), 
            "approach": approach_type
        }

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
    try:
        exchange.load_markets()
    except:
        pass

    symbol_perp = f"{coin.upper()}/USDT:USDT"
    symbol_spot = f"{coin.upper()}/USDT"

    symbol = symbol_perp if exchange.markets and symbol_perp in exchange.markets else symbol_spot
    date_suffix = TEST_START_DATE[:10] if TEST_START_DATE else "live"
    cache_file = f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}_{date_suffix}.csv"
    
    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)
    else:
        # Убрали принт со скачиванием, чтобы не мусорил
        try:
            since_ts = int((pd.to_datetime(TEST_START_DATE) - pd.Timedelta(days=1)).timestamp() * 1000) if TEST_START_DATE else None
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES, since=since_ts) if since_ts else exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            df = pd.DataFrame(ohlcv, columns=["Open_time", "Open", "High", "Low", "Close", "Volume"])
            df.index = pd.to_datetime(df["Open_time"], unit="ms")
            df.to_csv(cache_file)
            return df
        except Exception:
            return pd.DataFrame()
        
# --- ЗАГРУЗКА МАШИНЫ ВРЕМЕНИ ---
try:
    with open('levels_timeline.json', 'r') as f:
        GLOBAL_TIMELINE = json.load(f)
except Exception as e:
    print("❌ Файл levels_timeline.json не найден. Сначала запусти precalc.py!")
    GLOBAL_TIMELINE = {}

# Берем список монет из первого ключа, просто чтобы бот понимал, кого прогонять
first_time_key = list(GLOBAL_TIMELINE.keys())[0] if GLOBAL_TIMELINE else None
macro_db = GLOBAL_TIMELINE.get(first_time_key, {}) if first_time_key else {}

if TARGET_COIN.upper() == "ALL":
    print("🤖 Аудит запущен. Собираем данные (без спама)...")
    
    for coin, data in macro_db.items():
        TARGET_COIN_CURRENT = coin # <--- ДОБАВИТЬ ЭТО
        if not isinstance(data, dict): continue
        CURRENT_SUPPORTS = data.get("supports", [])
        CURRENT_RESISTANCES = data.get("resistances", [])
        if not CURRENT_SUPPORTS and not CURRENT_RESISTANCES: continue
        
        df = get_cached_data(coin)
        if df.empty: continue
        
        # Добавляем пустые колонки под динамические уровни
        df['sup_max'] = np.nan
        df['res_min'] = np.nan
        
        bt = Backtest(df, SmartSniperUniversal, cash=10000, commission=.0006, hedging=False)
        stats = bt.run()
        
        if int(stats['# Trades']) > 0:
            tr = stats['_trades']
            longs_win = len(tr[(tr['Size'] > 0) & (tr['PnL'] > 0)])
            longs_loss = len(tr[(tr['Size'] > 0) & (tr['PnL'] <= 0)])
            shorts_win = len(tr[(tr['Size'] < 0) & (tr['PnL'] > 0)])
            shorts_loss = len(tr[(tr['Size'] < 0) & (tr['PnL'] <= 0)])
            
            GLOBAL_REPORT.append({
                "Монета": coin.upper(),
                "Лонг (+/-)": f"{longs_win}/{longs_loss}",
                "Шорт (+/-)": f"{shorts_win}/{shorts_loss}",
                "Win Rate %": round(stats['Win Rate [%]'], 2),
                "Профит %": round(stats['Return [%]'], 2)
            })
            
            #
            # Заполняем логи для плюсов и минусов
            for idx, row in tr.iterrows():
                ctx = GLOBAL_TRADE_CONTEXTS.get(row['EntryPrice'], {})
                trade_type = "LONG" if row['Size'] > 0 else "SHORT"
                
                log_str = (f"{coin.upper()} | {trade_type} | Результат: {row['ReturnPct']*100:.2f}% | "
                           f"Статус: {ctx.get('state','?')} | Подход: {ctx.get('approach','?')} | Score: {ctx.get('score','?')} | "
                           f"ГЛУБИНА: {ctx.get('depth','?')}% | Ширина: {ctx.get('width','?')}% | Gap: {ctx.get('gap','?')}%")
                
                if row['PnL'] <= 0:
                    # 🔍 АНАЛИЗ БУДУЩЕГО (Что было после стопа?)
                    exit_time = row['ExitTime']
                    # Смотрим на 5 дней вперед (1 день = 96 свечей 15m. 5 дней = 480 свечей)
                    future_df = df.loc[exit_time:].iloc[1:481] 
                    
                    post_mortem = "ПЛОХОЙ УРОВЕНЬ (Цена ушла дальше и не вернулась за 5 дней)"
                    if not future_df.empty:
                        if trade_type == "LONG":
                            tp_price = row['EntryPrice'] * (1 + TAKE_PROFIT / 100)
                            if future_df['High'].max() >= tp_price:
                                post_mortem = "МАКРО-ВЫБИВАНИЕ (Сбило стоп, но за 5 дней дошло до тейка)"
                        else:
                            tp_price = row['EntryPrice'] * (1 - TAKE_PROFIT / 100)
                            if future_df['Low'].min() <= tp_price:
                                post_mortem = "МАКРО-ВЫБИВАНИЕ (Сбило стоп, но за 5 дней дошло до тейка)"
                                
                    GLOBAL_LOSERS_LOG.append("❌ " + log_str + f" | 🔍 Диагноз: {post_mortem}")
                else:
                    GLOBAL_WINNERS_LOG.append("✅ " + log_str)
        
        # 🔥 КРИТИЧЕСКИ ВАЖНО: Очищаем глобальный контекст перед следующей монетой!
        GLOBAL_TRADE_CONTEXTS = {}
        
    print("\n" + "="*85)
    print("📊 ИТОГОВЫЙ ГЛОБАЛЬНЫЙ ОТЧЕТ")
    print("="*85)
    if GLOBAL_REPORT:
        report_df = pd.DataFrame(GLOBAL_REPORT).sort_values(by="Профит %", ascending=False)
        print(report_df.to_string(index=False))
        print("-" * 85)
        print(f"📈 Суммарный профит портфеля: {report_df['Профит %'].sum():.2f}%")
        print(f"🏆 Средний Win Rate:         {report_df['Win Rate %'].mean():.2f}%")
    else:
        print("❌ Сделок не найдено.")
        
    print("\n" + "="*115)
    print("🚀 ОТЧЕТ ПО ПРИБЫЛЬНЫМ СДЕЛКАМ (ГДЕ МЫ ЗАРАБАТЫВАЕМ)")
    print("="*115)
    if GLOBAL_WINNERS_LOG:
        for log in GLOBAL_WINNERS_LOG:
            print(log)
            
    print("\n" + "="*115)
    print("📉 ОТЧЕТ ПО УБЫТОЧНЫМ СДЕЛКАМ (ДЛЯ АНАЛИЗА ПРИЧИН)")
    print("="*115)
    if GLOBAL_LOSERS_LOG:
        for log in GLOBAL_LOSERS_LOG:
            print(log)
     
    print("\n" + "="*115)
    print("🕵️ ДИАГНОСТИКА ОТМЕН (ПОЧЕМУ БОТ НЕ ВХОДИТ В СДЕЛКИ)")
    print("="*115)
    for key, val in GLOBAL_DEBUG_STATS.items():
        print(f"  {key}: {val}")
            
else:
    print(f"📥 Запускаю детальный тест для {TARGET_COIN.upper()}...")
    TARGET_COIN_CURRENT = TARGET_COIN.upper() # <--- ДОБАВИТЬ ЭТО
    coin_data = macro_db.get(TARGET_COIN.upper(), {}) if isinstance(macro_db.get(TARGET_COIN.upper()), dict) else {}
    CURRENT_SUPPORTS = coin_data.get("supports", [])
    CURRENT_RESISTANCES = coin_data.get("resistances", [])
    
    if not CURRENT_SUPPORTS and not CURRENT_RESISTANCES:
        print(f"❌ Нет уровней для {TARGET_COIN.upper()}.")
    else:
        df = get_cached_data(TARGET_COIN)
        if df.empty:
            print("❌ Ошибка загрузки данных.")
        else:
            # Добавляем пустые колонки под динамические уровни
            df['sup_max'] = np.nan
            df['res_min'] = np.nan
            
            bt = Backtest(df, SmartSniperUniversal, cash=10000, commission=.0006, hedging=False)
            stats = bt.run()
            
            print("\n" + "="*85)
            print(f"📊 ДЕТАЛЬНЫЙ ТЕСТ ДЛЯ {TARGET_COIN.upper()}")
            print("="*85)
            print(f"💵 Конечный баланс:   ${stats['Equity Final [$]']:,.2f}")
            print(f"📈 Чистый профит:     {stats['Return [%]']:.2f}%")
            print(f"📉 Макс. просадка:    {stats['Max. Drawdown [%]']:.2f}%")
            print(f"🤝 Всего сделок:      {int(stats['# Trades'])}")
            
            if int(stats['# Trades']) > 0:
                print(f"🏆 Процент плюсовых:  {stats['Win Rate [%]']:.2f}%")
                print("-" * 85)
                tr = stats['_trades']
                for i in range(len(tr)):
                    row = tr.iloc[i]
                    pct_val = row['ReturnPct'] * 100
                    sign = "+" if pct_val > 0 else ""
                    status = "✅ ПЛЮС" if row['PnL'] > 0 else "❌ МИНУС"
                    tr_type = "LONG " if row['Size'] > 0 else "SHORT"
                    t_in = row['EntryTime'].strftime('%d.%m %H:%M')
                    t_out = row['ExitTime'].strftime('%d.%m %H:%M')
                    
                    ctx = GLOBAL_TRADE_CONTEXTS.get(row['EntryPrice'], {})
                    
                    print(f"  ▪️ {t_in} -> {t_out} | {tr_type} | {status} ({sign}{pct_val:.2f}%)")
                    print(f"     Подход: {ctx.get('approach','?')} | Score: {ctx.get('score','?')} | ГЛУБИНА: {ctx.get('depth','?')}% | Ширина: {ctx.get('width','?')}% | Gap: {ctx.get('gap','?')}%\n")
            chart_path = os.path.abspath(f'chart_{TARGET_COIN.lower()}.html')
            try:
                bt.plot(filename=chart_path, open_browser=True)
            except Exception as e:
                print(f"⚠️ График не открылся: {e}")
            
            GLOBAL_TRADE_CONTEXTS = {} 