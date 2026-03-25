"""
群聊全量记录
──────────
旁路记录白名单群内的所有消息（不仅限 @Bot），
供 local__get_group_chat_log 工具按需检索。

存储位置: data/sessions/groups/<gid>/_chatlog.jsonl
每行格式: {"ts": 1711000000, "uid": "123", "name": "昵称", "text": "消息内容"}
"""

import json
import time
from pathlib import Path

from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.log import logger

from .utils import in_whitelist

CHATLOG_DIR = Path("data/sessions/groups")
CHATLOG_DIR.mkdir(parents=True, exist_ok=True)

# 保留天数（超过自动清理，防止文件无限增长）
RETENTION_DAYS = 7

# ──────────────────── 路径工具 ────────────────────

def _chatlog_path(group_id: str) -> Path:
    d = CHATLOG_DIR / group_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "_chatlog.jsonl"


# ──────────────────── 写入 ────────────────────

def append_chatlog(group_id: str, user_id: str, nickname: str, text: str) -> None:
    """追加一条群聊记录"""
    entry = {
        "ts": int(time.time()),
        "uid": user_id,
        "name": nickname,
        "text": text,
    }
    path = _chatlog_path(group_id)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ──────────────────── 读取 ────────────────────

def load_chatlog(
    group_id: str,
    *,
    hours: float = 24,
    user_name: str | None = None,
    keyword: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    按条件加载群聊记录。

    参数:
        group_id:  群号
        hours:     只返回最近 N 小时的记录（默认24）
        user_name: 按发送者昵称模糊过滤（可选）
        keyword:   按消息内容关键词过滤（可选）
        limit:     最多返回条数（默认200）
    """
    path = _chatlog_path(group_id)
    if not path.exists():
        return []

    cutoff = time.time() - hours * 3600
    results: list[dict] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # 时间过滤
        if entry.get("ts", 0) < cutoff:
            continue

        # 发送者过滤（模糊匹配）
        if user_name and user_name.lower() not in entry.get("name", "").lower():
            continue

        # 关键词过滤
        if keyword and keyword.lower() not in entry.get("text", "").lower():
            continue

        results.append(entry)

    # 取最新的 limit 条
    return results[-limit:]


# ──────────────────── 清理过期记录 ────────────────────

def purge_old_entries(group_id: str) -> int:
    """删除超过 RETENTION_DAYS 天的旧记录，返回清理条数"""
    path = _chatlog_path(group_id)
    if not path.exists():
        return 0

    cutoff = time.time() - RETENTION_DAYS * 86400
    lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("ts", 0) < cutoff:
                removed += 1
                continue
        except json.JSONDecodeError:
            removed += 1
            continue
        kept.append(line)

    if removed:
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    return removed


# ──────────────────── NoneBot 旁路记录器 ────────────────────
# priority 较高（数字大=优先级低），block=False 保证不影响其他 handler
_chatlog_recorder = on_message(priority=1, block=False)


@_chatlog_recorder.handle()
async def _record_group_message(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return

    text = event.get_plaintext().strip()
    if not text:
        return

    group_id = str(event.group_id)
    user_id = str(event.user_id)
    nickname = event.sender.nickname or user_id

    append_chatlog(group_id, user_id, nickname, text)
