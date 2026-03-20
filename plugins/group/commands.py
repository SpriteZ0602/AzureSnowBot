"""
群聊指令
──────
/reset, /help 等群聊指令处理。
"""

from nonebot import on_fullmatch
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

from ..persona.manager import clear_history as pm_clear_history
from ..mcp.manager import list_tools_summary
from ..skill.manager import list_skills_summary
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

    await help_cmd.finish(
        MessageSegment.reply(event.message_id) + text
    )
