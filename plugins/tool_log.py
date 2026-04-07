"""
工具调用日志
──────────
持久化记录每次工具调用的名称、参数、结果和来源。
存储为 JSONL 格式，方便后续分析和 Web Dashboard 展示。

使用方法:
    from plugins.tool_log import log_tool_call
    log_tool_call("chat", "local__set_reminder", {"message": "开会"}, "已设置提醒...")
"""

import json
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("data/tool_calls.jsonl")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log_tool_call(
    source: str,
    tool_name: str,
    arguments: dict,
    result: str,
    *,
    user_id: str = "",
    group_id: str = "",
) -> None:
    """
    记录一次工具调用。

    参数:
        source: 来源（"chat", "group", "heartbeat", "sub_agent"）
        tool_name: 工具全名（如 "local__set_reminder"）
        arguments: 工具参数
        result: 工具返回结果（截断到 500 字符）
        user_id: 调用者 QQ 号（可选）
        group_id: 群号（可选）
    """
    entry = {
        "ts": datetime.now().isoformat(),
        "source": source,
        "tool": tool_name,
        "args": arguments,
        "result_len": len(result) if result else 0,
        "ok": not result.startswith("[错误]") if result else True,
        "user_id": user_id,
        "group_id": group_id,
    }
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 日志写入失败不影响主流程
