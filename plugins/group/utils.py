"""
群聊工具函数
──────────
白名单、@检测、文本提取等公共函数，供 group 包内各模块及其他包使用。
"""

import json
import os

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Bot
from nonebot.log import logger

# ──────────────────── 配置 ────────────────────
config = get_driver().config
OPENAI_API_KEY: str = getattr(config, "openai_api_key", "") or os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str = getattr(config, "openai_base_url", "") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL: str = getattr(config, "openai_model", "") or os.environ.get("OPENAI_MODEL", "gpt-5.4")
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "openai")

# 群聊白名单
_raw_whitelist = getattr(config, "group_whitelist", [])
if isinstance(_raw_whitelist, str):
    try:
        GROUP_WHITELIST: list[str] = [str(x) for x in json.loads(_raw_whitelist)]
    except (json.JSONDecodeError, TypeError):
        GROUP_WHITELIST: list[str] = [x.strip() for x in _raw_whitelist.split(",") if x.strip()]
else:
    GROUP_WHITELIST: list[str] = [str(x) for x in _raw_whitelist]

logger.info(f"群聊白名单: {GROUP_WHITELIST}")

# Token 相关常量
MAX_CONTEXT_TOKENS = 256_000
SAFETY_MARGIN = 0.8
REPLY_RESERVE = 4096
MAX_HISTORY_TOKENS = int(MAX_CONTEXT_TOKENS * SAFETY_MARGIN) - REPLY_RESERVE


# ──────────────────── Token 估算 ────────────────────
def estimate_tokens(text: str) -> int:
    cn_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - cn_chars
    return int(cn_chars * 1.5 + other_chars / 4)


def estimate_message_tokens(msg: dict) -> int:
    return estimate_tokens(msg.get("content", "")) + 4


def trim_history(messages: list[dict], system_prompt: str) -> list[dict]:
    """从最新消息向前保留，直到累计 token 接近上限"""
    system_tokens = estimate_message_tokens({"role": "system", "content": system_prompt})
    budget = MAX_HISTORY_TOKENS - system_tokens
    trimmed: list[dict] = []
    total = 0
    for msg in reversed(messages):
        cost = estimate_message_tokens(msg)
        if total + cost > budget:
            break
        trimmed.append(msg)
        total += cost
    trimmed.reverse()
    return trimmed


# ──────────────────── 群聊工具函数 ────────────────────
def is_at_bot(event: GroupMessageEvent) -> bool:
    """检查消息是否 @了 Bot"""
    if event.is_tome():
        return True
    for seg in event.message:
        if seg.type == "at" and str(seg.data.get("qq")) == str(event.self_id):
            return True
    return False


def extract_text(event: GroupMessageEvent) -> str:
    """提取消息中的纯文本（去掉 @部分）"""
    return event.get_plaintext().strip()


def get_reply_id(event: GroupMessageEvent) -> int | None:
    """从消息中提取被引用消息的 ID"""
    if event.reply:
        return event.reply.message_id
    for seg in event.message:
        if seg.type == "reply":
            return int(seg.data["id"])
    return None


async def fetch_quoted_text(bot: Bot, message_id: int) -> str:
    """通过 API 获取被引用消息的纯文本内容"""
    try:
        msg_data = await bot.get_msg(message_id=message_id)
        raw_msg = msg_data.get("message", "")
        if isinstance(raw_msg, str):
            return raw_msg.strip()
        elif isinstance(raw_msg, list):
            parts = []
            for seg in raw_msg:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
            return "".join(parts).strip()
        return str(raw_msg).strip()
    except Exception as e:
        logger.warning(f"获取引用消息失败: {e}")
        return ""


def in_whitelist(group_id: int) -> bool:
    """检查群是否在白名单中"""
    if not GROUP_WHITELIST:
        return False
    return str(group_id) in GROUP_WHITELIST
