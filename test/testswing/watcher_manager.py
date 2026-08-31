"""
watcher_manager.py
==================
Оркестратор для watcher-ов. 
Он не знает, КАК работают стратегии. Он просто берет нужную функцию 
из watcher_methods.py, вызывает её и возвращает симулятору готовый ответ.
"""

import pandas as pd

from .watcher_methods import (ChochRetestWatcher, check_choch_zone,
                               check_pit_climax, _calc_tp_and_rr)
from .bounce_manager import BounceManager
from .v_bottom_watcher import VBottomWatcher
from .panic_trap_watcher import PanicTrapWatcher
from .v_green_bottom_watcher import VGreenBottomWatcher
from .v_red_cascade_watcher import VRedCascadeWatcher
from .v_red_top_watcher import VRedTopWatcher
from .breakout_retest_watcher import BreakoutRetestWatcher

STRATEGIES = ["BOUNCE", "VOLUME_REVERSAL", "PIT_CLIMAX", "PANIC_TRAP", "V_BOTTOM", "V_GREEN_BOTTOM", "V_RED_CASCADE", "V_RED_TOP", "BREAKOUT_RETEST"]

class WatcherManager:
    def __init__(self, strategy, config=None):
        if strategy not in STRATEGIES:
            raise ValueError(f"Unknown strategy: {strategy}. Use one of {STRATEGIES}")
        self.strategy = strategy
        self.config = config or {}
        self.burned_levels = set()
        self._watchers = {}
        self.bounce = BounceManager(self)

    def _level_id(self, level, trade_type):
        return f"{trade_type}_{level['min']}_{level['max']}"

    def _deny(self, reason):
        return {'allow': False, 'reason': reason, 'sl': 0.0, 'tp': 0.0, 'level_id': None, 'extreme_price': "0.0",
                'is_real_sweep': False, 'overshoot_pct': 0.0, 'candles_in_sweep': 0}

    def notify_breach(self, level, trade_type):
        """Симулятор зовёт это в момент НОВОГО пробития уровня (создание origin).
        Для V_BOTTOM: гарантированный сброс математики вотчера с нуля + учёт
        лимита повторных пробитий. Если вотчера ещё нет — ничего не делаем
        (он создастся свежим при первом evaluate)."""
        level_id = self._level_id(level, trade_type)
        watcher = self._watchers.get(level_id)
        if watcher is not None and hasattr(watcher, 'on_breach_start'):
            watcher.on_breach_start()


    def force_reset_watcher(self, level, trade_type):
        """Жестко обнуляет математику вотчера по прямому приказу симулятора."""
        level_id = self._level_id(level, trade_type)
        watcher = self._watchers.get(level_id)
        if watcher is not None and hasattr(watcher, '_reset_chain'):
            # ДОБАВЛЕНО: Запись в лог перед смертью
            if hasattr(watcher, '_dbg') and getattr(watcher, 'history_log', ''):
                watcher._dbg(f"⚡ [ПРИНУДИТЕЛЬНЫЙ СБРОС СИМУЛЯТОРОМ] Цена ушла за буфер. Память очищена.")
            watcher._reset_chain()

    def clear_dead_watchers(self, active_level_ids):
        """Удаляет инстансы стейт-машин для уровней, которые ушли с графика."""
        dead_keys = []
        for k, watcher in self._watchers.items():
            if k not in active_level_ids:
                # СПАСАЕМ КАПКАНЫ И SMC: Если бот заряжен на ожидание ретеста, оставляем его в памяти!
                if hasattr(watcher, 'state') and watcher.state in ["TRAP_SET", "WAIT_RETEST", "WAIT_GREEN", "WAIT_RED", "CANDIDATE_ARMED", "WAIT_CHOCH", "WAIT_BREAKOUT", "WAIT_PULLBACK", "WAIT_TRIGGER", "SCANNING", "TRIGGERED", "DEAD"]:
                    continue
                dead_keys.append(k)
                
        for k in dead_keys:
            del self._watchers[k]

    def has_active_bounce_watchers(self, trade_type=None):
        """Тонкая обёртка — реальная логика теперь в BounceManager (bounce_manager.py)."""
        return self.bounce.has_active_bounce_watchers(trade_type)

    def evaluate_bounce_side(self, trade_type, touched_levels, evaluator):
        """Тонкая обёртка — реальная логика теперь в BounceManager (bounce_manager.py)."""
        return self.bounce.evaluate_bounce_side(trade_type, touched_levels, evaluator)

    # -------------------------------------------------------------------------
    # 1. BOUNCE (Отбой от макро-уровня)
    # -------------------------------------------------------------------------
    def evaluate_bounce(self, level, df, trade_type, all_opposite_levels):
        """Тонкая обёртка — реальная логика теперь в BounceManager (bounce_manager.py)."""
        return self.bounce.evaluate_bounce(level, df, trade_type, all_opposite_levels)
    # -------------------------------------------------------------------------
    # 2. VOLUME REVERSAL (SMC)
    # -------------------------------------------------------------------------
    def evaluate_volume_reversal(self, level, df, trade_type, all_opposite_levels):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = ChochRetestWatcher(level['min'], level['max'], trade_type)
        watcher = self._watchers[level_id]

        # ФАЗА 1: пока watcher ждёт CHoCH - ищем его каждую свечу
        if watcher.state == "WAIT_CHOCH":
            zone = check_choch_zone(df, level, trade_type)
            if zone is not None:
                watcher.on_choch_detected(zone["origin_low"], zone["origin_high"], zone["choch_close"])
            # Даже если CHoCH найден только что - в эту же свечу не входим,
            # ждём следующую свечу для проверки ретеста (сигнал ещё не готов)
            return self._deny(f"No signal (state: {watcher.state})")

        # ФАЗА 2: watcher ждёт возврата цены в зону CHoCH
        c_open, c_high, c_low, c_close = (
            float(df['open'].iloc[-1]), float(df['high'].iloc[-1]),
            float(df['low'].iloc[-1]), float(df['close'].iloc[-1])
        )
        signal = watcher.update(c_open, c_high, c_low, c_close, all_opposite_levels)

        if not signal:
            return self._deny(f"No signal (state: {watcher.state})")

        current_price = c_close
        raw_sl = signal['sl']
        CONFIG = {
            'TP_MODE': 'structural', 'FIXED_TP_PCT': 8.0, 'TAKE_PROFIT': 8.0,
            'TP_BUFFER_PCT': 0.3, 'SL_BUFFER': 0.2, 'MIN_RR': 2.0, 'USE_RR_FILTER': True,
        }
        risk_data, err = _calc_tp_and_rr(current_price, raw_sl, trade_type, all_opposite_levels, CONFIG)
        if err or not risk_data:
            self.burned_levels.add(level_id)
            return self._deny(err or "Risk data is None")

        self.burned_levels.add(level_id)
        return {
            'allow': True, 'reason': signal['reason'], 'sl': risk_data['sl'], 'tp': risk_data['tp'],
            'level_id': level_id, 'extreme_price': str(raw_sl), 'is_real_sweep': True,
            'overshoot_pct': 0.0, 'candles_in_sweep': watcher.candles_in_retest,
        }

    # -------------------------------------------------------------------------
    # 3. PIT CLIMAX (Wyckoff)
    # -------------------------------------------------------------------------
    def evaluate_pit_climax(self, level, df, trade_type, all_opposite_levels):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        # Вызываем метод (настройки он берет сам внутри себя)
        signal = check_pit_climax(df, level, trade_type, all_opposite_levels)

        if not signal:
            return self._deny("No pit climax pattern in window")
            
        # Защита от ошибки калькулятора
        if 'error' in signal:
            return self._deny(signal['error'])
        self.burned_levels.add(level_id)

        signal['allow'] = True # type: ignore
        signal['level_id'] = level_id
        signal['extreme_price'] = str(signal.get('sl', '0.0'))
        signal['is_real_sweep'] = True# type: ignore
        return signal
    
    # -------------------------------------------------------------------------
    # 4. PANIC TRAP (Вторичный тест дна/вершины)
    # -------------------------------------------------------------------------
    def evaluate_v_bottom(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN', c_atr=None):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = VBottomWatcher(level['min'], level['max'], trade_type)

        watcher = self._watchers[level_id]

        if len(df) < 52:
            return self._deny("Not enough data for baseline volume")

        baseline_vol = float(df['volume'].iloc[-52:-2].mean())
        vol_std = float(df['volume'].iloc[-52:-2].std())

        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        signal = watcher.update(c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels,
                         trend=trend, vol_std=vol_std, candle_time=df.index[-1])

        if not signal:
            return self._deny("No V bottom signal")

        if 'error' in signal:
            return self._deny(signal['error'])

        self.burned_levels.add(level_id)

        signal['allow'] = True
        signal['level_id'] = level_id
        signal['extreme_price'] = str(watcher.sl_price) if watcher.sl_price is not None else "0.0"
        signal['is_real_sweep'] = True
        signal['overshoot_pct'] = 0.0
        signal['candles_in_sweep'] = 0

        return signal

    # -------------------------------------------------------------------------
    # 7. V_RED_CASCADE (Каскад с откатом - Метод 4)
    # -------------------------------------------------------------------------
    def evaluate_v_red_cascade(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN', c_atr=None, c_ema=None, c_atr_slow=None):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = VRedCascadeWatcher(level['min'], level['max'], trade_type)
        watcher = self._watchers[level_id]

        if len(df) < 52:
            return self._deny("Not enough data for baseline volume")

        baseline_vol = float(df['volume'].iloc[-52:-2].mean())
        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        c_rsi = float(c['rsi']) if 'rsi' in df.columns and c['rsi'] == c['rsi'] else 50.0

        signal = watcher.update(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, 
            candle_time=df.index[-1], c_ema=c_ema, c_atr_slow=c_atr_slow, c_rsi=c_rsi
        )

        if not signal:
            return self._deny("No V_RED_CASCADE signal")
            
        if 'error' in signal:
            return self._deny(signal['error'])

        # Уровень НЕ сжигаем, так как Каскад может делать добор позиций
        # self.burned_levels.add(level_id)
        
        signal['allow'] = True
        signal['level_id'] = level_id
        signal['extreme_price'] = str(watcher.sl_price) if getattr(watcher, 'sl_price', None) is not None else "0.0"
        signal['is_real_sweep'] = True
        signal['overshoot_pct'] = 0.0
        signal['candles_in_sweep'] = 0

        return signal

    def evaluate_panic_trap(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN'):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = PanicTrapWatcher(level['min'], level['max'], trade_type)

        watcher = self._watchers[level_id]

        if len(df) < 52:
            return self._deny("Not enough data for baseline volume")

        # Считаем средний объем (без последних двух свечей) для вычисления аномалии
        baseline_vol = float(df['volume'].iloc[-52:-2].mean())

        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        # КРАСИВОЕ РЕШЕНИЕ: передаем время прямо из датафрейма, как в V-Bottom
        signal = watcher.update(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol, all_opposite_levels, 
            trend=trend, candle_time=df.index[-1]
        )

        if not signal:
            return self._deny("No signal") 
            
        if 'error' in signal:
            return self._deny(signal['error'])
            
        self.burned_levels.add(level_id)

        signal['allow'] = True
        signal['level_id'] = level_id
        signal['extreme_price'] = str(watcher.sl_price) if watcher.sl_price is not None else "0.0"
        signal['is_real_sweep'] = True
        signal['overshoot_pct'] = 0.0
        signal['candles_in_sweep'] = 0
        
        return signal
    
    # 5. v_green_bottom
    # ДОБАВЛЕНО: c_ema=None в аргументы
    def evaluate_v_green_bottom(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN', c_atr=None, c_ema=None):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = VGreenBottomWatcher(level['min'], level['max'], trade_type)
        watcher = self._watchers[level_id]

        if len(df) < 52:
            return self._deny("Not enough data for baseline volume")

        baseline_vol = float(df['volume'].iloc[-52:-2].mean())
        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        # ДОБАВЛЕНО: передаем c_ema=c_ema в update
        signal = watcher.update(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, candle_time=df.index[-1], c_ema=c_ema
        )

        if not signal:
            return self._deny("No V-Green bottom signal")
        if 'error' in signal:
            return self._deny(signal['error'])

        self.burned_levels.add(level_id)
        signal['allow'] = True
        signal['level_id'] = level_id
        signal['extreme_price'] = str(watcher.sl_price) if getattr(watcher, 'sl_price', None) is not None else "0.0"
        signal['is_real_sweep'] = True
        signal['overshoot_pct'] = 0.0
        signal['candles_in_sweep'] = 0

        return signal
    
# -------------------------------------------------------------------------
    # 6. V_RED_TOP (Шортовая стратегия от хая)
    # -------------------------------------------------------------------------
    def evaluate_v_red_top(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN', c_atr=None, c_ema=None, c_atr_slow=None):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = VRedTopWatcher(level['min'], level['max'], trade_type)
        watcher = self._watchers[level_id]

        if len(df) < 52:
            return self._deny("Not enough data for baseline volume")

        baseline_vol = float(df['volume'].iloc[-52:-2].mean())
        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        c_rsi = float(c['rsi']) if 'rsi' in df.columns and c['rsi'] == c['rsi'] else 50.0

        signal = watcher.update(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, 
            candle_time=df.index[-1], c_ema=c_ema, c_atr_slow=c_atr_slow, c_rsi=c_rsi
        )

        if not signal:
            return self._deny("No V-Red top signal")
        if 'error' in signal:
            return self._deny(signal['error'])

        # self.burned_levels.add(level_id)  <--- Закомментировано! Менеджер больше не блочит уровень.
        
        signal['allow'] = True
        signal['level_id'] = level_id
        signal['extreme_price'] = str(watcher.sl_price) if getattr(watcher, 'sl_price', None) is not None else "0.0"
        signal['is_real_sweep'] = True
        signal['overshoot_pct'] = 0.0
        signal['candles_in_sweep'] = 0

        return signal 
# -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # BREAKOUT & RETEST (Пробой сопротивления и лонг на откате)
    # -------------------------------------------------------------------------
    def evaluate_breakout_retest(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN', c_atr=None, c_ema=None):
        level_id = self._level_id(level, trade_type)
        if level_id in self.burned_levels:
            return self._deny("Level already burned")

        if level_id not in self._watchers:
            self._watchers[level_id] = BreakoutRetestWatcher(level['min'], level['max'], trade_type)
        watcher = self._watchers[level_id]

        if len(df) < 52:
            return self._deny("Not enough data for baseline volume")

        baseline_vol = float(df['volume'].iloc[-52:-2].mean())
        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        signal = watcher.update(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels, candle_time=df.index[-1]
        )

        if not signal:
            return self._deny("No Breakout-Retest signal")
        if 'error' in signal:
            return self._deny(signal['error'])

        self.burned_levels.add(level_id)
        signal['allow'] = True
        signal['level_id'] = level_id
        signal['extreme_price'] = str(watcher.sl_price) if getattr(watcher, 'sl_price', None) is not None else "0.0"
        signal['is_real_sweep'] = True
        signal['overshoot_pct'] = 0.0
        signal['candles_in_sweep'] = 0

        return signal