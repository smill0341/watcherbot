"""
dashboard_actions.py — действия, которые дашборд может напрямую вызвать
у сканера (сброс вотчеров, пересчёт уровней) + заглушка
Notifier.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ ФАЙЛ, А НЕ ЧАСТЬ run_web.py: run_web.py — это точка
входа процесса, запускается командой `python run_web.py`, поэтому Python
регистрирует его как модуль "__main__", а не как "run_web". Если бы
web/backend/app.py (импортируется как `web.backend.app`, отдельно, изнутри
уже запущенного процесса) сделал `from run_web import ...`, Python не нашёл
бы уже загруженный "__main__" под именем "run_web" — он заново нашёл бы
run_web.py на диске и импортировал его ВТОРОЙ РАЗ как отдельный, независимый
модуль. Это значило бы ВТОРОЙ, отдельный notifier (со своим __init__,
никогда не установленный из main()) и вообще вторую копию всего в этом
файле — классическая ловушка Python с "entry-point заодно импортируемый
как библиотека". modules.cryptano.live_scan (bounce_mgr/v_bottom_mgr) этой
ловушки не боится, потому что его ВСЕГДА импортируют по полному пути
(`modules.cryptano.live_scan`), никогда не запускают напрямую как скрипт —
этот файл сделан по тому же принципу: только импортируется, никогда не
запускается напрямую, поэтому что run_web.py (при старте), что app.py (из
эндпоинтов) видят ровно один и тот же модуль/notifier/функции.
"""

import os
import time
import datetime
import threading
from typing import Optional

from modules.cryptano.utils.storage import load_json, save_json_atomic
from modules.cryptano.utils.paths import NOTIFICATIONS_FILE, RESCAN_STATUS_FILE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
MAX_NOTIFICATIONS = 200  # сколько последних сообщений хранить в файле

ADMIN_LABEL = "web-dashboard"  # заменяет chat_id — реального Telegram-чата тут нет

# Заполняется в run_web.py::main() до старта дашборда. До этого момента
# (то есть до полного старта процесса) остаётся None — ни одна из функций
# этого файла не вызывается раньше, поэтому None тут не должно всплывать
# на практике.
notifier: Optional["Notifier"] = None


class Notifier:
    """
    Заглушка вместо telebot.TeleBot — единственное, что от неё требуется,
    это метод send_message(chat_id, text, parse_mode=...), потому что
    именно так (и только так) crypto_orchestrator в background_tasks.py
    зовёт bot внутри себя (4 места).

    Вместо отправки в Telegram: печатает в консоль и копит последние
    сообщения в notifications.json (для будущей ленты сигналов в дашборде).
    """

    def __init__(self, notifications_path, max_items=MAX_NOTIFICATIONS):
        self._path = notifications_path
        self._max_items = max_items
        self._lock = threading.Lock()

    def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        timestamp = datetime.datetime.now().isoformat()
        print(f"[{timestamp}] [NOTIFY -> {chat_id}] {text}")
        with self._lock:
            # Получаем данные. default=None чтобы не ругался на аргумент
            raw_items = load_json(self._path, default=None)

            # Явно указываем тип list и добавляем комментарий игнорирования типов
            # для линтера, чтобы он забыл о том, что load_json "обещал" вернуть Dict
            items: list = raw_items if isinstance(raw_items, list) else []  # type: ignore

            items.append({"timestamp": timestamp, "chat_id": chat_id, "text": text})

            # Теперь линтер точно знает, что items - это список, и разрешает срез
            items = items[-self._max_items:]  # type: ignore
            save_json_atomic(self._path, items)


def init_notifier() -> Notifier:
    """Создаёт и устанавливает глобальный notifier этого модуля — вызывается
    ровно один раз, из run_web.py::main(), до старта дашборда/скан-цикла.
    Возвращает созданный инстанс — run_web.py передаёт его же в потоки
    crypto_orchestrator/football/nba (тем функциям notifier нужен явным
    аргументом, это не меняем)."""
    global notifier
    notifier = Notifier(NOTIFICATIONS_FILE)
    return notifier


def rebuild_levels():
    """Полный пересчёт macro-уровней с нуля — вызывается либо из консоли
    (run_web.py::_console_listener), либо напрямую из app.py (POST
    /api/rebuild_levels), в отдельном потоке (может занять пару минут —
    фетчит историю по ~70 монетам, не должна блокировать HTTP-запрос)."""
    from modules.cryptano.swing_hunter import build_macro_levels

    print("[dashboard_actions] ⏳ Запускаю построение уровней вручную (может занять пару минут)...")
    try:
        build_macro_levels(notifier, ADMIN_LABEL)
    except Exception as e:
        print(f"[dashboard_actions] ❌ Ошибка при построении уровней: {e}")


def reset_all_watchers():
    """
    Полный сброс живых вотчеров в памяти — по запросу с веб-дашборда
    (POST /api/reset_watchers, см. app.py). НЕ трогает macro_levels.json/
    watchlist.json — только состояние отслеживания (кто что пробил, кто в
    фокусе, кулдауны), чтобы всё начало отслеживаться заново с чистого
    листа по уже существующим уровням."""
    from modules.cryptano.live_scan import (
        v_bottom_mgr, bounce_mgr, tracked_origin_levels,
        tracked_origin_levels_vrt, watcher_cooldown_cache, save_watcher_state,
        _watcher_lock,
    )
    from modules.cryptano.utils.paths import ACTIVE_WATCHERS_FILE

    print("[dashboard_actions] 🧹 Сброс всех вотчеров по запросу с дашборда...")
    try:
        with _watcher_lock:
            v_bottom_mgr._watchers.clear()
            # bounce_mgr._watchers — property (parent._watchers), .clear() мутирует
            # тот же словарь на месте, так и должно работать.
            bounce_mgr._watchers.clear()
            bounce_mgr.graveyard.clear()
            bounce_mgr._graveyard_recorded.clear()
            bounce_mgr.level_trades.clear()  # счётчик сделок по уровням — полный сброс = с чистого листа
            bounce_mgr.pierced_count = 0
            tracked_origin_levels.clear()
            tracked_origin_levels_vrt.clear()
            watcher_cooldown_cache.clear()
            save_watcher_state()  # сразу на диск, не дожидаясь обычного цикла сохранения
            # Файл active_watchers.json тоже обнуляем явно — на диске он нужен
            # для персистентности между рестартами, дашборд же с недавних пор
            # читает список "в работе" напрямую из памяти (см. app.py::
            # get_active_watchers), но файл должен оставаться верным сам по себе.
            save_json_atomic(ACTIVE_WATCHERS_FILE, {})
        print("[dashboard_actions] ✅ Сброс завершён — watcher_state.json/bounce_state.json/active_watchers.json обнулены.")
    except Exception as e:
        print(f"[dashboard_actions] ❌ Ошибка при сбросе вотчеров: {e}")


# rescan_coin() (старый рескан монеты, кнопка 🔄) УДАЛЁН. Он откатывал
# монете bounce_mgr.last_processed_time назад (по умолчанию на неделю) и
# гнал всю монету заново через check_bounce внутри боевого бота. Смотреть
# прошлое монеты — теперь только симулятор (кнопка 📈 на дашборде). Сам
# механизм догона пропущенных свечей после рестарта (last_processed_time в
# check_bounce) НЕ тронут — он автоматический и к этой кнопке не привязан.