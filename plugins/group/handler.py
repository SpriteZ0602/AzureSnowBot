"""
群聊对话处理
──────────
@Bot 触发的群聊消息处理，包含 agentic loop（支持 MCP 工具调用）。
"""

import json

import httpx
from nonebot import on_message, get_bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.log import logger

from ..persona.manager import (
    get_active_persona, load_persona_prompt,
    load_history, append_message,
)
from ..mcp.manager import get_openai_tools, call_tool, MAX_TOOL_ROUNDS
from ..skill.manager import (
    build_catalog_prompt as skill_catalog_prompt,
    get_openai_tools as skill_openai_tools,
    handle_tool_call as skill_handle_tool_call,
)
from .utils import (
    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL,
    in_whitelist, is_at_bot, extract_text,
    get_reply_id, fetch_quoted_text, trim_history,
)

# ──────────────────── 群聊对话处理 ────────────────────
group_chat = on_message(priority=98, block=False)


@group_chat.handle()
async def handle_group_chat(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return

    user_input = extract_text(event)
    if not user_input:
        return

    if not OPENAI_API_KEY:
        await group_chat.finish("未配置 OpenAI API Key，请联系管理员。")

    # 检查是否引用了消息
    quoted_text = ""
    reply_id = get_reply_id(event)
    if reply_id:
        bot = get_bot()
        quoted_text = await fetch_quoted_text(bot, reply_id)

    group_id = str(event.group_id)
    sender = event.sender.nickname or str(event.user_id)

    # 获取当前人格与 prompt
    active_persona = get_active_persona(group_id)
    system_prompt = load_persona_prompt(active_persona, group_id)
    if system_prompt is None:
        logger.warning(f"群 {group_id} 人格 {active_persona} 的 prompt 文件不存在，跳过响应")
        return

    # 注入 Skill 目录（Level 1 渐进式披露）
    skill_catalog = skill_catalog_prompt()
    if skill_catalog:
        system_prompt += "\n" + skill_catalog

    # 组装用户消息（带发送者标识 + 引用内容）
    if quoted_text:
        content = f"[{sender}] (引用了一条消息: \"{quoted_text}\"): {user_input}"
    else:
        content = f"[{sender}]: {user_input}"
    user_msg = {"role": "user", "content": content}
    append_message(group_id, user_msg, active_persona)

    # 加载历史并截断
    history = load_history(group_id, active_persona)
    trimmed = trim_history(history, system_prompt)

    messages = [{"role": "system", "content": system_prompt}] + trimmed

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
    }

    # 合并 MCP 工具 + Skill 工具
    openai_tools = get_openai_tools() + skill_openai_tools()
    if openai_tools:
        payload["tools"] = openai_tools

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            # ── Agentic Loop ──
            for round_idx in range(MAX_TOOL_ROUNDS):
                resp = await client.post(
                    f"{OPENAI_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                assistant_msg = choice["message"]

                tool_calls = assistant_msg.get("tool_calls")
                if not tool_calls:
                    reply = (assistant_msg.get("content") or "").strip()
                    if reply:
                        append_message(group_id, {"role": "assistant", "content": reply}, active_persona)
                        await group_chat.finish(
                            MessageSegment.reply(event.message_id) + reply
                        )
                    return

                messages.append(assistant_msg)
                logger.info(f"LLM 请求工具调用 (round {round_idx + 1}): "
                            f"{[tc['function']['name'] for tc in tool_calls]}")

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}

                    # 优先尝试 Skill 本地工具，再走 MCP
                    skill_result = skill_handle_tool_call(fn_name, fn_args)
                    if skill_result is not None:
                        tool_result = skill_result
                    else:
                        tool_result = await call_tool(fn_name, fn_args)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })

                payload["messages"] = messages

            # 超过最大轮次
            reply = "（工具调用轮次已达上限，请重新提问）"
            append_message(group_id, {"role": "assistant", "content": reply}, active_persona)
            await group_chat.finish(
                MessageSegment.reply(event.message_id) + reply
            )
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenAI API 错误: {e.response.status_code} {e.response.text}")
        await group_chat.finish(f"API 请求失败 ({e.response.status_code})")
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"群聊 ChatGPT 插件异常: {e}")
        await group_chat.finish("请求出错，请稍后再试。")
