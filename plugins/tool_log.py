"""按群保存工具管理日志，滚动保留七天；不保存工具返回正文。

旧 data/tool_calls.jsonl 继续参与清理和 Dashboard 聚合，七天后自然淘汰。
此模块不依赖 NoneBot，生命周期清理由 group 插件调度。
"""

import heapq
import json
import os
from datetime import datetime
from pathlib import Path
from threading import Lock
from botcore.tools import tool_result

LOG_DIR = Path("data/tool_calls")
LEGACY_LOG_FILE = Path("data/tool_calls.jsonl")
RETENTION_DAYS = 7
CLEANUP_INTERVAL_SECONDS = 3600

_lock = Lock()
_last_cleanup: float | None = None


def _valid_group_id(value: str) -> bool:
    return value.isascii() and value.isdigit()


def _log_files() -> list[Path]:
    paths = [LEGACY_LOG_FILE, LOG_DIR / "admin.jsonl", LOG_DIR / "unscoped.jsonl"]
    paths.extend(path for path in (LOG_DIR / "groups").glob("*.jsonl")
                 if _valid_group_id(path.stem))
    return paths


def _timestamp(entry: object) -> float | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("ts"), str):
        return None
    try:
        # 兼容原先的本地时间，以及带时区的 ISO 时间。
        return datetime.fromisoformat(entry["ts"]).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _prune_file(path: Path, cutoff: float) -> None:
    if not path.exists():
        return
    temp = path.with_name(f".{path.name}.retention.tmp")
    kept = 0
    changed = False
    try:
        with path.open(encoding="utf-8", errors="replace") as source, temp.open("w", encoding="utf-8") as target:
            for line in source:
                try:
                    ts = _timestamp(json.loads(line))
                except json.JSONDecodeError:
                    ts = None
                if ts is None or ts < cutoff:
                    changed = True
                    continue
                target.write(line.rstrip("\n") + "\n")
                kept += 1
                changed |= not line.endswith("\n")
            target.flush()
            os.fsync(target.fileno())
        if not kept:
            path.unlink()
        elif changed:
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _cleanup_unlocked(now: float, *, force: bool = False) -> None:
    global _last_cleanup
    if (not force and _last_cleanup is not None
            and 0 <= now - _last_cleanup < CLEANUP_INTERVAL_SECONDS):
        return
    cutoff = now - RETENTION_DAYS * 86400
    for path in _log_files():
        try:
            _prune_file(path, cutoff)
        except OSError:
            continue  # 一个文件不可写不妨碍其他群清理，也不影响工具执行。
    _last_cleanup = now


def cleanup_tool_logs() -> None:
    """启动时及每小时调用；过期文件删除，部分过期文件原子重写。"""
    with _lock:
        _cleanup_unlocked(datetime.now().timestamp(), force=True)


def log_tool_call(
    source: str,
    tool_name: str,
    arguments: dict,
    result: str,
    *,
    user_id: str = "",
    group_id: str = "",
    context: dict | None = None,
) -> None:
    """记录名称、参数、结果长度及状态；上下文决定所属群或 Admin 私聊。"""
    chat_type = (context or {}).get("_chat_type", "")
    if chat_type == "group":
        group_id = str(context.get("_target_id", ""))
    elif chat_type == "private":
        group_id = ""
    group_id = str(group_id)
    user_id = str(user_id or (context or {}).get("_user_id", ""))
    if _valid_group_id(group_id):
        path = LOG_DIR / "groups" / f"{group_id}.jsonl"
    elif chat_type == "private" or (source == "chat" and chat_type != "group"):
        path = LOG_DIR / "admin.jsonl"
    else:
        path = LOG_DIR / "unscoped.jsonl"
    now = datetime.now().astimezone()
    entry = {
        "ts": now.isoformat(),
        "source": source,
        "tool": tool_name,
        "args": arguments,
        "result_len": len(result) if result else 0,
        "ok": tool_result(result or "").ok,
        "user_id": user_id,
        "group_id": group_id,
    }
    try:
        with _lock:
            _cleanup_unlocked(now.timestamp())
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 管理日志不可写不影响工具执行。


def load_recent_tool_calls(n: int = 10) -> list[dict]:
    """跨群和私聊按时间取最近 n 条，流式读取，内存只保留 n 条。"""
    if n <= 0:
        return []
    now = datetime.now().timestamp()
    cutoff = now - RETENTION_DAYS * 86400

    def entries():
        for path in _log_files():
            try:
                with path.open(encoding="utf-8", errors="replace") as file:
                    for line in file:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = _timestamp(entry)
                        if ts is not None and ts >= cutoff:
                            yield ts, entry
            except OSError:
                continue

    with _lock:
        _cleanup_unlocked(now)
        return [entry for _, entry in heapq.nlargest(n, entries(), key=lambda item: item[0])]
