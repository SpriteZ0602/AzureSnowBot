from nonebot import on_fullmatch
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

from .groupchat import in_whitelist, is_at_bot

HELP_TEXT = """/persona — 列出所有人格
/persona <名称> — 切换人格
/persona info — 查看当前人格详情
/persona reset — 清除当前人格的对话历史
/persona create <名称> <prompt> — 创建本群人格
/persona delete <名称> — 删除本群人格
/reset — 清除当前对话历史
/help — 显示本帮助"""

help_cmd = on_fullmatch("/help", priority=5, block=True)


@help_cmd.handle()
async def handle_help(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return
    await help_cmd.finish(
        MessageSegment.reply(event.message_id) + HELP_TEXT
    )
