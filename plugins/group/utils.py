"""
群聊工具函数
──────────
白名单、@检测、文本提取等公共函数，供 group 包内各模块及其他包使用。
"""

import asyncio
import json
from pathlib import Path

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Bot
from nonebot.log import logger


def is_group_event(event) -> bool:
    """NoneBot Rule：仅匹配群聊消息事件。

    防止群聊 matcher 吞掉私聊事件（handler 类型不符被 skip 后，
    block=True 的 matcher 依然会 StopPropagation，导致私聊回复失效）。
    """
    return isinstance(event, GroupMessageEvent)

# ──────────────────── 配置 ────────────────────
config = get_driver().config

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


async def extract_text(bot: Bot, event: GroupMessageEvent) -> str:
    """提取消息文本，@ 段转成「@昵称(qq号)」可读形式。

    get_plaintext() 会把 @ 段整个丢掉，LLM 就无法知道消息点了谁；
    这里逐段拼接：@Bot 渲染为 @Bot，@ 他人查一次群成员 API 取昵称
    （查不到时退化为 @qq号），uid 一并给出供检索工具精确匹配。
    """
    parts: list[str] = []
    for seg in event.message:
        if seg.type == "text":
            parts.append(seg.data.get("text", ""))
        elif seg.type == "at":
            parts.append(await _render_at(bot, event, seg))
    return "".join(parts).strip()


async def _render_at(bot: Bot, event: GroupMessageEvent, seg) -> str:
    """渲染单个 @ 段。QQ号来源是消息段（不可伪造昵称对应关系）。"""
    qq = str(seg.data.get("qq", ""))
    if not qq:
        return ""
    if qq == "all":
        return "@全体成员"
    if qq == str(event.self_id):
        return "@Bot"
    try:
        info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(qq))
        name = info.get("nickname") or ""
    except Exception as e:
        logger.warning(f"查询群成员 {qq} 昵称失败: {e}")
        name = ""
    return f"@{name}({qq})" if name else f"@{qq}"


def get_reply_id(event: GroupMessageEvent) -> int | None:
    """从消息中提取被引用消息的 ID"""
    if event.reply:
        return event.reply.message_id
    for seg in event.message:
        if seg.type == "reply":
            return int(seg.data["id"])
    return None


async def fetch_quoted_text(bot: Bot, message_id: int) -> tuple[str, str]:
    """通过 API 获取被引用消息的作者昵称与文本，返回 (作者, 内容)。

    群聊里"他说得对吗"这类提问依赖引用消息的作者，只带文字不带人
    LLM 无法解析"他"指谁。获取失败时返回 ("", "")。
    """
    try:
        msg_data = await bot.get_msg(message_id=message_id)
        sender = msg_data.get("sender") or {}
        author = sender.get("nickname") or "某人"
        raw_msg = msg_data.get("message", "")
        if isinstance(raw_msg, str):
            text = raw_msg.strip()
        elif isinstance(raw_msg, list):
            parts = []
            for seg in raw_msg:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
            text = "".join(parts).strip()
        else:
            text = str(raw_msg).strip()
        return (author, text)
    except Exception as e:
        logger.warning(f"获取引用消息失败: {e}")
        return ("", "")


async def fetch_quoted_image_urls(bot: Bot, message_id: int) -> list[str]:
    """通过 API 获取被引用消息中的图片 URL 列表"""
    try:
        msg_data = await bot.get_msg(message_id=message_id)
        raw_msg = msg_data.get("message", "")
        if not isinstance(raw_msg, list):
            return []
        urls: list[str] = []
        for seg in raw_msg:
            if isinstance(seg, dict) and seg.get("type") == "image":
                url = seg.get("data", {}).get("url", "")
                if url:
                    urls.append(url)
        return urls
    except Exception as e:
        logger.warning(f"获取引用图片失败: {e}")
        return []


def in_whitelist(group_id: int) -> bool:
    """检查群是否在白名单中"""
    if not GROUP_WHITELIST:
        return False
    return str(group_id) in GROUP_WHITELIST


# ──────────────────── 白名单管理（运行时 + .env 持久化） ────────────────────

ENV_PATH = Path(".env")


def list_whitelist() -> list[str]:
    """返回当前白名单群号列表"""
    return list(GROUP_WHITELIST)


def add_to_whitelist(group_id: str) -> bool:
    """添加群到白名单（立即生效 + 持久化到 .env），返回是否新增"""
    if group_id in GROUP_WHITELIST:
        return False
    GROUP_WHITELIST.append(group_id)
    _persist_whitelist()
    logger.info(f"白名单已添加群: {group_id}")
    return True


def remove_from_whitelist(group_id: str) -> bool:
    """从白名单删除群（立即生效 + 持久化到 .env），返回是否删除"""
    if group_id not in GROUP_WHITELIST:
        return False
    GROUP_WHITELIST.remove(group_id)
    _persist_whitelist()
    logger.info(f"白名单已删除群: {group_id}")
    return True


def _persist_whitelist() -> None:
    """把当前白名单写回 .env 的 GROUP_WHITELIST 行（重启后仍生效）"""
    line = f"GROUP_WHITELIST={json.dumps(GROUP_WHITELIST, ensure_ascii=False)}"
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        for i, l in enumerate(lines):
            if l.strip().startswith("GROUP_WHITELIST="):
                lines[i] = line
                break
        else:
            lines.append(line)
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        ENV_PATH.write_text(line + "\n", encoding="utf-8")


def handle_whitelist_command(text: str) -> str:
    """解析 /白名单 指令文本（list | add <群号> | delete <群号>），返回回复内容。

    供群聊和私聊两个 matcher 共用。
    """
    args = text[len("/白名单"):].strip().split()
    if not args:
        return "用法: /白名单 list | add <群号> | delete <群号>"

    cmd, *rest = args

    if cmd == "list":
        groups = list_whitelist()
        if not groups:
            return "白名单为空。"

        from ..persona.manager import get_listen_all, get_group_proactive, get_auto_trigger

        lines: list[str] = []
        for g in groups:
            listen = "开" if get_listen_all(g) else "关"
            proactive = "开" if get_group_proactive(g) else "关"
            trigger = "开" if get_auto_trigger(g) else "关"
            lines.append(
                f"- {g}　全量对话: {listen}　主动对话: {proactive}　复读插话: {trigger}"
            )
        return f"白名单中的群（{len(groups)} 个）:\n" + "\n".join(lines)

    if cmd == "add":
        if not rest:
            return "用法: /白名单 add <群号>"
        gid = rest[0]
        if not gid.isdigit():
            return f"无效的群号: {gid}"
        if add_to_whitelist(gid):
            return f"已将群 {gid} 加入白名单。"
        return f"群 {gid} 已在白名单中。"

    if cmd == "delete":
        if not rest:
            return "用法: /白名单 delete <群号>"
        gid = rest[0]
        if not gid.isdigit():
            return f"无效的群号: {gid}"
        if remove_from_whitelist(gid):
            return f"已将群 {gid} 移出白名单。"
        return f"群 {gid} 不在白名单中。"

    return f"未知指令: {cmd}（可用: list | add <群号> | delete <群号>）"


# ──────────────────── 会话级锁 ────────────────────
# 同一 (群, 人格) 的对话请求串行处理：
# 多人同时 @Bot 时，若并发跑 Agentic Loop，会互相读到对方的消息历史、
# 混淆工具调用结果、交错写入 history.jsonl。锁保证每个问题拿到完整的一轮。
_session_locks: dict[tuple[str, str], asyncio.Lock] = {}


def get_session_lock(group_id: str, persona: str) -> asyncio.Lock:
    """获取 (群, 人格) 维度的会话锁，同一会话的请求串行执行。"""
    key = (group_id, persona)
    lock = _session_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[key] = lock
    return lock

