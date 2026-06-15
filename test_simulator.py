import pandas as pd
import numpy as np
import os
import warnings

from sympy import true
warnings.filterwarnings("ignore")
from backtesting import Backtest, Strategy
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json

# =========================================================
# 1. ОСНОВНЫЕ НАСТРОЙКИ (ЕДИНЫЙ ПУЛЬТ УПРАВЛЕНИЯ)
# =========================================================
TARGET_COIN = "ALL"  # Впиши "ALL" для теста всего портфеля, или имя монеты для детального теста

TIMEFRAME = "15m"
LIMIT_CANDLES = 1000 # Оптимально: ~10 дней актуальной истории

# 🧪 ТУМБЛЕР МАШИНЫ ВРЕМЕНИ (Должен совпадать с BACKTEST_DATE из swing_hunter.py или None)
TEST_START_DATE = "2026-05-01 16:00:00"

TAKE_PROFIT = 10.0   # Цель в %
SL_BUFFER = 0.5      # Отступ стоп-лосса за уровень в %

# --- ТУМБЛЕРЫ ФИЛЬТРОВ ---
USE_CHOCH        = True   # Слом структуры
USE_ANTI_KNIFE   = True   # Запрет входа против агрессивных свечей
USE_RR_FILTER    = False   # Математический фильтр R/R
RR_RATIO         = 3.0    # Минимальный R/R

USE_RANGE_FILTER = False   # Игнорировать входы, если закрытие свечи ушло в средние 40% диапазона
USE_LEVEL_BURN   = False   # Сжигать уровень ТОЛЬКО ПОСЛЕ ПЛЮСА (чтобы не забирал 2 раза)


# =========================================================

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
        
        # МАГНИТ ДЛЯ БЕЗУБЫТКА (Новая строка)
        self.active_signals = []
        
        high_low = pd.Series(self.data.High) - pd.Series(self.data.Low)
        self.atr = self.I(SMA, high_low, 14)

        # ==========================================
        # 🎨 ОТРИСОВКА МАКРО-УРОВНЕЙ НА ГРАФИКЕ
        # ==========================================
        # Функция-пустышка для проброса линий
        def create_line(val):
            return pd.Series(val, index=self.data.index)

        # Рисуем поддержки (Зеленые зоны)
        for sup in CURRENT_SUPPORTS:
            # Верхняя граница зоны (где ждем касания)
            self.I(create_line, sup['max'], name=f"Support Top {sup['max']:.4f}", overlay=True, color="green")
            # Нижняя граница зоны (где стоит стоп)
            self.I(create_line, sup['min'], name=f"Support Bottom {sup['min']:.4f}", overlay=True, color="lightgreen")

        # Рисуем сопротивления (Красные зоны)
        for res in CURRENT_RESISTANCES:
            # Нижняя граница (где ждем касания шорта)
            self.I(create_line, res['min'], name=f"Resist Bottom {res['min']:.4f}", overlay=True, color="red")
            # Верхняя граница (где стоит стоп)
            self.I(create_line, res['max'], name=f"Resist Top {res['max']:.4f}", overlay=True, color="pink")

    def next(self):
        # ==========================================
        # 0. БЕЗУБЫТОК (БУ) ПРОВЕРКА
        # ==========================================
        for sig in self.active_signals:
            if not sig['sl_moved']:
                # Если Лонг достиг TP1
                if sig['type'] == 'LONG' and self.data.High[-1] >= sig['tp1']:
                    for t in self.trades:
                        if t.is_long and abs(t.entry_price - sig['entry']) < 1e-6:
                            # Переводим стоп в цену входа + 0.06% на комсу
                            t.sl = max(t.sl or 0, sig['entry'] * 1.0006) 
                    sig['sl_moved'] = True
                    
                # Если Шорт достиг TP1
                elif sig['type'] == 'SHORT' and self.data.Low[-1] <= sig['tp1']:
                    for t in self.trades:
                        if t.is_short and abs(t.entry_price - sig['entry']) < 1e-6:
                            # Переводим стоп в цену входа - 0.06% на комсу
                            t.sl = min(t.sl or float('inf'), sig['entry'] * 0.9994)
                    sig['sl_moved'] = True

        # Очистка памяти
        if len(self.trades) == 0:
            self.active_signals.clear()

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
        # 2. ФИЛЬТР ANTI-KNIFE
        # ==========================================
        is_falling_knife = False
        is_flying_rocket = False
        if USE_ANTI_KNIFE:
            c_atr = self.atr[-1] if not np.isnan(self.atr[-1]) else (c_high - c_low)
            if c_close < c_open and p_close < p_open:
                if (c_open - c_close) > (c_atr * 0.8) and c_vol >= p_vol:
                    is_falling_knife = True
            if c_close > c_open and p_close > p_open:
                if (c_close - c_open) > (c_atr * 0.8) and c_vol >= p_vol:
                    is_flying_rocket = True

        if self.position:
            self.wait_for_bullish_choch = False
            self.wait_for_bearish_choch = False
            return

# ==========================================
        # 3. ЛОГИКА LONG
        # ==========================================
        for sup in CURRENT_SUPPORTS:
            level_id = f"{sup['min']}_{sup['max']}"
            if USE_LEVEL_BURN and level_id in self.burned_levels: 
                continue 

            if c_low <= sup['max'] and c_close > sup['min']:
                if is_falling_knife: break
                
                if USE_RANGE_FILTER:
                    closest_res = min([r['min'] for r in CURRENT_RESISTANCES if r['min'] > sup['max']], default=None)
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
        # 4. ЛОГИКА SHORT
        # ==========================================
        for res in CURRENT_RESISTANCES:
            level_id = f"{res['min']}_{res['max']}"
            if USE_LEVEL_BURN and level_id in self.burned_levels: 
                continue 

            if c_high >= res['max'] and c_close < res['max']:
                if is_flying_rocket: break
                
                if USE_RANGE_FILTER:
                    closest_sup = max([s['max'] for s in CURRENT_SUPPORTS if s['max'] < res['min']], default=None)
                    if closest_sup:
                        range_size = res['min'] - closest_sup
                        if (res['min'] - c_close) > (range_size * 0.30):
                            break 

                if USE_CHOCH:
                    self.wait_for_bearish_choch = True
                    self.choch_bear_level = min(self.data.Low[-1], self.data.Low[-2])
                    self.active_level = res
                else:
                    self._execute_short(res, c_close)
                break

        if USE_CHOCH and self.wait_for_bearish_choch and self.active_level is not None:
            if c_close < self.choch_bear_level:
                self._execute_short(self.active_level, c_close)
                self.wait_for_bearish_choch = False
            elif c_high > self.active_level['max'] * 1.01:
                self.wait_for_bearish_choch = False


    # ==========================================
    # 5. ИСПОЛНЕНИЕ ОРДЕРОВ (3 Тейка + RR Фильтр)
    # ==========================================
    def _execute_long(self, level, current_price):
        # 1. Стоп строго за нижнюю границу зоны
        sl = level['min'] * (1 - SL_BUFFER / 100)
        risk = current_price - sl
        
        if risk <= 0: return

        # 2. TP2: Ближайшая макро-зона (Resistance) сверху
        valid_resistances = [r['min'] for r in CURRENT_RESISTANCES if r['min'] > current_price]
        if not valid_resistances:
            return  # Отмена: нет макро-целей сверху

        tp2 = min(valid_resistances)
        rr_to_tp2 = (tp2 - current_price) / risk

        # ⛔ ЖЕСТКИЙ ФИЛЬТР: Отмена, если до TP2 меньше 2.5 стопов
        if rr_to_tp2 < 2.5:
            return

        # 3. TP1: Локальный хай (15M) за последние 30 свечей
        lookback = min(30, len(self.data.High) - 1)
        if lookback > 0:
            local_high = max(self.data.High[-lookback:])
        else:
            local_high = current_price

        # Защита: если локальный хай дает меньше 1.5R, ставим математические 1.5R
        if local_high < current_price + (risk * 1.5):
            tp1 = current_price + (risk * 1.5)
        else:
            tp1 = local_high

        # 4. TP3: Дальний Runner (4R)
        tp3 = current_price + (risk * 4.0)

        print(f"[LONG] Вход: {current_price:.4f} | SL: {sl:.4f} | TP2(RR={rr_to_tp2:.1f}): {tp2:.4f}")
        
        self.current_trade_level_id = f"{level['min']}_{level['max']}"
        
        # Открываем сетку. Библиотека использует size как долю от текущего кэша.
        # Используем 0.3 для каждого, чтобы гарантированно хватило баланса на 3 ордера.
        self.buy(size=0.3, sl=sl, tp=tp1)
        self.buy(size=0.3, sl=sl, tp=tp2)
        self.buy(size=0.3, sl=sl, tp=tp3)


    def _execute_short(self, level, current_price):
        # 1. Стоп строго за верхнюю границу зоны
        sl = level['max'] * (1 + SL_BUFFER / 100)
        risk = sl - current_price
        
        if risk <= 0: return

        # 2. TP2: Ближайшая макро-зона (Support) снизу
        valid_supports = [s['max'] for s in CURRENT_SUPPORTS if s['max'] < current_price]
        if not valid_supports:
            return  # Отмена: нет макро-целей снизу

        tp2 = max(valid_supports)
        rr_to_tp2 = (current_price - tp2) / risk

        # ⛔ ЖЕСТКИЙ ФИЛЬТР: Отмена, если до TP2 меньше 2.5 стопов
        if rr_to_tp2 < 2.5:
            return

        # 3. TP1: Локальный лой (15M) за последние 12 свечей
        lookback = min(12, len(self.data.Low) - 1)
        if lookback > 0:
            local_low = min(self.data.Low[-lookback:])
        else:
            local_low = current_price

        # Защита: если локальный лой дает меньше 1.5R, ставим математические 1.5R
        if local_low > current_price - (risk * 1.5):
            tp1 = current_price - (risk * 1.5)
        elif local_low < current_price - (risk * 2.0):
            tp1 = current_price - (risk * 2.0)
        else:
            tp1 = local_low

        # 4. TP3: Дальний Runner (4R)
        tp3 = current_price - (risk * 4.0)

        print(f"[SHORT] Вход: {current_price:.4f} | SL: {sl:.4f} | TP2(RR={rr_to_tp2:.1f}): {tp2:.4f}")
        
        self.current_trade_level_id = f"{level['min']}_{level['max']}"
        
        self.sell(size=0.33, sl=sl, tp=tp1)
        self.sell(size=0.50, sl=sl, tp=tp2)
        self.sell(size=0.95, sl=sl, tp=tp3)
        
        # Записываем в память для БУ
        self.active_signals.append({
            'type': 'SHORT',
            'entry': current_price,
            'tp1': tp1,
            'sl_moved': False,
            'signal_time': self.data.index[-1]
        })

# =========================================================
# ЗАПУСК ТЕСТОВ И ОТЧЕТЫ
# =========================================================
macro_path = os.path.join("modules", "cryptano", "macro_levels.json")
macro_db = load_json(macro_path, default={}) if os.path.exists(macro_path) else {}

def get_cached_data(coin):
    symbol = f"{coin.upper()}/USDT"
    
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
            print(f"Ошибка скачивания {coin}: {e}")
            return pd.DataFrame()

# 🎯 НАСТРОЙКА ПУТИ К ТЕСТОВОЙ ПАПКЕ
if TEST_START_DATE:
    # Тестер берет файл макро-уровней прямо из изолированной папки testswing
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
        
        # --- СБОР СТАТИСТИКИ 1/5/10 ДЛЯ ВСЕХ МОНЕТ ---
        trades_df = stats.get('_trades')
        success_str = "-"
        real_trades_count = 0
        
        if trades_df is not None and not trades_df.empty:
            # Склеиваем 3 тейка в 1 сигнал
            grouped = trades_df.groupby('EntryTime').agg({'PnL': 'sum'}).reset_index()
            grouped = grouped.sort_values('EntryTime')
            
            real_trades_count = len(grouped)
            
            success_list = []
            for idx, row in grouped.iterrows():
                if row['PnL'] > 0:
                    success_list.append(str(idx + 1))
            
            # НОВАЯ ЛОГИКА ФОРМИРОВАНИЯ СТРОКИ: Плюсы / Всего
            if success_list:
                success_str = "/".join(success_list) + f"/{real_trades_count}"
            else:
                success_str = f"0/{real_trades_count}"
        else:
            real_trades_count = 0
            success_str = "0/0"
                
            real_trades_count = len(grouped)

        # Добавляем в таблицу только если были реальные сделки
        if real_trades_count > 0:
            win_rate = stats.get('Win Rate [%]', 0.0)
            if pd.isna(win_rate): win_rate = 0.0
            
            pf = stats.get('Profit Factor', 0.0)
            pf_val = round(pf, 2) if pd.notna(pf) else "Без убытка"
            
            portfolio_results.append({
                "Монета": coin.upper(),
                "Сделок": real_trades_count,
                "Win Rate %": round(win_rate, 2),
                "Profit Factor": pf_val,
                "Просадка %": round(stats.get('Max. Drawdown [%]', 0.0), 2),
                "Чистый Профит %": round(stats.get('Return [%]', 0.0), 2),
                "Успех": success_str
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
    
    print(f"📥 Запускаю детальный тест для {TARGET_COIN.upper()}...")
    df = get_cached_data(TARGET_COIN)
    if df.empty:
        print("❌ Ошибка загрузки данных.")
    else:
        bt = Backtest(df, SmartSniperUniversal, cash=10000, commission=.0006, hedging=False)
        stats = bt.run()
    
        trades_df = stats.get('_trades')
        success_str = "-"
        real_trades_count = 0
        grouped = pd.DataFrame()
        
        if trades_df is not None and not trades_df.empty:
            # Группируем ордера по времени входа (3 тейка = 1 общий сигнал)
            grouped = trades_df.groupby('EntryTime').agg({'PnL': 'sum'}).reset_index()
            grouped = grouped.sort_values('EntryTime')
            
            success_list = []
            for i, (_, row) in enumerate(grouped.iterrows(), start=1):
                if row['PnL'] > 0:
                    success_list.append(str(i))
                    
            if success_list:
                success_str = "/".join(success_list)
                
            real_trades_count = len(grouped) 
            
        print("\n" + "="*70)
        print(f"📊 ТЕСТ ДЛЯ {TARGET_COIN.upper()}")
        print(f"🛠 Фильтры: {filters_summary}")
        print("="*70)
        print(f"💵 Конечный баланс:   ${stats.get('Equity Final [$]', 0):,.2f}")
        print(f"📈 Чистый профит:     {stats.get('Return [%]', 0):.2f}%")
        print(f"📉 Макс. просадка:    {stats.get('Max. Drawdown [%]', 0):.2f}%")
        print(f"🤝 Всего сигналов:     {real_trades_count} (Успех: {success_str})")
        
        if real_trades_count > 0:
            win_rate = stats.get('Win Rate [%]', 0.0)
            if pd.isna(win_rate): win_rate = 0.0
            print(f"🏆 Процент плюсовых:  {win_rate:.2f}%")
            print("-" * 70)
            
            # Вывод лога склеенных сделок
            for i, (_, row) in enumerate(grouped.iterrows(), start=1):
                status = "✅ ПЛЮС" if row['PnL'] > 0 else "❌ МИНУС"
                t_in = row['EntryTime'].strftime('%d.%m %H:%M')
                sign = "+" if row['PnL'] > 0 else ""
                print(f"  ▪️ Сигнал №{i}: Вход {t_in} | Итог: {status} (PnL: {sign}${row['PnL']:.2f})")
                
        chart_path = os.path.abspath(f'chart_{TARGET_COIN.lower()}.html')
        bt.plot(filename=chart_path, open_browser=True)