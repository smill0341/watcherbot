"""
coin_settings.py
================
Настройки ПО МОНЕТАМ — отдельный файл jsonbank/coin_settings.json, не
config.json (там только общие настройки, влияющие на всё). Единственное
место, которое читает и пишет этот файл (app.py, level_filter.py,
bounce_watcher.py).

Формат:
  {"XAU": {"tp": 2.0, "sl": 10.0,
           "level_filter": {"short_min": null, "long_max": null,
                            "short_off": false, "long_off": false}}}

  tp/sl           — свой TP/SL монеты (оба сразу), приоритет над общим.
  level_filter    — свой фильтр уровней по EMA (см. levels/level_filter.py):
                    short_min / long_max — свои пороги (пусто = берётся общий);
                    short_off / long_off — "без фильтра" для этой стороны у
                    этой монеты (общий порог для неё не действует).
Пустая запись монеты удаляется.

Один раз при первом обращении (файла ещё нет) переносит старый
config.json -> crypto.coin_tp_sl сюда и удаляет его из config.json.
"""

import os
import threading

from backend.modules.cryptano.utils.storage import load_json, save_json_atomic
from backend.modules.cryptano.utils.paths import COIN_SETTINGS_FILE, CRYPTANO_DIR

_lock = threading.RLock()


def _config_file():
    # CRYPTANO_DIR = .../master_bot/backend/modules/cryptano -> два уровня вверх = backend (единый config.json)
    return os.path.join(os.path.dirname(os.path.dirname(CRYPTANO_DIR)), "config.json")


def _migrate_from_config():
    """Файла настроек монет ещё нет: переносим crypto.coin_tp_sl из config.json."""
    data = {}
    cfg_path = _config_file()
    cfg = load_json(cfg_path, default={})
    crypto = cfg.get("crypto") if isinstance(cfg, dict) else None
    old = (crypto or {}).get("coin_tp_sl") or {}
    for coin, v in old.items():
        if isinstance(v, dict) and (v.get("tp") is not None or v.get("sl") is not None):
            data[coin] = {"tp": v.get("tp"), "sl": v.get("sl")}
    save_json_atomic(COIN_SETTINGS_FILE, data)  # сначала новый файл, потом чистим старый
    if crypto is not None and "coin_tp_sl" in crypto:
        crypto.pop("coin_tp_sl", None)
        save_json_atomic(cfg_path, cfg)
    return data


def load_all():
    with _lock:
        if not os.path.exists(COIN_SETTINGS_FILE):
            return _migrate_from_config()
        data = load_json(COIN_SETTINGS_FILE, default={})
        return data if isinstance(data, dict) else {}


def get_coin_tpsl(coin):
    c = load_all().get(coin) or {}
    return {"tp": c.get("tp"), "sl": c.get("sl")}


def get_coin_level_filter(coin):
    lf = (load_all().get(coin) or {}).get("level_filter") or {}
    return {
        "short_min": lf.get("short_min"),
        "long_max": lf.get("long_max"),
        "short_off": bool(lf.get("short_off")),
        "long_off": bool(lf.get("long_off")),
    }


def _lf_empty(lf):
    return (lf.get("short_min") is None and lf.get("long_max") is None
            and not lf.get("short_off") and not lf.get("long_off"))


def _store(data, coin, entry):
    """Записывает entry монеты; пустое (нет tp/sl и нет фильтра) — удаляет запись."""
    has_tpsl = entry.get("tp") is not None or entry.get("sl") is not None
    lf = entry.get("level_filter")
    if lf is not None and _lf_empty(lf):
        entry.pop("level_filter", None)
        lf = None
    if not has_tpsl and lf is None:
        data.pop(coin, None)
    else:
        data[coin] = entry
    save_json_atomic(COIN_SETTINGS_FILE, data)


def set_coin_tpsl(coin, tp, sl):
    with _lock:
        data = load_all()
        entry = dict(data.get(coin) or {})
        entry["tp"], entry["sl"] = tp, sl
        _store(data, coin, entry)


def set_coin_level_filter(coin, short_min, long_max, short_off=False, long_off=False):
    with _lock:
        data = load_all()
        entry = dict(data.get(coin) or {})
        entry["level_filter"] = {
            "short_min": None if short_off else short_min,
            "long_max": None if long_off else long_max,
            "short_off": bool(short_off),
            "long_off": bool(long_off),
        }
        _store(data, coin, entry)