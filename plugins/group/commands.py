"""
群聊指令
──────
/reset, /compact, /取名, /help 等群聊指令处理。
"""

import re
from nonebot import on_fullmatch, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.log import logger

from ..persona.manager import clear_history as pm_clear_history
from ..persona.manager import get_active_persona, _session_path as persona_session_path
from ..mcp.manager import list_tools_summary
from ..skill.manager import list_skills_summary
from ..local_tools.manager import list_tools_summary as local_tools_summary
from .utils import in_whitelist, is_at_bot

# ──────────────────── /reset ────────────────────
group_reset = on_fullmatch("/reset", priority=10, block=True)


@group_reset.handle()
async def handle_group_reset(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return
    group_id = str(event.group_id)
    pm_clear_history(group_id)
    await group_reset.finish("本群对话历史已清除。")


# ──────────────────── /compact ────────────────────
group_compact = on_fullmatch("/compact", priority=10, block=True)


@group_compact.handle()
async def handle_group_compact(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return
    from ..chat.compaction import compact_history
    from pathlib import Path

    group_id = str(event.group_id)
    persona = get_active_persona(group_id)
    session_path = persona_session_path(group_id, persona)
    memory_path = Path(f"data/sessions/groups/{group_id}/MEMORY.md")
    compacted = await compact_history(group_id, session_path, memory_path)
    if compacted:
        await group_compact.finish("本群对话历史已压缩。")
    else:
        await group_compact.finish("当前历史不需要压缩。")


# ──────────────────── /取名 ────────────────────
nickname_cmd = on_message(priority=9, block=False)

_NICKNAME_TASK = (
    "你是一个取名专家。根据下面的群聊记录，给这个人起 2-3 个有趣的群昵称。"
    "分析他的用词习惯、话题偏好、说话风格，说明每个昵称的由来。"
    "昵称可以有趣，可以冒犯。直接输出结果，不要客套，一定不要输出markdown格式。"
)


@nickname_cmd.handle()
async def handle_nickname(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return

    text = event.get_plaintext().strip()
    if not text.startswith("/取名"):
        return

    args = text[len("/取名"):].strip()

    # 解析目标 QQ 号（从 @ 消息段提取）
    target_uid: str = ""
    for seg in event.message:
        if seg.type == "at":
            qq = str(seg.data.get("qq", ""))
            if qq and qq != str(event.self_id):
                target_uid = qq
                break

    # 如果没 @ 人，取发送者自己
    if not target_uid:
        target_uid = str(event.user_id)

    # 解析 limit（从文本参数中取数字）
    limit = 200
    num_match = re.search(r"\d+", args)
    if num_match:
        limit = max(10, min(500, int(num_match.group())))

    group_id = str(event.group_id)

    # 检索聊天记录
    from .chatlog import load_chatlog
    entries = load_chatlog(group_id, hours=168, user_id=target_uid, limit=limit)

    if not entries or len(entries) < 3:
        await nickname_cmd.finish(
            MessageSegment.reply(event.message_id)
            + f"这个人最近说话太少了（只找到 {len(entries)} 条），取不了名。"
        )

    # 格式化聊天记录
    from datetime import datetime as _dt
    lines: list[str] = []
    target_name = entries[0].get("name", target_uid)
    for e in entries:
        ts = _dt.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M")
        lines.append(f"[{ts}] {e.get('name', '?')}: {e.get('text', '')}")
    chat_data = f"目标用户: {target_name}\n\n" + "\n".join(lines)

    # 调用 Sub-Agent
    from ..local_tools.tools import run_sub_agent
    result = await run_sub_agent(
        task=_NICKNAME_TASK,
        data=chat_data,
        _context={
            "_chat_type": "group",
            "_target_id": group_id,
            "_user_id": str(event.user_id),
            "_sender_name": event.sender.nickname or str(event.user_id),
        },
    )

    from ..chunker import send_chunked, chunk_text
    from nonebot import get_bot
    bot = get_bot()
    chunks = chunk_text(result)
    await send_chunked(bot, event, chunks)


# ──────────────────── /help ────────────────────
HELP_TEXT = """/persona — 列出所有人格
/persona <名称> — 切换人格
/persona info — 查看当前人格详情
/persona reset — 清除当前人格的对话历史
/persona create <名称> <prompt> — 创建本群人格
/persona delete <名称> — 删除本群人格
/skill — 列出所有技能
/skill <名称> — 查看技能详情
/skill reload — 重新扫描技能
/compact — 压缩对话历史
/取名 @某人 [条数] — 根据聊天记录起群昵称
/reset — 清除当前对话历史
/help — 显示本帮助"""

help_cmd = on_fullmatch("/help", priority=5, block=True)


@help_cmd.handle()
async def handle_help(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return

    text = HELP_TEXT
    tool_lines = list_tools_summary()
    if tool_lines:
        text += "\n\n可用工具（由 MCP 提供，模型自动调用）：\n" + "\n".join(tool_lines)
    skill_lines = list_skills_summary()
    if skill_lines:
        text += "\n\n已加载技能（渐进式披露，模型按需加载）：\n" + "\n".join(skill_lines)
    local_lines = local_tools_summary()
    if local_lines:
        text += "\n\n本地工具（模型自动调用）：\n" + "\n".join(local_lines)

    await help_cmd.finish(
        MessageSegment.reply(event.message_id) + text
    )
