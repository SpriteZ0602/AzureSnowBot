"""
心跳 + 主动发言引擎（私聊 & 群聊通用）
────────────────────────────────────
按会话 key 管理空闲计时器，到期后执行心跳：
  1. 加载该会话的完整上下文（私聊 = Admin 上下文文件；群聊 = 人格 prompt）
  2. 注入 HEARTBEAT.md 心跳指令，带完整工具链
  3. LLM 自主决定：
     - 调用工具（读写记忆、整理文件等）→ 用户无感
     - 发消息（私聊发给 Admin / 群聊发到群里）→ 写入历史并发送
     - 回复 HEARTBEAT_OK → 静默，用户无感

key 格式:
  - "private"      → Admin 私聊（目标 = ADMIN_NUMBER）
  - "group:<gid>"  → 指定群的主动发言

开关:
  - 私聊: data/admin/config.json 的 proactive_enabled（默认开）
  - 群聊: data/sessions/groups/<gid>/config.json 的 proactive_enabled（默认关）
"""

import asyncio
import json
import re
from pathlib import Path

from nonebot import get_bot, get_driver
from nonebot.log import logger

from .chunker import send_chunked_raw
from .llm import API_KEY
from .local_tools.manager import (
    get_openai_tools as local_openai_tools,
    handle_tool_call as local_handle_tool_call,
)
from .mcp.manager import (
    get_openai_tools as mcp_openai_tools,
    call_tool as mcp_call_tool,
    MAX_TOOL_ROUNDS,
)
from .skill.manager import (
    get_openai_tools as skill_openai_tools,
    handle_tool_call as skill_handle_tool_call,
    build_catalog_prompt as skill_catalog_prompt,
)

# ──────────────────── 配置 ────────────────────
config = get_driver().config
ADMIN_NUMBER: str = str(getattr(config, "admin_number", ""))
IDLE_SECONDS: int = int(getattr(config, "proactive_idle_seconds", 3600))

HEARTBEAT_OK = "HEARTBEAT_OK"
HEARTBEAT_PATH = Path("data/admin/HEARTBEAT.md")

# 模型内联在正文里的思考块（成对的 / 未闭合的）
_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?\s*>.*?</think(?:ing)?\s*>",
    re.DOTALL | re.IGNORECASE,
)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?\s*>", re.IGNORECASE)

# 心跳只保留最近的对话（避免过长历史淹没心跳指令）
HEARTBEAT_MAX_MESSAGES = 30


def _is_admin_private(chat_type: str, target_id: str) -> bool:
    """判断是否为 Admin 私聊会话"""
    return chat_type == "private" and target_id == ADMIN_NUMBER


def _build_heartbeat_instruction(chat_type: str) -> str:
    """构建心跳指令。私聊/群聊共用 HEARTBEAT.md，仅"发给谁"措辞不同。"""
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
    if chat_type == "group":
        target_hint = "主动在群里发言（继续之前的话题、关心群友、提醒重要事项等）— 直接输出要发到群里的消息内容"
    else:
        target_hint = "主动给对方发消息（继续之前的话题、关心他、提醒重要事项等）— 直接输出消息内容"

    parts.append(
        "【系统指令 — 心跳】\n"
        "距离你们上次对话已经过去了一段时间。请根据上面的心跳任务和你的记忆，决定要做什么。\n"
        "你可以：\n"
        f"1. 调用工具（读写记忆、整理文件等）— 执行完后如果不需要发消息就回复 HEARTBEAT_OK\n"
        f"2. {target_hint}\n"
        "3. 什么都不需要做 — 只回复 HEARTBEAT_OK（仅这个词，不要加其他内容）\n\n"
        "注意：不要编造不存在的事情。真没什么事就 HEARTBEAT_OK，不用强行找话聊。"
    )

    return "\n\n".join(parts)


def _format_pending_reminders() -> str:
    """获取待触发的提醒列表，格式化为心跳指令的一部分。"""
    try:
        from .reminder.scheduler import get_all_reminders
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


# ──────────────────── 计时器状态（按 key） ────────────────────
_idle_tasks: dict[str, asyncio.Task] = {}
_idle_deadlines: dict[str, float] = {}  # monotonic 时间戳，下次心跳触发时刻

# 对话期间最短延迟（秒），防止频繁聊天导致心跳永远不触发
MIN_DEFER_SECONDS = 600  # 10 分钟


def _now() -> float:
    return asyncio.get_event_loop().time()


# ──────────────────── 公共 API ────────────────────

def reset_idle_timer(key: str) -> None:
    """
    重置指定会话的空闲计时器（Bot 回复后 / 启动时调用）。

    策略: max(MIN_DEFER_SECONDS, 当前剩余时间)
    - 如果剩余时间 > 10 分钟，不改变 deadline（保持原定触发时间）
    - 如果剩余时间 < 10 分钟，延后到 10 分钟后（避免对话中途触发）
    - 如果没有计时器在跑，按完整 IDLE_SECONDS 启动
    """
    global _idle_tasks, _idle_deadlines

    now = _now()
    task = _idle_tasks.get(key)

    if task and not task.done():
        remaining = _idle_deadlines.get(key, 0.0) - now
        if remaining > MIN_DEFER_SECONDS:
            # 剩余时间充足，不需要重置
            logger.debug(f"心跳计时器保持不变 [{key}] (剩余 {remaining:.0f}s)")
            return
        # 剩余时间不足，延后到 MIN_DEFER_SECONDS
        task.cancel()
        delay = MIN_DEFER_SECONDS
    else:
        # 没有计时器在跑，按完整间隔启动
        delay = IDLE_SECONDS

    _idle_deadlines[key] = now + delay
    _idle_tasks[key] = asyncio.create_task(_idle_countdown(key, delay))
    logger.debug(f"心跳计时器已设置 [{key}] ({delay:.0f}s)")


def cancel_idle_timer(key: str) -> None:
    """取消指定会话的空闲计时器。"""
    task = _idle_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()
    _idle_deadlines.pop(key, None)


# ──────────────────── 内部实现 ────────────────────

async def _idle_countdown(key: str, delay: float) -> None:
    """等待指定时间后触发心跳。"""
    try:
        await asyncio.sleep(delay)
        await run_heartbeat_key(key)
        # 心跳完成后按完整间隔重新启动（不走 reset 的防抖逻辑）
        if not _key_still_active(key):
            # 开关已关闭（或从未开启），不再重启计时器
            _idle_tasks.pop(key, None)
            return
        _restart_full_timer(key)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"心跳异常 [{key}]: {e}")


def _key_still_active(key: str) -> bool:
    """检查该会话的主动对话开关是否仍为开启（决定是否重启计时器）"""
    try:
        chat_type, target_id = _parse_key(key)
        if chat_type == "private":
            from .chat.handler import get_proactive_enabled
            return get_proactive_enabled()
        from .persona.manager import get_group_proactive
        return get_group_proactive(target_id)
    except Exception:
        return True  # 读不到开关时保守重启（与原行为一致）


def _restart_full_timer(key: str) -> None:
    """心跳完成后，按完整 IDLE_SECONDS 重新启动计时器。"""
    now = _now()
    _idle_deadlines[key] = now + IDLE_SECONDS
    _idle_tasks[key] = asyncio.create_task(_idle_countdown(key, IDLE_SECONDS))
    logger.debug(f"心跳计时器已重启 [{key}] ({IDLE_SECONDS}s)")


def _parse_key(key: str) -> tuple[str, str]:
    """解析 key 为 (chat_type, target_id)。target_id 为字符串。"""
    if key == "private":
        return "private", ADMIN_NUMBER
    if key.startswith("group:"):
        return "group", key[len("group:"):]
    raise ValueError(f"未知的心跳 key: {key}")


async def run_heartbeat_key(key: str) -> None:
    """按 key 执行心跳。"""
    chat_type, target_id = _parse_key(key)
    await run_heartbeat(chat_type, target_id)


async def run_heartbeat(chat_type: str, target_id: str) -> None:
    """执行心跳：加载会话上下文 + 工具链，让 LLM 自主决定做什么。

    chat_type: "private" 或 "group"
    target_id: 私聊 = Admin QQ 号；群聊 = 群号
    """
    if not ADMIN_NUMBER or not API_KEY:
        return

    # ── 会话级上下文加载（延迟导入避免循环引用）──
    if chat_type == "private":
        from .chat.handler import (
            load_history,
            trim_history,
            append_message,
            get_config,
            load_admin_prompt,
            get_proactive_enabled,
        )
        if not get_proactive_enabled():
            return
        history = load_history(ADMIN_NUMBER)
        trimmed = trim_history(history)
        prompt = load_admin_prompt() or "你是一个有用的助手。"
        cfg = get_config(ADMIN_NUMBER)
        last = cfg.get("last_message_at", "")
        prompt += _runtime_context(chat_type="private", last_message_at=last)
        tool_ctx = {
            "_chat_type": "private",
            "_target_id": ADMIN_NUMBER,
            "_user_id": ADMIN_NUMBER,
            "_sender_name": "系统心跳",
        }
        local_tools = local_openai_tools()
    else:
        from .persona.manager import (
            get_active_persona, load_history, append_message,
            load_persona_prompt, get_group_config, get_group_proactive,
            load_group_memory,
        )
        from .group.utils import trim_history as group_trim

        if not get_group_proactive(target_id):
            return
        persona = get_active_persona(target_id)
        persona_prompt = load_persona_prompt(persona, target_id)
        if persona_prompt is None:
            logger.warning(f"群 {target_id} 心跳: 人格 {persona} 的 prompt 不存在，跳过")
            return

        # 注入本群长期记忆（与群聊对话一致）
        group_memory = load_group_memory(target_id)
        if group_memory:
            persona_prompt += "\n\n" + group_memory

        # 注意：这里不再检查 history 末条是不是 assistant。
        # 全量监听（/listen on）下所有群消息都会以 role=user 写入历史，
        # 末条几乎恒为 user，该检查会让群聊心跳永远触发不了。
        # 防止"Bot 说话时被打断"改由对话入口提前重置计时器来保证（见 handler）。
        history = load_history(target_id, persona)
        trimmed = group_trim(history, persona_prompt)

        prompt = persona_prompt
        catalog = skill_catalog_prompt(chat_type="group")
        if catalog:
            prompt += "\n" + catalog
        group_cfg = get_group_config(target_id)
        last = group_cfg.get("last_message_at", "")
        prompt += _runtime_context(chat_type="group", last_message_at=last)
        tool_ctx = {
            "_chat_type": "group",
            "_target_id": target_id,
            "_user_id": "",
            "_sender_name": "系统心跳",
        }
        local_tools = local_openai_tools(chat_type="group")

    heartbeat_history = trimmed[-HEARTBEAT_MAX_MESSAGES:]

    messages = [{"role": "system", "content": prompt}] + heartbeat_history
    # 追加心跳指令（用 user 角色确保 LLM 优先遵循，而非被对话历史带偏）
    messages.append({"role": "user", "content": _build_heartbeat_instruction(chat_type)})

    # 注入完整工具链（Skill + 本地 + MCP）
    openai_tools = skill_openai_tools() + local_tools + mcp_openai_tools()

    try:
        from .llm import call_llm
        for round_idx in range(MAX_TOOL_ROUNDS):
            data = await call_llm(messages, tools=openai_tools or None, source="heartbeat")
            choice = data["choices"][0]
            assistant_msg = choice["message"]

            tool_calls = assistant_msg.get("tool_calls")
            if not tool_calls:
                # 最终回复。先剥掉内联的思考块，避免把 <think> 标签发给用户
                # （真正要发的话，标签里的内容本来也不该出现）
                reply = _strip_thinking(assistant_msg.get("content") or "").strip()

                if _is_heartbeat_ok(reply):
                    logger.debug(f"心跳 [{chat_type}:{target_id}]: LLM 回复 HEARTBEAT_OK，静默")
                    return

                # LLM 有话要说：写入历史并发送
                if chat_type == "private":
                    append_message(ADMIN_NUMBER, {"role": "assistant", "content": reply})
                    bot = get_bot()
                    await send_chunked_raw(bot, "private", int(ADMIN_NUMBER), reply)
                else:
                    append_message(target_id, {"role": "assistant", "content": reply}, persona)
                    bot = get_bot()
                    await send_chunked_raw(bot, "group", int(target_id), reply)
                logger.info(f"心跳 [{chat_type}:{target_id}]: 已发送主动消息 ({len(reply)} 字)")
                return

            # 处理工具调用（LLM 在心跳中可以读写记忆等）
            messages.append(assistant_msg)
            logger.info(
                f"心跳 [{chat_type}:{target_id}] LLM 工具调用 (round {round_idx + 1}): "
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
                        fn_name, fn_args, context=tool_ctx
                    )
                    if local_result is not None:
                        tool_result = local_result
                    else:
                        tool_result = await mcp_call_tool(fn_name, fn_args)

                from .tool_log import log_tool_call
                log_tool_call("heartbeat", fn_name, fn_args, tool_result)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })

        # 超过最大工具轮次
        logger.warning(f"心跳 [{chat_type}:{target_id}]: 工具调用轮次达上限")

    except Exception as e:
        logger.error(f"心跳 [{chat_type}:{target_id}] LLM 调用失败: {e}")

    # ── 结构化蒸馏（增量，仅私聊：把新对话蒸馏进 memories.jsonl）──
    if chat_type == "private":
        try:
            from .chat.handler import (
                load_history as chat_load_history, _load_config, _save_config,
            )
            from .memory.structured import distill_memories

            all_history = chat_load_history(ADMIN_NUMBER)
            cfg = _load_config(ADMIN_NUMBER)
            last_line = cfg.get("last_distill_line", 0)

            if len(all_history) > last_line:
                new_messages = all_history[last_line:]
                text_parts: list[str] = []
                for msg in new_messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role == "user":
                        text_parts.append(f"用户: {content}")
                    elif role == "assistant":
                        text_parts.append(f"助手: {content}")
                if text_parts:
                    memories_path = Path("data/admin/memories.jsonl")
                    await distill_memories("\n".join(text_parts), memories_path)
                    cfg["last_distill_line"] = len(all_history)
                    _save_config(ADMIN_NUMBER, cfg)
        except Exception as e:
            logger.debug(f"心跳蒸馏跳过: {e}")


def _strip_thinking(text: str) -> str:
    """剥离模型内联在正文里的思考过程。

    部分模型（尤其推理型）会把思考过程直接写进 content，
    而不是单独的 reasoning_content 字段，表现为正文前面一大段分析、
    最后才给出结论。心跳判定前必须先剥掉，否则整段思考会被当成主动消息发出去。
    """
    if not text:
        return ""

    # 成对的 <think>...</think> / <thinking>...</thinking>
    text = _THINK_BLOCK_RE.sub("", text)

    # 未闭合的 <think>（输出被截断时常见）：从开标签处整段截掉
    opened = _THINK_OPEN_RE.search(text)
    if opened:
        text = text[: opened.start()]

    return text.strip()


def _is_heartbeat_ok(text: str) -> bool:
    """检查回复是否为 HEARTBEAT_OK 或无实质内容（不应发给用户）

    模型有两种"吐思考"的写法，都要覆盖：
      1. 带 <think> 标签 —— 由 _strip_thinking() 在调用前剥掉
      2. 不带标签，把思考直接混在正文里，最后才给结论 —— 本函数处理

    第 2 种没法靠结构化剥离，所以判定放宽为"整条消息里出现 HEARTBEAT_OK 就算静默"。
    宁可偶尔漏发一句，也不能把整段思考过程发到群里。
    """
    stripped = _strip_thinking(text).strip()
    if not stripped:
        return True

    upper = stripped.upper()
    if upper in (HEARTBEAT_OK, "NO"):
        return True

    # 思考在前、结论在后（如 "……分析了一通……所以没事 HEARTBEAT_OK"）
    if HEARTBEAT_OK in upper:
        return True

    return False


def _runtime_context(chat_type: str, last_message_at: str) -> str:
    """构建运行时上下文（延迟导入，避免循环引用）。
    心跳是一次性请求，无跨轮缓存诉求，时间行直接拼在 system 里。"""
    from .runtime_context import build_runtime_context, build_time_context
    return build_runtime_context(chat_type=chat_type) + "\n" + build_time_context(last_message_at)
