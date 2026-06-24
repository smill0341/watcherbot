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
import time
import warnings
import json
import gc

warnings.filterwarnings("ignore")
from backtesting import Backtest, Strategy
from modules.cryptano.utils.crypto_utils import exchange
from modules.cryptano.utils.storage import load_json
from modules.cryptano.utils.testswing.context_filter import analyze_context
from modules.cryptano.utils.testswing.watcher_manager import WatcherManager
from modules.cryptano.utils.testswing.exit_manager import ExitManager
from typing import Optional


GLOBAL_DEBUG_STATS = {
    "Killed_by_CONTEXT": 0,
    "Killed_by_QUALITY": 0,   # score / zone_gap / level_burn отсеяли до watcher
    "No_Signal": 0,           # watcher не дал сигнала
    "Passed_to_Trade": 0,
}
GLOBAL_REPORT = []
GLOBAL_LOSERS_LOG = []
GLOBAL_TRADE_CONTEXTS = {}
GLOBAL_MAE_DIAGNOSTIC = []  # [{coin, mae_pct, hold_hours, exit_reason, result_pct}, ...]
GLOBAL_WINNERS_LOG = []
GLOBAL_APPROACH_STATS = {"IMPULSE": {"trades": 0, "win": 0}, "COMPRESSION": {"trades": 0, "win": 0}, "NORMAL": {"trades": 0, "win": 0}}

# =========================================================
# 1. ОСНОВНЫЕ НАСТРОЙКИ (ЕДИНЫЙ ПУЛЬТ УПРАВЛЕНИЯ)
# =========================================================
TARGET_COIN = "BCH"  # "ALL" для всего портфеля, или имя монеты для детального теста

TIMEFRAME = "15m"
LIMIT_CANDLES = 2880

TEST_START_DATE = "2026-04-01 00:00:00"
WARMUP_DAYS = 18  # запас данных ДО начала теста - нужен 4H контексту (64 свечи x 4ч = ~10.6 дней)

# --- DIAGNOSTIC: проверка качества точки входа без SL ---
# Если True: SL игнорируется, позиция держится до TP или до конца месяца (DIAGNOSTIC_DEADLINE_DAYS).
# Используется чтобы понять - вход реально близко к развороту (MAE маленький),
# или мы покупаем рано/в падении и просто пересиживаем минус.
DISABLE_SL_DIAGNOSTIC = True
DIAGNOSTIC_DEADLINE_DAYS = 7  # принудительное закрытие, если TP не достигнут за этот срок

ALLOW_LONG_TRADES = True
ALLOW_SHORT_TRADES = False

# Какой метод определения точки входа использовать: "SWEEP_RECLAIM" или "CHOCH" или "VOLUME_REVERSAL"
STRATEGY = "VOLUME_REVERSAL"

USE_CONTEXT_FILTER = False  # макро-контекст (тренд/импульс/поджатие) из context_filter.py

# Конфиг для WatcherManager - всё, что реально используется, в одном месте.
WATCHER_CONFIG = {
    'MIN_SCORE': 5,
    'USE_ZONE_GAP': True,
    'MIN_ZONE_GAP_PCT': 2.0,
    'USE_LEVEL_BURN': True,
    'SL_BUFFER': 1.0,
    # Изоляция двух разных паттернов внутри SWEEP_RECLAIM, чтобы тестировать их раздельно:
    # ALLOW_BOUNCE - касание + отказ БЕЗ выноса ликвидности за уровень
    # ALLOW_SWEEP  - настоящий вынос за уровень + возврат (Reclaim)
    'ALLOW_BOUNCE': True,
    'ALLOW_SWEEP': True,
    # TP теперь СТРУКТУРНЫЙ: считается от следующего противоположного уровня,
    # не от фиксированного %. TAKE_PROFIT используется только как fallback,
    # если структурного уровня вообще нет на графике.
    # TP режим: 'structural' (по уровню, текущий) или 'fixed_pct' (твой % без привязки к уровням)
    'TP_MODE': 'fixed_pct',
    'FIXED_TP_PCT': 8.0,      # используется только если TP_MODE='fixed_pct'
    'TAKE_PROFIT': 8.0,       # fallback %, если нет следующего уровня (только для structural режима)
    'TP_BUFFER_PCT': 0.3,     # не долетаем до самого уровня на этот %
    'MIN_RR': 1.5,            # если до следующего уровня R/R меньше - сделка отклоняется
    # только для CHOCH:
    'CHOCH_LOOKBACK': 15,
    'CHOCH_ANTI_KNIFE_ATR_MULT': 0.8,
    # НАСТРОЙКИ ДЛЯ VOLUME_REVERSAL ---
    'VOLUME_MULTIPLIER': 3.0,
    'VOLUME_WINDOW': 10,
}

CURRENT_SUPPORTS = []
CURRENT_RESISTANCES = []


def SMA(arr, n):
    return pd.Series(arr).rolling(n).mean()


class SmartSniperUniversal(Strategy):
    context_df_4h: Optional[pd.DataFrame] = None 
    original_df: Optional[pd.DataFrame] = None
    
    def init(self):
        self.manager = WatcherManager(strategy=STRATEGY, config=WATCHER_CONFIG)
        self.exit_mgr = ExitManager(disable_sl=DISABLE_SL_DIAGNOSTIC)  # Менеджер выхода из позиции
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
        
        # 1. Записываем ATR напрямую в original_df (чтобы срез мог его читать)
        if self.original_df is not None:
            self.original_df['atr'] = self.atr

        # 2. ПАРСИМ СТРОКУ ОДИН РАЗ (Убивает 40 секунд тормозов)
        self.test_start_dt = pd.to_datetime(TEST_START_DATE) if TEST_START_DATE else None

    def next(self):
        global GLOBAL_DEBUG_STATS, CURRENT_SUPPORTS, CURRENT_RESISTANCES, GLOBAL_TIMELINE, TARGET_COIN_CURRENT

        # Индекс backtesting УЖЕ является временем. Конвертация pd.to_datetime тут НЕ НУЖНА!
        current_time = self.data.index[-1]

        # === ПРОВЕРКА ВЫХОДА ИЗ ПОЗИЦИИ ===
        if self.exit_mgr.is_open() and self.position:
            c_high, c_low, c_close = self.data.High[-1], self.data.Low[-1], self.data.Close[-1]
            exit_triggered, exit_reason, exit_price = self.exit_mgr.check_exit(c_high, c_low, c_close, current_time=current_time)
            if exit_triggered:
                entry_key = getattr(self, 'current_trade_signal_time', None)
                if entry_key is not None and entry_key in GLOBAL_TRADE_CONTEXTS:
                    GLOBAL_TRADE_CONTEXTS[entry_key]['exit_reason'] = exit_reason
                    GLOBAL_TRADE_CONTEXTS[entry_key]['mae_pct'] = round(self.exit_mgr.last_closed_mae, 2)
                self.position.close()

        # === WARMUP (Мгновенное сравнение) ===
        if self.test_start_dt and current_time < self.test_start_dt:
            return

        # --- МАШИНА ВРЕМЕНИ: обновление уровней каждые 12 часов ---
        period_key = current_time.floor('12h').strftime("%Y-%m-%d %H:%M:%S")

        if getattr(self, 'current_period_key', None) != period_key:
            if period_key in GLOBAL_TIMELINE:
                coin_data = GLOBAL_TIMELINE[period_key].get(TARGET_COIN_CURRENT.upper(), {})
                CURRENT_SUPPORTS = coin_data.get("supports", [])
                CURRENT_RESISTANCES = coin_data.get("resistances", [])
                self.current_period_key = period_key

                if STRATEGY == "SWEEP_RECLAIM":
                    current_level_ids = set()
                    for s in CURRENT_SUPPORTS:
                        current_level_ids.add(f"LONG_{s['min']}_{s['max']}")
                    for r in CURRENT_RESISTANCES:
                        current_level_ids.add(f"SHORT_{r['min']}_{r['max']}")
                    self.manager.clear_dead_watchers(current_level_ids)

        # Отрисовка уровней на графике (ТОЛЬКО ДЛЯ ОДИНОЧНОЙ МОНЕТЫ)
        if TARGET_COIN.upper() != "ALL":
            if self.position and getattr(self, 'last_entered_level', None) is not None:
                active_min, active_max, entered_type = self.last_entered_level
                if entered_type == 'LONG':
                    active_sup, active_res = active_max, np.nan
                else:
                    active_sup, active_res = np.nan, active_min
            else:
                active_sup = CURRENT_SUPPORTS[0]['max'] if CURRENT_SUPPORTS else np.nan
                active_res = CURRENT_RESISTANCES[0]['min'] if CURRENT_RESISTANCES else np.nan
            
            if self.original_df is not None:
                self.original_df.at[current_time, 'sup_max'] = active_sup
                self.original_df.at[current_time, 'res_min'] = active_res

        # --- Сжигание уровня ---
        if len(self.closed_trades) > self.last_closed_trades:
            last_trade = self.closed_trades[-1]
            if last_trade.pl > 0 and self.current_trade_level_id is not None:
                if WATCHER_CONFIG.get('USE_LEVEL_BURN', True):
                    self.manager.burned_levels.add(self.current_trade_level_id)
            self.current_trade_level_id = None
            self.last_closed_trades = len(self.closed_trades)

        if len(self.data) < max(15, WATCHER_CONFIG.get('CHOCH_LOOKBACK', 15) + 1):
            return

        if self.position or (not CURRENT_SUPPORTS and not CURRENT_RESISTANCES):
            return

        c_open, c_close = self.data.Open[-1], self.data.Close[-1]
        c_high, c_low = self.data.High[-1], self.data.Low[-1]
        c_atr = self.atr[-1] if not np.isnan(self.atr[-1]) else (c_high - c_low)

        can_long = len(CURRENT_SUPPORTS) > 0 and ALLOW_LONG_TRADES
        can_short = len(CURRENT_RESISTANCES) > 0 and ALLOW_SHORT_TRADES

        df_slice = None
        if STRATEGY in ["CHOCH", "VOLUME_REVERSAL"]:
            lookback_size = 100 
            current_len = len(self.data)
            start_idx = max(0, current_len - lookback_size)
            if self.original_df is not None:
                df_slice = self.original_df.iloc[start_idx:current_len]

        recent_low = np.min(self.data.Low[-2:])
        recent_high = np.max(self.data.High[-2:])

        if can_long:
            for sup in CURRENT_SUPPORTS:
                if recent_low > sup['max']:
                    continue
                decision = self._evaluate(sup, 'LONG', c_open, c_high, c_low, c_close, CURRENT_RESISTANCES, df_slice)
                if decision['allow']:
                    self._try_enter(sup, 'LONG', c_close, c_atr, decision)
                    break

        if can_short:
            for res in CURRENT_RESISTANCES:
                if recent_high < res['min']:
                    continue
                decision = self._evaluate(res, 'SHORT', c_open, c_high, c_low, c_close, CURRENT_SUPPORTS, df_slice)
                if decision['allow']:
                    self._try_enter(res, 'SHORT', c_close, c_atr, decision)
                    break
    def _evaluate(self, level, trade_type, c_open, c_high, c_low, c_close, opposite_levels, df_slice):
        """Вызывает нужный метод WatcherManager в зависимости от STRATEGY."""
        if STRATEGY == "SWEEP_RECLAIM":
            decision = self.manager.evaluate_sweep_reclaim(
                level, c_open, c_high, c_low, c_close, opposite_levels, trade_type
            )
        elif STRATEGY == "VOLUME_REVERSAL":
            decision = self.manager.evaluate_volume_reversal(
                level, df_slice, trade_type, opposite_levels
            )
        else:  # CHOCH
            decision = self.manager.evaluate_choch(level, df_slice, trade_type, opposite_levels)

        if not decision['allow']:
            if 'No signal' in decision['reason'] or 'No CHoCH' in decision['reason'] or 'No volume reversal' in decision['reason']:
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

        # --- Контекст теперь считается на 4H, не на 15m ---
        # Берём только ЗАКРЫТЫЕ 4H свечи (открытая текущая 4H свеча не используется,
        # чтобы не заглядывать в будущее относительно текущего 15m бара).
        current_time = pd.to_datetime(self.data.index[-1])
        df_4h_ctx = getattr(self, 'context_df_4h', None)
        if df_4h_ctx is not None and len(df_4h_ctx) > 0:
            # Быстрая операция: встроенный поиск по индексу (отсекает всё будущее)
            cutoff_time = current_time - pd.Timedelta(hours=4)
            closed_4h = df_4h_ctx.loc[:cutoff_time]
        else:
            closed_4h = pd.DataFrame()

        if len(closed_4h) >= 20:
            ctx_window = closed_4h.tail(110)
            ctx_eval = analyze_context(ctx_window['Close'].values, ctx_window['High'].values,
                                        ctx_window['Low'].values, c_atr,
                                        trade_type, level['min'], level['max'])
        else:
            ctx_eval = {"allowed": True, "reason": "Not enough 4H data", "approach": "UNKNOWN",
                        "trend": "UNKNOWN", "energy": "UNKNOWN"}
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
            "level_min": round(level['min'], 4),
            "level_max": round(level['max'], 4),
            "width": round((zone_range / level['min']) * 100, 2),
            "gap": round(gap_pct, 2),
            "depth": round(entry_depth, 1),
            "approach": ctx_eval.get("approach", "UNKNOWN"),
            "trend": ctx_eval.get("trend", "UNKNOWN"),
            "energy": ctx_eval.get("energy", "UNKNOWN"),
            "context_reason": ctx_eval.get("reason", ""),  # Полное описание контекста
            "reason": decision['reason'],
            "ema_dist": round(ema_dist_pct, 2),
            "is_real_sweep": decision.get('is_real_sweep', False),
            "overshoot_pct": round(decision.get('overshoot_pct', 0.0), 3),
            "candles_in_sweep": decision.get('candles_in_sweep', 0),
            "entry_price": round(current_price, 8),
            "sl": round(decision.get('sl', 0.0), 8),
            "tp": round(decision.get('tp', 0.0), 8),
        }

        self.current_trade_level_id = decision['level_id']
        self.current_trade_signal_time = signal_time
        self.last_entered_level = (level['min'], level['max'], trade_type)
        GLOBAL_DEBUG_STATS["Passed_to_Trade"] += 1

        entry_time = pd.to_datetime(self.data.index[-1])
        deadline = None
        if DISABLE_SL_DIAGNOSTIC:
            deadline = entry_time + pd.Timedelta(days=DIAGNOSTIC_DEADLINE_DAYS)

        if trade_type == 'LONG':
            if DISABLE_SL_DIAGNOSTIC:
                self.buy()  # без sl и tp - закрытие ТОЛЬКО через exit_mgr (TP/DEADLINE)
            else:
                self.buy(sl=decision['sl'], tp=decision['tp'])
            self.exit_mgr.open_position('LONG', current_price, decision['tp'], decision['sl'],
                                         opened_at=entry_time, deadline=deadline)
        else:
            if DISABLE_SL_DIAGNOSTIC:
                self.sell()
            else:
                self.sell(sl=decision['sl'], tp=decision['tp'])
            self.exit_mgr.open_position('SHORT', current_price, decision['tp'], decision['sl'],
                                         opened_at=entry_time, deadline=deadline)


# =========================================================
# ЗАГРУЗКА ДАННЫХ И ЗАПУСК
# =========================================================
macro_path = os.path.join("modules", "cryptano", "macro_levels.json")
macro_db = load_json(macro_path, default={}) if os.path.exists(macro_path) else {}


def get_cached_data(coin):
    date_suffix = TEST_START_DATE[:10] if TEST_START_DATE else "live"
    cache_file = f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}_w{WARMUP_DAYS}_{date_suffix}.csv"

    # 1. ЕСЛИ ФАЙЛ ЕСТЬ — читаем мгновенно, без интернета
    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)
    
    # 2. ЕСЛИ ФАЙЛА НЕТ — включаем интернет и качаем (твой старый добрый код)
    else:
        try:
            # ВОТ ЭТО МЫ ПЕРЕНЕСЛИ СЮДА! Теперь биржа не тормозит загрузку кэша.
            try:
                exchange.load_markets()
            except Exception:
                pass

            symbol_perp = f"{coin.upper()}/USDT:USDT"
            symbol_spot = f"{coin.upper()}/USDT"
            symbol = symbol_perp if exchange.markets and symbol_perp in exchange.markets else symbol_spot
            
            CANDLES_PER_DAY_15M = 96  # 24ч * 4 свечи/час
            warmup_candles = WARMUP_DAYS * CANDLES_PER_DAY_15M
            total_limit = LIMIT_CANDLES + warmup_candles
            since_ts = int((pd.to_datetime(TEST_START_DATE) - pd.Timedelta(days=WARMUP_DAYS)).timestamp() * 1000) if TEST_START_DATE else None

            if since_ts is None:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            else:
                EXCHANGE_MAX_PER_CALL = 1000
                PAGINATION_DELAY_SEC = 0.25
                ohlcv = []
                cursor = since_ts
                while len(ohlcv) < total_limit:
                    chunk = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME,
                                                  limit=min(EXCHANGE_MAX_PER_CALL, total_limit - len(ohlcv)),
                                                  since=cursor)
                    if not chunk:
                        print(f"   [{coin}] chunk пустой, остановка. Всего набрано: {len(ohlcv)}")
                        break
                    ohlcv.extend(chunk)
                    last_ts = chunk[-1][0]
                    print(f"   [{coin}] chunk={len(chunk)} свечей, дата последней: {pd.to_datetime(last_ts, unit='ms')}, всего набрано: {len(ohlcv)}/{total_limit}")
                    if last_ts <= cursor:
                        break  # биржа не двигается - защита от бесконечного цикла
                    cursor = last_ts + 1
                    time.sleep(PAGINATION_DELAY_SEC)
                    if pd.to_datetime(last_ts, unit='ms', utc=True) >= pd.Timestamp.now(tz='UTC') - pd.Timedelta(minutes=30):
                        print(f"   [{coin}] дошли до текущего момента, остановка")
                        break

            df = pd.DataFrame(ohlcv, columns=["Open_time", "Open", "High", "Low", "Close", "Volume"])
            df.index = pd.to_datetime(df["Open_time"], unit="ms")
            df.to_csv(cache_file)
            return df
        except Exception as e:
            print(f"⚠️ Ошибка загрузки данных для {coin}: {type(e).__name__}: {e}")
            return pd.DataFrame()
def build_4h_context_df(df_15m):
    """
    Ресемплит 15m данные в 4H СВЕЧИ для контекста (тренд/энергия/импульс).
    Без сетевых запросов - чистая агрегация уже загруженных 15m данных.
    Контекст теперь смотрит на 4H картину, а не на последние ~16 часов 15m шума.
    """
    if df_15m.empty:
        return pd.DataFrame()
    df_4h = df_15m.resample('4h').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
    }).dropna()
    return df_4h


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

        # --- DIAGNOSTIC: MAE и время удержания (если режим включён) ---
        if DISABLE_SL_DIAGNOSTIC:
            hold_time = row['ExitTime'] - row['EntryTime']
            hold_hours = hold_time.total_seconds() / 3600
            GLOBAL_MAE_DIAGNOSTIC.append({
                "coin": coin,
                "mae_pct": ctx.get('mae_pct', 0.0),
                "hold_hours": round(hold_hours, 1),
                "exit_reason": ctx.get('exit_reason', '?'),
                "result_pct": round(row['ReturnPct'] * 100, 2),
            })

        app = ctx.get('approach', 'UNKNOWN').replace('_DUMP', '').replace('_PUMP', '')
        if app not in GLOBAL_APPROACH_STATS:
            GLOBAL_APPROACH_STATS[app] = {"trades": 0, "win": 0}
        GLOBAL_APPROACH_STATS[app]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_APPROACH_STATS[app]["win"] += 1

        sweep_type = "SWEEP+RECLAIM" if ctx.get('is_real_sweep', False) else "BOUNCE(no sweep)"
        if sweep_type not in GLOBAL_SWEEP_STATS:
            GLOBAL_SWEEP_STATS[sweep_type] = {"trades": 0, "win": 0}
        GLOBAL_SWEEP_STATS[sweep_type]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_SWEEP_STATS[sweep_type]["win"] += 1

        # --- Группировка по Score ---
        score = ctx.get('score', 0)
        score_bucket = f"{int(score)}" if score else "?"
        if score_bucket not in GLOBAL_SCORE_STATS:
            GLOBAL_SCORE_STATS[score_bucket] = {"trades": 0, "win": 0, "pnl": 0.0, "mae_list": []}
        GLOBAL_SCORE_STATS[score_bucket]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_SCORE_STATS[score_bucket]["win"] += 1
        GLOBAL_SCORE_STATS[score_bucket]["pnl"] += row['ReturnPct'] * 100
        if DISABLE_SL_DIAGNOSTIC:
            GLOBAL_SCORE_STATS[score_bucket]["mae_list"].append(ctx.get('mae_pct', 0.0))

        # --- Группировка по Gap (расстояние до следующего уровня) ---
        gap = ctx.get('gap', 0)
        if isinstance(gap, (int, float)):
            if gap < 4:
                gap_bucket = "0-4%"
            elif gap < 8:
                gap_bucket = "4-8%"
            elif gap < 15:
                gap_bucket = "8-15%"
            else:
                gap_bucket = "15%+"
        else:
            gap_bucket = "?"
        if gap_bucket not in GLOBAL_GAP_STATS:
            GLOBAL_GAP_STATS[gap_bucket] = {"trades": 0, "win": 0, "pnl": 0.0, "mae_list": []}
        GLOBAL_GAP_STATS[gap_bucket]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_GAP_STATS[gap_bucket]["win"] += 1
        GLOBAL_GAP_STATS[gap_bucket]["pnl"] += row['ReturnPct'] * 100
        if DISABLE_SL_DIAGNOSTIC:
            GLOBAL_GAP_STATS[gap_bucket]["mae_list"].append(ctx.get('mae_pct', 0.0))

        # --- Группировка по Trend ---
        trend = ctx.get('trend', 'UNKNOWN')
        if trend not in GLOBAL_TREND_STATS:
            GLOBAL_TREND_STATS[trend] = {"trades": 0, "win": 0, "pnl": 0.0}
        GLOBAL_TREND_STATS[trend]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_TREND_STATS[trend]["win"] += 1
        GLOBAL_TREND_STATS[trend]["pnl"] += row['ReturnPct'] * 100

        # --- Группировка по EMA позиции ---
        ema_dist = ctx.get('ema_dist', 0)
        if ema_dist is not None and isinstance(ema_dist, (int, float)):
            ema_bucket = "ВЫШЕ EMA" if ema_dist > 0 else "НИЖЕ EMA"
        else:
            ema_bucket = "?"
        if ema_bucket not in GLOBAL_EMA_STATS:
            GLOBAL_EMA_STATS[ema_bucket] = {"trades": 0, "win": 0, "pnl": 0.0}
        GLOBAL_EMA_STATS[ema_bucket]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_EMA_STATS[ema_bucket]["win"] += 1
        GLOBAL_EMA_STATS[ema_bucket]["pnl"] += row['ReturnPct'] * 100

        mae_str = f" | MAE:-{ctx.get('mae_pct', 0.0):.2f}%" if DISABLE_SL_DIAGNOSTIC else ""
        log_str = (f"{coin.upper()} | {trade_type} | Рез: {row['ReturnPct']*100:.2f}%{mae_str} | "
                   f"Entry:{ctx.get('entry_price','?')} SL:{ctx.get('sl','?')} TP:{ctx.get('tp','?')} | "
                   f"УРОВЕНЬ:[{ctx.get('level_min','?')}-{ctx.get('level_max','?')}] Gap:{ctx.get('gap','?')}% | "
                   f"Score:{ctx.get('score','?')} EMA:{ctx.get('ema_dist','?')}% Глубина:{ctx.get('depth','?')}%")

        if row['PnL'] <= 0:
            GLOBAL_LOSERS_LOG.append("❌ " + log_str)
        else:
            GLOBAL_WINNERS_LOG.append("✅ " + log_str)


GLOBAL_SWEEP_STATS = {}
GLOBAL_SCORE_STATS = {}
GLOBAL_GAP_STATS = {}
GLOBAL_TREND_STATS = {}
GLOBAL_EMA_STATS = {}


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

        cache_exists_before = os.path.exists(f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}_w{WARMUP_DAYS}_{TEST_START_DATE[:10] if TEST_START_DATE else 'live'}.csv")
        df = get_cached_data(coin)
        if df.empty:
            continue
        if not cache_exists_before:
            time.sleep(0.5)  # пауза между монетами, если только что качали с биржи (не из кэша)

        df['sup_max'] = np.nan
        df['res_min'] = np.nan
        
        df['ema'] = df['Close'].ewm(span=50, adjust=False).mean()
        df['avg_vol'] = df['Volume'].rolling(window=20).mean()
        
        # ДОБАВЛЯЕМ МАЛЕНЬКИЕ КОЛОНКИ ОДИН РАЗ
        df['open'] = df['Open']
        df['high'] = df['High']
        df['low'] = df['Low']
        df['close'] = df['Close']
        df['volume'] = df['Volume']

        SmartSniperUniversal.context_df_4h = build_4h_context_df(df)
        SmartSniperUniversal.original_df = df
        bt = Backtest(df, SmartSniperUniversal, cash=10000, commission=.0006, hedging=False)
        stats = bt.run()

        if int(stats['# Trades']) > 0:
            tr = stats['_trades']
            longs_win = len(tr[(tr['Size'] > 0) & (tr['PnL'] > 0)])
            longs_loss = len(tr[(tr['Size'] > 0) & (tr['PnL'] <= 0)])
            shorts_win = len(tr[(tr['Size'] < 0) & (tr['PnL'] > 0)])
            shorts_loss = len(tr[(tr['Size'] < 0) & (tr['PnL'] <= 0)])

            # Проверка: если Return [%] сильно отличается от суммы ReturnPct закрытых
            # сделок - подозрение на ВИСЯЩУЮ ОТКРЫТУЮ позицию на конец теста (особенно
            # в diagnostic-режиме, если deadline дальше конца самих тестовых данных).
            closed_sum_pct = tr['ReturnPct'].sum() * 100
            actual_return_pct = stats['Return [%]']
            open_position_suspected = abs(actual_return_pct - closed_sum_pct) > 2.0
            if open_position_suspected:
                print(f"⚠️ {coin.upper()}: подозрение на незакрытую позицию. "
                      f"Сумма закрытых сделок={closed_sum_pct:.2f}%, но Return [%]={actual_return_pct:.2f}%")

            GLOBAL_REPORT.append({
                "Монета": coin.upper(),
                "Лонг (+/-)": f"{longs_win}/{longs_loss}",
                "Шорт (+/-)": f"{shorts_win}/{shorts_loss}",
                "Win Rate %": round(stats['Win Rate [%]'], 2),
                "Профит %": round(stats['Return [%]'], 2)
            })
            print_trade_log(coin, tr)

        GLOBAL_TRADE_CONTEXTS = {}
        SmartSniperUniversal.context_df_4h = None
        del df
        gc.collect()

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

    if DISABLE_SL_DIAGNOSTIC and GLOBAL_MAE_DIAGNOSTIC:
        maes = [d['mae_pct'] for d in GLOBAL_MAE_DIAGNOSTIC]
        holds = [d['hold_hours'] for d in GLOBAL_MAE_DIAGNOSTIC]
        tp_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'TP']
        deadline_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'DEADLINE']
        print("\n" + "=" * 85)
        print("🩺 ДИАГНОСТИКА ВХОДА (SL отключён - смотрим качество точки входа)")
        print("=" * 85)
        print(f"Сделок всего: {len(GLOBAL_MAE_DIAGNOSTIC)} | Дошли до TP: {len(tp_hits)} | Не дошли (deadline): {len(deadline_hits)}")
        print(f"Средний MAE (макс. просадка от входа): {sum(maes)/len(maes):.2f}%  |  Худший MAE: {max(maes):.2f}%")
        print(f"Среднее время удержания: {sum(holds)/len(holds):.1f}ч  |  Самое долгое: {max(holds):.1f}ч")

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

    print("\n" + "=" * 85)
    print("🔍 ПРОВЕРКА: BOUNCE (без sweep) vs РЕАЛЬНЫЙ SWEEP+RECLAIM")
    print("=" * 85)
    total_sweep_trades = sum(d["trades"] for d in GLOBAL_SWEEP_STATS.values())
    for sweep_type, data in GLOBAL_SWEEP_STATS.items():
        if data["trades"] > 0:
            wr = (data["win"] / data["trades"]) * 100
            share = (data["trades"] / total_sweep_trades) * 100 if total_sweep_trades > 0 else 0
            print(f"{sweep_type}: trades={data['trades']} ({share:.0f}% от всех)  WR={wr:.1f}%")

    print("\n" + "=" * 85)
    print("📊 SCORE vs РЕЗУЛЬТАТ (даёт ли Score преимущество?)")
    print("=" * 85)
    for score in sorted(GLOBAL_SCORE_STATS.keys()):
        d = GLOBAL_SCORE_STATS[score]
        if d["trades"] > 0:
            wr = (d["win"] / d["trades"]) * 100
            avg = d["pnl"] / d["trades"]
            mae_part = ""
            if DISABLE_SL_DIAGNOSTIC and d.get("mae_list"):
                avg_mae = sum(d["mae_list"]) / len(d["mae_list"])
                worst_mae = max(d["mae_list"])
                mae_part = f"  MAE avg={avg_mae:.2f}% worst={worst_mae:.2f}%"
            print(f"Score {score}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%{mae_part}")

    print("\n" + "=" * 85)
    print("📊 GAP vs РЕЗУЛЬТАТ (расстояние до следующего уровня)")
    print("=" * 85)
    for gap_b in ["0-4%", "4-8%", "8-15%", "15%+", "?"]:
        if gap_b in GLOBAL_GAP_STATS:
            d = GLOBAL_GAP_STATS[gap_b]
            if d["trades"] > 0:
                wr = (d["win"] / d["trades"]) * 100
                avg = d["pnl"] / d["trades"]
                mae_part = ""
                if DISABLE_SL_DIAGNOSTIC and d.get("mae_list"):
                    avg_mae = sum(d["mae_list"]) / len(d["mae_list"])
                    worst_mae = max(d["mae_list"])
                    mae_part = f"  MAE avg={avg_mae:.2f}% worst={worst_mae:.2f}%"
                print(f"Gap {gap_b}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%{mae_part}")

    print("\n" + "=" * 85)
    print("📊 TREND vs РЕЗУЛЬТАТ")
    print("=" * 85)
    for trend, d in GLOBAL_TREND_STATS.items():
        if d["trades"] > 0:
            wr = (d["win"] / d["trades"]) * 100
            avg = d["pnl"] / d["trades"]
            print(f"{trend}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%")

    print("\n" + "=" * 85)
    print("📊 ПОЗИЦИЯ vs EMA (LONG выше/ниже EMA)")
    print("=" * 85)
    for ema, d in GLOBAL_EMA_STATS.items():
        if d["trades"] > 0:
            wr = (d["win"] / d["trades"]) * 100
            avg = d["pnl"] / d["trades"]
            print(f"{ema}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%")

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
            
            df['ema'] = df['Close'].ewm(span=13, adjust=False).mean()
            df['avg_vol'] = df['Volume'].rolling(window=20).mean()

            # Колонки для быстрого доступа, как и в ALL:
            df['open'] = df['Open']
            df['high'] = df['High']
            df['low'] = df['Low']
            df['close'] = df['Close']
            df['volume'] = df['Volume']

            SmartSniperUniversal.context_df_4h = build_4h_context_df(df)
            
            # ПЕРЕДАЕМ original_df, чтобы df_slice не был None!
            SmartSniperUniversal.original_df = df 
            
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

                if DISABLE_SL_DIAGNOSTIC and GLOBAL_MAE_DIAGNOSTIC:
                    maes = [d['mae_pct'] for d in GLOBAL_MAE_DIAGNOSTIC]
                    holds = [d['hold_hours'] for d in GLOBAL_MAE_DIAGNOSTIC]
                    tp_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'TP']
                    deadline_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'DEADLINE']
                    print("\n" + "=" * 85)
                    print("🩺 ДИАГНОСТИКА ВХОДА (SL отключён - смотрим качество точки входа)")
                    print("=" * 85)
                    print(f"Сделок всего: {len(GLOBAL_MAE_DIAGNOSTIC)} | Дошли до TP: {len(tp_hits)} | Не дошли (deadline): {len(deadline_hits)}")
                    print(f"Средний MAE (макс. просадка от входа): {sum(maes)/len(maes):.2f}%  |  Худший MAE: {max(maes):.2f}%")
                    print(f"Среднее время удержания: {sum(holds)/len(holds):.1f}ч  |  Самое долгое: {max(holds):.1f}ч")

                print("\n" + "=" * 85)
                print("📊 СТАТИСТИКА ПО ТИПАМ ПОДХОДА")
                print("=" * 85)
                for app, data in GLOBAL_APPROACH_STATS.items():
                    if data["trades"] > 0:
                        wr = (data["win"] / data["trades"]) * 100
                        print(f"{app}: trades={data['trades']}  WR={wr:.1f}%")

                print("\n" + "=" * 85)
                print("🔍 ПРОВЕРКА: BOUNCE (без sweep) vs РЕАЛЬНЫЙ SWEEP+RECLAIM")
                print("=" * 85)
                total_sweep_trades = sum(d["trades"] for d in GLOBAL_SWEEP_STATS.values())
                for sweep_type, data in GLOBAL_SWEEP_STATS.items():
                    if data["trades"] > 0:
                        wr = (data["win"] / data["trades"]) * 100
                        share = (data["trades"] / total_sweep_trades) * 100 if total_sweep_trades > 0 else 0
                        print(f"{sweep_type}: trades={data['trades']} ({share:.0f}% от всех)  WR={wr:.1f}%")

                print("\n" + "=" * 85)
                print("📊 SCORE vs РЕЗУЛЬТАТ (даёт ли Score преимущество?)")
                print("=" * 85)
                for score in sorted(GLOBAL_SCORE_STATS.keys()):
                    d = GLOBAL_SCORE_STATS[score]
                    if d["trades"] > 0:
                        wr = (d["win"] / d["trades"]) * 100
                        avg = d["pnl"] / d["trades"]
                        mae_part = ""
                        if DISABLE_SL_DIAGNOSTIC and d.get("mae_list"):
                            avg_mae = sum(d["mae_list"]) / len(d["mae_list"])
                            worst_mae = max(d["mae_list"])
                            mae_part = f"  MAE avg={avg_mae:.2f}% worst={worst_mae:.2f}%"
                        print(f"Score {score}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%{mae_part}")

                print("\n" + "=" * 85)
                print("📊 GAP vs РЕЗУЛЬТАТ (расстояние до следующего уровня)")
                print("=" * 85)
                for gap_b in ["0-4%", "4-8%", "8-15%", "15%+", "?"]:
                    if gap_b in GLOBAL_GAP_STATS:
                        d = GLOBAL_GAP_STATS[gap_b]
                        if d["trades"] > 0:
                            wr = (d["win"] / d["trades"]) * 100
                            avg = d["pnl"] / d["trades"]
                            mae_part = ""
                            if DISABLE_SL_DIAGNOSTIC and d.get("mae_list"):
                                avg_mae = sum(d["mae_list"]) / len(d["mae_list"])
                                worst_mae = max(d["mae_list"])
                                mae_part = f"  MAE avg={avg_mae:.2f}% worst={worst_mae:.2f}%"
                            print(f"Gap {gap_b}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%{mae_part}")

                print("\n" + "=" * 85)
                print("📊 TREND vs РЕЗУЛЬТАТ")
                print("=" * 85)
                for trend, d in GLOBAL_TREND_STATS.items():
                    if d["trades"] > 0:
                        wr = (d["win"] / d["trades"]) * 100
                        avg = d["pnl"] / d["trades"]
                        print(f"{trend}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%")

                print("\n" + "=" * 85)
                print("📊 ПОЗИЦИЯ vs EMA (LONG выше/ниже EMA)")
                print("=" * 85)
                for ema, d in GLOBAL_EMA_STATS.items():
                    if d["trades"] > 0:
                        wr = (d["win"] / d["trades"]) * 100
                        avg = d["pnl"] / d["trades"]
                        print(f"{ema}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%")

            chart_path = os.path.abspath(f'chart_{TARGET_COIN.lower()}.html')
            try:
                bt.plot(filename=chart_path, open_browser=True)
            except Exception as e:
                print(f"⚠️ График не открылся: {e}")

            GLOBAL_TRADE_CONTEXTS = {}
