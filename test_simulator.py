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
from modules.cryptano.utils.testswing.exit_manager import ExitManager


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
TARGET_COIN = "ALL"  # "ALL" для всего портфеля, или имя монеты для детального теста

TIMEFRAME = "15m"
LIMIT_CANDLES = 2880

TEST_START_DATE = "2026-04-01 00:00:00"
WARMUP_DAYS = 18  # запас данных ДО начала теста - нужен 4H контексту (64 свечи x 4ч = ~10.6 дней)

# --- DIAGNOSTIC: проверка качества точки входа без SL ---
# Если True: SL игнорируется, позиция держится до TP или до конца месяца (DIAGNOSTIC_DEADLINE_DAYS).
# Используется чтобы понять - вход реально близко к развороту (MAE маленький),
# или мы покупаем рано/в падении и просто пересиживаем минус.
DISABLE_SL_DIAGNOSTIC = True
DIAGNOSTIC_DEADLINE_DAYS = 30  # принудительное закрытие, если TP не достигнут за этот срок

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
}

CURRENT_SUPPORTS = []
CURRENT_RESISTANCES = []


def SMA(arr, n):
    return pd.Series(arr).rolling(n).mean()


class SmartSniperUniversal(Strategy):
    context_df_4h: pd.DataFrame = None  # type: ignore # будет установлен снаружи перед bt.run() через build_4h_context_df()

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

    def next(self):
        global GLOBAL_DEBUG_STATS, CURRENT_SUPPORTS, CURRENT_RESISTANCES, GLOBAL_TIMELINE, TARGET_COIN_CURRENT

        # === ПРОВЕРКА ВЫХОДА ИЗ ПОЗИЦИИ ===
        if self.exit_mgr.is_open() and self.position:
            c_high, c_low, c_close = self.data.High[-1], self.data.Low[-1], self.data.Close[-1]
            c_time = pd.to_datetime(self.data.index[-1])
            exit_triggered, exit_reason, exit_price = self.exit_mgr.check_exit(c_high, c_low, c_close, current_time=c_time)
            if exit_triggered:
                # Записываем MAE и причину закрытия в контекст этой сделки (по времени входа)
                entry_key = getattr(self, 'current_trade_signal_time', None)
                if entry_key is not None and entry_key in GLOBAL_TRADE_CONTEXTS:
                    GLOBAL_TRADE_CONTEXTS[entry_key]['exit_reason'] = exit_reason
                    GLOBAL_TRADE_CONTEXTS[entry_key]['mae_pct'] = round(self.exit_mgr.last_closed_mae, 2)
                # Закрываем позицию (close_position() уже вызван внутри check_exit при выходе)
                self.position.close()

        # --- МАШИНА ВРЕМЕНИ: обновление уровней каждые 12 часов ---
        current_time = pd.to_datetime(self.data.index[-1])

        # === WARMUP: до TEST_START_DATE сделки не открываем, это запас данных для 4H контекста ===
        if TEST_START_DATE and current_time < pd.to_datetime(TEST_START_DATE):
            return

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

        # Отрисовка уровней на графике.
        # Пока позиция открыта - показываем ИМЕННО тот уровень, по которому вошли
        # (не первый из списка), чтобы линия на графике совпадала с реальным входом.
        if self.position and getattr(self, 'last_entered_level', None) is not None:
            active_min, active_max, entered_type = self.last_entered_level
            if entered_type == 'LONG':
                active_sup, active_res = active_max, np.nan
            else:
                active_sup, active_res = np.nan, active_min
        else:
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

        # --- Контекст теперь считается на 4H, не на 15m ---
        # Берём только ЗАКРЫТЫЕ 4H свечи (открытая текущая 4H свеча не используется,
        # чтобы не заглядывать в будущее относительно текущего 15m бара).
        current_time = pd.to_datetime(self.data.index[-1])
        df_4h_ctx = getattr(self, 'context_df_4h', None)
        if df_4h_ctx is not None and len(df_4h_ctx) > 0:
            closed_4h = df_4h_ctx[df_4h_ctx.index + pd.Timedelta(hours=4) <= current_time]
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
    try:
        exchange.load_markets()
    except Exception:
        pass

    symbol_perp = f"{coin.upper()}/USDT:USDT"
    symbol_spot = f"{coin.upper()}/USDT"
    symbol = symbol_perp if exchange.markets and symbol_perp in exchange.markets else symbol_spot
    date_suffix = TEST_START_DATE[:10] if TEST_START_DATE else "live"
    cache_file = f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}_w{WARMUP_DAYS}_{date_suffix}.csv"

    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)
    else:
        try:
            CANDLES_PER_DAY_15M = 96  # 24ч * 4 свечи/час
            warmup_candles = WARMUP_DAYS * CANDLES_PER_DAY_15M
            total_limit = LIMIT_CANDLES + warmup_candles
            since_ts = int((pd.to_datetime(TEST_START_DATE) - pd.Timedelta(days=WARMUP_DAYS)).timestamp() * 1000) if TEST_START_DATE else None

            if since_ts is None:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            else:
                # Биржа отдаёт максимум ~1000 свечей за один запрос (молча режет, без ошибки).
                # Поэтому пагинируем: запрашиваем чанками, сдвигая since на последнюю
                # полученную свечу, пока не наберём total_limit или данные не закончатся.
                EXCHANGE_MAX_PER_CALL = 1000
                ohlcv = []
                cursor = since_ts
                while len(ohlcv) < total_limit:
                    chunk = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME,
                                                  limit=min(EXCHANGE_MAX_PER_CALL, total_limit - len(ohlcv)),
                                                  since=cursor)
                    if not chunk:
                        break
                    ohlcv.extend(chunk)
                    last_ts = chunk[-1][0]
                    if last_ts <= cursor:
                        break  # биржа не двигается - защита от бесконечного цикла
                    cursor = last_ts + 1
                    if len(chunk) < EXCHANGE_MAX_PER_CALL:
                        break  # данные закончились (дошли до текущего момента)

            df = pd.DataFrame(ohlcv, columns=["Open_time", "Open", "High", "Low", "Close", "Volume"])
            df.index = pd.to_datetime(df["Open_time"], unit="ms")
            df.to_csv(cache_file)
            return df
        except Exception:
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
            GLOBAL_SCORE_STATS[score_bucket] = {"trades": 0, "win": 0, "pnl": 0.0}
        GLOBAL_SCORE_STATS[score_bucket]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_SCORE_STATS[score_bucket]["win"] += 1
        GLOBAL_SCORE_STATS[score_bucket]["pnl"] += row['ReturnPct'] * 100

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

        log_str = (f"{coin.upper()} | {trade_type} | Рез: {row['ReturnPct']*100:.2f}% | "
                   f"Entry:{ctx.get('entry_price','?')} SL:{ctx.get('sl','?')} TP:{ctx.get('tp','?')} | "
                   f"УРОВЕНЬ:[{ctx.get('level_min','?')}-{ctx.get('level_max','?')}] | "
                   f"[{sweep_type} | overshoot:{ctx.get('overshoot_pct','?')}% | свечей_в_sweep:{ctx.get('candles_in_sweep','?')}] | "
                   f"{ctx.get('state','?')} | {ctx.get('approach','?')} | "
                   f"TREND:{ctx.get('trend','?')} ENERGY:{ctx.get('energy','?')} | "
                   f"EMA Dist: {ctx.get('ema_dist','?')}% | Score: {ctx.get('score','?')} | "
                   f"ГЛУБИНА: {ctx.get('depth','?')}% | Ширина: {ctx.get('width','?')}% | Gap: {ctx.get('gap','?')}% | "
                   f"CTX: {ctx.get('context_reason','')}")

        if row['PnL'] <= 0:
            GLOBAL_LOSERS_LOG.append("❌ " + log_str)
        else:
            GLOBAL_WINNERS_LOG.append("✅ " + log_str)


GLOBAL_SWEEP_STATS = {}
GLOBAL_SCORE_STATS = {}
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

        df = get_cached_data(coin)
        if df.empty:
            continue

        df['sup_max'] = np.nan
        df['res_min'] = np.nan

        SmartSniperUniversal.context_df_4h = build_4h_context_df(df)
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
            print(f"Score {score}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%")

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

            SmartSniperUniversal.context_df_4h = build_4h_context_df(df)
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
                        print(f"Score {score}: trades={d['trades']}  WR={wr:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%")

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
