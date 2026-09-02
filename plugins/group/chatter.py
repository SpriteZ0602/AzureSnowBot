"""
群聊热闹插话
────────────
每条群聊都按「最近 5 分钟消息条数」算基础概率，指数插值：
  1 条 → 1%，10 条 → 50%，20 条 → 100%。
Bot 每回一次话，该群插话概率乘 0.5；5 分钟内 Bot 没再说话则恢复 1.0。
不需要 @。
"""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger

from ..chunker import chunk_text, send_chunked
from ..llm import API_KEY, call_llm
from ..persona.manager import (
    append_message,
    get_active_persona,
    get_auto_trigger,
    get_group_config,
    get_group_proactive,
    load_group_memory,
    load_history,
    load_persona_prompt,
)
from ..proactive import reset_idle_timer
from ..runtime_context import build_runtime_context
from .chatlog import load_chatlog
from .utils import get_session_lock, in_whitelist, is_at_bot, is_group_event, trim_history

WINDOW_SECONDS = 300          # 统计窗口：5 分钟
RESET_SECONDS = 300           # 5 分钟没说话则重置衰减
RECENT_LOG_LIMIT = 15
SKIP_REPLIES = {"SKIP", "HEARTBEAT_OK", "NO"}

# 5 分钟条数 → 基础概率锚点
CHIME_N1, CHIME_P1 = 1, 0.01
CHIME_N10, CHIME_P10 = 10, 0.50
CHIME_N20, CHIME_P20 = 20, 1.00

_CHIME_INSTRUCTION = (
    "【系统】群里最近比较热闹。"
    "你可以插一句很短的话接话，像群友一样，一两句就停。"
    "不要解释、不要总结全场、不要点名。"
    "如果没什么好接的，只回复 SKIP。"
)


@dataclass
class ChatterState:
    stamps: deque[float] = field(default_factory=deque)
    dampener: float = 1.0
    last_reply_at: float | None = None
    inflight: bool = False


_states: dict[str, ChatterState] = {}


def _get_state(group_id: str) -> ChatterState:
    state = _states.get(group_id)
    if state is None:
        state = ChatterState()
        _states[group_id] = state
    return state


def _exp_interp(n: float, n0: float, p0: float, n1: float, p1: float) -> float:
    if n <= n0:
        return p0
    if n >= n1:
        return p1
    t = (n - n0) / (n1 - n0)
    if p0 <= 0:
        return p1 * t
    return p0 * ((p1 / p0) ** t)


def chime_base_probability(count: int) -> float:
    """最近 5 分钟消息条数 → 插话基础概率。"""
    if count <= 0:
        return 0.0
    if count >= CHIME_N20:
        return CHIME_P20
    if count <= CHIME_N10:
        return _exp_interp(count, CHIME_N1, CHIME_P1, CHIME_N10, CHIME_P10)
    return _exp_interp(count, CHIME_N10, CHIME_P10, CHIME_N20, CHIME_P20)


def _maybe_reset_dampener(state: ChatterState, now: float) -> None:
    if state.last_reply_at is None or (now - state.last_reply_at) >= RESET_SECONDS:
        state.dampener = 1.0


def note_bot_reply(group_id: str, *, now: float | None = None) -> None:
    """Bot 在该群发过言：插话概率减半，并刷新 5 分钟重置计时。"""
    state = _get_state(str(group_id))
    ts = time.time() if now is None else now
    state.dampener *= 0.5
    state.last_reply_at = ts


def should_chime_in(
    group_id: str,
    *,
    now: float,
    rng: Callable[[], float],
    roll: bool = True,
) -> bool:
    """记录一条人类消息，再按 5 分钟频率 × 衰减掷骰。"""
    state = _get_state(str(group_id))
    _maybe_reset_dampener(state, now)
    cutoff = now - WINDOW_SECONDS
    while state.stamps and state.stamps[0] < cutoff:
        state.stamps.popleft()
    state.stamps.append(now)

    if not roll or state.inflight:
        return False

    p = min(1.0, chime_base_probability(len(state.stamps)) * state.dampener)
    return rng() < p


def reset_group(group_id: str) -> None:
    _states.pop(str(group_id), None)


def _is_skip_reply(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    return stripped.upper() in SKIP_REPLIES


async def _generate_chime(group_id: str) -> str:
    """用当前人格 + 近期群聊生成一句插话。失败或 SKIP 返回空串。"""
    if not API_KEY:
        return ""

    persona = get_active_persona(group_id)
    prompt = load_persona_prompt(persona, group_id)
    if not prompt:
        return ""

    group_memory = load_group_memory(group_id)
    if group_memory:
        prompt += "\n\n" + group_memory

    last = get_group_config(group_id).get("last_message_at", "")
    prompt += build_runtime_context(chat_type="group", last_message_at=last)

    entries = load_chatlog(group_id, hours=WINDOW_SECONDS / 3600, limit=RECENT_LOG_LIMIT)
    if entries:
        lines = [f"[{e.get('name', '?')}]: {e.get('text', '')}" for e in entries]
        recent = "最近群聊：\n" + "\n".join(lines)
    else:
        history = trim_history(load_history(group_id, persona), prompt)
        recent = ""
        if history:
            bits = []
            for msg in history[-RECENT_LOG_LIMIT:]:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user":
                    bits.append(content)
                elif role == "assistant":
                    bits.append(f"我: {content}")
            recent = "最近对话：\n" + "\n".join(bits)

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"{recent}\n\n{_CHIME_INSTRUCTION}".strip()},
    ]

    from ..proactive import _strip_thinking

    data = await call_llm(messages, source="group_chime")
    reply = _strip_thinking(data["choices"][0]["message"].get("content") or "").strip()
    if _is_skip_reply(reply):
        return ""
    return reply


_chatter = on_message(rule=is_group_event, priority=91, block=False)


@_chatter.handle()
async def handle_chatter(bot: Bot, event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not get_auto_trigger(str(event.group_id)):
        return
    if event.user_id == event.self_id:
        return
    text = event.get_plaintext().strip()
    if not text:
        return

    group_id = str(event.group_id)
    # @ 和指令也计入 5 分钟频率，但不掷骰插话（避免和主对话叠一句）
    roll = (not is_at_bot(event)) and (not text.startswith("/"))
    if not should_chime_in(group_id, now=time.time(), rng=random.random, roll=roll):
        return

    state = _get_state(group_id)
    state.inflight = True
    persona = get_active_persona(group_id)
    try:
        async with get_session_lock(group_id, persona):
            try:
                reply = await _generate_chime(group_id)
            except Exception as e:
                logger.warning(f"群 {group_id} 热闹插话失败: {e}")
                return
            if not reply:
                logger.debug(f"群 {group_id} 热闹插话：LLM 选择沉默")
                return

            append_message(group_id, {"role": "assistant", "content": reply}, persona)
            await send_chunked(bot, event, chunk_text(reply), reply_first=False)
            note_bot_reply(group_id)
            if get_group_proactive(group_id):
                reset_idle_timer(f"group:{group_id}")
            logger.info(f"群 {group_id} 热闹插话: {reply[:40]}")
    finally:
        state.inflight = False
