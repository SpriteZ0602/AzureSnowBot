"""真实异步交错：reset 后旧工具请求和排队请求不得继续执行。"""

import asyncio
import pytest
from unittest.mock import AsyncMock
from botcore.sessions import session_lock, invalidate, ensure_current, SessionInvalidated
from botcore.agent import run_agent


@pytest.mark.asyncio
async def test_reset_discards_inflight_model_and_queued_request():
    started, release = asyncio.Event(), asyncio.Event()
    lock = session_lock("group", "lifecycle", "default")
    dispatch = AsyncMock()

    async def model(*args, **kwargs):
        started.set()
        await release.wait()
        return {"choices": [{"message": {"tool_calls": [{"id": "one", "function": {"name": "probe", "arguments": "{}"}}]}}]}

    async def active():
        async with lock:
            await run_agent([], request=model, dispatch=dispatch,
                            tools=[{"function": {"name": "probe"}}])

    async def queued():
        async with lock:
            pytest.fail("old queued request must not run")

    one = asyncio.create_task(active())
    await started.wait()
    two = asyncio.create_task(queued())
    await asyncio.sleep(0)
    invalidate("group", "lifecycle", "default")
    release.set()
    results = await asyncio.gather(one, two, return_exceptions=True)
    assert all(isinstance(result, SessionInvalidated) for result in results)
    dispatch.assert_not_awaited()
    async with session_lock("group", "lifecycle", "default"):
        ensure_current()


@pytest.mark.asyncio
async def test_reset_does_not_invalidate_another_persona():
    async with session_lock("group", "lifecycle", "cat"):
        invalidate("group", "lifecycle", "default")
        ensure_current()
