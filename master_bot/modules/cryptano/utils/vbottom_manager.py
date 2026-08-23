"""
vbottom_manager.py
==================
Минимальный менеджер для стратегии V_BOTTOM.
Держит реестр вотчеров по level_id, управляет их состоянием.
Готов к расширению: можно добавить новые evaluate_*_strategy методы
для других стратегий без переписания этого файла.
"""
import os
import json
import pandas as pd

from .v_bottom_watcher import VBottomWatcher
from .v_green_bottom_watcher import VGreenBottomWatcher
from .v_red_top_watcher import VRedTopWatcher

_WATCHER_CLASSES = {
    "VBottomWatcher": VBottomWatcher,
    "VGreenBottomWatcher": VGreenBottomWatcher,
    "VRedTopWatcher": VRedTopWatcher,
}


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
    
    def remove_watchers_by_coin(self, coin):
        """Удаляет все вотчеры конкретной монеты (используется при ручном рескане)."""
        keys_to_delete = [k for k, w in self._watchers.items() if getattr(w, 'coin', 'UNKNOWN') == coin]
        for k in keys_to_delete:
            del self._watchers[k]

    def clear_dead_watchers(self, active_level_ids):
        """Удаляет вотчеры для уровней, которых больше нет на графике.

        Возвращает {level_id: watcher} для удалённых — чтобы вызывающий код
        (background_tasks.py) успел архивировать их путь (event_log) в
        watcher_history.json ДО того, как объект будет потерян навсегда."""
        dead_keys = []
        for k, watcher in self._watchers.items():
            if k not in active_level_ids:
                # Если вотчер в процессе (не TRIGGERED/DEAD/IDLE) — оставляем.
                # IDLE добавлен отдельно: у V_RED_TOP это тоже финальное
                # состояние движения (цена ушла под уровень), без него такие
                # вотчеры зависали в памяти навсегда и не архивировались.
                if hasattr(watcher, 'state') and watcher.state not in ["TRIGGERED", "DEAD", "IDLE"]:
                    continue
                dead_keys.append(k)

        removed = {}
        for k in dead_keys:
            removed[k] = self._watchers.pop(k)
        return removed

    def evaluate_v_bottom(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN', c_atr=None, coin="UNKNOWN"):
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
            self._watchers[level_id] = VBottomWatcher(level['min'], level['max'], trade_type, coin=coin)

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

    def evaluate_v_green_bottom(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN', c_atr=None, coin="UNKNOWN"):
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
            self._watchers[level_id] = VGreenBottomWatcher(level['min'], level['max'], trade_type, coin=coin)

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

    def evaluate_v_red_top(self, level, df, trade_type, all_opposite_levels, trend='UNKNOWN',
                            c_atr=None, c_atr_slow=None, c_ema=None, c_rsi=None, coin="UNKNOWN"):
        """
        Проверяет V-RED-TOP паттерн (шорт от сопротивления, структура "три
        индейца" + якорь/реакция/подтверждение по трём маршрутам RED1-3).
        Работает только для SHORT — лимит сделок на уровень (MAX_TRADES_PER_LEVEL)
        контролирует сам вотчер, менеджер уровень не блокирует после сигнала.

        Args:
            level: dict {'min': float, 'max': float} — уровень сопротивления
            df: DataFrame с колонками [open, high, low, close, volume] и индексом time
            trade_type: должен быть 'SHORT' (LONG отклоняется сразу)
            all_opposite_levels: list[dict] — уровни поддержки (для TP расчёта)
            trend: str — тренд, пока не используется
            c_atr: float — ATR (быстрый), используется вотчером для отката/пробоя пиков
            c_atr_slow: float — ATR_slow (SMA100 от ATR), пока не используется вотчером,
                прокидывается для паритета с симулятором
            c_ema: float — EMA50, фильтр "цена должна быть выше EMA на N%"
            c_rsi: float — RSI, используется только в логах/reason при входе

        Returns:
            dict: {'allow': True/False, 'entry_price': float, 'sl': float, 'tp': float, ...}
        """
        if trade_type != 'SHORT':
            return self._deny("V-RED-TOP поддерживает только SHORT")

        level_id = self._level_id(level, trade_type, "VRT")

        is_new_watcher = False
        if level_id not in self._watchers:
            self._watchers[level_id] = VRedTopWatcher(level['min'], level['max'], trade_type, coin=coin)
            is_new_watcher = True

        watcher = self._watchers[level_id]

        if len(df) < 52:
            return self._deny("Not enough data (< 52 candles)")

        # ==============================================================
        # ИСТОРИЧЕСКИЙ РЕПЛЕЙ (ПРОМОТКА) ДЛЯ НОВЫХ ВОТЧЕРОВ
        # ==============================================================
        if is_new_watcher:
            # 1. Ищем, когда реально был пробой уровня в прошлом
            start_idx = len(df) - 1
            for i in range(len(df) - 2, -1, -1):
                if df['close'].iloc[i] <= level['max'] and df['high'].iloc[i] <= level['max']:
                    start_idx = i + 1
                    break
            
            # Если пробой был очень давно (нет в скачанной истории)
            if start_idx == len(df) - 1 and df['close'].iloc[0] > level['max']:
                start_idx = 0

            # 2. Проматываем свечи от момента пробоя до текущей
            for i in range(start_idx, len(df) - 1):
                hc = df.iloc[i]
                htime = df.index[i] if hasattr(df, 'index') else None
                hvol_base = float(df['volume'].iloc[max(0, i-52):max(1, i-2)].mean()) if i >= 52 else 0.1
                
                watcher.update(
                    float(hc['open']), float(hc['high']), float(hc['low']), float(hc['close']), float(hc['volume']), 
                    hvol_base, c_atr, all_opposite_levels,
                    c_atr_slow=c_atr_slow, c_ema=c_ema, c_rsi=c_rsi, candle_time=htime
                )

        baseline_vol = float(df['volume'].iloc[-52:-2].mean())

        c = df.iloc[-1]
        c_open, c_high, c_low, c_close, c_vol = (
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume'])
        )

        candle_time = df.index[-1] if hasattr(df, 'index') else None

        signal = watcher.update(
            c_open, c_high, c_low, c_close, c_vol, baseline_vol, c_atr, all_opposite_levels,
            c_atr_slow=c_atr_slow, c_ema=c_ema, c_rsi=c_rsi, candle_time=candle_time
        )

        if not signal:
            return self._deny(f"No V-red-top signal (state: {watcher.state})")

        if 'error' in signal:
            return self._deny(signal['error'])

        return {
            'allow': True,
            'reason': signal.get('reason', 'V-RED-TOP pattern detected'),
            'entry_price': signal.get('entry_price', c_close),
            'sl': signal.get('sl', 0.0),
            'tp': signal.get('tp', 0.0),
            'level_id': level_id,
            'history_log': watcher.history_log,
            'action': signal.get('action', 'SELL'),
        }

    # ==================================================================
    # ПЕРСИСТЕНТНОСТЬ: сохранение/восстановление живых вотчеров между
    # рестартами бота. Раньше весь накопленный прогресс паттерна (пики,
    # C1/C2, счётчик сделок) жил только в оперативной памяти и терялся
    # при любом рестарте — теперь раз в скан сохраняется на диск, а при
    # старте бота читается обратно.
    # ==================================================================

    def to_state_dict(self):
        """Сериализует всех вотчеров реестра в JSON-совместимый словарь."""
        result = {}
        for level_id, watcher in self._watchers.items():
            state = dict(watcher.__dict__)
            # pandas.Timestamp не сериализуется json.dump — переводим в ISO-строку
            last_time = state.get('_last_time')
            if last_time is not None and hasattr(last_time, 'isoformat'):
                state['_last_time'] = last_time.isoformat()
            result[level_id] = {
                'class': type(watcher).__name__,
                'min': watcher.min,
                'max': watcher.max,
                'trade_type': watcher.trade_type,
                'coin': getattr(watcher, 'coin', 'UNKNOWN'),
                'state': state,
            }
        return result

    def save_state(self, path):
        """Атомарно сохраняет текущее состояние всех вотчеров в файл."""
        try:
            data = self.to_state_dict()
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception as e:
            print(f"[VBottomManager] ⚠️ Не удалось сохранить состояние вотчеров: {e}")

    def load_state(self, path):
        """Восстанавливает вотчеров из файла состояния при старте бота.
        Если файла нет (первый запуск) или он битый — просто начинаем
        с чистого реестра, ничего не падает."""
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[VBottomManager] ⚠️ Не удалось прочитать состояние вотчеров: {e}")
            return

        restored = 0
        for level_id, entry in data.items():
            cls = _WATCHER_CLASSES.get(entry.get("class"))
            if cls is None:
                continue
            try:
                watcher = cls(entry["min"], entry["max"], entry["trade_type"], coin=entry.get("coin", "UNKNOWN"))
                saved_state = dict(entry.get("state", {}))
                last_time = saved_state.get("_last_time")
                if last_time is not None:
                    try:
                        saved_state["_last_time"] = pd.Timestamp(last_time)
                    except Exception:
                        saved_state["_last_time"] = None
                watcher.__dict__.update(saved_state)
                self._watchers[level_id] = watcher
                restored += 1
            except Exception as e:
                print(f"[VBottomManager] ⚠️ Пропущен вотчер {level_id} при восстановлении: {e}")

        if restored:
            print(f"[VBottomManager] 🔄 Восстановлено вотчеров из сохранённого состояния: {restored}")