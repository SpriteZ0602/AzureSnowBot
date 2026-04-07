"""总览路由"""

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends

from ..auth import get_current_user

router = APIRouter()

_START_TIME = time.time()


@router.get("")
async def get_overview(_user: str = Depends(get_current_user)):
    """聚合返回 Bot 运行状态概览"""
    from ...token_stats import get_today_stats

    # 今日 Token 统计
    today_stats = get_today_stats()
    total_tokens = sum(s.get("total", 0) for s in today_stats.values())
    total_calls = sum(s.get("calls", 0) for s in today_stats.values())

    # 活跃群聊
    groups_dir = Path("data/sessions/groups")
    active_groups = []
    if groups_dir.is_dir():
        for gdir in groups_dir.iterdir():
            if gdir.is_dir():
                cfg_path = gdir / "config.json"
                last_at = ""
                if cfg_path.exists():
                    try:
                        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                        last_at = cfg.get("last_message_at", "")
                    except (json.JSONDecodeError, OSError):
                        pass
                active_groups.append({
                    "group_id": gdir.name,
                    "last_message_at": last_at,
                })

    # 待执行提醒
    reminders_file = Path("data/reminders.json")
    reminder_count = 0
    if reminders_file.exists():
        try:
            data = json.loads(reminders_file.read_text(encoding="utf-8"))
            reminder_count = len(data)
        except (json.JSONDecodeError, OSError):
            pass

    # 最近工具调用
    recent_tools = _load_recent_tool_calls(10)

    # Uptime
    uptime_seconds = int(time.time() - _START_TIME)

    return {
        "uptime_seconds": uptime_seconds,
        "today_tokens": total_tokens,
        "today_calls": total_calls,
        "today_stats": today_stats,
        "active_groups": active_groups,
        "reminder_count": reminder_count,
        "recent_tool_calls": recent_tools,
    }


def _load_recent_tool_calls(n: int) -> list[dict]:
    """读取最近 n 条工具调用日志"""
    log_file = Path("data/tool_calls.jsonl")
    if not log_file.exists():
        return []
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    result = []
    for line in reversed(lines):
        if len(result) >= n:
            break
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result
