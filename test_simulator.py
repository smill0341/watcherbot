"""
test_simulator.py
==================
Бэктест-движок. Только оркестрация:
загружает данные -> для каждой свечи спрашивает WatcherManager "входить?"
-> если да, исполняет ордер в backtesting.py -> копит статистику.

Вся логика "входить или нет", а также расчеты TP/SL живут в watcher_methods.py.
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
    "Killed_by_QUALITY": 0,   
    "No_Signal": 0,           
    "Passed_to_Trade": 0,
    "Origins_Long_Total": 0,  
    "Origins_Short_Total": 0, 
}
GLOBAL_REPORT = []
GLOBAL_LOSERS_LOG = []
GLOBAL_TRADE_CONTEXTS = {}
GLOBAL_MAE_DIAGNOSTIC = []  
GLOBAL_WINNERS_LOG = []
GLOBAL_APPROACH_STATS = {"IMPULSE": {"trades": 0, "win": 0}, "COMPRESSION": {"trades": 0, "win": 0}, "NORMAL": {"trades": 0, "win": 0}}

# =========================================================
# 1. ОСНОВНЫЕ НАСТРОЙКИ БЭКТЕСТА (ЕДИНЫЙ ПУЛЬТ)
# =========================================================
TARGET_COIN = "ALL"  # "ALL" для всего портфеля, или имя монеты для детального теста

TIMEFRAME = "15m"
LIMIT_CANDLES = 2880

TEST_START_DATE = "2026-02-01 00:00:00"
WARMUP_DAYS = 18  
MIN_LEVEL_SCORE = 1.0

# STRATEGY "SWEEP_RECLAIM" или "VOLUME_REVERSAL" или "PIT_CLIMAX" или "PANIC_TRAP" или "V_BOTTOM" 
STRATEGY = "PANIC_TRAP"
VBOTTOM_BREATH_BUFFER_PCT = 3.0  # должно совпадать с CONFIG['BREATH_BUFFER_PCT'] в v_bottom_watcher.py

# --- DIAGNOSTIC: проверка качества точки входа без SL ---
# Если True: SL игнорируется, позиция держится до TP или до конца дедлайна.
DISABLE_SL_DIAGNOSTIC = True
DIAGNOSTIC_DEADLINE_DAYS = 3  

ALLOW_LONG_TRADES = True
ALLOW_SHORT_TRADES = False

USE_CONTEXT_FILTER = False  
USE_LEVEL_BURN = True # Сжигать ли уровень после успешной сделки

# ВАЖНО: Настройки самих стратегий (TP_MODE, FIXED_TP_PCT, SL_BUFFER, MIN_RR, SWING_LENGTH и т.д.)
# теперь находятся строго внутри файла watcher_methods.py в личных словарях CONFIG!

CURRENT_SUPPORTS = []
CURRENT_RESISTANCES = []


def SMA(arr, n):
    return pd.Series(arr).rolling(n).mean()


class SmartSniperUniversal(Strategy):
    context_df_4h: Optional[pd.DataFrame] = None 
    original_df: Optional[pd.DataFrame] = None
    
    def init(self):
        # Инициализируем менеджера без передачи глобального конфига
        self.manager = WatcherManager(strategy=STRATEGY)
        self.exit_mgr = ExitManager(disable_sl=DISABLE_SL_DIAGNOSTIC)  
        self.level_states = {}
        self.last_closed_trades = 0
        self.current_trade_level_id = None

        self.origin_level_long = None
        self.origin_level_short = None

        high_low = pd.Series(self.data.High) - pd.Series(self.data.Low)
        self.atr = self.I(SMA, high_low, 14)

        def EMA(values, n):
            return pd.Series(values).ewm(span=n, adjust=False).mean()

        self.ema_4h_200 = self.I(EMA, self.data.Close, 3200)

        self.draw_sup_max = self.I(lambda: self.data.df['sup_max'], name="Support Top", overlay=True)
        self.draw_res_min = self.I(lambda: self.data.df['res_min'], name="Resist Bottom", overlay=True)
        self.draw_vbottom_event = self.I(lambda: self.data.df['vbottom_event'], name="V_BOTTOM событие",
                                          overlay=True, scatter=True, color='yellow')
        
        if self.original_df is not None:
            self.original_df['atr'] = self.atr

        self.test_start_dt = pd.Timestamp(TEST_START_DATE) if TEST_START_DATE else None

    def next(self):
        global GLOBAL_DEBUG_STATS, CURRENT_SUPPORTS, CURRENT_RESISTANCES, GLOBAL_TIMELINE, TARGET_COIN_CURRENT

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

        # === WARMUP ===
        if self.test_start_dt and current_time < self.test_start_dt:
            return

        # --- МАШИНА ВРЕМЕНИ: обновление уровней каждые 12 часов ---
        period_key = current_time.floor('12h').strftime("%Y-%m-%d %H:%M:%S")

        if getattr(self, 'current_period_key', None) != period_key:
            if period_key in GLOBAL_TIMELINE:
                coin_data = GLOBAL_TIMELINE[period_key].get(TARGET_COIN_CURRENT.upper(), {})
                
                # Сохраняем старый уровень, если база прислала пустоту
                new_sups = coin_data.get("supports", [])
                if new_sups:
                    CURRENT_SUPPORTS = new_sups
                    
                new_res = coin_data.get("resistances", [])
                if new_res:
                    CURRENT_RESISTANCES = new_res
                    
                self.current_period_key = period_key

                if STRATEGY == "V_BOTTOM":
                    n_before = len(CURRENT_SUPPORTS)
                    n_after = len([s for s in CURRENT_SUPPORTS if s.get('score', 0) >= MIN_LEVEL_SCORE])
                    

                if STRATEGY == "SWEEP_RECLAIM":
                    current_level_ids = set()
                    for s in CURRENT_SUPPORTS:
                        current_level_ids.add(f"LONG_{s['min']}_{s['max']}")
                    for r in CURRENT_RESISTANCES:
                        current_level_ids.add(f"SHORT_{r['min']}_{r['max']}")
                    self.manager.clear_dead_watchers(current_level_ids)
            elif STRATEGY == "V_BOTTOM":
                print(f"🗓️ [ПЕРИОД {period_key}] ОТСУТСТВУЕТ в GLOBAL_TIMELINE вообще (нет данных на этот период)")
                self.current_period_key = period_key

        # Отрисовка уровней на графике (БЕЗ МУСОРНЫХ СТУПЕНЕК)
        if TARGET_COIN.upper() != "ALL":
            active_sup, active_res = np.nan, np.nan
            
            # 1. Приоритет: Активный пробитый уровень, за которым мы СЕЙЧАС следим
            if STRATEGY in ("VOLUME_REVERSAL", "PIT_CLIMAX", "PANIC_TRAP", "V_BOTTOM"):
                if self.origin_level_long is not None:
                    active_sup = self.origin_level_long['max']
                if self.origin_level_short is not None:
                    active_res = self.origin_level_short['min']
                    
            # 2. Если активного поиска нет (ждем пробоя или сидим в сделке), тянем линию последнего уровня
            if np.isnan(active_sup) and getattr(self, 'last_entered_level', None) is not None:
                if self.last_entered_level[2] == 'LONG':
                    active_sup = self.last_entered_level[1]
            if np.isnan(active_res) and getattr(self, 'last_entered_level', None) is not None:
                if self.last_entered_level[2] == 'SHORT':
                    active_res = self.last_entered_level[0]
            
            if self.original_df is not None:
                self.original_df.at[current_time, 'sup_max'] = active_sup
                self.original_df.at[current_time, 'res_min'] = active_res

        # --- Сжигание уровня ---
        if len(self.closed_trades) > self.last_closed_trades:
            # Сжигаем отработанный уровень (и плюсовой, и минусовой), чтобы не входить в него дважды
            if self.current_trade_level_id is not None:
                self.manager.burned_levels.add(self.current_trade_level_id)
            self.current_trade_level_id = None
            self.last_closed_trades = len(self.closed_trades)

        if len(self.data) < 20: # Жесткий минимум свечей для старта
            return

        # Не выходим досрочно, если уже идёт активное слежение за уровнем
        # (origin_level_long/short) — оно не должно зависеть от того, пуст ли
        # СЕЙЧАС внешний список уровней: у origin уже есть своя сохранённая
        # зона, найденная в момент старта слежения.
        has_active_origin = self.origin_level_long is not None or self.origin_level_short is not None
        if self.position or (not CURRENT_SUPPORTS and not CURRENT_RESISTANCES and not has_active_origin):
            return

        c_open, c_close = self.data.Open[-1], self.data.Close[-1]
        c_high, c_low = self.data.High[-1], self.data.Low[-1]
        c_atr = self.atr[-1] if not np.isnan(self.atr[-1]) else (c_high - c_low)

        # can_long/can_short разрешают либо поиск НОВОГО уровня (когда список
        # не пуст), либо продолжение слежения за УЖЕ АКТИВНЫМ origin —
        # это не должно зависеть от текущего состояния списка уровней.
        can_long = (len(CURRENT_SUPPORTS) > 0 or self.origin_level_long is not None) and ALLOW_LONG_TRADES
        can_short = (len(CURRENT_RESISTANCES) > 0 or self.origin_level_short is not None) and ALLOW_SHORT_TRADES

        df_slice = None
        if STRATEGY in ["VOLUME_REVERSAL", "PIT_CLIMAX", "PANIC_TRAP", "V_BOTTOM"]:
            lookback_size = 260 if STRATEGY == "VOLUME_REVERSAL" else 100
            current_len = len(self.data)
            start_idx = max(0, current_len - lookback_size)

            if self.original_df is not None:
                for origin in (self.origin_level_long, self.origin_level_short):
                    if origin is not None and '_pit_start_time' in origin:
                        try:
                            origin_idx = self.original_df.index.get_loc(origin['_pit_start_time'])
                            if isinstance(origin_idx, slice):
                                origin_idx = origin_idx.start
                            start_idx = min(start_idx, origin_idx)
                        except KeyError:
                            pass

            if self.original_df is not None:
                df_slice = self.original_df.iloc[start_idx:current_len]

        recent_low = np.min(self.data.Low[-2:])
        recent_high = np.max(self.data.High[-2:])
        
        # ---  ФИЛЬТР ПО БАЛЛАМ ---
        CURRENT_SUPPORTS = [s for s in CURRENT_SUPPORTS if s.get('score', 0) >= MIN_LEVEL_SCORE]
        CURRENT_RESISTANCES = [r for r in CURRENT_RESISTANCES if r.get('score', 0) >= MIN_LEVEL_SCORE]

        # --- Отслеживание уровня "исхода" ---
        if CURRENT_SUPPORTS:
            if self.origin_level_long is None:
                prev_close = float(self.data.Close[-2]) if len(self.data.Close) > 1 else c_close
                found = None
                for sup in CURRENT_SUPPORTS:
                    # Ловим пробой, даже если тело осталось выше, но тень (c_low) прошила уровень
                    if (c_close < sup['min'] or c_low < sup['min']) and prev_close >= sup['min']:
                        found = sup
                        break
                if found is None:
                    for sup in CURRENT_SUPPORTS:
                        if c_close < sup['min'] or c_low < sup['min']:
                            found = sup
                            break
                if found is None:
                    for sup in CURRENT_SUPPORTS:
                        if c_close < sup['min'] or c_low < sup['min']:
                            found = sup
                            break
                if found is not None:
                    self.origin_level_long = dict(found)
                    GLOBAL_DEBUG_STATS["Origins_Long_Total"] += 1
                    self.origin_level_long['_pit_start_time'] = current_time
                    if STRATEGY == "V_BOTTOM":
                        # новое пробитие уровня: математика вотчера с нуля + лимит повторов
                        self.manager.notify_breach(self.origin_level_long, 'LONG')
            else:
                origin_max = self.origin_level_long['max']
                if STRATEGY == "V_BOTTOM":
                    # буфер должен совпадать с BREATH_BUFFER_PCT в v_bottom_watcher.py
                    origin_max = origin_max * (1 + VBOTTOM_BREATH_BUFFER_PCT / 100.0)
                
                if c_close > origin_max:
                    # Для V_BOTTOM жестко сбрасываем при пробое буфера, игнорируя 'still_needed'
                    if STRATEGY == "V_BOTTOM" or not self._origin_still_needed(self.origin_level_long, 'LONG'):
                        if STRATEGY == "V_BOTTOM":
                            
                            self.manager.force_reset_watcher(self.origin_level_long, 'LONG')
                        self.origin_level_long = None

        if CURRENT_RESISTANCES:
            if self.origin_level_short is None:
                prev_close = float(self.data.Close[-2]) if len(self.data.Close) > 1 else c_close
                found = None
                for res in CURRENT_RESISTANCES:
                    if c_close > res['max'] and prev_close <= res['max']:
                        found = res
                        break
                if found is None:
                    for res in CURRENT_RESISTANCES:
                        if c_close > res['max']:
                            found = res
                            break
                if found is not None:
                    self.origin_level_short = dict(found)
                    GLOBAL_DEBUG_STATS["Origins_Short_Total"] += 1
                    self.origin_level_short['_pit_start_time'] = current_time
            else:
                if c_close < self.origin_level_short['min'] and not self._origin_still_needed(self.origin_level_short, 'SHORT'):
                    self.origin_level_short = None

        if can_long:
            # ТЕПЕРЬ КАПКАН ЗДЕСЬ. Работает строго с одним пробитым уровнем.
            if STRATEGY in ("VOLUME_REVERSAL", "PIT_CLIMAX", "PANIC_TRAP", "V_BOTTOM"):
                if self.origin_level_long is not None:
                    ctx_eval_long = self._get_context(self.origin_level_long, 'LONG', c_atr)
                    decision = self._evaluate(self.origin_level_long, 'LONG', c_open, c_high, c_low, c_close,
                                               CURRENT_RESISTANCES, df_slice, trend=ctx_eval_long.get('trend', 'UNKNOWN'),
                                               c_atr=c_atr)
                    if STRATEGY in ("V_BOTTOM", "PANIC_TRAP") and self.original_df is not None:
                        # Авто-маркер: если у вотчера этого уровня было debug-событие
                        # ИМЕННО на текущей свече — отмечаем точкой на графике.
                        level_id = self.manager._level_id(self.origin_level_long, 'LONG')
                        watcher = self.manager._watchers.get(level_id)
                        if watcher is not None and getattr(watcher, 'last_event_time', None) == current_time:
                            self.original_df.at[current_time, 'vbottom_event'] = c_close
                    if decision.get('allow'):
                        self._try_enter(self.origin_level_long, 'LONG', c_close, c_atr, decision, ctx_eval=ctx_eval_long)
                        self.origin_level_long = None
            else:
                for sup in CURRENT_SUPPORTS:
                    if recent_low > sup['max']:
                        continue
                    decision = self._evaluate(sup, 'LONG', c_open, c_high, c_low, c_close, CURRENT_RESISTANCES, df_slice)
                    if decision.get('allow'):
                        self._try_enter(sup, 'LONG', c_close, c_atr, decision)
                        break

        if can_short:
            if STRATEGY in ("VOLUME_REVERSAL", "PIT_CLIMAX", "PANIC_TRAP", "V_BOTTOM"):
                if self.origin_level_short is not None:
                    ctx_eval_short = self._get_context(self.origin_level_short, 'SHORT', c_atr)
                    decision = self._evaluate(self.origin_level_short, 'SHORT', c_open, c_high, c_low, c_close,
                                               CURRENT_SUPPORTS, df_slice, trend=ctx_eval_short.get('trend', 'UNKNOWN'),
                                               c_atr=c_atr)
                    if decision.get('allow'):
                        self._try_enter(self.origin_level_short, 'SHORT', c_close, c_atr, decision, ctx_eval=ctx_eval_short)
                        self.origin_level_short = None
            else:
                for res in CURRENT_RESISTANCES:
                    if recent_high < res['min']:
                        continue
                    decision = self._evaluate(res, 'SHORT', c_open, c_high, c_low, c_close, CURRENT_SUPPORTS, df_slice)
                    if decision.get('allow'):
                        self._try_enter(res, 'SHORT', c_close, c_atr, decision)
                        break
                        
    def _evaluate(self, level, trade_type, c_open, c_high, c_low, c_close, opposite_levels, df_slice, trend='UNKNOWN', c_atr=None):
        if STRATEGY == "SWEEP_RECLAIM":
            decision = self.manager.evaluate_sweep_reclaim(
                level, c_open, c_high, c_low, c_close, opposite_levels, trade_type
            )
        elif STRATEGY == "VOLUME_REVERSAL":
            decision = self.manager.evaluate_volume_reversal(
                level, df_slice, trade_type, opposite_levels
            )
        elif STRATEGY == "PIT_CLIMAX":
            decision = self.manager.evaluate_pit_climax(
                level, df_slice, trade_type, opposite_levels
            )
        elif STRATEGY == "PANIC_TRAP":
            decision = self.manager.evaluate_panic_trap(
                level, df_slice, trade_type, opposite_levels, trend=trend
            )
        elif STRATEGY == "V_BOTTOM":
            decision = self.manager.evaluate_v_bottom(
                level, df_slice, trade_type, opposite_levels, trend=trend, c_atr=c_atr
            )
        else: 
            decision = {'allow': False, 'reason': 'Unknown strategy'}

        if not decision.get('allow'):
            reason_str = decision.get('reason', '')
            if ('No signal' in reason_str or 'No CHoCH' in reason_str
                    or 'No volume reversal' in reason_str or 'No pit climax' in reason_str
                    or 'No V bottom' in reason_str):
                GLOBAL_DEBUG_STATS["No_Signal"] += 1
            else:
                GLOBAL_DEBUG_STATS["Killed_by_QUALITY"] += 1
        return decision

    def _origin_still_needed(self, level, trade_type):
        """Не сбрасываем origin_level, пока watcher для этого уровня ещё ждёт
        подтверждения входа (WAIT_GREEN/WAIT_RED/TRAP_SET/... для старых стратегий,
        WAIT_START/WAIT_PEAK/WAIT_GREEN/WAIT_NEW_PEAK для V_BOTTOM) — иначе сделка
        теряется насовсем, даже если через пару свечей появился бы валидный вход."""
        level_id = self.manager._level_id(level, trade_type)
        watcher = self.manager._watchers.get(level_id)
        if watcher is not None and hasattr(watcher, 'state'):
            return watcher.state in ("WAIT_GREEN", "WAIT_RED", "TRAP_SET", "CANDIDATE_ARMED", "TRACKING_CASCADE",
                                      "WAIT_START", "WAIT_PEAK", "WAIT_NEW_PEAK")
        return False

    def _get_context(self, level, trade_type, c_atr):
        """Считает макро-контекст (TREND/ENERGY/APPROACH) на 4H. Вынесено из _try_enter,
        чтобы можно было узнать trend ДО того, как стратегия примет решение о входе
        (нужно для volume-decay фильтра в PanicTrapWatcher)."""
        current_time = pd.to_datetime(self.data.index[-1])
        df_4h_ctx = getattr(self, 'context_df_4h', None)
        if df_4h_ctx is not None and len(df_4h_ctx) > 0:
            cutoff_time = current_time - pd.Timedelta(hours=4)
            closed_4h = df_4h_ctx.loc[:cutoff_time]
        else:
            closed_4h = pd.DataFrame()

        if len(closed_4h) >= 20:
            ctx_window = closed_4h.tail(110)
            return analyze_context(ctx_window['Close'].values, ctx_window['High'].values,
                                    ctx_window['Low'].values, c_atr,
                                    trade_type, level['min'], level['max'],
                                    opens=ctx_window['Open'].values)
        return {"allowed": True, "reason": "Not enough 4H data", "approach": "UNKNOWN",
                "trend": "UNKNOWN", "energy": "UNKNOWN"}

    def _try_enter(self, level, trade_type, current_price, c_atr, decision, ctx_eval=None):
        global GLOBAL_TRADE_CONTEXTS, GLOBAL_DEBUG_STATS

        if ctx_eval is None:
            ctx_eval = self._get_context(level, trade_type, c_atr)

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
            dist_from_level_pct = ((level['max'] - current_price) / current_price) * 100
        else:
            entry_depth = ((current_price - level['min']) / zone_range) * 100 if zone_range > 0 else 0.0
            closest = max([s['max'] for s in CURRENT_SUPPORTS if s['max'] < level['min']], default=None)
            gap_pct = ((level['min'] - closest) / closest) * 100 if closest else 999.0
            dist_from_level_pct = ((current_price - level['min']) / current_price) * 100

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
            "context_reason": ctx_eval.get("reason", ""),  
            "reason": decision['reason'],
            "ema_dist": round(ema_dist_pct, 2),
            "dist_from_level": round(dist_from_level_pct, 2),            
            "is_real_sweep": str(decision.get('is_real_sweep', 'False')),
            "overshoot_pct": round(decision.get('overshoot_pct', 0.0), 3),
            "candles_in_sweep": decision.get('candles_in_sweep', 0),
            "legs_count": decision.get('legs_count', '?'),
            "entry_price": round(current_price, 8),
            "sl": round(decision.get('sl', 0.0), 8),
            "tp": round(decision.get('tp', 0.0), 8),
        }

        self.current_trade_level_id = decision['level_id']
        self.current_trade_signal_time = signal_time
        # ВОТ ЭТА СТРОКА ДОЛЖНА БЫТЬ ТОЛЬКО ЗДЕСЬ:
        self.last_entered_level = (level['min'], level['max'], trade_type)
        GLOBAL_DEBUG_STATS["Passed_to_Trade"] += 1

        entry_time = pd.to_datetime(self.data.index[-1])
        deadline = None
        if DISABLE_SL_DIAGNOSTIC:
            deadline = entry_time + pd.Timedelta(days=DIAGNOSTIC_DEADLINE_DAYS)

        if trade_type == 'LONG':
            if DISABLE_SL_DIAGNOSTIC:
                self.buy()  
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
    os.makedirs("data_cache", exist_ok=True)
    
    date_suffix = TEST_START_DATE[:10] if TEST_START_DATE else "live"
    # Название файла остается строго твоим:
    file_name = f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}_w{WARMUP_DAYS}_{date_suffix}.csv"
    cache_file = os.path.join("data_cache", file_name)

    if os.path.exists(cache_file):
        return pd.read_csv(cache_file, index_col=0, parse_dates=True)
    else:
        try:
            try:
                exchange.load_markets()
            except Exception:
                pass

            symbol_perp = f"{coin.upper()}/USDT:USDT"
            symbol_spot = f"{coin.upper()}/USDT"
            symbol = symbol_perp if exchange.markets and symbol_perp in exchange.markets else symbol_spot
            
            CANDLES_PER_DAY_15M = 96  
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
                        break  
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
    for idx, row in tr.iterrows():
        signal_time_str = str(row['EntryTime'] - pd.Timedelta(minutes=15))
        ctx = GLOBAL_TRADE_CONTEXTS.get(signal_time_str, {})
        trade_type = "LONG" if row['Size'] > 0 else "SHORT"

        # --- ВСЕГДА считаем MAE и время удержания ---
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

        score = ctx.get('score', 0)
        score_bucket = f"{int(score)}" if score else "?"
        if score_bucket not in GLOBAL_SCORE_STATS:
            GLOBAL_SCORE_STATS[score_bucket] = {"trades": 0, "win": 0, "pnl": 0.0, "mae_list": []}
        GLOBAL_SCORE_STATS[score_bucket]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_SCORE_STATS[score_bucket]["win"] += 1
        GLOBAL_SCORE_STATS[score_bucket]["pnl"] += row['ReturnPct'] * 100
        GLOBAL_SCORE_STATS[score_bucket]["mae_list"].append(ctx.get('mae_pct', 0.0))

        gap = ctx.get('gap', 0)
        if isinstance(gap, (int, float)):
            if gap < 4: gap_bucket = "0-4%"
            elif gap < 8: gap_bucket = "4-8%"
            elif gap < 15: gap_bucket = "8-15%"
            else: gap_bucket = "15%+"
        else:
            gap_bucket = "?"
        if gap_bucket not in GLOBAL_GAP_STATS:
            GLOBAL_GAP_STATS[gap_bucket] = {"trades": 0, "win": 0, "pnl": 0.0, "mae_list": []}
        GLOBAL_GAP_STATS[gap_bucket]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_GAP_STATS[gap_bucket]["win"] += 1
        GLOBAL_GAP_STATS[gap_bucket]["pnl"] += row['ReturnPct'] * 100
        GLOBAL_GAP_STATS[gap_bucket]["mae_list"].append(ctx.get('mae_pct', 0.0))

        trend = ctx.get('trend', 'UNKNOWN')
        if trend not in GLOBAL_TREND_STATS:
            GLOBAL_TREND_STATS[trend] = {"trades": 0, "win": 0, "pnl": 0.0}
        GLOBAL_TREND_STATS[trend]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_TREND_STATS[trend]["win"] += 1
        GLOBAL_TREND_STATS[trend]["pnl"] += row['ReturnPct'] * 100

        GLOBAL_COIN_TRENDS.setdefault(coin, []).append(
            f"{trend}(ног:{ctx.get('legs_count', '?')},{'W' if row['PnL'] > 0 else 'L'})"
        )

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

        lvl_type = ctx.get('type', 'UNKNOWN')
        if lvl_type not in GLOBAL_LEVEL_TYPE_STATS:
            GLOBAL_LEVEL_TYPE_STATS[lvl_type] = {"trades": 0, "win": 0, "pnl": 0.0, "mae_list": []}
        GLOBAL_LEVEL_TYPE_STATS[lvl_type]["trades"] += 1
        if row['PnL'] > 0:
            GLOBAL_LEVEL_TYPE_STATS[lvl_type]["win"] += 1
        GLOBAL_LEVEL_TYPE_STATS[lvl_type]["pnl"] += row['ReturnPct'] * 100
        GLOBAL_LEVEL_TYPE_STATS[lvl_type]["mae_list"].append(ctx.get('mae_pct', 0.0))

        # Вывод MAE в лог теперь происходит ВСЕГДА
        mae_str = f" | MAE:-{ctx.get('mae_pct', 0.0):.2f}%"
        log_str = (f"{coin.upper()} | {trade_type} | Рез: {row['ReturnPct']*100:.2f}%{mae_str} | "
                   f"Entry:{ctx.get('entry_price','?')} SL:{ctx.get('sl','?')} TP:{ctx.get('tp','?')} | "
                   f"УРОВЕНЬ:[{ctx.get('level_min','?')}-{ctx.get('level_max','?')}] Gap:{ctx.get('gap','?')}% | "
                   f"Score:{ctx.get('score','?')} EMA:{ctx.get('ema_dist','?')}% "
                   f"УрВыше:{ctx.get('dist_from_level','?')}% Глубина:{ctx.get('depth','?')}% | "
                   f"{ctx.get('reason', '')}")

        if row['PnL'] <= 0:
            GLOBAL_LOSERS_LOG.append("❌ " + log_str)
        else:
            GLOBAL_WINNERS_LOG.append("✅ " + log_str)


GLOBAL_SWEEP_STATS = {}
GLOBAL_SCORE_STATS = {}
GLOBAL_GAP_STATS = {}
GLOBAL_TREND_STATS = {}
GLOBAL_EMA_STATS = {}
GLOBAL_LEVEL_TYPE_STATS = {}
GLOBAL_COIN_TRENDS = {}


if TARGET_COIN.upper() == "ALL":
    print(f"🤖 Аудит запущен (стратегия: {STRATEGY}). Собираем данные...")

    for coin, data in macro_db.items():
        TARGET_COIN_CURRENT = coin
        if not isinstance(data, dict): continue
        CURRENT_SUPPORTS = data.get("supports", [])
        CURRENT_RESISTANCES = data.get("resistances", [])
        if not CURRENT_SUPPORTS and not CURRENT_RESISTANCES: continue

        cache_exists_before = os.path.exists(os.path.join("data_cache", f"cache_{coin.lower()}_{TIMEFRAME}_{LIMIT_CANDLES}_w{WARMUP_DAYS}_{TEST_START_DATE[:10] if TEST_START_DATE else 'live'}.csv"))
        df = get_cached_data(coin)
        
        if df.empty: continue
        if not cache_exists_before: time.sleep(0.5)

        df['sup_max'] = np.nan
        df['res_min'] = np.nan
        df['vbottom_event'] = np.nan
        df['ema'] = df['Close'].ewm(span=50, adjust=False).mean()
        df['avg_vol'] = df['Volume'].rolling(window=20).mean()
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

            closed_sum_pct = tr['ReturnPct'].sum() * 100
            actual_return_pct = stats['Return [%]']
            open_position_suspected = abs(actual_return_pct - closed_sum_pct) > 2.0
            if open_position_suspected:
                print(f"⚠️ {coin.upper()}: подозрение на незакрытую позицию. "
                      f"Сумма закрытых={closed_sum_pct:.2f}%, но Return [%]={actual_return_pct:.2f}%")

            print_trade_log(coin, tr)

            GLOBAL_REPORT.append({
                "Монета": coin.upper(),
                "Лонг (+/-)": f"{longs_win}/{longs_loss}",
                "Шорт (+/-)": f"{shorts_win}/{shorts_loss}",
                "Win Rate %": round(stats['Win Rate [%]'], 2),
                "Профит %": round(stats['Return [%]'], 2),
                "Trend": ", ".join(GLOBAL_COIN_TRENDS.get(coin, []))
            })

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

    # Вывод блока диагностики теперь происходит ВСЕГДА, если были сделки
    if GLOBAL_MAE_DIAGNOSTIC:
        maes = [d['mae_pct'] for d in GLOBAL_MAE_DIAGNOSTIC]
        holds = [d['hold_hours'] for d in GLOBAL_MAE_DIAGNOSTIC]
        tp_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'TP']
        deadline_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'DEADLINE']
        sl_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'SL']
        print("\n" + "=" * 85)
        print("🩺 ДИАГНОСТИКА ВХОДА И УДЕРЖАНИЯ (МАЕ)")
        print("=" * 85)
        print(f"Сделок всего: {len(GLOBAL_MAE_DIAGNOSTIC)} | Дошли до TP: {len(tp_hits)} | По стопу: {len(sl_hits)} | Дедлайн: {len(deadline_hits)}")
        print(f"Средний MAE (макс. просадка от входа): {sum(maes)/len(maes):.2f}%  |  Худший MAE: {max(maes):.2f}%")
        print(f"Среднее время удержания: {sum(holds)/len(holds):.1f}ч  |  Самое долгое: {max(holds):.1f}ч")

    print("\n" + "=" * 115)
    print("🚀 ПРИБЫЛЬНЫЕ СДЕЛКИ")
    print("=" * 115)
    for log in GLOBAL_WINNERS_LOG: print(log)

    print("\n" + "=" * 115)
    print("📉 УБЫТОЧНЫЕ СДЕЛКИ")
    print("=" * 115)
    for log in GLOBAL_LOSERS_LOG: print(log)

    print("\n" + "=" * 115)
    print("🕵️ ДИАГНОСТИКА ОТМЕН")
    print("=" * 115)
    for key, val in GLOBAL_DEBUG_STATS.items(): print(f"  {key}: {val}")

    total_origins = GLOBAL_DEBUG_STATS["Origins_Long_Total"] + GLOBAL_DEBUG_STATS["Origins_Short_Total"]
    if total_origins > 0:
        conversion = (GLOBAL_DEBUG_STATS["Passed_to_Trade"] / total_origins) * 100
        print(f"\n  📐 КОНВЕРСИЯ: из {total_origins} пробитых уровней получилось {GLOBAL_DEBUG_STATS['Passed_to_Trade']} сделок = {conversion:.1f}%")

    print("\n" + "=" * 85)
    print("📊 СТАТИСТИКА ПО ТИПАМ ПОДХОДА")
    print("=" * 85)
    for app, data in GLOBAL_APPROACH_STATS.items():
        if data["trades"] > 0:
            print(f"{app}: trades={data['trades']}  WR={(data['win'] / data['trades']) * 100:.1f}%")

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
            avg = d["pnl"] / d["trades"]
            mae_part = ""
            if d.get("mae_list"): # Вывод MAE теперь работает для всех режимов
                avg_mae = sum(d["mae_list"]) / len(d["mae_list"])
                worst_mae = max(d["mae_list"])
                mae_part = f"  MAE avg={avg_mae:.2f}% worst={worst_mae:.2f}%"
            print(f"Score {score}: trades={d['trades']}  WR={(d['win'] / d['trades']) * 100:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%{mae_part}")

    print("\n" + "=" * 85)
    print("📊 GAP vs РЕЗУЛЬТАТ (расстояние до следующего уровня)")
    print("=" * 85)
    for gap_b in ["0-4%", "4-8%", "8-15%", "15%+", "?"]:
        if gap_b in GLOBAL_GAP_STATS:
            d = GLOBAL_GAP_STATS[gap_b]
            if d["trades"] > 0:
                avg = d["pnl"] / d["trades"]
                mae_part = ""
                if d.get("mae_list"): # Вывод MAE теперь работает для всех режимов
                    avg_mae = sum(d["mae_list"]) / len(d["mae_list"])
                    worst_mae = max(d["mae_list"])
                    mae_part = f"  MAE avg={avg_mae:.2f}% worst={worst_mae:.2f}%"
                print(f"Gap {gap_b}: trades={d['trades']}  WR={(d['win'] / d['trades']) * 100:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%{mae_part}")

    print("\n" + "=" * 85)
    print("📊 TREND vs РЕЗУЛЬТАТ")
    print("=" * 85)
    for trend, d in GLOBAL_TREND_STATS.items():
        if d["trades"] > 0:
            print(f"{trend}: trades={d['trades']}  WR={(d['win'] / d['trades']) * 100:.1f}%  Σ profit={d['pnl']:.2f}%  avg={d['pnl'] / d['trades']:.2f}%")

    print("\n" + "=" * 85)
    print("📊 ПОЗИЦИЯ vs EMA (LONG выше/ниже EMA)")
    print("=" * 85)
    for ema, d in GLOBAL_EMA_STATS.items():
        if d["trades"] > 0:
            print(f"{ema}: trades={d['trades']}  WR={(d['win'] / d['trades']) * 100:.1f}%  Σ profit={d['pnl']:.2f}%  avg={d['pnl'] / d['trades']:.2f}%")

    print("\n" + "=" * 85)
    print("📊 ТИП УРОВНЯ vs РЕЗУЛЬТАТ")
    print("=" * 85)
    for l_type in sorted(GLOBAL_LEVEL_TYPE_STATS.keys()):
        d = GLOBAL_LEVEL_TYPE_STATS[l_type]
        if d["trades"] > 0:
            avg = d["pnl"] / d["trades"]
            avg_mae = sum(d["mae_list"]) / len(d["mae_list"]) if d["mae_list"] else 0.0
            print(f"{l_type}: trades={d['trades']}  WR={(d['win'] / d['trades']) * 100:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%  MAE avg={avg_mae:.2f}%")


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
            df['vbottom_event'] = np.nan
            df['ema'] = df['Close'].ewm(span=13, adjust=False).mean()
            df['avg_vol'] = df['Volume'].rolling(window=20).mean()
            df['open'] = df['Open']
            df['high'] = df['High']
            df['low'] = df['Low']
            df['close'] = df['Close']
            df['volume'] = df['Volume']

            SmartSniperUniversal.context_df_4h = build_4h_context_df(df)
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

                if GLOBAL_MAE_DIAGNOSTIC:
                    maes = [d['mae_pct'] for d in GLOBAL_MAE_DIAGNOSTIC]
                    holds = [d['hold_hours'] for d in GLOBAL_MAE_DIAGNOSTIC]
                    tp_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'TP']
                    deadline_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'DEADLINE']
                    sl_hits = [d for d in GLOBAL_MAE_DIAGNOSTIC if d['exit_reason'] == 'SL']
                    print("\n" + "=" * 85)
                    print("🩺 ДИАГНОСТИКА ВХОДА И УДЕРЖАНИЯ (МАЕ)")
                    print("=" * 85)
                    print(f"Сделок всего: {len(GLOBAL_MAE_DIAGNOSTIC)} | Дошли до TP: {len(tp_hits)} | По стопу: {len(sl_hits)} | Дедлайн: {len(deadline_hits)}")
                    print(f"Средний MAE (макс. просадка от входа): {sum(maes)/len(maes):.2f}%  |  Худший MAE: {max(maes):.2f}%")
                    print(f"Среднее время удержания: {sum(holds)/len(holds):.1f}ч  |  Самое долгое: {max(holds):.1f}ч")

                print("\n" + "=" * 85)
                print("📊 СТАТИСТИКА ПО ТИПАМ ПОДХОДА")
                print("=" * 85)
                for app, data in GLOBAL_APPROACH_STATS.items():
                    if data["trades"] > 0: print(f"{app}: trades={data['trades']}  WR={(data['win'] / data['trades']) * 100:.1f}%")

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
                print("📊 SCORE vs РЕЗУЛЬТАТ")
                print("=" * 85)
                for score in sorted(GLOBAL_SCORE_STATS.keys()):
                    d = GLOBAL_SCORE_STATS[score]
                    if d["trades"] > 0:
                        mae_part = ""
                        if d.get("mae_list"):
                            mae_part = f"  MAE avg={sum(d['mae_list']) / len(d['mae_list']):.2f}% worst={max(d['mae_list']):.2f}%"
                        print(f"Score {score}: trades={d['trades']}  WR={(d['win'] / d['trades']) * 100:.1f}%  Σ profit={d['pnl']:.2f}%  avg={d['pnl'] / d['trades']:.2f}%{mae_part}")

                print("\n" + "=" * 85)
                print("📊 GAP vs РЕЗУЛЬТАТ (расстояние до следующего уровня)")
                print("=" * 85)
                for gap_b in ["0-4%", "4-8%", "8-15%", "15%+", "?"]:
                    if gap_b in GLOBAL_GAP_STATS:
                        d = GLOBAL_GAP_STATS[gap_b]
                        if d["trades"] > 0:
                            mae_part = ""
                            if d.get("mae_list"):
                                mae_part = f"  MAE avg={sum(d['mae_list']) / len(d['mae_list']):.2f}% worst={max(d['mae_list']):.2f}%"
                            print(f"Gap {gap_b}: trades={d['trades']}  WR={(d['win'] / d['trades']) * 100:.1f}%  Σ profit={d['pnl']:.2f}%  avg={d['pnl'] / d['trades']:.2f}%{mae_part}")

                print("\n" + "=" * 85)
                print("📊 ТИП УРОВНЯ vs РЕЗУЛЬТАТ")
                print("=" * 85)
                for l_type in sorted(GLOBAL_LEVEL_TYPE_STATS.keys()):
                    d = GLOBAL_LEVEL_TYPE_STATS[l_type]
                    if d["trades"] > 0:
                        avg = d["pnl"] / d["trades"]
                        avg_mae = sum(d["mae_list"]) / len(d["mae_list"]) if d["mae_list"] else 0.0
                        print(f"{l_type}: trades={d['trades']}  WR={(d['win'] / d['trades']) * 100:.1f}%  Σ profit={d['pnl']:.2f}%  avg={avg:.2f}%  MAE avg={avg_mae:.2f}%")


            chart_path = os.path.abspath(f'chart_{TARGET_COIN.lower()}.html')
            try:
                bt.plot(filename=chart_path, open_browser=True)
            except Exception as e:
                print(f"⚠️ График не открылся: {e}")

            GLOBAL_TRADE_CONTEXTS = {}