import json
from pathlib import Path

import httpx
from nonebot import on_message, on_fullmatch, get_driver, get_bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment, Bot, Message
from nonebot.exception import FinishedException
from nonebot.log import logger

# ──────────────────── 配置 ────────────────────
config = get_driver().config
OPENAI_API_KEY: str = getattr(config, "openai_api_key", "")
OPENAI_BASE_URL: str = getattr(config, "openai_base_url", "https://api.openai.com/v1")
OPENAI_MODEL: str = getattr(config, "openai_model", "gpt-5.4")

# 群聊白名单（在 .env 中配置，如 GROUP_WHITELIST=["123456789"]）
_raw_whitelist = getattr(config, "group_whitelist", [])
# .env 可能传入字符串，需要解析
if isinstance(_raw_whitelist, str):
    try:
        GROUP_WHITELIST: list[str] = [str(x) for x in json.loads(_raw_whitelist)]
    except (json.JSONDecodeError, TypeError):
        GROUP_WHITELIST: list[str] = [x.strip() for x in _raw_whitelist.split(",") if x.strip()]
else:
    GROUP_WHITELIST: list[str] = [str(x) for x in _raw_whitelist]

logger.info(f"群聊白名单: {GROUP_WHITELIST}")

SYSTEM_PROMPT = "你是一个群聊助手，回复时不需要 @用户，不要使用markdown格式，请用不太冗长的中文回答用户的问题。"

# 群聊历史限制更严格，避免 token 消耗过快
MAX_CONTEXT_TOKENS = 256_000
SAFETY_MARGIN = 0.8
REPLY_RESERVE = 4096
MAX_HISTORY_TOKENS = int(MAX_CONTEXT_TOKENS * SAFETY_MARGIN) - REPLY_RESERVE

# 会话文件目录（群聊按 group_id 存储）
SESSION_DIR = Path("data/sessions/groups")
SESSION_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────── Token 估算 ────────────────────
def estimate_tokens(text: str) -> int:
    cn_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - cn_chars
    return int(cn_chars * 1.5 + other_chars / 4)


def estimate_message_tokens(msg: dict) -> int:
    return estimate_tokens(msg.get("content", "")) + 4


# ──────────────────── JSONL 会话持久化 ────────────────────
def _session_path(group_id: str) -> Path:
    return SESSION_DIR / f"{group_id}.jsonl"


def load_history(group_id: str) -> list[dict]:
    path = _session_path(group_id)
    if not path.exists():
        return []
    messages = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


def append_message(group_id: str, message: dict) -> None:
    path = _session_path(group_id)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


def clear_history(group_id: str) -> None:
    path = _session_path(group_id)
    if path.exists():
        path.unlink()


# ──────────────────── 历史截断 ────────────────────
def trim_history(messages: list[dict]) -> list[dict]:
    system_tokens = estimate_message_tokens({"role": "system", "content": SYSTEM_PROMPT})
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


# ──────────────────── 工具函数 ────────────────────
def is_at_bot(event: GroupMessageEvent) -> bool:
    """检查消息是否 @了 Bot（优先用 to_me，兼容 at segment）"""
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
        # raw_msg 可能是字符串、segment dict 列表等
        if isinstance(raw_msg, str):
            return raw_msg.strip()
        elif isinstance(raw_msg, list):
            # 从 segment 列表中提取所有 text 段
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


# ──────────────────── 清除对话指令 ────────────────────
group_reset = on_fullmatch("清除对话", priority=10, block=True)


@group_reset.handle()
async def handle_group_reset(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return
    clear_history(str(event.group_id))
    await group_reset.finish("✅ 本群对话历史已清除。")


# ──────────────────── 群聊对话处理 ────────────────────
group_chat = on_message(priority=98, block=False)


@group_chat.handle()
async def handle_group_chat(event: GroupMessageEvent):
    # 白名单校验
    if not in_whitelist(event.group_id):
        return

    # 必须 @Bot 才触发
    if not is_at_bot(event):
        return

    user_input = extract_text(event)
    if not user_input:
        return

    if not OPENAI_API_KEY:
        await group_chat.finish("⚠️ 未配置 OpenAI API Key，请联系管理员。")

    # 检查是否引用了消息
    quoted_text = ""
    reply_id = get_reply_id(event)
    if reply_id:
        bot = get_bot()
        quoted_text = await fetch_quoted_text(bot, reply_id)

    group_id = str(event.group_id)
    sender = event.sender.nickname or str(event.user_id)

    # 组装用户消息（带发送者标识 + 引用内容）
    if quoted_text:
        content = f"[{sender}] (引用了一条消息: \"{quoted_text}\"): {user_input}"
    else:
        content = f"[{sender}]: {user_input}"
    user_msg = {"role": "user", "content": content}
    append_message(group_id, user_msg)

    # 加载历史并截断
    history = load_history(group_id)
    trimmed = trim_history(history)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + trimmed

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"].strip()

            append_message(group_id, {"role": "assistant", "content": reply})

            # 引用原消息回复
            await group_chat.finish(
                MessageSegment.reply(event.message_id) + reply
            )
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenAI API 错误: {e.response.status_code} {e.response.text}")
        await group_chat.finish(f"⚠️ API 请求失败 ({e.response.status_code})")
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"群聊 ChatGPT 插件异常: {e}")
        await group_chat.finish("⚠️ 请求出错，请稍后再试。")
