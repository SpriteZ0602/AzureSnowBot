"""并发安全的 JSONL 对话历史读写工具"""

import json
import os
import re
from pathlib import Path
from threading import Lock


_LOCKS_GUARD = Lock()
_HISTORY_LOCKS: dict[Path, Lock] = {}


def _history_lock(path: Path) -> Lock:
    """返回进程内按历史文件路径隔离的写锁。"""
    key = path.resolve()
    with _LOCKS_GUARD:
        lock = _HISTORY_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            _HISTORY_LOCKS[key] = lock
        return lock


def _read_unlocked(path: Path) -> list[dict]:
    if not path.exists():
        return []

    messages: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            messages.append(message)
    return messages


def load_jsonl_history(path: Path) -> list[dict]:
    """读取历史；与写入互斥，避免读到原子替换前的中间状态。"""
    with _history_lock(path):
        return _read_unlocked(path)


def append_jsonl_message(path: Path, message: dict) -> None:
    """并发安全地追加一条历史消息。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _history_lock(path):
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(message, ensure_ascii=False) + "\n")


def clear_jsonl_history(path: Path) -> None:
    """并发安全地清除历史文件。"""
    with _history_lock(path):
        if path.exists():
            path.unlink()


def rewrite_compacted_history(
    path: Path,
    snapshot: list[dict],
    compacted: list[dict],
) -> list[dict] | None:
    """原子写入压缩结果，同时保留压缩期间追加的消息。

    ``snapshot`` 是 compact 开始时读取的历史。如果最终写入前文件不再以
    该快照开头，说明历史被 reset、被另一轮 compact 重写或发生了其他修改；
    此时返回 ``None``，绝不拿旧快照覆盖新状态。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _history_lock(path):
        current = _read_unlocked(path)
        if current[: len(snapshot)] != snapshot:
            return None

        final_messages = compacted + current[len(snapshot):]
        temp_path = path.with_name(f".{path.name}.compact.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                for message in final_messages:
                    file.write(json.dumps(message, ensure_ascii=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

        return final_messages


def replace_history_content(path: Path, replacements: dict[str, str]) -> bool:
    """在最新历史上原子替换图片占位，保留并发追加；不创建已 reset 的文件。"""
    if not replacements:
        return False
    pattern = re.compile("|".join(re.escape(key) for key in replacements))
    with _history_lock(path):
        if not path.exists():
            return False
        messages = _read_unlocked(path)
        changed = False
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                updated = pattern.sub(lambda match: replacements[match.group(0)], content)
                changed |= updated != content
                message["content"] = updated
        if not changed:
            return False
        temp_path = path.with_name(f".{path.name}.images.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                for message in messages:
                    file.write(json.dumps(message, ensure_ascii=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return True
