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

    start_swing_hunter(notifier, ADMIN_LABEL)

    threading.Thread(target=_console_listener, args=(notifier,), daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[run_web] Останов по Ctrl+C...")


if __name__ == "__main__":
    main()