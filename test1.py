import pandas as pd
import numpy as np
import os
from backtesting import Backtest, Strategy
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json

# =========================================================
# 1. ОСНОВНЫЕ НАСТРОЙКИ
# =========================================================
COIN = "XLM"  
SYMBOL = f"{COIN}/USDT"
TIMEFRAME = "15m"
LIMIT_CANDLES = 5000
TAKE_PROFIT = 10.0  # Твоя цель в процентах (можешь ставить 10, 15 и т.д.)
SL_BUFFER = 0.3

# =========================================================
# 2. ТУМБЛЕРЫ ФИЛЬТРОВ (Включай/Выключай для тестов)
# =========================================================
USE_CHOCH       = True   # Ждать пробития локального максимума/минимума после прокола зоны
USE_COOLDOWN    = False  # Пауза 4 часа после сделки, закрытой по Стоп-Лоссу
USE_RR_FILTER   = True   # Вход только если математический R/R (Риск к Прибыли) >= 1:3

USE_ANTI_KNIFE  = True  # Запрет входа против агрессивных полнотелых свечей с ростом объема
USE_BUFFER      = False  # Допуск 0.2% на "недолет" до уровня (Front-run)
USE_MICRO_CHOCH = False  # Ранний вход: слом структуры по телу свечи, а не по тени
# =========================================================
USE_RR_FILTER = True
RR_RATIO = 3.0  # Поставь 1.0 или 1.5 для скальпинга (вместо 3.0)

print(f"📥 Загружаю уровни для {COIN}...")
macro_path = os.path.join("modules", "cryptano", "macro_levels.json")
macro_db = load_json(macro_path, default={}) if os.path.exists(macro_path) else {}

# Жесткая проверка: если монеты нет или файл пустой — берем пустой список вместо None
COIN_DATA = macro_db.get(COIN, {}) if isinstance(macro_db.get(COIN), dict) else {}
SUPPORTS = COIN_DATA.get("supports", []) if COIN_DATA.get("supports") is not None else []
RESISTANCES = COIN_DATA.get("resistances", []) if COIN_DATA.get("resistances") is not None else []

cache_file = f"cache_{COIN}_{TIMEFRAME}_{LIMIT_CANDLES}.csv"
if os.path.exists(cache_file):
    print(f"📦 Беру свечи из локального кэша ({cache_file})...")
    df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
else:
    print("🌐 Скачиваю свечи с биржи и создаю кэш...")
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
    df = pd.DataFrame(ohlcv, columns=["Open_time", "Open", "High", "Low", "Close", "Volume"])
    df.index = pd.to_datetime(df["Open_time"], unit="ms")
    df.to_csv(cache_file)

# Вспомогательная функция для ATR (нужна для Анти-Ножа)
def SMA(arr, n):
    return pd.Series(arr).rolling(n).mean()

class SmartSniperV2(Strategy):
    def init(self):
        # Память для CHoCH
        self.wait_for_bullish_choch = False
        self.choch_bull_level = 0.0
        
        self.wait_for_bearish_choch = False
        self.choch_bear_level = 0.0
        
        self.active_level = None # Запоминаем уровень, от которого ждем слома
        
        # Расчет среднего размера свечи для Анти-Ножа
        high_low = pd.Series(self.data.High) - pd.Series(self.data.Low)
        self.atr = self.I(SMA, high_low, 14)

    def next(self):
        if len(self.data) < 5: return
        
        c_close, c_open = self.data.Close[-1], self.data.Open[-1]
        c_high, c_low = self.data.High[-1], self.data.Low[-1]
        c_vol = self.data.Volume[-1]
        
        p_close, p_open = self.data.Close[-2], self.data.Open[-2]
        p_vol = self.data.Volume[-2]
        
        # --- ФИЛЬТР 1: COOLDOWN ---
        if USE_COOLDOWN and len(self.closed_trades) > 0:
            last_trade = self.closed_trades[-1]
            if last_trade.pl < 0 and last_trade.exit_time is not None:
                exit_time = pd.Timestamp(last_trade.exit_time)
                current_time = pd.Timestamp(self.data.index[-1])
                if (current_time - exit_time) < pd.Timedelta(hours=4):
                    return

        # --- ФИЛЬТР: ANTI-KNIFE ---
        is_falling_knife = False
        is_flying_rocket = False
        
        if USE_ANTI_KNIFE and len(self.data) > 15:
            c_atr = self.atr[-1] if not np.isnan(self.atr[-1]) else (c_high - c_low)
            # Нож: 2 красные подряд, последняя полнотелая (>80% ATR), объем растет
            if c_close < c_open and p_close < p_open:
                if (c_open - c_close) > (c_atr * 0.8) and c_vol >= p_vol:
                    is_falling_knife = True
            # Ракета: 2 зеленые подряд, полнотелая, объем растет
            if c_close > c_open and p_close > p_open:
                if (c_close - c_open) > (c_atr * 0.8) and c_vol >= p_vol:
                    is_flying_rocket = True

        if self.position: 
            self.wait_for_bullish_choch = False
            self.wait_for_bearish_choch = False
            return

        # --- ФИЛЬТР: BUFFER ---
        buf_sup = 1.002 if USE_BUFFER else 1.0  # +0.2% допуска к поддержке
        buf_res = 0.998 if USE_BUFFER else 1.0  # -0.2% допуска к сопротивлению

        # ==========================================
        # ЛОГИКА LONG (Поддержки)
        # ==========================================
        for sup in SUPPORTS:
            # 1. Ищем SFP (с учетом буфера недолета)
            target_low = sup['min'] * buf_sup
            is_sweep = c_low <= target_low and c_close > sup['min']
            
            if is_sweep:
                if is_falling_knife:
                    break # Летит нож, игнорируем уровень
                
                if USE_CHOCH:
                    self.wait_for_bullish_choch = True
                    self.active_level = sup
                    # --- ФИЛЬТР: MICRO-CHOCH ---
                    if USE_MICRO_CHOCH:
                        self.choch_bull_level = max(c_open, c_close) # Слом по телу
                    else:
                        self.choch_bull_level = max(self.data.High[-1], self.data.High[-2]) # Слом по тени
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
        # ЛОГИКА SHORT (Сопротивления)
        # ==========================================
        for res in RESISTANCES:
            target_high = res['max'] * buf_res
            is_sweep = c_high >= target_high and c_close < res['max']
            
            if is_sweep:
                if is_flying_rocket:
                    break # Летит ракета, игнорируем уровень
                
                if USE_CHOCH:
                    self.wait_for_bearish_choch = True
                    self.active_level = res
                    # --- ФИЛЬТР: MICRO-CHOCH ---
                    if USE_MICRO_CHOCH:
                        self.choch_bear_level = min(c_open, c_close) # Слом по телу
                    else:
                        self.choch_bear_level = min(self.data.Low[-1], self.data.Low[-2]) # Слом по тени
                else:
                    self._execute_short(res, c_close)
                break

        if USE_CHOCH and self.wait_for_bearish_choch and self.active_level is not None:
            if c_close < self.choch_bear_level:
                self._execute_short(self.active_level, c_close)
                self.wait_for_bearish_choch = False
            elif c_high > self.active_level['max'] * 1.01:
                self.wait_for_bearish_choch = False

    # --- ФУНКЦИИ ВХОДА (С расчетом R/R) ---
    def _execute_long(self, level, current_price):
        sl = level['min'] * (1 - SL_BUFFER / 100)
        tp = current_price * (1 + TAKE_PROFIT / 100)
        risk = current_price - sl
        reward = tp - current_price
        # Для _execute_long:
        if USE_RR_FILTER and (reward / risk) < RR_RATIO:
            return
        self.buy(sl=sl, tp=tp)

    def _execute_short(self, level, current_price):
        sl = level['max'] * (1 + SL_BUFFER / 100)
        tp = current_price * (1 - TAKE_PROFIT / 100)
        risk = sl - current_price
        reward = current_price - tp
        # Для _execute_short:
        if USE_RR_FILTER and (reward / risk) < RR_RATIO:
            return
        self.sell(sl=sl, tp=tp)


# Запуск
bt = Backtest(df, SmartSniperV2, cash=10000, commission=.0006, hedging=False)
stats = bt.run()

# ОТЧЕТ
print("\n" + "="*60)
print(f"📊 A/B ТЕСТ СТРАТЕГИИ ДЛЯ {COIN}")
print(f"   [ Anti-Knife: {USE_ANTI_KNIFE} | Buffer: {USE_BUFFER} | Micro-CHoCH: {USE_MICRO_CHOCH} ]")
print("="*60)
print(f"💰 Начальный депозит:  $10,000.00")
print(f"💵 Конечный баланс:   ${stats['Equity Final [$]']:,.2f}")
print(f"📈 Чистый профит:     {stats['Return [%]']:.2f}%")
print(f"📉 Макс. просадка:    {stats['Max. Drawdown [%]']:.2f}%")
print("-" * 60)
print(f"🤝 Всего сделок:       {int(stats['# Trades'])}")

if int(stats['# Trades']) > 0:
    print(f"🏆 Процент плюсовых:  {stats['Win Rate [%]']:.2f}%")
    pf = stats['Profit Factor']
    print(f"⚖️ Profit Factor:      {pf:.2f}" if pd.notna(pf) else "⚖️ Profit Factor:      Без убытка")
    print("-" * 60)
    
    trades = stats['_trades']
    for idx, row in trades.iterrows():
        pnl_cash = row['PnL']
        pnl_pct = row['ReturnPct'] * 100 
        status = f"✅ ПЛЮС  ( +${pnl_cash:,.2f} | +{pnl_pct:.2f}% )" if pnl_cash > 0 else f"❌ МИНУС ( -${abs(pnl_cash):,.2f} | {pnl_pct:.2f}% )"
        trade_type = "LONG " if row['Size'] > 0 else "SHORT"
        print(f"  ▪️ Сделка №{idx+1} ({trade_type}): Вход {row['EntryTime'].strftime('%d.%m %H:%M')} -> Выход {row['ExitTime'].strftime('%d.%m %H:%M')} | {status}")
else:
    print("❌ Ни одной сделки не прошло фильтры.")
print("="*60 + "\n")

chart_path = os.path.abspath('my_chart.html')
print(f"🔎 ИЩИ ГРАФИК ЗДЕСЬ: {chart_path}")
bt.plot(filename=chart_path, open_browser=True)