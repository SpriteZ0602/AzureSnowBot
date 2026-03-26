"""
Admin 私聊主动发言
──────────────────
对话结束后启动空闲计时器，到期后携带对话上下文询问 LLM 是否想主动发消息。
- LLM 有话说 → 直接发送并写入对话历史
- LLM 回复 NO → 无事发生

仅在管理员私聊中生效，计时器全局唯一。
"""

import asyncio
import json

import httpx
from nonebot import get_bot, get_driver
from nonebot.log import logger

from ..chunker import chunk_text, send_chunked_raw
from ..llm import API_KEY, BASE_URL, MODEL

# ──────────────────── 配置 ────────────────────
config = get_driver().config
ADMIN_NUMBER: str = str(getattr(config, "admin_number", ""))
IDLE_SECONDS: int = int(getattr(config, "proactive_idle_seconds", 3600))

# 引导词 —— 触发时动态生成，带当前时间
def _build_proactive_instruction() -> str:
    return (
        "【系统指令】距离你们上次对话已经过去了一段时间。"
        "请决定是否主动给对方发消息。"
        "如果你有想说的（比如继续之前的话题、分享感想、吐槽、找话聊等），"
        "直接输出消息内容，会被原样发送给对方。"
        "如果没什么想说的，只回复 NO（仅这两个字母，不要加任何其他内容）。"
    )

# ──────────────────── 计时器状态 ────────────────────
_idle_task: asyncio.Task | None = None


# ──────────────────── 公共 API ────────────────────

def reset_idle_timer() -> None:
    """重置空闲计时器（在 Bot 回复 admin 后调用）。"""
    global _idle_task
    if _idle_task and not _idle_task.done():
        _idle_task.cancel()
    _idle_task = asyncio.create_task(_idle_countdown())
    logger.debug(f"主动发言计时器已重置 ({IDLE_SECONDS}s)")


def cancel_idle_timer() -> None:
    """取消空闲计时器。"""
    global _idle_task
    if _idle_task and not _idle_task.done():
        _idle_task.cancel()
    _idle_task = None


# ──────────────────── 内部实现 ────────────────────

async def _idle_countdown() -> None:
    """等待空闲时间到期后触发主动发言检查。"""
    try:
        await asyncio.sleep(IDLE_SECONDS)
        await _try_proactive_message()
        # 无论 LLM 是否选择发送，都重新启动计时器
        reset_idle_timer()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"主动发言异常: {e}")


async def _try_proactive_message() -> None:
    """加载对话历史，询问 LLM 是否想主动发消息。"""
    # 延迟导入，避免循环引用
    from .handler import (
        load_history,
        trim_history,
        append_message,
        build_time_context,
        ADMIN_PROMPT,
        SYSTEM_PROMPT,
    )

    if not ADMIN_NUMBER or not API_KEY:
        return

    history = load_history(ADMIN_NUMBER)
    if not history:
        return

    # 最后一条不是 assistant，说明对话状态异常（用户发了消息但 Bot 没回），跳过
    if history[-1].get("role") != "assistant":
        return

    trimmed = trim_history(history)
    prompt = ADMIN_PROMPT if ADMIN_PROMPT else SYSTEM_PROMPT
    prompt += build_time_context(ADMIN_NUMBER)

    messages = [{"role": "system", "content": prompt}] + trimmed
    # 把引导词作为独立 system 消息追加在末尾
    messages.append({"role": "system", "content": _build_proactive_instruction()})

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": messages,
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        reply = (data["choices"][0]["message"].get("content") or "").strip()

        # LLM 选择不发送
        if not reply or reply.upper() == "NO":
            logger.debug("主动发言: LLM 选择不发送")
            return

        # 写入对话历史
        append_message(ADMIN_NUMBER, {"role": "assistant", "content": reply})

        # 发送消息（使用 send_chunked_raw，无需 event 对象）
        bot = get_bot()
        await send_chunked_raw(bot, "private", int(ADMIN_NUMBER), reply)
        logger.info(f"主动发言: 已发送 ({len(reply)} 字)")

    except Exception as e:
        logger.error(f"主动发言 LLM 调用失败: {e}")
