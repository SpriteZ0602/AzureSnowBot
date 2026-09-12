"""
群聊全量记录
──────────
旁路记录白名单群内的所有消息（不仅限 @Bot），
写入 SQLite（data/sessions/chatlog.db，存储层见 chatlog_db.py），
供 get_group_chat_log 工具、chatter 插话统计和 Dashboard 按需检索。
"""

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger

from .chatlog_db import (
    RETENTION_DAYS,
    append_chatlog,
    load_chatlog,
    purge_all_old_entries,
    purge_old_entries,
)
from .utils import extract_text, is_group_event, in_whitelist, is_at_bot
from ..persona.manager import get_listen_all

__all__ = ["append_chatlog", "load_chatlog", "purge_old_entries", "RETENTION_DAYS"]


# ──────────────────── 启动清理 ────────────────────

@get_driver().on_startup
async def _purge_expired_on_startup() -> None:
    """启动时清理一次超过保留期的旧记录（旧 JSONL 时代的 purge 从未被调度过）"""
    try:
        removed = purge_all_old_entries()
        if removed:
            logger.info(f"聊天记录清理: 删除 {removed} 条超过 {RETENTION_DAYS} 天的旧记录")
    except Exception as e:
        logger.warning(f"聊天记录启动清理失败（不影响运行）: {e}")


# ──────────────────── NoneBot 旁路记录器 ────────────────────
# priority 较高（数字大=优先级低），block=False 保证不影响其他 handler
_chatlog_recorder = on_message(rule=is_group_event, priority=1, block=False)


@_chatlog_recorder.handle()
async def _record_group_message(bot: Bot, event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return

    # @ 感知提取：@ 段转成 @昵称(qq号)，否则记录里丢失"点了谁"的信息
    text = await extract_text(bot, event)
    if not text:
        return

    # /bind 消息携带水鱼 Token（成绩数据凭证），不落任何记录（全量记录与 listen_all 上下文都跳过）
    if text.startswith("/bind") or text.startswith("/unbind"):
        return

    group_id = str(event.group_id)
    user_id = str(event.user_id)
    nickname = event.sender.nickname or user_id

    append_chatlog(group_id, user_id, nickname, text)

    # 全量上下文模式（/listen on）：把非 @Bot 的群消息也写入会话历史，
    # 使 @Bot 回复时能加载全量群聊上下文。@Bot 消息由 handler 记录（格式更完整）。
    if (
        user_id != str(event.self_id)
        and not is_at_bot(event)
        and get_listen_all(group_id)
    ):
        from ..persona.manager import get_active_persona, append_message
        persona = get_active_persona(group_id)
        append_message(
            group_id,
            {"role": "user", "content": f"[{nickname}]: {text}"},
            persona,
        )
