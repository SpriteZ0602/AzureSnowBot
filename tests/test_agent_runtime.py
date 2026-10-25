"""共用工具循环的真实行为，不依赖 NoneBot 或 sys.modules 桩。"""

from unittest.mock import AsyncMock
import pytest
from botcore.agent import run_agent
from botcore.tools import ToolResult

TOOLS = [{"type": "function", "function": {"name": "local__probe"}}]


def response(*, arguments="{}", name="local__probe", text=None):
    message = {"role": "assistant", "content": text}
    if text is None:
        message["tool_calls"] = [{"id": "one", "function": {"name": name, "arguments": arguments}}]
    return {"choices": [{"message": message}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["group", "chat", "heartbeat", "task", "sub_agent"])
async def test_all_sources_share_tool_protocol(source):
    request = AsyncMock(side_effect=[response(), response(text="done")])
    dispatch = AsyncMock(return_value=ToolResult("actual-result"))
    messages = [{"role": "user", "content": "request"}]
    result = await run_agent(messages, request=request, dispatch=dispatch, tools=TOOLS, source=source)
    assert result["content"] == "done"
    dispatch.assert_awaited_once_with("local__probe", {})
    assert messages[-1] == {"role": "tool", "tool_call_id": "one", "content": "actual-result"}
    assert all(call.kwargs["source"] == source for call in request.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,name", [("[]", "local__probe"), ("broken", "local__probe"), ("{}", "local__hidden")])
async def test_invalid_or_unavailable_tool_is_not_executed(arguments, name):
    request = AsyncMock(side_effect=[response(arguments=arguments, name=name), response(text="done")])
    dispatch = AsyncMock()
    messages = []
    await run_agent(messages, request=request, dispatch=dispatch, tools=TOOLS)
    dispatch.assert_not_awaited()
    assert "[错误]" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_budget_exhaustion_summarizes_without_tools_and_preserves_format():
    request = AsyncMock(side_effect=[response(), response(text="done")])
    await run_agent([], request=request, dispatch=AsyncMock(return_value="ok"), tools=TOOLS,
                    max_rounds=1, request_options={"response_format": {"type": "json_object"}})
    assert "tools" not in request.await_args.kwargs
    assert request.await_args.kwargs["response_format"] == {"type": "json_object"}
