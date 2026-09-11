"""
run_web.py — точка входа для крипто-сканера БЕЗ Telegram.

Крутит тот же crypto_orchestrator, что и main.py после нажатия
"Автобот Старт" в Telegram, но без telebot/токена — чтобы веб-дашборд
мог получать данные независимо от Telegram-процесса.

Управление статусом: автоматическое.
  - Запустил скрипт  -> crypto.status = RUNNING (сканер стартует сам)
  - Ctrl+C / штатный выход -> crypto.status = STOPPED

ВАЖНО: не запускать одновременно с main.py, если там включён крипто-автобот
("Автобот Старт" в Telegram) — оба процесса пишут в одни и те же JSON-файлы
(watchlist.json, active_watchers.json и т.д.) и будут создавать гонки.

main.py / football / NBA этот скрипт не трогает и не запускает — он только
про крипту.
"""

import os
import time
import atexit
import datetime
import threading

from modules.cryptano.utils.storage import load_json, save_json_atomic
from background_tasks import crypto_orchestrator
from modules.cryptano.swing_hunter import start_swing_hunter, build_macro_levels

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
from modules.cryptano.utils.paths import NOTIFICATIONS_FILE
MAX_NOTIFICATIONS = 200  # сколько последних сообщений хранить в файле

ADMIN_LABEL = "web-dashboard"  # заменяет chat_id — реального Telegram-чата тут нет


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
            items = items[-self._max_items:]# type: ignore
            save_json_atomic(self._path, items)


def _rebuild_levels(notifier):
    """Ручной запуск построения macro-уровней (аналог /rebuild_levels в Telegram)."""
    print("[run_web] ⏳ Запускаю построение уровней вручную (может занять пару минут)...")
    try:
        result = build_macro_levels(notifier, ADMIN_LABEL)
        print(f"[run_web] ✅ Построение уровней завершено: {result}")
    except Exception as e:
        print(f"[run_web] ❌ Ошибка при построении уровней: {e}")


def _reset_all_watchers():
    """
    Полный сброс живых вотчеров в памяти — по запросу с веб-дашборда.
    НЕ трогает macro_levels.json/watchlist.json — только состояние
    отслеживания (кто что пробил, кто в фокусе, кулдауны), чтобы всё
    начало отслеживаться заново с чистого листа по уже существующим
    уровням.
    """
    print("[run_web] 🧹 Сброс всех вотчеров по запросу с дашборда...")
    try:
        from modules.cryptano.live_scan import (
            v_bottom_mgr, bounce_mgr, tracked_origin_levels,
            tracked_origin_levels_vrt, watcher_cooldown_cache, save_watcher_state,
        )
        from modules.cryptano.utils.paths import ACTIVE_WATCHERS_FILE
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
        # active_watchers.json save_watcher_state() НЕ трогает вообще — этот файл
        # обновляется только внутри 15-минутного скан-цикла в background_tasks.py.
        # Без явной очистки тут дашборд ещё до 15 минут показывал бы старый список,
        # хотя в памяти уже пусто.
        save_json_atomic(ACTIVE_WATCHERS_FILE, {})
        print("[run_web] ✅ Сброс завершён — watcher_state.json/bounce_state.json/active_watchers.json обнулены.")
    except Exception as e:
        print(f"[run_web] ❌ Ошибка при сбросе вотчеров: {e}")


def _handle_rescan_flags(notifier):
    """
    Проверяет rescan_{coin}.flag файлы — вынесено из 15-минутного каскада
    (background_tasks.py) в тот же быстрый 1-секундный опрос, что уже есть
    у "Сбросить"/"Rebuild". Раньше рескан ждал, пока основной скан-цикл
    дойдёт до watcher-секции (это происходит не чаще раза в 15 минут, на
    границе закрытия свечи, + минимум 5 минут после старта бота) — то
    есть ожидание могло доходить до ~15+ минут, и всё это время статус на
    дашборде молчал (⏳ появлялся только на те несколько секунд, что реально
    шёл сам реплей).

    Мутации bounce_mgr идут под тем же _watcher_lock, что и основной
    скан-цикл в background_tasks.py — без этого два потока могли бы
    одновременно менять один и тот же словарь вотчеров.
    """
    from modules.cryptano.live_scan import v_bottom_mgr, bounce_mgr, _watcher_lock
    from modules.cryptano.utils.paths import RESCAN_STATUS_FILE
    from modules.cryptano.watcher_plan import check_bounce
    from background_tasks import export_dashboard_state

    cryptano_dir = os.path.join(BASE_DIR, "modules", "cryptano")
    try:
        flag_files = [f for f in os.listdir(cryptano_dir) if f.startswith("rescan_") and f.endswith(".flag")]
    except FileNotFoundError:
        return

    for fname in flag_files:
        coin = fname[len("rescan_"):-len(".flag")]
        flag_path = os.path.join(cryptano_dir, fname)
        flag_data = load_json(flag_path, default={})
        since = flag_data.get("since") if isinstance(flag_data, dict) else None
        try:
            os.remove(flag_path)
        except Exception:
            pass

        since_val = int(since) if since is not None else int(time.time()) - 7 * 24 * 3600
        since_label = datetime.datetime.utcfromtimestamp(since_val).strftime("%Y-%m-%d %H:%M") + " UTC"

        # "running" — сразу же, ДО того как поток реально стартовал (не
        # дожидаясь захвата лока/своей очереди) — раньше значок ⏳ появлялся
        # только на те секунды, что шёл сам реплей, а всё время ожидания
        # (могло быть 15+ минут) дашборд просто молчал, будто ничего не
        # происходит.
        try:
            rescan_status = load_json(RESCAN_STATUS_FILE, default={})
            rescan_status[coin] = {
                "status": "running", "since": since_label,
                "started_at": datetime.datetime.now().isoformat(),
            }
            save_json_atomic(RESCAN_STATUS_FILE, rescan_status)
        except Exception as e:
            print(f"[run_web] ⚠️ Не удалось записать статус старта рескана {coin}: {e}")

        def _run_rescan(coin=coin, since_val=since_val, since_label=since_label):
            print(f"[run_web] 🔄 Рескан {coin} — обрабатываю сразу, не жду 15-минутный скан-цикл...")
            _watcher_lock.acquire()  # блокирующе — дождаться, если основной цикл сейчас держит лок
            try:
                v_bottom_mgr.remove_watchers_by_coin(coin)
                bc_coin_watchers = [w for w in bounce_mgr._watchers.values() if getattr(w, "coin", None) == coin]
                for w in bc_coin_watchers:
                    w.reset_for_rescan()
                bounce_mgr.clear_graveyard_by_coin(coin)
                bounce_mgr.last_processed_time[coin] = since_val

                bc_count, bc_reports, bc_levels = check_bounce(coin, True, True, bounce_mgr)
                for bc_report in bc_reports:
                    notifier.send_message(ADMIN_LABEL, bc_report, parse_mode="Markdown")

                # Без этого результат реплея (точки на графике, архив
                # сработавшей/умершей сделки, статус TRIGGERED) был бы не
                # виден на дашборде до следующего планового 15-минутного
                # цикла — реплей отрабатывал честно, а дашборд просто не
                # успевал об этом узнать.
                export_dashboard_state(v_bottom_mgr, bounce_mgr)
            except Exception as e:
                bc_count = 0
                print(f"[run_web] ❌ Ошибка рескана {coin}: {e}")
            finally:
                _watcher_lock.release()

            try:
                status = load_json(RESCAN_STATUS_FILE, default={})
                status[coin] = {
                    "status": "done", "since": since_label,
                    "finished_at": datetime.datetime.now().isoformat(), "found": bc_count,
                }
                save_json_atomic(RESCAN_STATUS_FILE, status)
            except Exception as e:
                print(f"[run_web] ⚠️ Не удалось записать статус завершения рескана {coin}: {e}")

        threading.Thread(target=_run_rescan, daemon=True).start()


def _flag_listener(notifier):
    """
    Проверяет флаг-файлы от веб-дашборда КАЖДУЮ СЕКУНДУ — специально
    отдельно от 15-минутного каскада скана в background_tasks.py, чтобы
    команды с дашборда ("сбросить всё", "rebuild", рескан монеты)
    применялись почти мгновенно, а не ждали ближайшего цикла скана.
    """
    reset_flag_path = os.path.join(BASE_DIR, "modules", "cryptano", "reset_watchers.flag")
    rebuild_flag_path = os.path.join(BASE_DIR, "modules", "cryptano", "rebuild_levels.flag")
    while True:
        time.sleep(1)
        if os.path.exists(reset_flag_path):
            try:
                os.remove(reset_flag_path)
            except Exception:
                pass
            _reset_all_watchers()
        if os.path.exists(rebuild_flag_path):
            try:
                os.remove(rebuild_flag_path)
            except Exception:
                pass
            threading.Thread(target=_rebuild_levels, args=(notifier,), daemon=True).start()
        _handle_rescan_flags(notifier)


def _console_listener(notifier):
    """
    Слушает консоль в отдельном потоке. Команды:
      rebuild + Enter -> вручную запустить построение macro-уровней
    """
    print("[run_web] Команда доступна в этой консоли: введи 'rebuild' и нажми Enter,"
          " чтобы вручную пересчитать уровни (обычно это по расписанию в 03:05 и 15:05).")
    while True:
        try:
            cmd = input().strip().lower()
        except EOFError:
            break
        if cmd == "rebuild":
            threading.Thread(target=_rebuild_levels, args=(notifier,), daemon=True).start()
        elif cmd:
            print(f"[run_web] Неизвестная команда: '{cmd}'. Доступно: rebuild")


def _set_crypto_status(status: str):
    config = load_json(CONFIG_FILE, default={})
    if "crypto" not in config:
        config["crypto"] = {"status": status, "fasttrade": False}
    else:
        config["crypto"]["status"] = status
    save_json_atomic(CONFIG_FILE, config, indent=4)


def main():
    print("🪙 run_web.py — крипто-сканер без Telegram (для веб-дашборда)")
    print(f"   config:        {CONFIG_FILE}")
    print(f"   notifications: {NOTIFICATIONS_FILE}")

    notifier = Notifier(NOTIFICATIONS_FILE)

    # На штатный выход (Ctrl+C, sys.exit) подстрахуемся и погасим статус.
    # Не гарантия на 100%: принудительное закрытие окна крестиком/taskkill -F
    # это может не поймать — тогда status в config.json останется RUNNING,
    # и его придётся сбросить вручную перед следующим запуском main.py.
    atexit.register(_set_crypto_status, "STOPPED")

    _set_crypto_status("RUNNING")
    print("[run_web] crypto.status -> RUNNING")

    threading.Thread(
        target=crypto_orchestrator,
        args=(notifier, ADMIN_LABEL),
        daemon=True,
    ).start()

    # Футбол/NBA — тот же notifier вместо телеграм-бота (send_message просто
    # печатает в консоль + пишет в notifications.json, реального Telegram
    # тут нет и не нужен — нужны только данные, которые эти функции теперь
    # также сохраняют в football_signals.json/nba_signals.json для страницы
    # дашборда). Оба ждут status: RUNNING в своей секции config.json — как
    # и раньше, просто больше нет телеграм-команды, которая его включает,
    # выставляй вручную в config.json ("football": {"status": "RUNNING"}).
    try:
        from modules.footballnogoal.football import run_football_monitor
        threading.Thread(target=run_football_monitor, args=(notifier, ADMIN_LABEL), daemon=True).start()
    except Exception as e:
        print(f"[run_web] ⚠️ Футбол-монитор не запущен: {e}")

    try:
        from modules.playerpropsbasket.player_props import run_nba_monitor
        threading.Thread(target=run_nba_monitor, args=(notifier, ADMIN_LABEL), daemon=True).start()
    except Exception as e:
        print(f"[run_web] ⚠️ NBA-монитор не запущен: {e}")

    start_swing_hunter(notifier, ADMIN_LABEL)

    threading.Thread(target=_console_listener, args=(notifier,), daemon=True).start()
    threading.Thread(target=_flag_listener, args=(notifier,), daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[run_web] Останов по Ctrl+C...")


if __name__ == "__main__":
    main()