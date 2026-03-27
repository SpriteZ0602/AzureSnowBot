"""
proactive 心跳 + 主动发言模块单元测试
──────────────────────────────────────
测试空闲计时器的重置/取消逻辑 + 心跳 LLM 决策分支。
"""

import sys
import os
import types
import json
import asyncio
import importlib
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# ── 设置路径 & mock NoneBot ──
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.exception", MagicMock())
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

# mock nonebot.get_driver / get_bot
_mock_config = MagicMock()
_mock_config.admin_number = "373900859"
_mock_config.proactive_idle_seconds = 1  # 1 秒用于测试
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)
sys.modules["nonebot"].get_bot = MagicMock()

# ── 构造 plugins / plugins.chat 包 ──
_plugins_pkg = types.ModuleType("plugins")
_plugins_pkg.__path__ = [str(ROOT / "plugins")]
_plugins_pkg.__package__ = "plugins"
sys.modules["plugins"] = _plugins_pkg

_chat_pkg = types.ModuleType("plugins.chat")
_chat_pkg.__path__ = [str(ROOT / "plugins" / "chat")]
_chat_pkg.__package__ = "plugins.chat"
sys.modules["plugins.chat"] = _chat_pkg

# mock plugins.chunker
_mock_chunker = types.ModuleType("plugins.chunker")
_mock_chunker.chunk_text = lambda text: [text] if text else []
_mock_chunker.send_chunked_raw = AsyncMock()
sys.modules["plugins.chunker"] = _mock_chunker

# mock plugins.llm
_mock_llm = types.ModuleType("plugins.llm")
_mock_llm.API_KEY = "test-key"
_mock_llm.BASE_URL = "https://test.example.com"
_mock_llm.MODEL = "test-model"
sys.modules["plugins.llm"] = _mock_llm

# mock plugins.local_tools.manager
_mock_lt_pkg = types.ModuleType("plugins.local_tools")
_mock_lt_pkg.__path__ = [str(ROOT / "plugins" / "local_tools")]
sys.modules["plugins.local_tools"] = _mock_lt_pkg
_mock_lt_manager = types.ModuleType("plugins.local_tools.manager")
_mock_lt_manager.get_openai_tools = MagicMock(return_value=[])
_mock_lt_manager.handle_tool_call = AsyncMock(return_value=None)
_mock_lt_manager.list_tools_summary = MagicMock(return_value=[])
sys.modules["plugins.local_tools.manager"] = _mock_lt_manager

# mock plugins.mcp.manager
_mock_mcp_pkg = types.ModuleType("plugins.mcp")
_mock_mcp_pkg.__path__ = [str(ROOT / "plugins" / "mcp")]
sys.modules["plugins.mcp"] = _mock_mcp_pkg
_mock_mcp_manager = types.ModuleType("plugins.mcp.manager")
_mock_mcp_manager.get_openai_tools = MagicMock(return_value=[])
_mock_mcp_manager.call_tool = AsyncMock(return_value="")
_mock_mcp_manager.MAX_TOOL_ROUNDS = 10
_mock_mcp_manager.list_tools_summary = MagicMock(return_value=[])
sys.modules["plugins.mcp.manager"] = _mock_mcp_manager

# mock plugins.skill.manager
_mock_skill_pkg = types.ModuleType("plugins.skill")
_mock_skill_pkg.__path__ = [str(ROOT / "plugins" / "skill")]
sys.modules["plugins.skill"] = _mock_skill_pkg
_mock_skill_manager = types.ModuleType("plugins.skill.manager")
_mock_skill_manager.get_openai_tools = MagicMock(return_value=[])
_mock_skill_manager.handle_tool_call = MagicMock(return_value=None)
_mock_skill_manager.list_skills_summary = MagicMock(return_value=[])
sys.modules["plugins.skill.manager"] = _mock_skill_manager

# ── 预设 handler mock（防止 proactive.py 延迟导入时加载真实 handler.py）──
_default_handler_mock = MagicMock()
_default_handler_mock.load_history = MagicMock(return_value=[])
_default_handler_mock.trim_history = MagicMock(side_effect=lambda msgs: msgs[-20:])
_default_handler_mock.append_message = MagicMock()
_default_handler_mock.get_config = MagicMock(return_value={"last_message_at": "2026-03-26 11:00:00"})
_default_handler_mock.load_admin_prompt = MagicMock(return_value="")
sys.modules["plugins.chat.handler"] = _default_handler_mock

# mock plugins.runtime_context
_mock_runtime_context = types.ModuleType("plugins.runtime_context")
_mock_runtime_context.build_runtime_context = MagicMock(return_value="\n当前时间: 2026-03-26 12:00:00（星期四）")
sys.modules["plugins.runtime_context"] = _mock_runtime_context

# ── 用 importlib 加载 proactive.py ──
_proactive_path = ROOT / "plugins" / "chat" / "proactive.py"
_spec = importlib.util.spec_from_file_location("plugins.chat.proactive", _proactive_path)
proactive = importlib.util.module_from_spec(_spec)
sys.modules["plugins.chat.proactive"] = proactive
_spec.loader.exec_module(proactive)


# ── fixtures ──

@pytest.fixture(autouse=True)
def _cleanup_timer():
    """每个测试前后确保计时器被取消，并重置 send_chunked_raw mock。"""
    _mock_chunker.send_chunked_raw.reset_mock()
    yield
    proactive.cancel_idle_timer()


@pytest.fixture
def tmp_session_dir(tmp_path):
    """创建临时会话目录，并 mock handler 的相关函数。"""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    return session_dir


# ── 辅助 ──

def _make_history(tmp_dir: Path, messages: list[dict]) -> None:
    """向临时文件写入对话历史。"""
    path = tmp_dir / "373900859.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def _setup_handler_mock(tmp_dir: Path, messages: list[dict],
                        admin_prompt: str = "", system_prompt: str = "你是助手"):
    """设置 handler mock 到 sys.modules 并返回 mock 对象。"""
    _make_history(tmp_dir, messages)

    def load_history(uid):
        path = tmp_dir / f"{uid}.jsonl"
        if not path.exists():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                result.append(json.loads(line))
        return result

    def trim_history(msgs):
        return msgs[-20:]  # 简化版

    mock_handler = MagicMock()
    mock_handler.load_history = MagicMock(side_effect=load_history)
    mock_handler.trim_history = MagicMock(side_effect=trim_history)
    mock_handler.append_message = MagicMock()
    mock_handler.get_config = MagicMock(return_value={"last_message_at": "2026-03-26 11:00:00"})
    mock_handler.load_admin_prompt = MagicMock(return_value=admin_prompt or system_prompt)
    sys.modules["plugins.chat.handler"] = mock_handler
    return mock_handler


# ══════════════════════════════════════════════════════
# 测试
# ══════════════════════════════════════════════════════


class TestIdleTimer:
    """计时器重置 / 取消测试"""

    @pytest.mark.asyncio
    async def test_reset_creates_task(self):
        proactive.reset_idle_timer()
        assert proactive._idle_task is not None
        assert not proactive._idle_task.done()

    @pytest.mark.asyncio
    async def test_cancel_clears_task(self):
        proactive.reset_idle_timer()
        proactive.cancel_idle_timer()
        assert proactive._idle_task is None

    @pytest.mark.asyncio
    async def test_reset_cancels_previous_task(self):
        proactive.reset_idle_timer()
        old_task = proactive._idle_task
        proactive.reset_idle_timer()
        # cancel() 已调用，但需要让 event loop 跑一轮才能完成取消
        await asyncio.sleep(0)
        assert old_task.cancelled()
        assert proactive._idle_task is not old_task

    @pytest.mark.asyncio
    async def test_cancel_is_idempotent(self):
        proactive.cancel_idle_timer()
        proactive.cancel_idle_timer()  # 不应出错


class TestHeartbeatDecision:
    """心跳 LLM 决策分支测试"""

    @pytest.mark.asyncio
    async def test_no_history_does_not_crash(self, tmp_session_dir):
        """对话历史为空时，心跳仍可正常运行（不跳过，因为心跳不要求有历史）。"""
        _setup_handler_mock(tmp_session_dir, [])
        # 心跳允许空历史运行，需要 mock LLM
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "HEARTBEAT_OK"}}]
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        with patch("httpx.AsyncClient", return_value=mock_client):
            await proactive._try_heartbeat()
        _mock_chunker.send_chunked_raw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_last_msg_not_assistant_skips(self, tmp_session_dir):
        """最后一条消息不是 assistant 时跳过。"""
        _setup_handler_mock(tmp_session_dir, [{"role": "user", "content": "你好"}])
        await proactive._try_heartbeat()
        _mock_chunker.send_chunked_raw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_says_heartbeat_ok(self, tmp_session_dir):
        """LLM 回复 HEARTBEAT_OK → 不发消息。"""
        _setup_handler_mock(tmp_session_dir, [
            {"role": "user", "content": "今天天气真好"},
            {"role": "assistant", "content": "是啊，阳光明媚"},
        ], admin_prompt="你是绘名")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "HEARTBEAT_OK"}}]
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await proactive._try_heartbeat()

        _mock_chunker.send_chunked_raw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_sends_message(self, tmp_session_dir):
        """LLM 有话说 → 发送消息并写入历史。"""
        handler_mock = _setup_handler_mock(tmp_session_dir, [
            {"role": "user", "content": "晚安"},
            {"role": "assistant", "content": "晚安，做个好梦"},
        ], admin_prompt="你是绘名")

        proactive_reply = "话说你昨天画的那幅画完成了吗？"
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": proactive_reply}}]
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_bot = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("plugins.chat.proactive.get_bot", return_value=mock_bot):
            await proactive._try_heartbeat()

        _mock_chunker.send_chunked_raw.assert_awaited_once_with(
            mock_bot, "private", 373900859, proactive_reply
        )
        handler_mock.append_message.assert_called_once_with(
            "373900859", {"role": "assistant", "content": proactive_reply}
        )

    @pytest.mark.asyncio
    async def test_llm_empty_reply_skips(self, tmp_session_dir):
        """LLM 回复空字符串 → 不发消息。"""
        _setup_handler_mock(tmp_session_dir, [
            {"role": "user", "content": "嗯"},
            {"role": "assistant", "content": "嗯嗯"},
        ])

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": ""}}]
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await proactive._try_heartbeat()

        _mock_chunker.send_chunked_raw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uses_admin_prompt_when_available(self, tmp_session_dir):
        """有 ADMIN_PROMPT 时应使用它作为 system prompt。"""
        _setup_handler_mock(tmp_session_dir, [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ], admin_prompt="你是東雲絵名")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "NO"}}]
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await proactive._try_heartbeat()

        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        system_msg = payload["messages"][0]
        assert system_msg["role"] == "system"
        assert system_msg["content"].startswith("你是東雲絵名")

    @pytest.mark.asyncio
    async def test_heartbeat_instruction_appended(self, tmp_session_dir):
        """心跳指令应作为最后一条 system 消息追加。"""
        _setup_handler_mock(tmp_session_dir, [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ], admin_prompt="你是绘名")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "NO"}}]
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await proactive._try_heartbeat()

        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        last_msg = payload["messages"][-1]
        assert last_msg["role"] == "system"
        assert "心跳" in last_msg["content"] or "HEARTBEAT" in last_msg["content"]

    @pytest.mark.asyncio
    async def test_no_api_key_skips(self):
        """API_KEY 为空时跳过。"""
        original = proactive.API_KEY
        try:
            proactive.API_KEY = ""
            await proactive._try_heartbeat()
            _mock_chunker.send_chunked_raw.assert_not_awaited()
        finally:
            proactive.API_KEY = original

    @pytest.mark.asyncio
    async def test_no_admin_number_skips(self):
        """ADMIN_NUMBER 为空时跳过。"""
        original = proactive.ADMIN_NUMBER
        try:
            proactive.ADMIN_NUMBER = ""
            await proactive._try_heartbeat()
            _mock_chunker.send_chunked_raw.assert_not_awaited()
        finally:
            proactive.ADMIN_NUMBER = original


class TestTimerIntegration:
    """计时器到期后的集成流程测试"""

    @pytest.mark.asyncio
    async def test_timer_fires_after_idle(self):
        """计时器到期后应触发心跳。"""
        with patch.object(proactive, "_try_heartbeat", new_callable=AsyncMock) as mock_try:
            proactive.IDLE_SECONDS = 0.15
            proactive.MIN_DEFER_SECONDS = 0.05
            proactive.reset_idle_timer()
            await asyncio.sleep(0.1)
            mock_try.assert_not_awaited()
            await asyncio.sleep(0.1)  # 总共 0.2s > 0.15s
            assert mock_try.await_count >= 1
            proactive.cancel_idle_timer()
            proactive.IDLE_SECONDS = 1
            proactive.MIN_DEFER_SECONDS = 600

    @pytest.mark.asyncio
    async def test_reset_during_chat_defers_to_min(self):
        """聊天中重置计时器，应延后到 MIN_DEFER_SECONDS 而非完全重置。"""
        with patch.object(proactive, "_try_heartbeat", new_callable=AsyncMock) as mock_try:
            proactive.IDLE_SECONDS = 0.2
            proactive.MIN_DEFER_SECONDS = 0.1
            proactive.reset_idle_timer()        # deadline = now + 0.2s
            await asyncio.sleep(0.15)           # 剩余 0.05s < MIN_DEFER(0.1s)
            proactive.reset_idle_timer()        # 延后到 now + 0.1s
            await asyncio.sleep(0.05)           # 距延后仅 0.05s
            mock_try.assert_not_awaited()       # 还没到
            await asyncio.sleep(0.1)            # 距延后 0.15s > 0.1s
            mock_try.assert_awaited_once()
            proactive.IDLE_SECONDS = 1
            proactive.MIN_DEFER_SECONDS = 600

    @pytest.mark.asyncio
    async def test_reset_keeps_deadline_when_plenty_remaining(self):
        """剩余时间充足时，reset 不改变 deadline。"""
        with patch.object(proactive, "_try_heartbeat", new_callable=AsyncMock) as mock_try:
            proactive.IDLE_SECONDS = 0.3
            proactive.MIN_DEFER_SECONDS = 0.05
            proactive.reset_idle_timer()        # deadline = now + 0.3s
            await asyncio.sleep(0.05)           # 剩余 0.25s > MIN_DEFER(0.05s)
            old_deadline = proactive._idle_deadline
            proactive.reset_idle_timer()        # 不应改变
            assert proactive._idle_deadline == old_deadline
            proactive.cancel_idle_timer()
            proactive.IDLE_SECONDS = 1
            proactive.MIN_DEFER_SECONDS = 600
