"""
群聊对话处理
──────────
@Bot 触发的群聊消息处理，包含 agentic loop（支持 MCP 工具调用）。
"""

import json
from datetime import datetime

import httpx
from nonebot import on_message, get_bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.log import logger

from ..runtime_context import build_runtime_context
from ..persona.manager import (
    get_active_persona, load_persona_prompt,
    load_history, append_message, get_group_config,
)
from ..mcp.manager import get_openai_tools, call_tool, MAX_TOOL_ROUNDS
from ..skill.manager import (
    build_catalog_prompt as skill_catalog_prompt,
    get_openai_tools as skill_openai_tools,
    handle_tool_call as skill_handle_tool_call,
)
from ..local_tools.manager import (
    get_openai_tools as local_openai_tools,
    handle_tool_call as local_handle_tool_call,
)
from ..llm import API_KEY as OPENAI_API_KEY, BASE_URL as OPENAI_BASE_URL, MODEL as OPENAI_MODEL
from .utils import (
    in_whitelist, is_at_bot, extract_text,
    get_reply_id, fetch_quoted_text, fetch_quoted_image_urls, trim_history,
)
from ..chunker import chunk_text, send_chunked

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

    # 检查 /withoutChunking 标记
    no_chunk = False
    if user_input.startswith("/withoutChunking"):
        no_chunk = True
        user_input = user_input[len("/withoutChunking"):].strip()
        if not user_input:
            return

    if not OPENAI_API_KEY:
        await group_chat.finish("未配置 OpenAI API Key，请联系管理员。")

    # 检查是否引用了消息
    quoted_text = ""
    quoted_image_urls: list[str] = []
    reply_id = get_reply_id(event)
    if reply_id:
        bot = get_bot()
        quoted_text = await fetch_quoted_text(bot, reply_id)
        quoted_image_urls = await fetch_quoted_image_urls(bot, reply_id)

    group_id = str(event.group_id)
    sender = event.sender.nickname or str(event.user_id)

    # 获取当前人格与 prompt
    active_persona = get_active_persona(group_id)
    system_prompt = load_persona_prompt(active_persona, group_id)
    if system_prompt is None:
        logger.warning(f"群 {group_id} 人格 {active_persona} 的 prompt 文件不存在，跳过响应")
        return

    # 注入 Skill 目录（Level 1 渐进式披露）
    skill_catalog = skill_catalog_prompt(chat_type="group")
    if skill_catalog:
        system_prompt += "\n" + skill_catalog

    # 注入运行时上下文
    group_cfg = get_group_config(group_id)
    last = group_cfg.get("last_message_at", "")
    system_prompt += build_runtime_context(chat_type="group", last_message_at=last)

    # 构建工具调用上下文（供需要环境信息的工具使用，如定时提醒）
    _tool_context = {
        "_chat_type": "group",
        "_target_id": group_id,
        "_user_id": str(event.user_id),
        "_sender_name": sender,
    }

    # 组装用户消息（带发送者标识 + 引用内容）
    if quoted_text:
        content_text = f"[{sender}] (引用了一条消息: \"{quoted_text}\"): {user_input}"
    else:
        content_text = f"[{sender}]: {user_input}"

    # 历史记录始终用纯文本
    user_msg = {"role": "user", "content": content_text}
    append_message(group_id, user_msg, active_persona)

    # 如果引用的消息包含图片，构建多模态 content（仅用于 LLM 请求，不存历史）
    if quoted_image_urls:
        multimodal_content: list[dict] = [{"type": "text", "text": content_text}]
        for img_url in quoted_image_urls:
            multimodal_content.append({
                "type": "image_url",
                "image_url": {"url": img_url},
            })
        llm_user_msg = {"role": "user", "content": multimodal_content}
    else:
        llm_user_msg = user_msg

    # 加载历史并截断
    history = load_history(group_id, active_persona)
    trimmed = trim_history(history, system_prompt)

    # 组装 messages：最后一条用 LLM 版本（可能含图片），其余用纯文本
    messages = [{'role': 'system', 'content': system_prompt}] + trimmed[:-1]
    if trimmed:
        # 如果最后一条是刚发的用户消息且有图片，替换为多模态版本
        if quoted_image_urls and trimmed[-1].get('content') == content_text:
            messages.append(llm_user_msg)
        else:
            messages.append(trimmed[-1])
    elif quoted_image_urls:
        messages.append(llm_user_msg)

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
    }

    # 合并 MCP 工具 + Skill 工具 + 本地工具（群聊过滤 admin_only 工具）
    openai_tools = get_openai_tools() + skill_openai_tools() + local_openai_tools(chat_type="group")
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
                from ..token_stats import record_usage
                record_usage("group", data.get("usage"))
                choice = data["choices"][0]
                assistant_msg = choice["message"]

                tool_calls = assistant_msg.get("tool_calls")
                if not tool_calls:
                    reply = (assistant_msg.get("content") or "").strip()
                    if reply:
                        append_message(group_id, {"role": "assistant", "content": reply}, active_persona)
                        bot = get_bot()
                        if no_chunk:
                            await bot.send_group_msg(
                                group_id=event.group_id,
                                message=MessageSegment.reply(event.message_id) + reply,
                            )
                        else:
                            chunks = chunk_text(reply)
                            await send_chunked(bot, event, chunks)
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

                    # 分发链路：Skill → 本地工具 → MCP
                    skill_result = skill_handle_tool_call(fn_name, fn_args)
                    if skill_result is not None:
                        tool_result = skill_result
                    else:
                        local_result = await local_handle_tool_call(fn_name, fn_args, context=_tool_context)
                        if local_result is not None:
                            tool_result = local_result
                        else:
                            tool_result = await call_tool(fn_name, fn_args)

                    from ..tool_log import log_tool_call
                    log_tool_call("group", fn_name, fn_args, tool_result,
                                  user_id=str(event.user_id), group_id=group_id)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })

            # 超过最大轮次
            reply = "（工具调用轮次已达上限，请重新提问）"
            append_message(group_id, {"role": "assistant", "content": reply}, active_persona)
            bot = get_bot()
            await send_chunked(bot, event, [reply])
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenAI API 错误: {e.response.status_code} {e.response.text}")
        await group_chat.finish(f"API 请求失败 ({e.response.status_code})")
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"群聊 ChatGPT 插件异常: {e}")
        await group_chat.finish("请求出错，请稍后再试。")
