import pandas as pd
import numpy as np
import os
from backtesting import Backtest, Strategy
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json

# =========================================================
# 1. ОСНОВНЫЕ НАСТРОЙКИ (ЕДИНЫЙ ПУЛЬТ УПРАВЛЕНИЯ)
# =========================================================
TARGET_COIN = "XLM"  # Впиши "ALL" для теста всего портфеля, или имя монеты для детального теста

TIMEFRAME = "15m"
LIMIT_CANDLES = 5000

TAKE_PROFIT = 10.0    # Цель в %
SL_BUFFER = 0.5      # Отступ стоп-лосса за уровень в %

# --- ТУМБЛЕРЫ ФИЛЬТРОВ ---
USE_CHOCH        = True   # Слом структуры
USE_ANTI_KNIFE   = True   # Запрет входа против агрессивных свечей
USE_RR_FILTER    = True   # Математический фильтр R/R
RR_RATIO         = 3.0    # Минимальный R/R

USE_RANGE_FILTER = True  # Игнорировать входы, если закрытие свечи ушло в средние 40% диапазона
USE_LEVEL_BURN   = True  # Сжигать уровень (удалять из памяти) после открытия сделки от него
# =========================================================

CURRENT_SUPPORTS = []
CURRENT_RESISTANCES = []

def SMA(arr, n):
    return pd.Series(arr).rolling(n).mean()

class SmartSniperUniversal(Strategy):
    def init(self):
        self.burned_levels = set()      # Память для уровней, давших ПЛЮС
        self.active_level_id = None     # Ярлык уровня для текущей сделки

        self.wait_for_bullish_choch = False
        self.choch_bull_level = 0.0
        self.wait_for_bearish_choch = False
        self.choch_bear_level = 0.0
        self.active_level = None
        
        high_low = pd.Series(self.data.High) - pd.Series(self.data.Low)
        self.atr = self.I(SMA, high_low, 14)

    def next(self):
        if len(self.data) < 15: return
        
        # ==========================================
        # ЖЕЛЕЗОБЕТОННОЕ СЖИГАНИЕ УРОВНЯ
        # ==========================================
        # Если мы были в позиции, а теперь нет — значит сделка только что закрылась
        if not self.position and self.active_level_id is not None:
            if len(self.closed_trades) > 0:
                last_trade = self.closed_trades[-1]
                # Если закрыли в плюс — сжигаем уровень навсегда
                if last_trade.pl > 0:
                    self.burned_levels.add(self.active_level_id)
            # Очищаем ярлык до следующей сделки
            self.active_level_id = None

        c_close, c_open = self.data.Close[-1], self.data.Open[-1]
        c_high, c_low = self.data.High[-1], self.data.Low[-1]
        c_vol = self.data.Volume[-1]
        
        p_close, p_open = self.data.Close[-2], self.data.Open[-2]
        p_vol = self.data.Volume[-2]

        # ANTI-KNIFE
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
        # ЛОГИКА LONG
        # ==========================================
        for sup in CURRENT_SUPPORTS:
            level_id = f"{sup['min']}_{sup['max']}"
            
            # Если уровень уже дал плюс — пропускаем его
            if USE_LEVEL_BURN and level_id in self.burned_levels:
                continue 

            if c_low <= sup['max'] and c_close > sup['min']:
                if is_falling_knife: break
                
                if USE_CHOCH:
                    self.wait_for_bullish_choch = True
                    self.choch_bull_level = max(self.data.High[-1], self.data.High[-2])
                    self.active_level = sup
                else:
                    self._execute_long(sup, c_close)
                break

        if USE_CHOCH and self.wait_for_bullish_choch and self.active_level is not None:
            if c_close > self.choch_bull_level:
                is_valid_range = True
                if USE_RANGE_FILTER:
                    closest_res = min([r['min'] for r in CURRENT_RESISTANCES if r['min'] > self.active_level['max']], default=None)
                    if closest_res:
                        range_size = closest_res - self.active_level['max']
                        if (c_close - self.active_level['max']) > (range_size * 0.30):
                            is_valid_range = False 
                
                if is_valid_range:
                    self._execute_long(self.active_level, c_close)
                
                self.wait_for_bullish_choch = False
            elif c_low < self.active_level['min'] * 0.99:
                self.wait_for_bullish_choch = False

        # ==========================================
        # ЛОГИКА SHORT
        # ==========================================
        for res in CURRENT_RESISTANCES:
            level_id = f"{res['min']}_{res['max']}"
            
            # Если уровень уже дал плюс — пропускаем его
            if USE_LEVEL_BURN and level_id in self.burned_levels:
                continue 

            if c_high >= res['max'] and c_close < res['max']:
                if is_flying_rocket: break
                
                if USE_CHOCH:
                    self.wait_for_bearish_choch = True
                    self.choch_bear_level = min(self.data.Low[-1], self.data.Low[-2])
                    self.active_level = res
                else:
                    self._execute_short(res, c_close)
                break

        if USE_CHOCH and self.wait_for_bearish_choch and self.active_level is not None:
            if c_close < self.choch_bear_level:
                is_valid_range = True
                if USE_RANGE_FILTER:
                    closest_sup = max([s['max'] for s in CURRENT_SUPPORTS if s['max'] < self.active_level['min']], default=None)
                    if closest_sup:
                        range_size = self.active_level['min'] - closest_sup
                        if (self.active_level['min'] - c_close) > (range_size * 0.30):
                            is_valid_range = False
                
                if is_valid_range:
                    self._execute_short(self.active_level, c_close)
                
                self.wait_for_bearish_choch = False
            elif c_high > self.active_level['max'] * 1.01:
                self.wait_for_bearish_choch = False

    # ==========================================
    # ИСПОЛНЕНИЕ ОРДЕРОВ И ПРИВЯЗКА ЯРЛЫКА
    # ==========================================
    def _execute_long(self, level, current_price):
        sl = level['min'] * (1 - SL_BUFFER / 100)
        tp = current_price * (1 + TAKE_PROFIT / 100)
        risk = current_price - sl
        reward = tp - current_price
        
        if USE_RR_FILTER and (risk == 0 or (reward / risk) < RR_RATIO): return
        
        # Клеим ярлык с ID уровня на эту сделку
        self.active_level_id = f"{level['min']}_{level['max']}"
        self.buy(sl=sl, tp=tp)

    def _execute_short(self, level, current_price):
        sl = level['max'] * (1 + SL_BUFFER / 100)
        tp = current_price * (1 - TAKE_PROFIT / 100)
        risk = sl - current_price
        reward = current_price - tp
        
        if USE_RR_FILTER and (risk == 0 or (reward / risk) < RR_RATIO): return
        
        # Клеим ярлык с ID уровня на эту сделку
        self.active_level_id = f"{level['min']}_{level['max']}"
        self.sell(sl=sl, tp=tp)

# =========================================================
# ЗАПУСК ТЕСТОВ
# =========================================================
macro_path = os.path.join("modules", "cryptano", "macro_levels.json")
macro_db = load_json(macro_path, default={}) if os.path.exists(macro_path) else {}

def get_cached_data(coin):
    symbol = f"{coin.upper()}/USDT"
    cache_file = f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}.csv"
    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)
    else:
        print(f"🌐 Скачиваю свечи для {symbol}...")
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            df = pd.DataFrame(ohlcv, columns=["Open_time", "Open", "High", "Low", "Close", "Volume"])
            df.index = pd.to_datetime(df["Open_time"], unit="ms")
            df.to_csv(cache_file)
            return df
        except Exception as e:
            return pd.DataFrame()

if TARGET_COIN.upper() == "ALL":
    print(f"🤖 Начинаю глобальный аудит портфеля (TP={TAKE_PROFIT}%, R/R={RR_RATIO}, RangeFilter={USE_RANGE_FILTER}, Burn={USE_LEVEL_BURN})...")
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

    print("\n" + "="*75)
    print(f"📊 СВОДНЫЙ СРЕЗ ГЛОБАЛЬНОГО ТЕСТА СТРАТЕГИИ")
    print("="*75)
    if portfolio_results:
        report_df = pd.DataFrame(portfolio_results).sort_values(by="Чистый Профит %", ascending=False)
        print(report_df.to_string(index=False))
        print("-" * 75)
        print(f"📈 Суммарный профит портфеля: {report_df['Чистый Профит %'].sum():.2f}%")
        print(f"🏆 Средний Win Rate:          {report_df['Win Rate %'].mean():.2f}%")
    else:
        print("❌ Нет сделок. Фильтры отсекли всё.")
    print("="*75 + "\n")

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
        
        print("\n" + "="*60)
        print(f"📊 ТЕСТ ДЛЯ {TARGET_COIN.upper()} | RangeFilter={USE_RANGE_FILTER} | Burn={USE_LEVEL_BURN}")
        print("="*60)
        print(f"💵 Конечный баланс:   ${stats['Equity Final [$]']:,.2f}")
        print(f"📈 Чистый профит:     {stats['Return [%]']:.2f}%")
        print(f"📉 Макс. просадка:    {stats['Max. Drawdown [%]']:.2f}%")
        print(f"🤝 Всего сделок:       {int(stats['# Trades'])}")
        
        if int(stats['# Trades']) > 0:
            print(f"🏆 Процент плюсовых:  {stats['Win Rate [%]']:.2f}%")
            print("-" * 60)
            for idx, row in stats['_trades'].iterrows():
                status = f"✅ ПЛЮС" if row['PnL'] > 0 else f"❌ МИНУС"
                tr_type = "LONG " if row['Size'] > 0 else "SHORT"
                print(f"  ▪️ Сделка №{idx+1} ({tr_type}): {row['EntryTime'].strftime('%d.%m %H:%M')} -> {row['ExitTime'].strftime('%d.%m %H:%M')} | {status} ({row['ReturnPct']*100:.2f}%)")
        
        chart_path = os.path.abspath(f'chart_{TARGET_COIN.lower()}.html')
        bt.plot(filename=chart_path, open_browser=True)