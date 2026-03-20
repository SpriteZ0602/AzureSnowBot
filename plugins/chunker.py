"""
消息分条发送模块
────────────────
模仿 OpenClaw 的 Block Streaming + Human-like Pacing：
  - 将长回复按段落/句子拆分成多条消息
  - 每条之间加随机延迟，模拟人类打字节奏
  - 第一条引用原消息，后续条直接发送
"""

import re
import asyncio
import random
from collections import defaultdict

from nonebot.adapters.onebot.v11 import Bot, MessageSegment, Message

# ──────────────────── 配置 ────────────────────
# 分条发送的阈值：短于此长度的回复直接整条发送
CHUNK_THRESHOLD = 60

# 单条消息的字符限制
MIN_CHUNK_CHARS = 10    # 太短的片段会和下一段合并
MAX_CHUNK_CHARS = 200   # 超过此长度的段落会按句子再拆

# 人类节奏：每条消息之间的随机延迟（秒）
HUMAN_DELAY_MIN = 3.0
HUMAN_DELAY_MAX = 5.0

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

    主要按换行拆分：每个 \\n 就是一条新消息。
    超长单行再按句子 → 硬切进一步拆分。
    短回复（< CHUNK_THRESHOLD）直接返回整条。
    """
    text = text.strip()
    if not text:
        return []

    # 短回复不拆
    if len(text) <= CHUNK_THRESHOLD:
        return [text]

    chunks: list[str] = []

    # 按单个换行拆分
    lines = text.split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 单行足够短，直接作为一条
        if len(line) <= MAX_CHUNK_CHARS:
            chunks.append(line)
            continue

        # 单行太长，按句子拆
        sentences = _split_sentences(line)
        buffer = ""
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(buffer) + len(sent) + 1 <= MAX_CHUNK_CHARS:
                buffer = f"{buffer}{sent}" if buffer else sent
            else:
                if buffer:
                    chunks.append(buffer)
                # 单句超长，硬切
                if len(sent) > MAX_CHUNK_CHARS:
                    while sent:
                        chunks.append(sent[:MAX_CHUNK_CHARS])
                        sent = sent[MAX_CHUNK_CHARS:]
                else:
                    buffer = sent
        if buffer:
            chunks.append(buffer)

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


# ──────────────────── 分条发送 ────────────────────

async def send_chunked(
    bot: Bot,
    event,
    chunks: list[str],
    *,
    reply_first: bool = True,
) -> None:
    """
    分条发送消息列表，每条之间加随机延迟。

    参数:
        bot: NoneBot Bot 实例
        event: 消息事件（用于提取 group_id / user_id）
        chunks: 要发送的文本列表
        reply_first: 第一条是否引用原消息
    """
    if not chunks:
        return

    key = _session_key(event)

    async with _session_locks[key]:
        # 判断是群聊还是私聊
        group_id = getattr(event, "group_id", None)

        for i, chunk in enumerate(chunks):
            # 第一条引用原消息
            if i == 0 and reply_first and hasattr(event, "message_id"):
                msg = MessageSegment.reply(event.message_id) + chunk
            else:
                msg = Message(chunk)

            if group_id:
                await bot.send_group_msg(group_id=group_id, message=msg)
            else:
                await bot.send_private_msg(user_id=event.user_id, message=msg)

            # 非最后一条，加随机延迟
            if i < len(chunks) - 1:
                delay = random.uniform(HUMAN_DELAY_MIN, HUMAN_DELAY_MAX)
                await asyncio.sleep(delay)
