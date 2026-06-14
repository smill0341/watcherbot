import pandas as pd
import numpy as np
import os
from backtesting import Backtest, Strategy
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json

# =========================================================
# 1. ОСНОВНЫЕ НАСТРОЙКИ ПОРТФЕЛЯ
# =========================================================
TIMEFRAME = "15m"
LIMIT_CANDLES = 5000
TAKE_PROFIT = 7.0   # Тейк-профит
SL_BUFFER = 0.5     # Отступ стопа за уровень в %

# =========================================================
# 2. ТУМБЛЕРЫ СТРАТЕГИИ
# =========================================================
USE_CHOCH       = True
USE_ANTI_KNIFE  = True
USE_RR_FILTER   = True

# Глобальные переменные для передачи уровней внутрь стратегии
CURRENT_SUPPORTS = []
CURRENT_RESISTANCES = []

def SMA(arr, n):
    return pd.Series(arr).rolling(n).mean()

class SmartSniperPortfolio(Strategy):
    def init(self):
        self.wait_for_bullish_choch = False
        self.choch_bull_level = 0.0
        self.wait_for_bearish_choch = False
        self.choch_bear_level = 0.0
        self.active_level = None
        
        high_low = pd.Series(self.data.High) - pd.Series(self.data.Low)
        self.atr = self.I(SMA, high_low, 14)

    def next(self):
        if len(self.data) < 15: return
        
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

        # LONG
        for sup in CURRENT_SUPPORTS:
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
                self._execute_long(self.active_level, c_close)
                self.wait_for_bullish_choch = False
            elif c_low < self.active_level['min'] * 0.99:
                self.wait_for_bullish_choch = False

        # SHORT
        for res in CURRENT_RESISTANCES:
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
                self._execute_short(self.active_level, c_close)
                self.wait_for_bearish_choch = False
            elif c_high > self.active_level['max'] * 1.01:
                self.wait_for_bearish_choch = False

    def _execute_long(self, level, current_price):
        sl = level['min'] * (1 - SL_BUFFER / 100)
        tp = current_price * (1 + TAKE_PROFIT / 100)
        if USE_RR_FILTER and ((tp - current_price) / (current_price - sl)) < 3.0: return
        self.buy(sl=sl, tp=tp)

    def _execute_short(self, level, current_price):
        sl = level['max'] * (1 + SL_BUFFER / 100)
        tp = current_price * (1 - TAKE_PROFIT / 100)
        if USE_RR_FILTER and ((current_price - tp) / (sl - current_price)) < 3.0: return
        self.sell(sl=sl, tp=tp)

# =========================================================
# ГЛАВНЫЙ ЦИКЛ ОЦЕНКИ ПОРТФЕЛЯ
# =========================================================
macro_path = os.path.join("modules", "cryptano", "macro_levels.json")
macro_db = load_json(macro_path, default={}) if os.path.exists(macro_path) else {}

portfolio_results = []

print(f"🤖 Начинаю глобальный аудит портфеля на основе макро-уровней...")

for coin, data in macro_db.items():
    if not isinstance(data, dict): continue
    
    # Извлекаем зоны
    CURRENT_SUPPORTS = data.get("supports", [])
    CURRENT_RESISTANCES = data.get("resistances", [])
    
    # Если по монете нет прописанных уровней — пропускаем её
    if not CURRENT_SUPPORTS and not CURRENT_RESISTANCES: continue
    
    symbol = f"{coin.upper()}/USDT"
    cache_file = f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}.csv"
    
    # Загрузка / Кэширование
    try:
        if os.path.exists(cache_file):
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
        else:
            print(f"🌐 Скачиваю свечи для {symbol}...")
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            df = pd.DataFrame(ohlcv, columns=["Open_time", "Open", "High", "Low", "Close", "Volume"])
            df.index = pd.to_datetime(df["Open_time"], unit="ms")
            df.to_csv(cache_file)
            
        if df.empty: continue
        
        # Запуск бэктеста для конкретной монеты (без вывода графиков)
        bt = Backtest(df, SmartSniperPortfolio, cash=10000, commission=.0006, hedging=False)
        stats = bt.run()
        
        trades_count = int(stats['# Trades'])
        
        # Сохраняем метрики, только если были сделки
        if trades_count > 0:
            portfolio_results.append({
                "Монета": coin.upper(),
                "Сделок": trades_count,
                "Win Rate %": round(stats['Win Rate [%]'], 2),
                "Profit Factor": round(stats['Profit Factor'], 2) if pd.notna(stats['Profit Factor']) else "Без убытка",
                "Просадка %": round(stats['Max. Drawdown [%]'], 2),
                "Чистый Профит %": round(stats['Return [%]'], 2)
            })
            
    except Exception as e:
        # Если монеты нет на споте Bybit или произошла ошибка — просто идем дальше
        continue

# Вывод итогового сжатого отчета
print("\n" + "="*75)
print(f"📊 СВОДНЫЙ СРЕЗ ГЛОБАЛЬНОГО ТЕСТА СТРАТЕГИИ")
print(f"   Настройки: TP={TAKE_PROFIT}% | SL Buffer={SL_BUFFER}% | CHoCH={USE_CHOCH} | Anti-Knife={USE_ANTI_KNIFE}")
print("="*75)

if portfolio_results:
    report_df = pd.DataFrame(portfolio_results)
    # Сортируем таблицу: самые прибыльные монеты будут сверху
    report_df = report_df.sort_values(by="Чистый Профит %", ascending=False)
    
    # Красивый вывод таблицы в консоль без лишнего мусора
    print(report_df.to_string(index=False))
    
    print("-" * 75)
    total_profit = report_df["Чистый Профит %"].sum()
    avg_winrate = report_df["Win Rate %"].mean()
    max_dd = report_df["Просадка %"].min() # Максимальный минус
    
    print(f"📈 Суммарный результат по всем торгуемым активам: {total_profit:.2f}%")
    print(f"🏆 Средний Win Rate портфеля:                    {avg_winrate:.2f}%")
    print(f"📉 Худшая просадка среди всех монет:             {max_dd:.2f}%")
else:
    print("❌ Фильтры оказались настолько жесткими, что ни одна монета из базы не нашла условий для входа.")
print("="*75 + "\n")