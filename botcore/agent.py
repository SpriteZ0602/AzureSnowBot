"""渠道无关的工具循环：模型请求、参数验证、轮次上限及最终总结。"""

import json
from .tools import ToolResult, tool_result
from .sessions import ensure_current


async def run_agent(messages, *, request, dispatch, tools=None, source="unknown", max_rounds=10,
                    on_prelude=None, on_tool=None, request_options=None):
    options = request_options or {}
    allowed = {t["function"]["name"] for t in tools or []}
    for _ in range(max_rounds):
        ensure_current()
        data = await request(messages, tools=tools or None, source=source, **options)
        ensure_current()
        assistant = data["choices"][0]["message"]
        calls = assistant.get("tool_calls")
        if not calls:
            return assistant
        if on_prelude:
            await on_prelude(assistant)
        messages.append(assistant)
        for call in calls:
            name = call["function"]["name"]
            arguments = {}
            try:
                arguments = json.loads(call["function"].get("arguments", "{}"))
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数必须是 JSON 对象")
                if name not in allowed:
                    raise ValueError("本轮未开放此工具")
            except (ValueError, TypeError) as exc:
                result = ToolResult(f"[错误] {exc}", ok=False)
            else:
                ensure_current()
                result = tool_result(await dispatch(name, arguments))
                ensure_current()
            if on_tool:
                await on_tool(name, arguments, result)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
    messages.append({"role": "user", "content": "你已经无法再调用工具了，请根据上面已经获取到的信息，直接回答用户的问题。"})
    data = await request(messages, source=source, **options)
    ensure_current()
    return data["choices"][0]["message"]
