import json
import os
import tempfile
import threading


_locks_guard = threading.Lock()
_path_locks = {}


def _get_lock(path):
    abs_path = os.path.abspath(path)
    with _locks_guard:
        if abs_path not in _path_locks:
            _path_locks[abs_path] = threading.Lock()
        return _path_locks[abs_path]


def load_json(path, default=None):
    lock = _get_lock(path)
    with lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default


def save_json_atomic(path, data, indent=4, ensure_ascii=False):
    abs_path = os.path.abspath(path)
    directory = os.path.dirname(abs_path) or "."
    os.makedirs(directory, exist_ok=True)

    lock = _get_lock(abs_path)
    with lock:
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(abs_path)}.",
            suffix=".tmp",
            dir=directory,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, abs_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
