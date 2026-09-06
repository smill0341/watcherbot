"""
paths.py
========
Единая точка правды для расположения json-файлов бота. Раньше они лежали
прямо в modules/cryptano/, теперь — в modules/cryptano/jsonbank/. Все модули
берут пути ОТСЮДА, а не собирают их каждый по-своему через
os.path.dirname(__file__) — иначе при следующем переносе снова придётся
руками править десяток файлов (как случилось в этот раз).
"""
import os

CRYPTANO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../modules/cryptano
JSONBANK_DIR = os.path.join(CRYPTANO_DIR, "jsonbank")

WATCHLIST_FILE = os.path.join(JSONBANK_DIR, "watchlist.json")
MACRO_LEVELS_FILE = os.path.join(JSONBANK_DIR, "macro_levels.json")
WATCHER_STATE_FILE = os.path.join(JSONBANK_DIR, "watcher_state.json")
BOUNCE_STATE_FILE = os.path.join(JSONBANK_DIR, "bounce_state.json")
TRACKED_LONG_FILE = os.path.join(JSONBANK_DIR, "tracked_origin_levels.json")
TRACKED_VRT_FILE = os.path.join(JSONBANK_DIR, "tracked_origin_levels_vrt.json")
WATCHER_HISTORY_FILE = os.path.join(JSONBANK_DIR, "watcher_history.json")
ACTIVE_WATCHERS_FILE = os.path.join(JSONBANK_DIR, "active_watchers.json")
SIGNALS_FILE = os.path.join(JSONBANK_DIR, "signals.json")
NOTIFICATIONS_FILE = os.path.join(JSONBANK_DIR, "notifications.json")
