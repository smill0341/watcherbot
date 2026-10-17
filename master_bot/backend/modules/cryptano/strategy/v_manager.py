"""
v_manager.py
============
Менеджер ОДНОЙ V-стратегии (VB / VGB / VRT). Устроен как BounceManager: свои
вотчеры, свой отслеживаемый уровень на монету, своё время последней обработанной
свечи, свой файл состояния. Все правила слежения за уровнем (какой уровень
взять, когда снять, как кормить вотчер) — здесь, в step(): бой (check_v) и
симулятор (simulate_engine.py) вызывают один и тот же код.
Три стратегии различаются только строкой в _STRATEGIES.
"""
import os
import json
import inspect
import pandas as pd

from .v_bottom_watcher import VBottomWatcher
from .v_green_bottom_watcher import VGreenBottomWatcher
from .v_red_top_watcher import VRedTopWatcher

_STATE_VERSION = 3

# Буфер отмены (должен совпадать с симулятором): цена ушла выше уровня на столько % —
# слежение за ним снимается и вотчер сбрасывается.
VBOTTOM_BREATH_BUFFER_PCT = 3.0

# tag: (класс вотчера, направление, отмена выше буфера, несколько сигналов на уровень)
#  - отмена выше буфера: цена ушла от уровня -> сброс и снятие со слежения;
#  - несколько сигналов: уровень снимается, только когда вотчер завершился
#    (иначе после 1-го сигнала).
_STRATEGIES = {
    "VB": (VBottomWatcher, "LONG", True, False),
    "VGB": (VGreenBottomWatcher, "LONG", True, False),
    "VRT": (VRedTopWatcher, "SHORT", False, True),
}


def _find_fresh_breach(levels, c_close, c_low, prev_close):
    """Уровень, который цена только что пробила вниз. Сначала "свежий" пробой
    (пересекли границу на этой свече), иначе — любой уровень, под которым цена
    уже находится (гэп/пропуск свечи)."""
    for lvl in levels:
        if (c_close < lvl['min'] or c_low < lvl['min']) and prev_close >= lvl['min']:
            return lvl
    for lvl in levels:
        if c_close < lvl['min'] or c_low < lvl['min']:
            return lvl
    return None


def _find_fresh_breach_up(levels, c_close, c_high, prev_close):
    """Зеркало _find_fresh_breach для SHORT: пробой вверх через lvl['max']."""
    for lvl in levels:
        if (c_close > lvl['max'] or c_high > lvl['max']) and prev_close <= lvl['max']:
            return lvl
    for lvl in levels:
        if c_close > lvl['max'] or c_high > lvl['max']:
            return lvl
    return None


class VManager:
    def __init__(self, tag, log_dir_override=None):
        self.tag = tag                  # префикс level_id: VB / VGB / VRT
        self._cls, self.direction, self._cancel_above, self._multi_signal = _STRATEGIES[tag]
        self._log_dir = log_dir_override
        self._watchers = {}             # {level_id: вотчер}
        self.tracked = {}               # {coin: уровень, который ведём сейчас}
        self.last_processed_time = {}   # {coin: unix-секунды последней обработанной свечи}

    # ------------------------------------------------------------------
    # РЕЕСТР
    # ------------------------------------------------------------------

    def level_id(self, level):
        return f"{self.tag}_{self.direction}_{level['min']}_{level['max']}"

    def watcher_count(self):
        return len(self._watchers)

    def reset_all(self):
        self._watchers.clear()
        self.tracked.clear()

    def remove_watchers_by_coin(self, coin):
        for k in [k for k, w in self._watchers.items() if getattr(w, 'coin', None) == coin]:
            del self._watchers[k]

    def notify_breach(self, level):
        """Новый заход на уровень: если вотчер уже есть — он сбрасывает свою математику."""
        w = self._watchers.get(self.level_id(level))
        if w is not None:
            w.on_breach_start()

    def force_reset(self, level):
        """Жёсткий сброс вотчера (цена ушла за буфер отмены)."""
        w = self._watchers.get(self.level_id(level))
        reset = getattr(w, '_reset_chain', None) or getattr(w, '_reset', None)
        if reset is not None:
            reset()

    def clear_dead_watchers(self, active_level_ids):
        """Убирает вотчеры отработавших уровней, которых больше нет на графике.
        Возвращает {level_id: вотчер} — вызывающий успевает заархивировать их путь."""
        dead = [k for k, w in self._watchers.items()
                if k not in active_level_ids and getattr(w, 'state', None) in ("TRIGGERED", "DEAD", "IDLE")]
        return {k: self._watchers.pop(k) for k in dead}

    # ------------------------------------------------------------------
    # ОДНА СВЕЧА
    # ------------------------------------------------------------------

    def step(self, coin, levels, opposite, df, c_atr=None, replay=True, **ind):
        """Обработка одной свечи монеты (последняя в df). На монету ведём ОДИН
        уровень (self.tracked): пока ничего не ведём — берём свежий пробой из
        levels (уровни своей стороны, уже активные на эту свечу); ведём —
        кормим свечой именно его, пока не сработает сигнал, цена не уйдёт от
        него или вотчер не завершится. opposite — уровни другой стороны (для TP).
        Возвращает (level, result, cancelled): level — уровень, с которым
        работали (None — ничего не ведём), result — сигнал или None,
        cancelled — слежение снято, цена ушла от уровня."""
        c_close = float(df['close'].iloc[-1])
        prev_close = float(df['close'].iloc[-2])
        level = self.tracked.get(coin)
        if level is None:
            if self.direction == "LONG":
                found = _find_fresh_breach(levels, c_close, float(df['low'].iloc[-1]), prev_close)
            else:
                found = _find_fresh_breach_up(levels, c_close, float(df['high'].iloc[-1]), prev_close)
            if found is None:
                return None, None, False
            level = self.tracked[coin] = dict(found)
            self.notify_breach(level)
        elif self._cancel_above and c_close > level['max'] * (1 + VBOTTOM_BREATH_BUFFER_PCT / 100.0):
            self.force_reset(level)
            del self.tracked[coin]
            return level, None, True

        result = self.feed(coin, level, df, opposite, c_atr=c_atr, replay=replay, **ind)

        w = self._watchers.get(self.level_id(level))
        if self._multi_signal:
            if w is not None and w.state in ("TRIGGERED", "DEAD", "IDLE"):
                del self.tracked[coin]
        elif result:
            del self.tracked[coin]
        return level, result, False

    def feed(self, coin, level, df, opposite, c_atr=None, replay=True, **ind):
        """Кормит вотчер уровня ПОСЛЕДНЕЙ свечой df (df — история до неё включительно).
        ind — доп. индикаторы вотчера (VRT: c_atr_slow, c_ema, c_rsi).
        replay=True: у нового вотчера сперва восстанавливается путь от первого
        пробоя уровня (бой); False — симулятор сам ведёт путь свеча за свечой.
        Возвращает {entry_price, sl, tp, level_id, history_log} при входе, иначе None."""
        if len(df) < 52:
            return None
        level_id = self.level_id(level)
        candle_time = df.index[-1]

        w = self._watchers.get(level_id)
        if w is None:
            kw = dict(coin=coin, level_date=level.get('date'), level_type=level.get('type'),
                      level_score=level.get('score'))
            if self._log_dir:
                kw['log_dir_override'] = self._log_dir
            if 'candle_time' in inspect.signature(self._cls).parameters:
                kw['candle_time'] = candle_time  # время самой первой строки лога
            w = self._watchers[level_id] = self._cls(level['min'], level['max'], self.direction, **kw)
            if replay:
                self._replay(w, df, level, opposite, c_atr, ind)

        vols = df['volume'].iloc[-52:-2]
        c = df.iloc[-1]
        signal = w.update(
            float(c['open']), float(c['high']), float(c['low']), float(c['close']), float(c['volume']),
            float(vols.mean()), c_atr, opposite,
            trend='UNKNOWN', vol_std=float(vols.std()), candle_time=candle_time, **ind
        )
        if not signal or 'error' in signal:
            return None
        return {
            'entry_price': signal.get('entry_price', float(c['close'])),
            'sl': signal.get('sl', 0.0),
            'tp': signal.get('tp', 0.0),
            'level_id': level_id,
            'history_log': w.history_log,
        }

    def _replay(self, w, df, level, opposite, c_atr, ind):
        """Новый вотчер: проигрываем путь от первого пробоя уровня (после его
        рождения) до предыдущей свечи — состояние вотчера как будто он следил всё время."""
        n = len(df)
        if n < 3:
            return
        long_side = self.direction == "LONG"

        anchor = 0
        if level.get('date'):
            try:
                anchor = max(0, min(int(df.index.searchsorted(pd.Timestamp(level['date'], tz='UTC'))), n - 1))
            except Exception:
                anchor = 0

        closes = df['close']
        start = None
        for i in range(anchor + 1, n):
            if long_side:
                crossed = closes.iloc[i - 1] >= level['min'] and closes.iloc[i] < level['min']
            else:
                crossed = closes.iloc[i - 1] <= level['max'] and closes.iloc[i] > level['max']
            if crossed:
                start = i
                break
        if start is None:
            last = float(closes.iloc[-1])
            if not (last < level['min'] if long_side else last > level['max']):
                return  # уровень ещё не тронут — следить начнём с текущей свечи
            start = anchor  # пробит гэпом, явного перехода нет — берём дату рождения уровня

        for i in range(start, n - 1):
            hc = df.iloc[i]
            vol_base = (float(df['volume'].iloc[i - 52:i - 2].mean()) if i >= 52
                        else float(df['volume'].iloc[:max(1, i)].mean()) if i > 0 else float(hc['volume']))
            try:
                w.update(float(hc['open']), float(hc['high']), float(hc['low']), float(hc['close']),
                         float(hc['volume']), vol_base, c_atr, opposite,
                         trend='UNKNOWN', candle_time=df.index[i], **ind)
            except Exception:
                pass  # ошибки прошлого не должны ронять восстановление

    # ------------------------------------------------------------------
    # СОСТОЯНИЕ НА ДИСКЕ
    # ------------------------------------------------------------------

    def _watchers_to_dict(self):
        result = {}
        for level_id, w in self._watchers.items():
            state = dict(w.__dict__)
            state.pop('debug_ring', None)  # только для лога в памяти
            for f in ('_last_time', 'last_event_time'):
                if getattr(state.get(f), 'isoformat', None):
                    state[f] = state[f].isoformat()
            result[level_id] = {'min': w.min, 'max': w.max, 'coin': getattr(w, 'coin', None), 'state': state}
        return result

    def save_state(self, path):
        try:
            data = {"version": _STATE_VERSION, "watchers": self._watchers_to_dict(),
                    "tracked": self.tracked, "last_processed_time": self.last_processed_time}
            with open(f"{path}.tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(f"{path}.tmp", path)
        except Exception as e:
            print(f"[VManager {self.tag}] ⚠️ Не удалось сохранить состояние: {e}")

    def load_state(self, path, legacy_path=None):
        """Читает свой файл. Нет его (первый запуск после перехода на отдельные
        файлы) — берёт свои вотчеры из старого общего файла legacy_path."""
        source = path if os.path.exists(path) else legacy_path
        if not source or not os.path.exists(source):
            return
        try:
            with open(source, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[VManager {self.tag}] ⚠️ Не удалось прочитать состояние: {e}")
            return

        if source == path:
            self.tracked.update(data.get("tracked") or {})
            self.last_processed_time.update(data.get("last_processed_time") or {})
        watchers = data.get("watchers", data) if "version" in data else data

        restored = 0
        for level_id, entry in watchers.items():
            if not level_id.startswith(f"{self.tag}_") or not entry.get("coin") or entry["coin"] == "UNKNOWN":
                continue  # чужой вотчер (из общего файла) или зомби без монеты
            try:
                w = self._cls(entry["min"], entry["max"], self.direction, coin=entry["coin"])
                state = dict(entry.get("state", {}))
                if state.get("_last_time") is not None:
                    state["_last_time"] = pd.Timestamp(state["_last_time"])
                w.__dict__.update(state)
                self._watchers[level_id] = w
                restored += 1
            except Exception as e:
                print(f"[VManager {self.tag}] ⚠️ Пропущен вотчер {level_id}: {e}")
        if restored:
            print(f"[VManager {self.tag}] Восстановлено вотчеров: {restored}")