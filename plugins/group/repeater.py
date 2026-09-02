"""
群聊复读
────────
白名单群里连续相同的纯文本（来自不同成员）时，按复读句数掷骰决定是否跟一句。
不走 LLM、不需要 @。同一轮复读只跟一次，避免自己把自己复读起来。

概率（指数）：2 句 10%，6 句 100%。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger

from ..persona.manager import get_auto_trigger
from .utils import in_whitelist, is_at_bot, is_group_event

# 太长的内容不当复读（防止跟超长复制粘贴）
MAX_REPEAT_LEN = 100

# 复读句数 → 概率锚点（指数插值）
REPEAT_N_LOW = 2
REPEAT_P_LOW = 0.10
REPEAT_N_HIGH = 6
REPEAT_P_HIGH = 1.00


@dataclass
class RepeatState:
    last_text: str = ""
    last_uid: str = ""
    count: int = 0
    already_repeated: bool = False


_states: dict[str, RepeatState] = {}


def _get_state(group_id: str) -> RepeatState:
    state = _states.get(group_id)
    if state is None:
        state = RepeatState()
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


def repeat_probability(count: int) -> float:
    """连续相同句数 → 跟读概率。未满 2 句为 0。"""
    if count < REPEAT_N_LOW:
        return 0.0
    return min(1.0, _exp_interp(
        count, REPEAT_N_LOW, REPEAT_P_LOW, REPEAT_N_HIGH, REPEAT_P_HIGH,
    ))


def should_repeat(
    group_id: str,
    user_id: str,
    text: str,
    self_id: str,
    *,
    rng: Callable[[], float] = random.random,
) -> bool:
    """根据一条新消息判断是否该复读。群状态写在模块内，便于单测。"""
    text = (text or "").strip()
    user_id = str(user_id)
    self_id = str(self_id)
    state = _get_state(str(group_id))

    # Bot 自己的消息：不触发，也不刷新「上一条」以免把自己当成人
    if user_id == self_id:
        return False

    if not text or text.startswith("/") or len(text) > MAX_REPEAT_LEN:
        state.last_text = ""
        state.last_uid = ""
        state.count = 0
        state.already_repeated = False
        return False

    if text == state.last_text and user_id != state.last_uid:
        state.last_uid = user_id
        state.count += 1
        if state.already_repeated:
            return False
        p = repeat_probability(state.count)
        if p > 0 and rng() < p:
            state.already_repeated = True
            return True
        return False

    if text != state.last_text:
        state.last_text = text
        state.last_uid = user_id
        state.count = 1
        state.already_repeated = False

    return False


def reset_group(group_id: str) -> None:
    """测试 / 调试用：清空某群复读状态。"""
    _states.pop(str(group_id), None)


_repeater = on_message(rule=is_group_event, priority=90, block=False)


@_repeater.handle()
async def handle_repeat(bot: Bot, event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not get_auto_trigger(str(event.group_id)):
        return
    if is_at_bot(event):
        return
    if event.user_id == event.self_id:
        return

    text = event.get_plaintext().strip()
    if should_repeat(str(event.group_id), str(event.user_id), text, str(event.self_id)):
        logger.info(f"群 {event.group_id} 复读: {text[:40]}")
        await bot.send_group_msg(group_id=event.group_id, message=text)
        from .chatter import note_bot_reply
        note_bot_reply(str(event.group_id))
