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
# database/ — накопительные базы (candles.db, levels_timeline_*.json), в
# отличие от jsonbank/ (текущее состояние "как есть прямо сейчас").
DATABASE_DIR = os.path.join(CRYPTANO_DIR, "database")

WATCHLIST_FILE = os.path.join(JSONBANK_DIR, "watchlist.json")
MACRO_LEVELS_FILE = os.path.join(JSONBANK_DIR, "macro_levels.json")
WATCHER_STATE_FILE = os.path.join(JSONBANK_DIR, "watcher_state.json")
RESCAN_STATUS_FILE = os.path.join(JSONBANK_DIR, "rescan_status.json")
BOUNCE_STATE_FILE = os.path.join(JSONBANK_DIR, "bounce_state.json")
TRACKED_LONG_FILE = os.path.join(JSONBANK_DIR, "tracked_origin_levels.json")
TRACKED_VRT_FILE = os.path.join(JSONBANK_DIR, "tracked_origin_levels_vrt.json")
WATCHER_HISTORY_FILE = os.path.join(JSONBANK_DIR, "watcher_history.json")
ACTIVE_WATCHERS_FILE = os.path.join(JSONBANK_DIR, "active_watchers.json")
SIGNALS_FILE = os.path.join(JSONBANK_DIR, "signals.json")
FOOTBALL_SIGNALS_FILE = os.path.join(JSONBANK_DIR, "football_signals.json")
FOOTBALL_STATUS_FILE = os.path.join(JSONBANK_DIR, "football_status.json")
NBA_SIGNALS_FILE = os.path.join(JSONBANK_DIR, "nba_signals.json")
NBA_STATUS_FILE = os.path.join(JSONBANK_DIR, "nba_status.json")
NOTIFICATIONS_FILE = os.path.join(JSONBANK_DIR, "notifications.json")