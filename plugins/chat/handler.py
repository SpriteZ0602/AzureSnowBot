"""
私聊对话处理
──────────
私聊消息的 ChatGPT 对话处理。
"""

import json
import os
from pathlib import Path

import httpx
from nonebot import on_message, on_fullmatch, get_driver, get_bot
from nonebot.adapters.onebot.v11 import PrivateMessageEvent, Bot
from nonebot.exception import FinishedException
from nonebot.log import logger

from ..chunker import chunk_text, send_chunked

# ──────────────────── 配置 ────────────────────
config = get_driver().config
OPENAI_API_KEY: str = getattr(config, "openai_api_key", "") or os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str = getattr(config, "openai_base_url", "") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL: str = getattr(config, "openai_model", "") or os.environ.get("OPENAI_MODEL", "gpt-5.4")
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "openai")
ADMIN_NUMBER: str = getattr(config, "admin_number", "373900859")

# 加载公共基底提示词
from pathlib import Path as _Path
_base_path = _Path("data/personas/_base.txt")
_BASE_PROMPT = _base_path.read_text(encoding="utf-8").strip() if _base_path.exists() else ""

SYSTEM_PROMPT = "你是一个有用的助手，请用中文回答用户的问题。"
if _BASE_PROMPT:
    SYSTEM_PROMPT = f"{SYSTEM_PROMPT}\n\n{_BASE_PROMPT}"

# 加载 Admin 专属人格
_admin_persona_path = _Path("data/sessions/admin_persona.txt")
ADMIN_PROMPT = _admin_persona_path.read_text(encoding="utf-8").strip() if _admin_persona_path.exists() else ""

# 256K 上下文窗口，预留 20% 安全缓冲 + 4096 给回复
MAX_CONTEXT_TOKENS = 256_000
SAFETY_MARGIN = 0.8
REPLY_RESERVE = 4096
MAX_HISTORY_TOKENS = int(MAX_CONTEXT_TOKENS * SAFETY_MARGIN) - REPLY_RESERVE

# 会话文件目录
SESSION_DIR = Path("data/sessions")
SESSION_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────── Token 估算 ────────────────────
def estimate_tokens(text: str) -> int:
    """中文约 1 字 ≈ 1.5 token，英文/数字约 4 字符 ≈ 1 token"""
    cn_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - cn_chars
    return int(cn_chars * 1.5 + other_chars / 4)


def estimate_message_tokens(msg: dict) -> int:
    """估算单条 message 的 token 数（含 role 开销约 4 token）"""
    return estimate_tokens(msg.get("content", "")) + 4


# ──────────────────── JSONL 会话持久化 ────────────────────
def _session_path(user_id: str) -> Path:
    return SESSION_DIR / f"{user_id}.jsonl"


def load_history(user_id: str) -> list[dict]:
    """从 JSONL 文件加载对话历史"""
    path = _session_path(user_id)
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


def append_message(user_id: str, message: dict) -> None:
    """追加一条消息到 JSONL 文件"""
    path = _session_path(user_id)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


def clear_history(user_id: str) -> None:
    """清除用户的对话历史"""
    path = _session_path(user_id)
    if path.exists():
        path.unlink()


# ──────────────────── 历史截断 ────────────────────
def trim_history(messages: list[dict]) -> list[dict]:
    """从最新消息向前保留，直到累计 token 接近上限"""
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


# ──────────────────── 清除对话指令 ────────────────────
reset = on_fullmatch("/reset", priority=10, block=True)


@reset.handle()
async def handle_reset(event: PrivateMessageEvent):
    clear_history(str(event.user_id))
    await reset.finish("对话历史已清除。")


# ──────────────────── 主对话处理 ────────────────────
chat = on_message(priority=99, block=False)


@chat.handle()
async def handle_chat(event: PrivateMessageEvent):
    user_input = event.get_plaintext().strip()
    if not user_input:
        return

    if not OPENAI_API_KEY:
        await chat.finish("未配置 OpenAI API Key，请联系管理员。")

    user_id = str(event.user_id)

    # 检查是否引用了消息
    quoted_text = ""
    reply_id = None
    if event.reply:
        reply_id = event.reply.message_id
    else:
        for seg in event.message:
            if seg.type == "reply":
                reply_id = int(seg.data["id"])
                break
    if reply_id:
        try:
            bot = get_bot()
            msg_data = await bot.get_msg(message_id=reply_id)
            raw_msg = msg_data.get("message", "")
            if isinstance(raw_msg, str):
                quoted_text = raw_msg.strip()
            elif isinstance(raw_msg, list):
                parts = [seg.get("data", {}).get("text", "") for seg in raw_msg if isinstance(seg, dict) and seg.get("type") == "text"]
                quoted_text = "".join(parts).strip()
        except Exception as e:
            logger.warning(f"获取引用消息失败: {e}")

    # 记录用户消息（带引用内容）
    if quoted_text:
        content = f"(引用了一条消息: \"{quoted_text}\"): {user_input}"
    else:
        content = user_input
    user_msg = {"role": "user", "content": content}
    append_message(user_id, user_msg)

    # 加载历史并截断
    history = load_history(user_id)
    trimmed = trim_history(history)

    # 组装 messages（Admin 使用专属人格）
    prompt = ADMIN_PROMPT if (ADMIN_NUMBER and user_id == str(ADMIN_NUMBER) and ADMIN_PROMPT) else SYSTEM_PROMPT
    messages = [{"role": "system", "content": prompt}] + trimmed

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

            # 记录助手回复
            append_message(user_id, {"role": "assistant", "content": reply})

            bot = get_bot()
            chunks = chunk_text(reply)
            await send_chunked(bot, event, chunks, reply_first=False)
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenAI API 错误: {e.response.status_code} {e.response.text}")
        await chat.finish(f"API 请求失败 ({e.response.status_code})")
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"ChatGPT 插件异常: {e}")
        await chat.finish("请求出错，请稍后再试。")
