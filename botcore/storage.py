"""进程内串行、原子替换的文件更新。"""

import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from contextlib import contextmanager

_guard = RLock()
_locks: dict[Path, RLock] = {}


@contextmanager
def file_lock(path: Path):
    with _guard:
        lock = _locks.setdefault(path.resolve(), RLock())
    with lock:
        yield


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def atomic_write_json(path: Path, value) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def load_json(path: Path, default=None):
    with file_lock(path):
        if not path.exists():
            return {} if default is None else default
        return json.loads(path.read_text(encoding="utf-8"))


def update_json(path: Path, changes: dict) -> dict:
    with file_lock(path):
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValueError(f"配置必须是 JSON 对象：{path.name}")
        value.update(changes)
        atomic_write_json(path, value)
        return value
