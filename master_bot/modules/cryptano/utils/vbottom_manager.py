"""
vbottom_manager.py
==================
Минимальный менеджер для стратегии V_BOTTOM.
Держит реестр вотчеров по level_id, управляет их состоянием.
Готов к расширению: можно добавить новые evaluate_*_strategy методы
для других стратегий без переписания этого файла.
"""

from .v_bottom_watcher import VBottomWatcher
from .v_green_bottom_watcher import VGreenBottomWatcher


class VBottomManager:
    """Менеджер вотчеров для V-BOTTOM и V-GREEN-BOTTOM стратегий (работают в паре)."""
    
    def __init__(self):
        self._watchers = {}  # {level_id: Watcher}

    def _level_id(self, level, trade_type, strategy="VB"):
        """Генерирует уникальный ID уровня для реестра вотчеров.
        Префикс стратегии обязателен — иначе V_BOTTOM и V_GREEN_BOTTOM
        на одном и том же уровне столкнутся в одном словаре и будут
        портить друг другу состояние (разные классы, разные state-машины)."""
        return f"{strategy}_{trade_type}_{level['min']}_{level['max']}"

    def _deny(self, reason):
        """Стандартный формат отказа."""
        return {
            'allow': False,
            'reason': reason,
            'sl': 0.0,
            'tp': 0.0,
            'level_id': None,
            'entry_price': None,
            'history_log': '',
        }

    def notify_breach(self, level, trade_type):
        """
        Вызывается в момент пробития уровня (новый заход).
        Если вотчер уже существует — сбрасываем его математику.
        """
        level_id = self._level_id(level, trade_type, "VB")
        watcher = self._watchers.get(level_id)
        if watcher is not None and hasattr(watcher, 'on_breach_start'):
            watcher.on_breach_start()

    def force_reset_watcher(self, level, trade_type):
        """Жестко сбрасывает вотчер (цена ушла за буфер, выход из движения)."""
        level_id = self._level_id(level, trade_type, "VB")
        watcher = self._watchers.get(level_id)
        if watcher is not None and hasattr(watcher, '_reset_chain'):
            watcher._reset_chain()

    def watcher_count(self):
        """Сколько вотчеров сейчас держим в памяти (для мониторинга/логов)."""
        return len(self._watchers)

    def clear_dead_watchers(self, active_level_ids):
        """Удаляет вотчеры для уровней, которых больше нет на графике."""
        dead_keys = []
        for k, watcher in self._watchers.items():
            if k not in active_level_ids:
                # Если вотчер в процессе (не TRIGGERED, не DEAD) — оставляем
                if hasattr(watcher, 'state') and watcher.state not in ["TRIGGERED", "DEAD"]:
                    continue
                dead_keys.append(k)
        
        for k in dead_keys:
            del self._watchers[k]

    def evaluate_v_bottom(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN', c_atr=None):
        """
        Проверяет V-BOTTOM паттерн на уровне по DataFrame свечей.
        
        Args:
            level: dict {'min': float, 'max': float} — уровень поддержки
            df: DataFrame с колонками [open, high, low, close, volume] и индексом time
            trade_type: 'LONG' или 'SHORT'
            all_opposite_levels: list[dict] — уровни сопротивления (для TP расчёта)
            trend: str — тренд ('UP', 'DOWN', 'UNKNOWN'), пока не используется
            c_atr: float — ATR, пока не используется
        
        Returns:
            dict: {'allow': True/False, 'entry_price': float, 'sl': float, 'tp': float, ...}
        """
        level_id = self._level_id(level, trade_type, "VB")

        # Если это новый уровень — создаём вотчер
        if level_id not in self._watchers:
            self._watchers[level_id] = VBottomWatcher(level['min'], level['max'], trade_type)

        watcher = self._watchers[level_id]

        # Нужно минимум 52 свечи для расчёта baseline volume
        if len(df) < 52:
            return self._deny("Not enough data (< 52 candles)")

        # Считаем средний объём из последних 52 свечей (исключаем последние 2)
        baseline_vol = float(df['volume'].iloc[-52:-2].mean())
        vol_std = float(df['volume'].iloc[-52:-2].std()) if len(df) >= 52 else None

        # Берём параметры последней свечи
        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        # Передаём время свечи (индекс DataFrame) для логирования
        candle_time = df.index[-1] if hasattr(df, 'index') else None

        # Запускаем логику вотчера
        signal = watcher.update(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels,
            trend=trend, vol_std=vol_std, candle_time=candle_time
        )

        # Если вотчер не дал сигнал
        if not signal:
            return self._deny(f"No V-bottom signal (state: {watcher.state})")

        # Если калькулятор отклонил из-за R/R или другой проблемы
        if 'error' in signal:
            return self._deny(signal['error'])

        # ВХОД СОСТОЯЛСЯ
        return {
            'allow': True,
            'reason': signal.get('reason', 'V-BOTTOM pattern detected'),
            'entry_price': signal.get('entry_price', c_close),
            'sl': signal.get('sl', 0.0),
            'tp': signal.get('tp', 0.0),
            'level_id': level_id,
            'history_log': watcher.history_log,
            'action': signal.get('action', 'BUY'),
        }

    def evaluate_v_green_bottom(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN', c_atr=None):
        """
        Проверяет V-GREEN-BOTTOM паттерн (лестница из нескольких ям + режим
        кульминации на аномальном объёме). Работает только для LONG.

        Args:
            level: dict {'min': float, 'max': float} — уровень поддержки
            df: DataFrame с колонками [open, high, low, close, volume] и индексом time
            trade_type: 'LONG' или 'SHORT' (SHORT не поддерживается, отклоняется сразу)
            all_opposite_levels: list[dict] — уровни сопротивления (для TP расчёта)
            trend: str — тренд, пока не используется
            c_atr: float — ATR (14, 15m) — используется для фильтра размера свечи входа

        Returns:
            dict: {'allow': True/False, 'entry_price': float, 'sl': float, 'tp': float, ...}
        """
        if trade_type != 'LONG':
            return self._deny("V-GREEN-BOTTOM поддерживает только LONG")

        level_id = self._level_id(level, trade_type, "VGB")

        if level_id not in self._watchers:
            self._watchers[level_id] = VGreenBottomWatcher(level['min'], level['max'], trade_type)

        watcher = self._watchers[level_id]

        if len(df) < 52:
            return self._deny("Not enough data (< 52 candles)")

        baseline_vol = float(df['volume'].iloc[-52:-2].mean())

        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        candle_time = df.index[-1] if hasattr(df, 'index') else None

        signal = watcher.update(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels,
            trend=trend, candle_time=candle_time
        )

        if not signal:
            return self._deny(f"No V-green-bottom signal (state: {watcher.state})")

        if 'error' in signal:
            return self._deny(signal['error'])

        return {
            'allow': True,
            'reason': signal.get('reason', 'V-GREEN-BOTTOM pattern detected'),
            'entry_price': signal.get('entry_price', c_close),
            'sl': signal.get('sl', 0.0),
            'tp': signal.get('tp', 0.0),
            'level_id': level_id,
            'history_log': watcher.history_log,
            'action': signal.get('action', 'BUY'),
        }