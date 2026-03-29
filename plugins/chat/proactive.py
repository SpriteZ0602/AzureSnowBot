"""
Admin 私聊心跳 + 主动发言
─────────────────────────
对话结束后启动空闲计时器，到期后执行心跳：
  1. 加载完整上下文（SOUL + AGENTS + USER + MEMORY + HEARTBEAT）
  2. 注入心跳指令，带完整工具链
  3. LLM 自主决定：
     - 调用工具（读写记忆、整理文件等）→ 用户无感
     - 发消息给用户（主动聊天、提醒）→ 写入历史并发送
     - 回复 HEARTBEAT_OK → 静默，用户无感

仅在管理员私聊中生效，计时器全局唯一。
"""

import asyncio
import json
from pathlib import Path

import httpx
from nonebot import get_bot, get_driver
from nonebot.log import logger

from ..chunker import chunk_text, send_chunked_raw
from ..llm import API_KEY, BASE_URL, MODEL
from ..local_tools.manager import (
    get_openai_tools as local_openai_tools,
    handle_tool_call as local_handle_tool_call,
)
from ..mcp.manager import (
    get_openai_tools as mcp_openai_tools,
    call_tool as mcp_call_tool,
    MAX_TOOL_ROUNDS,
)
from ..skill.manager import (
    get_openai_tools as skill_openai_tools,
    handle_tool_call as skill_handle_tool_call,
)

# ──────────────────── 配置 ────────────────────
config = get_driver().config
ADMIN_NUMBER: str = str(getattr(config, "admin_number", ""))
IDLE_SECONDS: int = int(getattr(config, "proactive_idle_seconds", 3600))

HEARTBEAT_OK = "HEARTBEAT_OK"
HEARTBEAT_PATH = Path("data/admin/HEARTBEAT.md")


def _build_heartbeat_instruction() -> str:
    """构建心跳指令。如果有 HEARTBEAT.md 就加载，否则用默认指令。"""
    parts: list[str] = []

    # 加载 HEARTBEAT.md（如果存在）
    if HEARTBEAT_PATH.exists():
        content = HEARTBEAT_PATH.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"# HEARTBEAT.md\n{content}")

    # 注入 pending reminders，防止心跳重复提醒已有定时器的事项
    pending_info = _format_pending_reminders()
    if pending_info:
        parts.append(pending_info)

    # 核心指令
    parts.append(
        "【系统指令 — 心跳】\n"
        "距离你们上次对话已经过去了一段时间。请根据上面的心跳任务和你的记忆，决定要做什么。\n"
        "你可以：\n"
        "1. 调用工具（读写记忆、整理文件等）— 执行完后如果不需要发消息就回复 HEARTBEAT_OK\n"
        "2. 主动给碧碧发消息（继续之前的话题、关心他、提醒重要事项等）— 直接输出消息内容\n"
        "3. 什么都不需要做 — 只回复 HEARTBEAT_OK（仅这个词，不要加其他内容）\n\n"
        "注意：不要编造不存在的事情。真没什么事就 HEARTBEAT_OK，不用强行找话聊。"
    )

    return "\n\n".join(parts)


def _format_pending_reminders() -> str:
    """获取待触发的提醒列表，格式化为心跳指令的一部分。"""
    try:
        from ..reminder.scheduler import get_all_reminders
        jobs = get_all_reminders()
    except Exception:
        return ""

    if not jobs:
        return ""

    lines = ["【已设置的定时提醒 — 不要重复提醒这些事项】"]
    for job in jobs:
        if job.recurring == "daily":
            lines.append(f"- 每天 {job.daily_time} 提醒{job.creator_name}：{job.message}")
        else:
            lines.append(f"- {job.fire_at} 提醒{job.creator_name}：{job.message}")
    lines.append("以上事项已有定时器会自动提醒，你不需要也不应该提前或重复提醒。")
    return "\n".join(lines)


# ──────────────────── 计时器状态 ────────────────────
_idle_task: asyncio.Task | None = None
_idle_deadline: float = 0.0  # monotonic 时间戳，下次心跳触发时刻

# 对话期间最短延迟（秒），防止频繁聊天导致心跳永远不触发
MIN_DEFER_SECONDS = 600  # 10 分钟


# ──────────────────── 公共 API ────────────────────

def reset_idle_timer() -> None:
    """
    重置空闲计时器（在 Bot 回复 admin 后 / 启动时调用）。

    策略: max(MIN_DEFER_SECONDS, 当前剩余时间)
    - 如果剩余时间 > 10 分钟，不改变 deadline（保持原定触发时间）
    - 如果剩余时间 < 10 分钟，延后到 10 分钟后（避免对话中途触发）
    - 如果没有计时器在跑，按完整 IDLE_SECONDS 启动
    """
    global _idle_task, _idle_deadline

    now = asyncio.get_event_loop().time()

    if _idle_task and not _idle_task.done():
        remaining = _idle_deadline - now
        if remaining > MIN_DEFER_SECONDS:
            # 剩余时间充足，不需要重置
            logger.debug(f"心跳计时器保持不变 (剩余 {remaining:.0f}s)")
            return
        # 剩余时间不足，延后到 MIN_DEFER_SECONDS
        _idle_task.cancel()
        delay = MIN_DEFER_SECONDS
    else:
        # 没有计时器在跑，按完整间隔启动
        delay = IDLE_SECONDS

    _idle_deadline = now + delay
    _idle_task = asyncio.create_task(_idle_countdown(delay))
    logger.debug(f"心跳计时器已设置 ({delay:.0f}s)")


def cancel_idle_timer() -> None:
    """取消空闲计时器。"""
    global _idle_task, _idle_deadline
    if _idle_task and not _idle_task.done():
        _idle_task.cancel()
    _idle_task = None
    _idle_deadline = 0.0


# ──────────────────── 内部实现 ────────────────────

async def _idle_countdown(delay: float | None = None) -> None:
    """等待指定时间后触发心跳。"""
    try:
        await asyncio.sleep(delay if delay is not None else IDLE_SECONDS)
        await _try_heartbeat()
        # 心跳完成后按完整间隔重新启动（不走 reset 的防抖逻辑）
        _restart_full_timer()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"心跳异常: {e}")


def _restart_full_timer() -> None:
    """心跳完成后，按完整 IDLE_SECONDS 重新启动计时器。"""
    global _idle_task, _idle_deadline
    now = asyncio.get_event_loop().time()
    _idle_deadline = now + IDLE_SECONDS
    _idle_task = asyncio.create_task(_idle_countdown(IDLE_SECONDS))
    logger.debug(f"心跳计时器已重启 ({IDLE_SECONDS}s)")


def _is_heartbeat_ok(text: str) -> bool:
    """检查回复是否为 HEARTBEAT_OK 或无实质内容的短回复（不应发给用户）"""
    stripped = text.strip().upper()
    # 兼容 "HEARTBEAT_OK"、"NO"、纯空
    if stripped in (HEARTBEAT_OK, "NO", ""):
        return True
    # 过短的回复大概率是 LLM 延续对话惯性，不是有意义的主动消息
    if len(stripped) <= 10:
        return True
    return False


async def _try_heartbeat() -> None:
    """执行心跳：加载完整上下文 + 工具链，让 LLM 自主决定做什么。"""
    # 延迟导入，避免循环引用
    from .handler import (
        load_history,
        trim_history,
        append_message,
        get_config,
        load_admin_prompt,
    )
    from ..runtime_context import build_runtime_context

    if not ADMIN_NUMBER or not API_KEY:
        return

    history = load_history(ADMIN_NUMBER)

    # 如果有历史，最后一条不是 assistant 说明对话状态异常，跳过
    if history and history[-1].get("role") != "assistant":
        return

    trimmed = trim_history(history)

    # 心跳只保留最近的对话（避免过长历史淹没心跳指令）
    HEARTBEAT_MAX_MESSAGES = 30
    heartbeat_history = trimmed[-HEARTBEAT_MAX_MESSAGES:]

    # 组装完整 system prompt（与正常对话一致）
    prompt = load_admin_prompt() or "你是一个有用的助手。"
    cfg = get_config(ADMIN_NUMBER)
    last = cfg.get("last_message_at", "")
    prompt += build_runtime_context(chat_type="private", last_message_at=last)

    messages = [{"role": "system", "content": prompt}] + heartbeat_history
    # 追加心跳指令（用 user 角色确保 LLM 优先遵循，而非被对话历史带偏）
    messages.append({"role": "user", "content": _build_heartbeat_instruction()})

    # DEBUG: 打印组装好的完整 prompt
    logger.debug("=== 心跳 Prompt 开始 ===")
    for i, m in enumerate(messages):
        logger.debug(f"[{i}] {m['role']}:\n{m.get('content', '')}")
    logger.debug(f"=== 心跳 Prompt 结束 (共 {len(messages)} 条) ===")

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": MODEL,
        "messages": messages,
    }

    # 注入完整工具链（Skill + 本地 + MCP）
    openai_tools = skill_openai_tools() + local_openai_tools() + mcp_openai_tools()
    if openai_tools:
        payload["tools"] = openai_tools

    # 工具调用上下文
    _tool_context = {
        "_chat_type": "private",
        "_target_id": ADMIN_NUMBER,
        "_user_id": ADMIN_NUMBER,
        "_sender_name": "系统心跳",
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for round_idx in range(MAX_TOOL_ROUNDS):
                resp = await client.post(
                    f"{BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                assistant_msg = choice["message"]

                tool_calls = assistant_msg.get("tool_calls")
                if not tool_calls:
                    # 最终回复
                    reply = (assistant_msg.get("content") or "").strip()

                    if _is_heartbeat_ok(reply):
                        logger.debug("心跳: LLM 回复 HEARTBEAT_OK，静默")
                        return

                    # LLM 有话要发给用户
                    append_message(ADMIN_NUMBER, {"role": "assistant", "content": reply})
                    bot = get_bot()
                    await send_chunked_raw(bot, "private", int(ADMIN_NUMBER), reply)
                    logger.info(f"心跳: 已发送主动消息 ({len(reply)} 字)")
                    return

                # 处理工具调用（LLM 在心跳中可以读写记忆等）
                messages.append(assistant_msg)
                logger.info(
                    f"心跳 LLM 工具调用 (round {round_idx + 1}): "
                    f"{[tc['function']['name'] for tc in tool_calls]}"
                )

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}

                    # 分发链路：Skill → 本地工具 → MCP
                    skill_result = skill_handle_tool_call(fn_name, fn_args)
                    if skill_result is not None:
                        tool_result = skill_result
                    else:
                        local_result = await local_handle_tool_call(
                            fn_name, fn_args, context=_tool_context
                        )
                        if local_result is not None:
                            tool_result = local_result
                        else:
                            tool_result = await mcp_call_tool(fn_name, fn_args)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })

                payload["messages"] = messages

        # 超过最大工具轮次
        logger.warning("心跳: 工具调用轮次达上限")

    except Exception as e:
        logger.error(f"心跳 LLM 调用失败: {e}")

