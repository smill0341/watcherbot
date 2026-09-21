"""
dashboard_actions.py — действия, которые дашборд может напрямую вызвать
у сканера (сброс вотчеров, пересчёт уровней, рескан монеты) + заглушка
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
# на практике; там, где это важно (rescan_coin), проверка на None есть.
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


def rescan_coin(coin, since=None):
    """Честный повтор пути ОДНОЙ монеты (BOUNCE) от `since` до сейчас —
    убивает (архивирует) текущих живых вотчеров этой монеты и честно
    пересобирает реальный путь через уже готовый механизм реплея
    (bounce_mgr.last_processed_time), как будто бот сканировал её всё это
    время.

    Вызывается из app.py (POST /api/rescan/{coin}) в отдельном потоке —
    сама функция синхронная и блокирующая на время реплея."""
    from modules.cryptano.live_scan import v_bottom_mgr, bounce_mgr, _watcher_lock
    from modules.cryptano.watcher_plan import check_bounce
    from background_tasks import export_dashboard_state

    coin = coin.upper().strip()
    since_val = int(since) if since is not None else int(time.time()) - 7 * 24 * 3600
    since_label = datetime.datetime.utcfromtimestamp(since_val).strftime("%Y-%m-%d %H:%M") + " UTC"

    # "running" — сразу же, до того как реально начали: ожидание лока
    # (если сейчас идёт полный скан-цикл) не должно выглядеть как тишина
    # на дашборде.
    try:
        rescan_status = load_json(RESCAN_STATUS_FILE, default={})
        rescan_status[coin] = {
            "status": "running", "since": since_label,
            "started_at": datetime.datetime.now().isoformat(),
        }
        save_json_atomic(RESCAN_STATUS_FILE, rescan_status)
    except Exception as e:
        print(f"[dashboard_actions] ⚠️ Не удалось записать статус старта рескана {coin}: {e}")

    print(f"[dashboard_actions] 🔄 Рескан {coin}...")
    bc_count = 0
    with _watcher_lock:  # блокирующе — дождаться, если основной цикл сейчас держит лок
        try:
            v_bottom_mgr.remove_watchers_by_coin(coin)
            bc_coin_ids = [lid for lid, w in bounce_mgr._watchers.items() if getattr(w, "coin", None) == coin]
            for lid in bc_coin_ids:
                bounce_mgr._watchers[lid].reset_for_rescan()
            # clear_graveyard_for_ids (не clear_graveyard_by_coin!) — точечно
            # только по уровням, которые прямо сейчас реально сбрасываем
            # (bc_coin_ids выше). clear_graveyard_by_coin сносил защиту
            # кладбища для ВСЕЙ монеты разом, включая уровни, умершие раньше
            # и не участвующие в этом рескане — из-за этого следующий же
            # скан/рескан мог "воскресить" вчера честно убитый вотчер на том
            # же месте (см. докстринг clear_graveyard_for_ids в
            # bounce_manager.py — тот самый описанный там баг, найден при
            # слиянии дашборда и сканера и исправлен заодно).
            bounce_mgr.clear_graveyard_for_ids(bc_coin_ids)
            bounce_mgr.last_processed_time[coin] = since_val

            # Читаем СВЕЖИЙ конфиг на каждый рескан — направление могло
            # переключиться на дашборде между кликами (единая точка правды
            # с боевым сканом и симулятором, см. app.py::get_bounce_direction).
            # Отсутствие ключа — включено (True).
            _cfg = load_json(CONFIG_FILE, default={})
            _crypto_cfg = _cfg.get("crypto", {})
            bc_count, bc_reports, bc_levels = check_bounce(
                coin, _crypto_cfg.get("allow_long", True), _crypto_cfg.get("allow_short", True), bounce_mgr
            )
            for bc_report in bc_reports:
                if notifier is not None:
                    notifier.send_message(ADMIN_LABEL, bc_report, parse_mode="Markdown")

            # Без этого результат реплея (точки на графике, архив
            # сработавшей/умершей сделки, статус TRIGGERED) был бы не виден
            # на дашборде до следующего планового скан-цикла — реплей
            # отрабатывал честно, а дашборд просто не успевал об этом узнать.
            export_dashboard_state(v_bottom_mgr, bounce_mgr)
        except Exception as e:
            bc_count = 0
            print(f"[dashboard_actions] ❌ Ошибка рескана {coin}: {e}")

    try:
        status = load_json(RESCAN_STATUS_FILE, default={})
        status[coin] = {
            "status": "done", "since": since_label,
            "finished_at": datetime.datetime.now().isoformat(), "found": bc_count,
        }
        save_json_atomic(RESCAN_STATUS_FILE, status)
    except Exception as e:
        print(f"[dashboard_actions] ⚠️ Не удалось записать статус завершения рескана {coin}: {e}")