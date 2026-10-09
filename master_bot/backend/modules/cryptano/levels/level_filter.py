"""
level_filter.py
===============
Фильтр уровней по расстоянию от EMA200 (4h) — ОТДЕЛЬНЫЙ слой между базой
уровней и стратегией. Стратегия (BounceManager) ничего про EMA не решает:
она получает уже отфильтрованные зоны. Одна и та же функция используется
боем (watcher_plan.py::check_bounce), симулятором (bounce_simulate.py) и
графиком (app.py::/api/levels/{coin}) — поэтому результаты совпадают.

Расстояние зоны от EMA — от БЛИЖАЙШЕГО к EMA края зоны, в % от EMA,
со знаком (плюс = зона выше EMA). Если EMA внутри зоны — 0.

Правило:
  SHORT (resistance): зона проходит, если расстояние >= short_min.
  LONG  (support):    зона проходит, если расстояние <= long_max.
  Порог None (пусто) = фильтра по этой стороне нет.
  Нет данных для EMA -> зона проходит (не блокируем молча).
  Ручные зоны (type == "MANUAL") не фильтруются никогда.

Пороги:
  общие  — config.json -> "crypto" -> "level_filter" = {"short_min": 10.0, "long_max": 20.0}
  у монеты — jsonbank/coin_settings.json (см. utils/coin_settings.py):
             свой порог и/или «без фильтра» (short_off / long_off) по каждой стороне.
Приоритет по каждой стороне отдельно: у монеты «без фильтра» -> свой порог -> общий.
"""

import os
import pandas as pd

from backend.modules.cryptano.utils.storage import load_json
from backend.modules.cryptano.utils.paths import CRYPTANO_DIR

_FOUR_H = pd.Timedelta("4h")
_CANDLE = pd.Timedelta(minutes=15)


def _config_file():
    # CRYPTANO_DIR = .../master_bot/backend/modules/cryptano -> два уровня вверх = backend,
    # там лежит ЕДИНЫЙ config.json бота (его же читают background_tasks, swing_hunter, bybit).
    backend_dir = os.path.dirname(os.path.dirname(CRYPTANO_DIR))
    return os.path.join(backend_dir, "config.json")


def _num(v):
    """Число или None (пусто/мусор -> None = порога нет)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_thresholds(coin, cfg=None, coin_cfg=None):
    """(short_min, long_max) для монеты; None = для этой стороны фильтра нет.
    cfg — раздел "crypto" config.json (общие пороги, ключ level_filter);
    coin_cfg — coin_settings.get_coin_level_filter(coin) (свои пороги/«без фильтра»).
    Не переданы — читаем сами. Приоритет по каждой стороне отдельно:
    у монеты «без фильтра» -> свой порог монеты -> общий порог."""
    if cfg is None:
        try:
            cfg = load_json(_config_file(), default={}).get("crypto", {}) or {}
        except Exception:
            cfg = {}
    if coin_cfg is None:
        try:
            from backend.modules.cryptano.utils.coin_settings import get_coin_level_filter
            coin_cfg = get_coin_level_filter(coin)
        except Exception:
            coin_cfg = {}
    general = cfg.get("level_filter") or {}

    def _side(off, own, gen):
        if off:
            return None
        own = _num(own)
        return own if own is not None else _num(gen)

    short_min = _side(coin_cfg.get("short_off"), coin_cfg.get("short_min"), general.get("short_min"))
    long_max = _side(coin_cfg.get("long_off"), coin_cfg.get("long_max"), general.get("long_max"))
    return short_min, long_max


def zone_ema_dist(ema_dist_fn, ts, zone):
    """Расстояние ближайшего к EMA края зоны от EMA200(4h), %. None — нет данных.
    ema_dist_fn — make_ema_dist_fn(...) из simulator/outcome.py; ts — время
    СВЕЧИ (открытие 15m), как в evaluate_bounce."""
    if ema_dist_fn is None:
        return None
    try:
        lo = ema_dist_fn(ts, zone["min"])
        hi = ema_dist_fn(ts, zone["max"])
    except Exception:
        return None
    if lo is None or hi is None:
        return None
    if lo <= 0 <= hi:
        return 0.0
    return lo if lo > 0 else hi


def _passes(zone, is_support, dist, short_min, long_max):
    if zone.get("type") == "MANUAL":
        return True
    if dist is None:
        return True
    if is_support:
        return long_max is None or dist <= long_max
    return short_min is None or dist >= short_min


class LevelFilter:
    """Фильтр одной монеты. apply(ts, supports, resistances) -> (supports, resistances)
    — те же словари зон, только прошедшие порог. EMA меняется раз в 4 часа,
    поэтому результат кэшируется на 4h-бакет, пока те же списки уровней —
    симулятор не считает EMA заново на каждой 15m-свече."""

    def __init__(self, coin, ema_dist_fn, thresholds=None):
        self.coin = coin
        self.ema_dist_fn = ema_dist_fn
        self.short_min, self.long_max = thresholds if thresholds is not None else load_thresholds(coin)
        self._cache = None  # (bucket, supports_ref, resistances_ref, result)

    @property
    def active(self):
        return self.short_min is not None or self.long_max is not None

    def _bucket(self, ts):
        end = pd.Timestamp(ts) + _CANDLE
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        return int(end.timestamp() // _FOUR_H.total_seconds())

    def apply(self, ts, supports, resistances):
        if not self.active or self.ema_dist_fn is None:
            return supports, resistances
        bucket = self._bucket(ts)
        c = self._cache
        if c is not None and c[0] == bucket and c[1] is supports and c[2] is resistances:
            return c[3]
        sup = [z for z in supports
               if _passes(z, True, zone_ema_dist(self.ema_dist_fn, ts, z), self.short_min, self.long_max)]
        res = [z for z in resistances
               if _passes(z, False, zone_ema_dist(self.ema_dist_fn, ts, z), self.short_min, self.long_max)]
        result = (sup, res)
        self._cache = (bucket, supports, resistances, result)
        return result