"""
消息分条发送模块
────────────────
模仿 OpenClaw 的 Block Streaming + Human-like Pacing：
  - 将长回复按段落/句子拆分成多条消息
  - 每条之间加随机延迟，模拟人类打字节奏
  - 第一条引用原消息，后续条直接发送
  - 分段数达到 FORWARD_THRESHOLD 时改用合并转发，避免刷屏

合并转发说明：
  send_group_forward_msg / send_private_forward_msg 不是 OneBot v11 标准 API，
  是 go-cqhttp 的扩展（标准里只有 get_forward_msg）。NapCat 已实现，
  且两种字段命名都认：user_id 优先于 uin，nickname 优先于 name。
  这里按标准写 user_id + nickname，失败时回退逐条发送。
"""

import re
import asyncio
import random
import base64
from collections import defaultdict
from dataclasses import dataclass

import httpx

from nonebot.adapters.onebot.v11 import Bot, MessageSegment, Message
from nonebot.log import logger

# ──────────────────── 配置 ────────────────────
# 分条发送的阈值：短于此长度的回复直接整条发送
CHUNK_THRESHOLD = 60

# 分段数达到此值时改用合并转发（一条「聊天记录」承载所有分段，避免刷屏）。
# 合并转发是扩展 API，发送失败会自动回退到逐条发送。
FORWARD_THRESHOLD = 3

# 单条消息的字符上限。
# 分条以换行号为准（一个换行 = 一条消息），只有单段超过 MAX_CHUNK_CHARS
# 时才硬切 —— 这是防止 QQ 单条消息长度超限导致发送失败的兜底，正常不会触发。
MIN_CHUNK_CHARS = 10      # 太短的片段会和下一段合并
MAX_CHUNK_CHARS = 1800    # 单段硬切阈值（QQ 单条消息安全上限内）

# 人类节奏：每条消息之间的随机延迟（秒）
HUMAN_DELAY_MIN = 1.0
HUMAN_DELAY_MAX = 3.0

# 句子结束符（中文 + 英文）
_SENTENCE_END_RE = re.compile(r"(?<=[。！？!?\n])")
# 段落分隔符
_PARAGRAPH_RE = re.compile(r"\n{2,}")


# ──────────────────── 文本拆分 ────────────────────

def _split_sentences(text: str) -> list[str]:
    """按句子结束符拆分文本"""
    parts = _SENTENCE_END_RE.split(text)
    return [p for p in parts if p.strip()]


def chunk_text(text: str) -> list[str]:
    """
    将文本拆分为适合分条发送的块。

    按换行拆分：每个 \\n 就是一条新消息，不再限制单条字数。
    仅当某一段本身超过 MAX_CHUNK_CHARS 时才硬切（发送失败的兜底）。
    短回复（< CHUNK_THRESHOLD）直接返回整条。
    """
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []

    # 按单个换行拆分
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        # 超长行硬切
        while len(line) > MAX_CHUNK_CHARS:
            chunks.append(line[:MAX_CHUNK_CHARS])
            line = line[MAX_CHUNK_CHARS:]
        if line:
            chunks.append(line)

    return chunks if chunks else [text]


# ──────────────────── 会话锁 ────────────────────
# 每个会话（群/私聊）一把锁，保证同一会话的分条发送不交叉
_session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _session_key(event) -> str:
    """生成会话唯一键：群聊用 group_id，私聊用 user_id"""
    group_id = getattr(event, "group_id", None)
    if group_id:
        return f"g:{group_id}"
    return f"u:{event.user_id}"


# ──────────────────── 合并转发 ────────────────────

# Bot 自己的昵称缓存（合并转发节点要填发送者昵称，没必要每次现查）
_self_nickname: str | None = None


async def _get_self_nickname(bot: Bot) -> str:
    """获取 Bot 自己的昵称，取不到就用 self_id 兜底。"""
    global _self_nickname
    if _self_nickname is not None:
        return _self_nickname
    try:
        info = await bot.call_api("get_login_info")
        nickname = (info or {}).get("nickname") or ""
    except Exception as e:
        logger.debug(f"获取登录信息失败，使用 self_id 作为昵称: {e}")
        nickname = ""
    if not nickname:
        nickname = str(getattr(bot, "self_id", "")) or "Bot"
    _self_nickname = nickname
    return nickname


def reset_self_nickname_cache() -> None:
    """清空昵称缓存（Bot 改昵称或测试时需要）"""
    global _self_nickname
    _self_nickname = None


def build_forward_nodes(
    chunks: list[str],
    *,
    user_id: int | str,
    nickname: str,
) -> list[dict]:
    """把分段构造成合并转发的自定义节点列表。

    节点格式遵循 OneBot v11 的「合并转发自定义节点」：
    {"type": "node", "data": {"user_id": ..., "nickname": ..., "content": ...}}
    """
    uid = int(user_id) if str(user_id).isdigit() else 0
    return [
        {
            "type": "node",
            "data": {
                "user_id": uid,
                "nickname": nickname,
                "content": chunk,
            },
        }
        for chunk in chunks
    ]


@dataclass(frozen=True)
class SendResult:
    status: str  # sent / failed / unknown
    message_ids: tuple[int, ...] = ()
    sticker_result: "SendResult | None" = None


def _delivery_error(exc: Exception) -> SendResult:
    unknown = isinstance(exc, (TimeoutError, ConnectionError, OSError, httpx.TransportError)) or type(exc).__name__ == "NetworkError"
    status = "unknown" if unknown else "failed"
    logger.warning(f"消息发送状态={status}: {type(exc).__name__}")
    return SendResult(status)


def _remember_delivery(chat_type: str, target_id: int, response, text: str, author: str) -> tuple[int, ...]:
    response = response if isinstance(response, dict) else {}
    mid = response.get("message_id")
    ids = (int(mid),) if isinstance(mid, (int, str)) and str(mid).lstrip("-").isdigit() else ()
    if chat_type == "group":
        try:
            from .message_store import remember_sent_message
            remember_sent_message(str(target_id), response, text, author)
        except Exception as exc:
            # 发送已经成功，保存索引失败绝不能引发重发。
            logger.warning(f"保存发送消息关联失败: {type(exc).__name__}")
    return ids


async def send_forward(
    bot: Bot,
    chat_type: str,
    target_id: int,
    chunks: list[str],
) -> SendResult:
    """用合并转发一次性发送所有分段。

    返回发送状态和 QQ 消息 ID；超时视为 unknown，禁止盲目重发。

    注意：合并转发是 go-cqhttp 扩展 API，不在 OneBot v11 标准里。
    如果实现端不支持会抛异常，这里捕获并返回 False，不影响主流程。
    """
    if not chunks:
        return SendResult("sent")

    nickname = await _get_self_nickname(bot)
    nodes = build_forward_nodes(
        chunks, user_id=getattr(bot, "self_id", 0), nickname=nickname,
    )

    try:
        if chat_type == "group":
            response = await bot.call_api(
                "send_group_forward_msg", group_id=target_id, messages=nodes,
            )
        else:
            response = await bot.call_api(
                "send_private_forward_msg", user_id=target_id, messages=nodes,
            )
    except Exception as e:
        return _delivery_error(e)
    ids = _remember_delivery(chat_type, target_id, response, "\n".join(chunks), nickname)
    return SendResult("sent", ids)


# ──────────────────── 分条发送 ────────────────────

async def _send_sticker_unlocked(bot, target_id: int, sticker: dict, persona: str | None) -> SendResult | None:
    from . import stickers as library
    gid = str(target_id)
    try:
        item, content = library.read_image(gid, sticker["id"])
        if sticker.get("_automatic") and not library.reserve_auto(gid):
            return None
    except Exception as exc:
        logger.warning(f"发送前表情读取失败: {type(exc).__name__}")
        result = SendResult("failed")
        item = sticker
    else:
        try:
            response = await bot.send_group_msg(group_id=target_id, message=MessageSegment.image(
                "base64://" + base64.b64encode(content).decode("ascii")))
        except Exception as exc:
            result = _delivery_error(exc)
        else:
            mid = response.get("message_id") if isinstance(response, dict) else None
            ids = (int(mid),) if isinstance(mid, (str, int)) and str(mid).lstrip("-").isdigit() else ()
            result = SendResult("sent", ids)
            try:
                from .message_store import remember_sticker_message
                remember_sticker_message(gid, response if isinstance(response, dict) else {}, item, _self_nickname or "Bot")
                library.mark_used(gid, item["id"])
            except Exception as exc:
                logger.warning(f"表情已发送，保存关联失败: {type(exc).__name__}")
    try:
        from .persona.manager import append_message
        state = {"sent": "已发送", "failed": "发送失败", "unknown": "发送状态未知"}[result.status]
        append_message(gid, {"role": "assistant", "content":
            f"[表情包{state} #{item['id'][:8]}：{'、'.join(item['tags'])}；{item.get('description', '')}]"}, persona)
    except Exception as exc:
        logger.warning(f"保存表情发送记录失败: {type(exc).__name__}")
    return result

async def send_chunked(
    bot: Bot,
    event,
    chunks: list[str],
    *,
    reply_first: bool = True,
    sticker: dict | None = None,
    persona: str | None = None,
) -> SendResult:
    """
    分条发送消息列表，每条之间加随机延迟。

    参数:
        bot: NoneBot Bot 实例
        event: 消息事件（用于提取 group_id / user_id）
        chunks: 要发送的文本列表
        reply_first: 第一条是否引用原消息（分段数达到 FORWARD_THRESHOLD 时
                     走合并转发，此时引用会被丢弃 —— 聊天记录无法带引用）
    """
    if not chunks and not sticker:
        return SendResult("sent")

    key = _session_key(event)

    async with _session_locks[key]:
        # 判断是群聊还是私聊
        group_id = getattr(event, "group_id", None)
        chat_type = "group" if group_id else "private"
        target_id = group_id if group_id else event.user_id
        if group_id and sticker and persona is None:
            from .persona.manager import get_active_persona
            persona = get_active_persona(str(group_id))

        # 分段太多时合并成一条「聊天记录」，避免刷屏
        if len(chunks) >= FORWARD_THRESHOLD:
            forwarded = await send_forward(bot, chat_type, target_id, chunks)
            if forwarded.status == "sent":
                image_result = await _send_sticker_unlocked(bot, target_id, sticker, persona) if group_id and sticker else None
                return SendResult("sent", forwarded.message_ids, image_result)
            if forwarded.status == "unknown":
                return forwarded

        message_ids = []
        for i, chunk in enumerate(chunks):
            # 第一条引用原消息
            if i == 0 and reply_first and hasattr(event, "message_id"):
                msg = MessageSegment.reply(event.message_id) + chunk
            else:
                msg = Message(chunk)

            try:
                if group_id:
                    response = await bot.send_group_msg(group_id=group_id, message=msg)
                else:
                    response = await bot.send_private_msg(user_id=event.user_id, message=msg)
            except Exception as exc:
                failure = _delivery_error(exc)
                return SendResult(failure.status, tuple(message_ids))
            message_ids.extend(_remember_delivery(chat_type, target_id, response, chunk, _self_nickname or "Bot"))

            # 非最后一条，加随机延迟
            if i < len(chunks) - 1:
                delay = random.uniform(HUMAN_DELAY_MIN, HUMAN_DELAY_MAX)
                await asyncio.sleep(delay)
        image_result = await _send_sticker_unlocked(bot, target_id, sticker, persona) if group_id and sticker else None
        return SendResult("sent", tuple(message_ids), image_result)


async def send_chunked_raw(
    bot: Bot,
    chat_type: str,
    target_id: int,
    text: str,
    *,
    sticker: dict | None = None,
    persona: str | None = None,
) -> SendResult:
    """
    无需 event 对象的分条发送。供提醒等主动推送场景使用。

    参数:
        bot: NoneBot Bot 实例
        chat_type: "group" 或 "private"
        target_id: group_id 或 user_id
        text: 要发送的完整文本

    分段数达到 FORWARD_THRESHOLD 时改用合并转发，失败回退逐条发送。
    """
    chunks = chunk_text(text)
    if not chunks and not sticker:
        return SendResult("sent")

    key = f"{'g' if chat_type == 'group' else 'u'}:{target_id}"

    async with _session_locks[key]:
        if chat_type == "group" and sticker and persona is None:
            from .persona.manager import get_active_persona
            persona = get_active_persona(str(target_id))
        if len(chunks) >= FORWARD_THRESHOLD:
            forwarded = await send_forward(bot, chat_type, target_id, chunks)
            if forwarded.status == "sent":
                image_result = await _send_sticker_unlocked(bot, target_id, sticker, persona) if chat_type == "group" and sticker else None
                return SendResult("sent", forwarded.message_ids, image_result)
            if forwarded.status == "unknown":
                return forwarded

        message_ids = []
        for i, chunk in enumerate(chunks):
            msg = Message(chunk)

            try:
                if chat_type == "group":
                    response = await bot.send_group_msg(group_id=target_id, message=msg)
                else:
                    response = await bot.send_private_msg(user_id=target_id, message=msg)
            except Exception as exc:
                failure = _delivery_error(exc)
                return SendResult(failure.status, tuple(message_ids))
            message_ids.extend(_remember_delivery(chat_type, target_id, response, chunk, _self_nickname or "Bot"))

            if i < len(chunks) - 1:
                delay = random.uniform(HUMAN_DELAY_MIN, HUMAN_DELAY_MAX)
                await asyncio.sleep(delay)
        image_result = await _send_sticker_unlocked(bot, target_id, sticker, persona) if chat_type == "group" and sticker else None
        return SendResult("sent", tuple(message_ids), image_result)
