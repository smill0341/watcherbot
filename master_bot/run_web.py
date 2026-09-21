"""
run_web.py — единая точка входа: крипто-сканер + веб-дашборд, В ОДНОМ
процессе (без Telegram).

ДО этой правки дашборд (web/backend/app.py) запускался ОТДЕЛЬНЫМ процессом
(`uvicorn web.backend.app:app`), и у каждого процесса при импорте
modules.cryptano.live_scan.py заводился СВОЙ, независимый bounce_mgr/
v_bottom_mgr в памяти — оба процесса общались между собой только через
JSON-файлы на диске. Отсюда была целая серия багов: удалённый с дашборда
вотчер возвращался обратно через время, "Очистка BOUNCE: удалено 0" при
явно неверном счётчике, список "в работе" расходился с тем, что реально
видел сканер, и так далее — дашборд правил СВОЮ, замороженную с момента
своего старта копию состояния, а следующее же сохранение реального
сканера тихо затирало эту правку обратно.

Теперь дашборд поднимается ПРЯМО ТУТ, в конце main() (см. uvicorn.run
внизу) — один-единственный bounce_mgr/v_bottom_mgr на весь бот, дашборд и
сканер работают с одной и той же памятью, гонка между "двумя правдами"
исключена конструктивно, а не патчем поверх патча. Флаг-файлы
(rescan_{coin}.flag, reset_watchers.flag, rebuild_levels.flag), которыми
раньше дашборд "стучался" во второй процесс — тоже не нужны: web/backend/
app.py импортирует функции из dashboard_actions.py и зовёт их напрямую,
под тем же _watcher_lock, что и сам скан-цикл (background_tasks.py).

Сами эти функции (reset_all_watchers/rebuild_levels/rescan_coin) и
Notifier-заглушка вынесены в ОТДЕЛЬНЫЙ модуль dashboard_actions.py, а не
живут прямо здесь — см. подробное объяснение в его докстринге: этот файл
запускается как `python run_web.py`, из-за чего Python регистрирует его
как "__main__", а не как "run_web", и `from run_web import ...` из app.py
импортировал бы ВТОРУЮ, независимую копию этого модуля (со своим никогда
не инициализированным notifier) — классическая ловушка Python. dashboard_
actions.py же только импортируется, никогда не запускается напрямую,
поэтому что этот файл, что app.py видят ровно один и тот же его экземпляр.

Управление статусом: автоматическое.
  - Запустил скрипт  -> crypto.status = RUNNING (сканер стартует сам)
  - Ctrl+C / штатный выход -> crypto.status = STOPPED

ВАЖНО: не запускать одновременно с main.py, если там включён крипто-автобот
("Автобот Старт" в Telegram) — оба процесса писали бы в одни и те же
JSON-файлы (watchlist.json, active_watchers.json и т.д.) и создавали бы
гонки. main.py / football / NBA этот скрипт не трогает и не запускает —
он только про крипту (+ дашборд).

На практике main.py (Telegram-бот) сейчас вообще не используется — сайт
единственный интерфейс, поэтому PID-guard ниже защищает в первую очередь
от случайного повторного запуска ЭТОГО ЖЕ скрипта (например, из второго
терминала), а не от main.py — но раз оба пишут в одни файлы, предупреждение
в шапке оставлено как есть, на будущее.
"""

import os
import time
import atexit
import threading

from modules.cryptano.utils.storage import load_json, save_json_atomic
from background_tasks import crypto_orchestrator
from modules.cryptano.swing_hunter import start_swing_hunter
import dashboard_actions
from dashboard_actions import NOTIFICATIONS_FILE, CONFIG_FILE, ADMIN_LABEL

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# PID-файл — не даёт запустить второй экземпляр run_web.py поверх уже
# работающего (оба тогда независимо трогали бы одни и те же JSON-файлы
# и объект bounce_mgr/v_bottom_mgr был бы снова не один, а два — та же
# болезнь, от которой уходит всё слияние выше, просто с другой стороны).
PID_FILE = os.path.join(BASE_DIR, "run_web.pid")

# Порт/хост дашборда — раньше жили только в команде запуска
# ("python -m uvicorn web.backend.app:app --port 8010", см. run_all.bat),
# теперь эта команда не используется вообще (см. main() ниже), поэтому
# параметры переехали сюда. Хост по умолчанию — 127.0.0.1, как и раньше
# (старая команда запускалась без --host, а это и есть дефолт uvicorn) —
# то есть дашборд виден только с этого же компьютера, не по сети. Если
# нужен доступ с других устройств в локальной сети — задай переменную
# окружения DASHBOARD_HOST=0.0.0.0 (или свой IP), не трогая код.
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8010"))


def _console_listener():
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
            threading.Thread(target=dashboard_actions.rebuild_levels, daemon=True).start()
        elif cmd:
            print(f"[run_web] Неизвестная команда: '{cmd}'. Доступно: rebuild")


def _set_crypto_status(status: str):
    config = load_json(CONFIG_FILE, default={})
    if "crypto" not in config:
        config["crypto"] = {"status": status, "fasttrade": False}
    else:
        config["crypto"]["status"] = status
    save_json_atomic(CONFIG_FILE, config, indent=4)


def _release_single_instance_lock():
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def _acquire_single_instance_lock():
    """Не даёт запустить run_web.py дважды одновременно — оба экземпляра
    независимо импортировали бы modules.cryptano.live_scan и завели бы СВОЙ
    bounce_mgr/v_bottom_mgr, то есть ту же болезнь двух несинхронных копий
    состояния, от которой уходит всё слияние в этом файле, просто вместо
    "сканер+дашборд" получили бы "сканер+сканер".

    Это ПРОСТАЯ, не железобетонная защита: проверяется только сам факт
    существования PID-файла, без проверки "а жив ли ещё тот процесс" (кросс-
    платформенная проверка через os.kill(pid, 0) на Windows ведёт себя не
    так, как на Linux/macOS, и рисковать неверно завершить чужой процесс не
    стоит). Если предыдущий запуск упал аварийно (kill -9, обрыв питания) и
    не подчистил за собой файл — следующий запуск честно откажется
    стартовать и подскажет, что делать."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r", encoding="utf-8") as f:
                old_pid = f.read().strip()
        except Exception:
            old_pid = "?"
        print(f"[run_web] ❌ Похоже, run_web.py уже запущен (PID-файл {PID_FILE}, PID {old_pid}).")
        print("           Если это не так (предыдущий запуск упал и не подчистил за собой) —")
        print(f"           удали файл {PID_FILE} и запусти снова.")
        raise SystemExit(1)

    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_single_instance_lock)


def main():
    print("🪙 run_web.py — крипто-сканер + веб-дашборд (единый процесс)")
    print(f"   config:        {CONFIG_FILE}")
    print(f"   notifications: {NOTIFICATIONS_FILE}")

    _acquire_single_instance_lock()

    notifier = dashboard_actions.init_notifier()

    # На штатный выход (Ctrl+C, sys.exit) подстрахуемся и погасим статус.
    # Не гарантия на 100%: принудительное закрытие окна крестиком/taskkill -F
    # это может не поймать — тогда status в config.json останется RUNNING,
    # и его придётся сбросить вручную перед следующим запуском.
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

    threading.Thread(target=_console_listener, daemon=True).start()

    # --- Дашборд — ПРЯМО ТУТ, в этом же процессе (см. докстринг файла) ---
    # Раньше здесь стояли threading.Thread(target=_flag_listener, ...) +
    # while True: time.sleep(1) — этот скрипт был просто "не Telegram"-
    # заглушкой вокруг background_tasks, а веб-дашборд (web/backend/app.py)
    # запускался ОТДЕЛЬНОЙ командой (uvicorn web.backend.app:app), отдельным
    # процессом со своим собственным bounce_mgr в памяти.
    import uvicorn
    from web.backend.app import app as fastapi_app

    print(f"[run_web] 🌐 Дашборд поднимается на http://{DASHBOARD_HOST}:{DASHBOARD_PORT} (в этом же процессе)")
    print("           (адрес/порт можно переопределить переменными окружения DASHBOARD_HOST/DASHBOARD_PORT)")

    # uvicorn.run() с ГОТОВЫМ объектом приложения (а не строкой "module:app")
    # — это не просто удобство записи: так uvicorn физически не может включить
    # --reload или несколько workers. Оба варианта тихо возвращают ровно ту же
    # болезнь, от которой уходит всё слияние — reload перезапускает импорт
    # модулей во ВТОРОМ подпроцессе, workers>1 поднимает несколько отдельных
    # процессов — в обоих случаях снова несколько независимых bounce_mgr.
    # Передавая готовый инстанс, а не строку, это исключается на уровне API,
    # а не только "не забыть флаг --reload".
    uvicorn.run(fastapi_app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, log_level="info")

    print("\n[run_web] Дашборд/процесс остановлен.")


if __name__ == "__main__":
    main()