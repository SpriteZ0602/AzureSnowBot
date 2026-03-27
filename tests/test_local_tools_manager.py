"""
tests/test_local_tools_manager.py
──────────────────────────────────
测试本地工具管理器的核心功能:
  - admin_only 工具过滤（get_openai_tools / list_tools_summary）
  - handle_tool_call 分发逻辑
  - register_tool 装饰器
"""

import sys
import os
import types
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock nonebot
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

import pytest
from plugins.local_tools.manager import (
    _registry,
    register_tool,
    get_openai_tools,
    list_tools_summary,
    handle_tool_call,
    TOOL_PREFIX,
    LocalTool,
)


# ──────────────────── Fixtures ────────────────────

@pytest.fixture(autouse=True)
def _clean_registry():
    """每个测试前保存注册表，测试后恢复"""
    saved = dict(_registry)
    yield
    _registry.clear()
    _registry.update(saved)


def _register_test_tools():
    """注册一个普通工具和一个 admin_only 工具用于测试"""
    @register_tool(name="test_public", description="公共工具")
    async def _public(**kwargs):
        return "public_result"

    @register_tool(name="test_admin", description="管理员工具", admin_only=True)
    async def _admin(**kwargs):
        return "admin_result"


# ──────────────────── register_tool ────────────────────

class TestRegisterTool:
    """装饰器注册行为"""

    def test_registers_in_registry(self):
        @register_tool(name="reg_test", description="测试")
        async def fn(**kw):
            return "ok"

        assert "reg_test" in _registry
        assert _registry["reg_test"].description == "测试"
        assert _registry["reg_test"].admin_only is False

    def test_admin_only_flag(self):
        @register_tool(name="reg_admin", description="管理员", admin_only=True)
        async def fn(**kw):
            return "ok"

        assert _registry["reg_admin"].admin_only is True

    def test_default_parameters(self):
        @register_tool(name="reg_default_param", description="无参数")
        async def fn(**kw):
            return "ok"

        tool = _registry["reg_default_param"]
        assert tool.parameters["type"] == "object"
        assert tool.parameters["properties"] == {}


# ──────────────────── get_openai_tools（admin_only 过滤） ────────────────────

class TestGetOpenaiTools:
    """admin_only 过滤行为"""

    def test_private_sees_all(self):
        _register_test_tools()
        tools = get_openai_tools(chat_type="private")
        names = [t["function"]["name"] for t in tools]
        assert f"{TOOL_PREFIX}__test_public" in names
        assert f"{TOOL_PREFIX}__test_admin" in names

    def test_group_hides_admin_only(self):
        _register_test_tools()
        tools = get_openai_tools(chat_type="group")
        names = [t["function"]["name"] for t in tools]
        assert f"{TOOL_PREFIX}__test_public" in names
        assert f"{TOOL_PREFIX}__test_admin" not in names

    def test_default_is_private(self):
        _register_test_tools()
        tools_default = get_openai_tools()
        tools_private = get_openai_tools(chat_type="private")
        assert len(tools_default) == len(tools_private)

    def test_openai_format(self):
        _register_test_tools()
        tools = get_openai_tools(chat_type="private")
        for t in tools:
            assert t["type"] == "function"
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert "parameters" in t["function"]


# ──────────────────── list_tools_summary ────────────────────

class TestListToolsSummary:

    def test_private_includes_admin_tools(self):
        _register_test_tools()
        lines = list_tools_summary(chat_type="private")
        text = "\n".join(lines)
        assert "test_public" in text
        assert "test_admin" in text

    def test_group_excludes_admin_tools(self):
        _register_test_tools()
        lines = list_tools_summary(chat_type="group")
        text = "\n".join(lines)
        assert "test_public" in text
        assert "test_admin" not in text


# ──────────────────── handle_tool_call ────────────────────

class TestHandleToolCall:

    @pytest.mark.asyncio
    async def test_dispatch_known_tool(self):
        @register_tool(name="dispatch_test", description="分发测试")
        async def fn(**kw):
            return "dispatch_ok"

        result = await handle_tool_call(f"{TOOL_PREFIX}__dispatch_test", {})
        assert result == "dispatch_ok"

    @pytest.mark.asyncio
    async def test_dispatch_unknown_prefix(self):
        """不属于本地工具前缀，返回 None"""
        result = await handle_tool_call("mcp__some_tool", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_dispatch_nonexistent_tool(self):
        result = await handle_tool_call(f"{TOOL_PREFIX}__nonexistent_xyz", {})
        assert result is not None
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_context_passed_to_tool(self):
        received = {}

        @register_tool(name="ctx_test", description="上下文测试")
        async def fn(_context=None, **kw):
            received.update(_context or {})
            return "ok"

        ctx = {"_chat_type": "private", "_target_id": "123"}
        await handle_tool_call(f"{TOOL_PREFIX}__ctx_test", {}, context=ctx)
        assert received["_chat_type"] == "private"
        assert received["_target_id"] == "123"

    @pytest.mark.asyncio
    async def test_tool_error_returns_message(self):
        @register_tool(name="err_test", description="错误测试")
        async def fn(**kw):
            raise ValueError("炸了")

        result = await handle_tool_call(f"{TOOL_PREFIX}__err_test", {})
        assert "工具调用出错" in result
        assert "炸了" in result

    @pytest.mark.asyncio
    async def test_sync_tool_supported(self):
        @register_tool(name="sync_test", description="同步工具")
        def fn(**kw):
            return "sync_ok"

        result = await handle_tool_call(f"{TOOL_PREFIX}__sync_test", {})
        assert result == "sync_ok"
