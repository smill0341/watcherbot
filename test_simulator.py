"""
test_simulator.py
==================
Бэктест-движок. Только оркестрация:
загружает данные -> для каждой свечи спрашивает WatcherManager "входить?"
-> если да, исполняет ордер в backtesting.py -> копит статистику.

Вся логика "входить или нет" живёт в watcher_manager.py / watcher_methods.py.
Этот файл не содержит правил входа — только их вызов и учёт результатов.

Переключение стратегии: STRATEGY = "SWEEP_RECLAIM" или "CHOCH" (см. ниже).
"""

import pandas as pd
import numpy as np
import os
import warnings
import json

warnings.filterwarnings("ignore")
from backtesting import Backtest, Strategy
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json
from modules.cryptano.utils.testswing.context_filter import analyze_context
from modules.cryptano.utils.testswing.watcher_manager import WatcherManager


GLOBAL_DEBUG_STATS = {
    "Killed_by_CONTEXT": 0,
    "Killed_by_QUALITY": 0,   # score / zone_gap / level_burn отсеяли до watcher
    "No_Signal": 0,           # watcher не дал сигнала
    "Passed_to_Trade": 0,
}
GLOBAL_REPORT = []
GLOBAL_LOSERS_LOG = []
GLOBAL_TRADE_CONTEXTS = {}
GLOBAL_WINNERS_LOG = []
GLOBAL_APPROACH_STATS = {"IMPULSE": {"trades": 0, "win": 0}, "COMPRESSION": {"trades": 0, "win": 0}, "NORMAL": {"trades": 0, "win": 0}}

# =========================================================
# 1. ОСНОВНЫЕ НАСТРОЙКИ (ЕДИНЫЙ ПУЛЬТ УПРАВЛЕНИЯ)
# =========================================================
TARGET_COIN = "APT"  # "ALL" для всего портфеля, или имя монеты для детального теста

TIMEFRAME = "15m"
LIMIT_CANDLES = 1000

TEST_START_DATE = "2026-04-01 00:00:00"

ALLOW_LONG_TRADES = True
ALLOW_SHORT_TRADES = False

# Какой метод определения точки входа использовать: "SWEEP_RECLAIM" или "CHOCH"
STRATEGY = "SWEEP_RECLAIM"

USE_CONTEXT_FILTER = True  # макро-контекст (тренд/импульс/поджатие) из context_filter.py

# Конфиг для WatcherManager - всё, что реально используется, в одном месте.
WATCHER_CONFIG = {
    'MIN_SCORE': 5,
    'USE_ZONE_GAP': True,
    'MIN_ZONE_GAP_PCT': 2.0,
    'USE_LEVEL_BURN': True,
    'TAKE_PROFIT': 8.0,
    'SL_BUFFER': 1.0,
    # только для CHOCH:
    'CHOCH_LOOKBACK': 15,
    'CHOCH_ANTI_KNIFE_ATR_MULT': 0.8,
    'USE_RR_FILTER': True,
    'RR_RATIO': 2.0,
}

CURRENT_SUPPORTS = []
CURRENT_RESISTANCES = []


def SMA(arr, n):
    return pd.Series(arr).rolling(n).mean()


class SmartSniperUniversal(Strategy):
    def init(self):
        self.manager = WatcherManager(strategy=STRATEGY, config=WATCHER_CONFIG)
        self.level_states = {}
        self.last_closed_trades = 0
        self.current_trade_level_id = None

        high_low = pd.Series(self.data.High) - pd.Series(self.data.Low)
        self.atr = self.I(SMA, high_low, 14)

        def EMA(values, n):
            return pd.Series(values).ewm(span=n, adjust=False).mean()

        self.ema_4h_200 = self.I(EMA, self.data.Close, 3200)

        self.draw_sup_max = self.I(lambda: self.data.df['sup_max'], name="Support Top", overlay=True)
        self.draw_res_min = self.I(lambda: self.data.df['res_min'], name="Resist Bottom", overlay=True)

    def next(self):
        global GLOBAL_DEBUG_STATS, CURRENT_SUPPORTS, CURRENT_RESISTANCES, GLOBAL_TIMELINE, TARGET_COIN_CURRENT

        # --- МАШИНА ВРЕМЕНИ: обновление уровней каждые 12 часов ---
        current_time = pd.to_datetime(self.data.index[-1])
        period_key = current_time.floor('12h').strftime("%Y-%m-%d %H:%M:%S")

        if getattr(self, 'current_period_key', None) != period_key:
            if period_key in GLOBAL_TIMELINE:
                coin_data = GLOBAL_TIMELINE[period_key].get(TARGET_COIN_CURRENT.upper(), {})
                CURRENT_SUPPORTS = coin_data.get("supports", [])
                CURRENT_RESISTANCES = coin_data.get("resistances", [])
                self.current_period_key = period_key

                # Сжигаем уровни заново под новый список, но НЕ трогаем watcher'ы
                # в процессе (BELOW/ABOVE) - им нужно дожить свой цикл.
                # Только для SWEEP_RECLAIM (CHOCH не хранит persistent watcher).
                if STRATEGY == "SWEEP_RECLAIM":
                    current_level_ids = set()
                    for s in CURRENT_SUPPORTS:
                        current_level_ids.add(f"LONG_{s['min']}_{s['max']}")
                    for r in CURRENT_RESISTANCES:
                        current_level_ids.add(f"SHORT_{r['min']}_{r['max']}")
                    self.manager.clear_dead_watchers(current_level_ids)

        # Отрисовка уровней на графике
        active_sup = CURRENT_SUPPORTS[0]['max'] if CURRENT_SUPPORTS else np.nan
        active_res = CURRENT_RESISTANCES[0]['min'] if CURRENT_RESISTANCES else np.nan
        self.data.df.loc[self.data.index[-1], 'sup_max'] = active_sup
        self.data.df.loc[self.data.index[-1], 'res_min'] = active_res

        # --- Сжигание уровня ТОЛЬКО после прибыльного закрытия ---
        if len(self.closed_trades) > self.last_closed_trades:
            last_trade = self.closed_trades[-1]
            if last_trade.pl > 0 and self.current_trade_level_id is not None:
                if WATCHER_CONFIG.get('USE_LEVEL_BURN', True):
                    self.manager.burned_levels.add(self.current_trade_level_id)
            self.current_trade_level_id = None
            self.last_closed_trades = len(self.closed_trades)

        if len(self.data) < max(15, WATCHER_CONFIG.get('CHOCH_LOOKBACK', 15) + 1):
            return

        if self.position:
            return

        if not CURRENT_SUPPORTS and not CURRENT_RESISTANCES:
            return

        c_open, c_close = self.data.Open[-1], self.data.Close[-1]
        c_high, c_low = self.data.High[-1], self.data.Low[-1]
        c_atr = self.atr[-1] if not np.isnan(self.atr[-1]) else (c_high - c_low)

        can_long = len(CURRENT_SUPPORTS) > 0 and ALLOW_LONG_TRADES
        can_short = len(CURRENT_RESISTANCES) > 0 and ALLOW_SHORT_TRADES

        # df-срез для CHOCH (нужна растущая история с колонкой atr)
        df_slice = None
        if STRATEGY == "CHOCH":
            df_slice = self.data.df.iloc[:len(self.data)].copy()
            df_slice.columns = [c.lower() for c in df_slice.columns]
            df_slice['atr'] = self.atr[:len(self.data)]

        # =========================================================
        # LONG
        # =========================================================
        if can_long:
            for sup in CURRENT_SUPPORTS:
                decision = self._evaluate(sup, 'LONG', c_open, c_high, c_low, c_close,
                                           CURRENT_RESISTANCES, df_slice)
                if decision['allow']:
                    self._try_enter(sup, 'LONG', c_close, c_atr, decision)
                    break

        # =========================================================
        # SHORT
        # =========================================================
        if can_short:
            for res in CURRENT_RESISTANCES:
                decision = self._evaluate(res, 'SHORT', c_open, c_high, c_low, c_close,
                                           CURRENT_SUPPORTS, df_slice)
                if decision['allow']:
                    self._try_enter(res, 'SHORT', c_close, c_atr, decision)
                    break

    def _evaluate(self, level, trade_type, c_open, c_high, c_low, c_close, opposite_levels, df_slice):
        """Вызывает нужный метод WatcherManager в зависимости от STRATEGY."""
        if STRATEGY == "SWEEP_RECLAIM":
            decision = self.manager.evaluate_sweep_reclaim(
                level, c_open, c_high, c_low, c_close, opposite_levels, trade_type
            )
        else:  # CHOCH
            decision = self.manager.evaluate_choch(level, df_slice, trade_type, opposite_levels)

        if not decision['allow']:
            if 'No signal' in decision['reason'] or 'No CHoCH' in decision['reason']:
                GLOBAL_DEBUG_STATS["No_Signal"] += 1
            else:
                GLOBAL_DEBUG_STATS["Killed_by_QUALITY"] += 1
        return decision

    def _try_enter(self, level, trade_type, current_price, c_atr, decision):
        """
        Общая точка входа для LONG и SHORT.
        Проверяет context_filter, логирует контекст сделки, исполняет ордер.
        """
        global GLOBAL_TRADE_CONTEXTS, GLOBAL_DEBUG_STATS

        ctx_eval = analyze_context(self.data.Close, self.data.High, self.data.Low, c_atr,
                                    trade_type, level['min'], level['max'])
        if USE_CONTEXT_FILTER and not ctx_eval['allowed']:
            GLOBAL_DEBUG_STATS["Killed_by_CONTEXT"] += 1
            return

        level_id = f"{level['min']}_{level['max']}"
        lvl_state = self.level_states.get(level_id, 'FRESH')

        zone_range = level['max'] - level['min']
        if trade_type == 'LONG':
            entry_depth = ((level['max'] - current_price) / zone_range) * 100 if zone_range > 0 else 0.0
            closest = min([r['min'] for r in CURRENT_RESISTANCES if r['min'] > level['max']], default=None)
            gap_pct = ((closest - level['max']) / level['max']) * 100 if closest else 999.0
        else:
            entry_depth = ((current_price - level['min']) / zone_range) * 100 if zone_range > 0 else 0.0
            closest = max([s['max'] for s in CURRENT_SUPPORTS if s['max'] < level['min']], default=None)
            gap_pct = ((level['min'] - closest) / closest) * 100 if closest else 999.0

        ema_val = self.ema_4h_200[-1]
        ema_dist_pct = ((current_price - ema_val) / ema_val) * 100 if ema_val and ema_val > 0 else 0.0

        signal_time = str(self.data.index[-1])
        GLOBAL_TRADE_CONTEXTS[signal_time] = {
            "state": lvl_state,
            "score": level.get('score', 0),
            "type": level.get('type', 'unknown'),
            "width": round((zone_range / level['min']) * 100, 2),
            "gap": round(gap_pct, 2),
            "depth": round(entry_depth, 1),
            "approach": ctx_eval.get("approach", "UNKNOWN"),
            "reason": decision['reason'],
            "ema_dist": round(ema_dist_pct, 2),
        }

        self.current_trade_level_id = decision['level_id']
        GLOBAL_DEBUG_STATS["Passed_to_Trade"] += 1

        if trade_type == 'LONG':
            self.buy(sl=decision['sl'], tp=decision['tp'])
        else:
            self.sell(sl=decision['sl'], tp=decision['tp'])


# =========================================================
# ЗАГРУЗКА ДАННЫХ И ЗАПУСК
# =========================================================
macro_path = os.path.join("modules", "cryptano", "macro_levels.json")
macro_db = load_json(macro_path, default={}) if os.path.exists(macro_path) else {}


def get_cached_data(coin):
    try:
        exchange.load_markets()
    except Exception:
        pass

    symbol_perp = f"{coin.upper()}/USDT:USDT"
    symbol_spot = f"{coin.upper()}/USDT"
    symbol = symbol_perp if exchange.markets and symbol_perp in exchange.markets else symbol_spot
    date_suffix = TEST_START_DATE[:10] if TEST_START_DATE else "live"
    cache_file = f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}_{date_suffix}.csv"

    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)
    else:
        try:
            since_ts = int((pd.to_datetime(TEST_START_DATE) - pd.Timedelta(days=1)).timestamp() * 1000) if TEST_START_DATE else None
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES, since=since_ts) if since_ts else exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            df = pd.DataFrame(ohlcv, columns=["Open_time", "Open", "High", "Low", "Close", "Volume"])
            df.index = pd.to_datetime(df["Open_time"], unit="ms")
            df.to_csv(cache_file)
            return df
        except Exception:
            return pd.DataFrame()


try:
    with open('levels_timeline.json', 'r') as f:
        GLOBAL_TIMELINE = json.load(f)
except Exception:
    print("❌ Файл levels_timeline.json не найден. Сначала запусти precalc.py!")
    GLOBAL_TIMELINE = {}

first_time_key = list(GLOBAL_TIMELINE.keys())[0] if GLOBAL_TIMELINE else None
macro_db = GLOBAL_TIMELINE.get(first_time_key, {}) if first_time_key else {}


def print_trade_log(coin, tr, trade_type_filter=None):
    """Печатает лог сделок монеты, заполняет GLOBAL_WINNERS/LOSERS_LOG и GLOBAL_APPROACH_STATS."""
    for idx, row in tr.iterrows():
        signal_time_str = str(row['EntryTime'] - pd.Timedelta(minutes=15))
        ctx = GLOBAL_TRADE_CONTEXTS.get(signal_time_str, {})
        trade_type = "LONG" if row['Size'] > 0 else "SHORT"

        app = ctx.get('approach', 'UNKNOWN').replace('_DUMP', '').replace('_PUMP', '')
        if app not in GLOBAL_APPROACH_STATS:
            GLOBAL_APPROACH_STATS[app] = {"trades": 0, "win": 0}
        GLOBAL_APPROACH_STATS[app]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_APPROACH_STATS[app]["win"] += 1

        log_str = (f"{coin.upper()} | {trade_type} | Рез: {row['ReturnPct']*100:.2f}% | "
                   f" {ctx.get('state','?')} |  {ctx.get('approach','?')} | EMA Dist: {ctx.get('ema_dist','?')}% | Score: {ctx.get('score','?')} | "
                   f"ГЛУБИНА: {ctx.get('depth','?')}% | Ширина: {ctx.get('width','?')}% | Gap: {ctx.get('gap','?')}%")

        if row['PnL'] <= 0:
            GLOBAL_LOSERS_LOG.append("❌ " + log_str)
        else:
            GLOBAL_WINNERS_LOG.append("✅ " + log_str)


GLOBAL_APPROACH_STATS = {"IMPULSE": {"trades": 0, "win": 0}, "COMPRESSION": {"trades": 0, "win": 0}, "NORMAL": {"trades": 0, "win": 0}}

if TARGET_COIN.upper() == "ALL":
    print(f"🤖 Аудит запущен (стратегия: {STRATEGY}). Собираем данные...")

    for coin, data in macro_db.items():
        TARGET_COIN_CURRENT = coin
        if not isinstance(data, dict):
            continue
        CURRENT_SUPPORTS = data.get("supports", [])
        CURRENT_RESISTANCES = data.get("resistances", [])
        if not CURRENT_SUPPORTS and not CURRENT_RESISTANCES:
            continue

        df = get_cached_data(coin)
        if df.empty:
            continue

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
            print_trade_log(coin, tr)

        GLOBAL_TRADE_CONTEXTS = {}

    print("\n" + "=" * 85)
    print(f"📊 ИТОГОВЫЙ ГЛОБАЛЬНЫЙ ОТЧЕТ (стратегия: {STRATEGY})")
    print("=" * 85)
    if GLOBAL_REPORT:
        report_df = pd.DataFrame(GLOBAL_REPORT).sort_values(by="Профит %", ascending=False)
        print(report_df.to_string(index=False))
        print("-" * 85)
        print(f"📈 Суммарный профит портфеля: {report_df['Профит %'].sum():.2f}%")
        print(f"🏆 Средний Win Rate:         {report_df['Win Rate %'].mean():.2f}%")
    else:
        print("❌ Сделок не найдено.")

    print("\n" + "=" * 115)
    print("🚀 ПРИБЫЛЬНЫЕ СДЕЛКИ")
    print("=" * 115)
    for log in GLOBAL_WINNERS_LOG:
        print(log)

    print("\n" + "=" * 115)
    print("📉 УБЫТОЧНЫЕ СДЕЛКИ")
    print("=" * 115)
    for log in GLOBAL_LOSERS_LOG:
        print(log)

    print("\n" + "=" * 115)
    print("🕵️ ДИАГНОСТИКА ОТМЕН")
    print("=" * 115)
    for key, val in GLOBAL_DEBUG_STATS.items():
        print(f"  {key}: {val}")

    print("\n" + "=" * 85)
    print("📊 СТАТИСТИКА ПО ТИПАМ ПОДХОДА")
    print("=" * 85)
    for app, data in GLOBAL_APPROACH_STATS.items():
        if data["trades"] > 0:
            wr = (data["win"] / data["trades"]) * 100
            print(f"{app}: trades={data['trades']}  WR={wr:.1f}%")

else:
    print(f"📥 Запускаю детальный тест для {TARGET_COIN.upper()} (стратегия: {STRATEGY})...")
    TARGET_COIN_CURRENT = TARGET_COIN.upper()
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
            df['sup_max'] = np.nan
            df['res_min'] = np.nan

            bt = Backtest(df, SmartSniperUniversal, cash=10000, commission=.0006, hedging=False)
            stats = bt.run()

            print("\n" + "=" * 85)
            print(f"📊 ДЕТАЛЬНЫЙ ТЕСТ ДЛЯ {TARGET_COIN.upper()} (стратегия: {STRATEGY})")
            print("=" * 85)
            print(f"💵 Конечный баланс:   ${stats['Equity Final [$]']:,.2f}")
            print(f"📈 Чистый профит:     {stats['Return [%]']:.2f}%")
            print(f"📉 Макс. просадка:    {stats['Max. Drawdown [%]']:.2f}%")
            print(f"🤝 Всего сделок:      {int(stats['# Trades'])}")

            if int(stats['# Trades']) > 0:
                print(f"🏆 Процент плюсовых:  {stats['Win Rate [%]']:.2f}%")
                print("-" * 85)
                tr = stats['_trades']
                print_trade_log(TARGET_COIN, tr)
                for log in GLOBAL_WINNERS_LOG + GLOBAL_LOSERS_LOG:
                    print(log)

                print("\n" + "=" * 85)
                print("📊 СТАТИСТИКА ПО ТИПАМ ПОДХОДА")
                print("=" * 85)
                for app, data in GLOBAL_APPROACH_STATS.items():
                    if data["trades"] > 0:
                        wr = (data["win"] / data["trades"]) * 100
                        print(f"{app}: trades={data['trades']}  WR={wr:.1f}%")

            chart_path = os.path.abspath(f'chart_{TARGET_COIN.lower()}.html')
            try:
                bt.plot(filename=chart_path, open_browser=True)
            except Exception as e:
                print(f"⚠️ График не открылся: {e}")

            GLOBAL_TRADE_CONTEXTS = {}