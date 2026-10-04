"""Dashboard actions for resetting watchers and rebuilding levels."""

import os

from backend.modules.cryptano.utils.paths import ACTIVE_WATCHERS_FILE
from backend.modules.cryptano.utils.storage import save_json_atomic
from backend.modules.cryptano.utils.notifier import notify

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def rebuild_levels():
    """Пересчитывает macro-уровни в отдельном потоке."""
    from backend.modules.cryptano.levels.swing_hunter import build_macro_levels

    print("[dashboard_actions] ⏳ Запускаю построение уровней вручную (может занять пару минут)...")
    try:
        build_macro_levels()
    except Exception as e:
        notify(f"[dashboard_actions] ❌ Ошибка при построении уровней: {e}", kind="error", source="dashboard")


def reset_all_watchers():
    """
    Полный сброс живых вотчеров в памяти — по запросу с веб-дашборда
    (POST /api/reset_watchers, см. app.py). НЕ трогает macro_levels.json/
    watchlist.json — только состояние отслеживания (кто что пробил, кто в
    фокусе, кулдауны), чтобы всё начало отслеживаться заново с чистого
    листа по уже существующим уровням.
    """
    from backend.modules.cryptano.backstage.live_scan import (
        v_bottom_mgr, bounce_mgr, tracked_origin_levels,
        tracked_origin_levels_vrt, watcher_cooldown_cache, save_watcher_state,
        _watcher_lock,
    )

    print("[dashboard_actions] 🧹 Сброс всех вотчеров по запросу с дашборда...")
    try:
        with _watcher_lock:
            v_bottom_mgr._watchers.clear()
            bounce_mgr._watchers.clear()
            bounce_mgr.graveyard.clear()
            bounce_mgr._graveyard_recorded.clear()
            bounce_mgr.level_trades.clear()
            bounce_mgr.pierced_count = 0
            tracked_origin_levels.clear()
            tracked_origin_levels_vrt.clear()
            watcher_cooldown_cache.clear()
            save_watcher_state()
            save_json_atomic(ACTIVE_WATCHERS_FILE, {})
        print("[dashboard_actions] ✅ Сброс завершён — watcher_state.json/bounce_state.json/active_watchers.json обнулены.")
    except Exception as e:
        notify(f"[dashboard_actions] ❌ Ошибка при сбросе вотчеров: {e}", kind="error", source="dashboard")
