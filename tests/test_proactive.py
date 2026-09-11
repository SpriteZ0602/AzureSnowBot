"""
proactive 心跳 + 主动发言引擎单元测试
──────────────────────────────────────
测试 keyed 空闲计时器 + 私聊/群聊心跳执行 + 开关控制。
"""

import sys
import os
import types
import json
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

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
sys.modules["nonebot"].get_bot = MagicMock(return_value=MagicMock())


# ── sys.modules 快照/还原 ──
# 本文件在收集期用桩模块覆盖多个 plugins.* 公共模块。proactive.py 加载完就还原，
# 之后仅在运行期懒加载需要时（每个测试 setup）临时装回，防止 mock 泄漏给
# 同会话的其他测试文件（test_chunker / test_dashboard_memory 等）。
_ORIGINAL_MODULES: dict = {}
_STUBBED_MODULES: dict = {}


def _save_and_set(name: str, mod) -> None:
    if name not in _ORIGINAL_MODULES:
        _ORIGINAL_MODULES[name] = sys.modules.get(name)
    sys.modules[name] = mod


def _install_stubs() -> None:
    for name, mod in _STUBBED_MODULES.items():
        sys.modules[name] = mod


def _restore_modules() -> None:
    for name, mod in _ORIGINAL_MODULES.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod

# ── 构造 plugins 包 ──
_plugins_pkg = types.ModuleType("plugins")
_plugins_pkg.__path__ = [str(ROOT / "plugins")]
_plugins_pkg.__package__ = "plugins"
_save_and_set("plugins", _plugins_pkg)


def _make_pkg(name: str, path: str) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = [path]
    pkg.__package__ = name
    _save_and_set(name, pkg)
    return pkg


# mock plugins.chunker
_mock_chunker = types.ModuleType("plugins.chunker")
_mock_chunker.chunk_text = lambda text: [text] if text else []
_mock_chunker.send_chunked_raw = AsyncMock()
_save_and_set("plugins.chunker", _mock_chunker)

# mock plugins.llm
_mock_llm = types.ModuleType("plugins.llm")
_mock_llm.API_KEY = "test-key"
_mock_llm.BASE_URL = "https://test.example.com"
_mock_llm.MODEL = "test-model"
_mock_llm.LLM_PROVIDER = "deepseek"
_mock_llm.SUPPORTS_VISION = False
_mock_llm.call_llm = AsyncMock(return_value={"choices": [{"message": {"content": ""}}], "usage": {}})
_save_and_set("plugins.llm", _mock_llm)

# mock plugins.local_tools.manager
_make_pkg("plugins.local_tools", str(ROOT / "plugins" / "local_tools"))
_mock_lt_manager = types.ModuleType("plugins.local_tools.manager")
_mock_lt_manager.get_openai_tools = MagicMock(return_value=[])
_mock_lt_manager.handle_tool_call = AsyncMock(return_value=None)
_mock_lt_manager.list_tools_summary = MagicMock(return_value=[])
_save_and_set("plugins.local_tools.manager", _mock_lt_manager)

# mock plugins.mcp.manager
_make_pkg("plugins.mcp", str(ROOT / "plugins" / "mcp"))
_mock_mcp_manager = types.ModuleType("plugins.mcp.manager")
_mock_mcp_manager.get_openai_tools = MagicMock(return_value=[])
_mock_mcp_manager.call_tool = AsyncMock(return_value="")
_mock_mcp_manager.MAX_TOOL_ROUNDS = 10
_mock_mcp_manager.list_tools_summary = MagicMock(return_value=[])
_save_and_set("plugins.mcp.manager", _mock_mcp_manager)

# mock plugins.skill.manager
_make_pkg("plugins.skill", str(ROOT / "plugins" / "skill"))
_mock_skill_manager = types.ModuleType("plugins.skill.manager")
_mock_skill_manager.get_openai_tools = MagicMock(return_value=[])
_mock_skill_manager.handle_tool_call = MagicMock(return_value=None)
_mock_skill_manager.list_skills_summary = MagicMock(return_value=[])
_mock_skill_manager.build_catalog_prompt = MagicMock(return_value="")
_save_and_set("plugins.skill.manager", _mock_skill_manager)

# mock plugins.runtime_context
_mock_runtime_context = types.ModuleType("plugins.runtime_context")
_mock_runtime_context.build_runtime_context = MagicMock(return_value="\nRuntime: model=test-model")
_mock_runtime_context.build_time_context = MagicMock(return_value="当前时间: 2026-03-26 12:00:00（星期四）")
_save_and_set("plugins.runtime_context", _mock_runtime_context)

# mock plugins.chat.handler（私聊心跳的会话上下文）
_make_pkg("plugins.chat", str(ROOT / "plugins" / "chat"))
_mock_handler = MagicMock()
_mock_handler.load_history = MagicMock(return_value=[])
_mock_handler.trim_history = MagicMock(side_effect=lambda msgs: msgs)
_mock_handler.append_message = MagicMock()
_mock_handler.get_config = MagicMock(return_value={"last_message_at": "2026-03-26 11:00:00"})
_mock_handler.load_admin_prompt = MagicMock(return_value="你是助手")
_mock_handler.get_proactive_enabled = MagicMock(return_value=True)
_save_and_set("plugins.chat.handler", _mock_handler)

# mock plugins.persona.manager + plugins.group.utils（群聊心跳的会话上下文）
_make_pkg("plugins.persona", str(ROOT / "plugins" / "persona"))
_mock_persona = MagicMock()
_mock_persona.get_active_persona = MagicMock(return_value="default")
_mock_persona.load_history = MagicMock(return_value=[{"role": "assistant", "content": "上一条回复"}])
_mock_persona.append_message = MagicMock()
_mock_persona.load_persona_prompt = MagicMock(return_value="群人格 prompt")
_mock_persona.get_group_config = MagicMock(return_value={"last_message_at": "2026-03-26 11:00:00"})
_mock_persona.get_group_proactive = MagicMock(return_value=True)
_save_and_set("plugins.persona.manager", _mock_persona)

_make_pkg("plugins.group", str(ROOT / "plugins" / "group"))
_mock_group_utils = types.ModuleType("plugins.group.utils")
_mock_group_utils.trim_history = MagicMock(side_effect=lambda msgs, sp: msgs)
_save_and_set("plugins.group.utils", _mock_group_utils)

# ── 用 importlib 加载 proactive.py（根级引擎）──
_spec = importlib.util.spec_from_file_location(
    "plugins.proactive",
    ROOT / "plugins" / "proactive.py",
)
proactive = importlib.util.module_from_spec(_spec)
_save_and_set("plugins.proactive", proactive)
_spec.loader.exec_module(proactive)

# 记录本文件安装的全部桩，供运行期临时装回；随即还原 sys.modules，
# 让收集期晚于本文件的其他测试模块在运行期拿到真实模块。
_STUBBED_MODULES = {
    "plugins": _plugins_pkg,
    "plugins.chunker": _mock_chunker,
    "plugins.llm": _mock_llm,
    "plugins.local_tools": sys.modules["plugins.local_tools"],
    "plugins.local_tools.manager": _mock_lt_manager,
    "plugins.mcp": sys.modules["plugins.mcp"],
    "plugins.mcp.manager": _mock_mcp_manager,
    "plugins.skill": sys.modules["plugins.skill"],
    "plugins.skill.manager": _mock_skill_manager,
    "plugins.runtime_context": _mock_runtime_context,
    "plugins.chat": sys.modules["plugins.chat"],
    "plugins.chat.handler": _mock_handler,
    "plugins.persona": sys.modules["plugins.persona"],
    "plugins.persona.manager": _mock_persona,
    "plugins.group": sys.modules["plugins.group"],
    "plugins.group.utils": _mock_group_utils,
    "plugins.proactive": proactive,
}
_restore_modules()


@pytest.fixture(autouse=True)
def _cleanup_timer():
    """每个测试前后确保所有计时器被取消，并重置 mocks。

    运行期临时装回桩模块（proactive.py 内部大量懒加载依赖它们），
    测试结束后再次还原，避免泄漏给后续测试文件。
    """
    _mock_chunker.send_chunked_raw.reset_mock()
    _mock_handler.reset_mock()
    _mock_persona.reset_mock()
    _mock_llm.call_llm.reset_mock()
    _install_stubs()
    yield
    for key in list(proactive._idle_tasks):
        proactive.cancel_idle_timer(key)
    _restore_modules()


def _mock_call_llm(reply_content: str):
    """让 call_llm 返回指定回复"""
    async def _fake_call_llm(messages, *, tools=None, source="unknown", timeout=120):
        return {"choices": [{"message": {"content": reply_content}}], "usage": {}}
    _mock_llm.call_llm = AsyncMock(side_effect=_fake_call_llm)


# ══════════════════════════════════════════════════════
# 计时器（按 key）
# ══════════════════════════════════════════════════════

class TestIdleTimer:

    async def test_reset_creates_task(self):
        proactive.reset_idle_timer("private")
        assert "private" in proactive._idle_tasks
        assert not proactive._idle_tasks["private"].done()

    async def test_cancel_clears_task(self):
        proactive.reset_idle_timer("private")
        proactive.cancel_idle_timer("private")
        assert "private" not in proactive._idle_tasks

    async def test_keys_are_independent(self):
        proactive.reset_idle_timer("private")
        proactive.reset_idle_timer("group:123")
        assert "private" in proactive._idle_tasks
        assert "group:123" in proactive._idle_tasks

    async def test_reset_while_running_defers_to_min(self):
        """剩余时间 < MIN_DEFER 时延后到 MIN_DEFER_SECONDS"""
        proactive.reset_idle_timer("private")  # IDLE_SECONDS=1
        deadline1 = proactive._idle_deadlines["private"]
        proactive.reset_idle_timer("private")  # remaining ≈ 1s < 600s → 延后
        assert proactive._idle_deadlines["private"] >= deadline1
        assert proactive._idle_deadlines["private"] >= proactive._now() + 500

    async def test_cancel_nonexistent_no_error(self):
        proactive.cancel_idle_timer("group:999")  # 不应报错


# ══════════════════════════════════════════════════════
# 私聊心跳
# ══════════════════════════════════════════════════════

class TestHeartbeatPrivate:

    async def test_heartbeat_ok_is_silent(self):
        _mock_call_llm("HEARTBEAT_OK")
        await proactive.run_heartbeat("private", "373900859")
        _mock_chunker.send_chunked_raw.assert_not_awaited()
        _mock_handler.append_message.assert_not_called()

    async def test_heartbeat_sends_message(self):
        _mock_call_llm("记得喝水哦，保重身体呀")
        await proactive.run_heartbeat("private", "373900859")
        _mock_chunker.send_chunked_raw.assert_awaited_once()
        args = _mock_chunker.send_chunked_raw.await_args.args
        assert args[1] == "private"
        assert "记得喝水哦" in args[3]
        _mock_handler.append_message.assert_called_once()

    async def test_disabled_flag_no_send(self):
        _mock_handler.get_proactive_enabled.return_value = False
        _mock_call_llm("记得喝水哦")
        await proactive.run_heartbeat("private", "373900859")
        _mock_llm.call_llm.assert_not_awaited()
        _mock_chunker.send_chunked_raw.assert_not_awaited()

    async def test_history_ending_with_user_still_runs(self):
        """历史末条是 user 时心跳照常执行，不再跳过

        旧实现要求末条必须是 assistant 才执行心跳。全量监听（/listen on）下
        所有群消息都以 role=user 写入历史，末条几乎恒为 user，心跳永远触发不了。

        注：本文件模块级 mock 的 return_value 会被前面的测试改动且
        autouse fixture 只 reset_mock() 不还原 return_value，
        所以这里显式设置自身依赖的状态，避免顺序依赖。
        """
        _mock_handler.get_proactive_enabled.return_value = True
        _mock_handler.load_history.return_value = [{"role": "user", "content": "在吗"}]
        _mock_call_llm("记得喝水哦，保重身体呀")
        await proactive.run_heartbeat("private", "373900859")
        _mock_llm.call_llm.assert_awaited_once()


# ══════════════════════════════════════════════════════
# 群聊心跳
# ══════════════════════════════════════════════════════

class TestHeartbeatGroup:

    async def test_group_heartbeat_sends(self):
        _mock_call_llm("大家今天怎么样？都还好吗")
        await proactive.run_heartbeat("group", "123")
        _mock_chunker.send_chunked_raw.assert_awaited_once()
        args = _mock_chunker.send_chunked_raw.await_args.args
        assert args[1] == "group"
        assert args[2] == 123
        assert "大家今天怎么样？" in args[3]
        # 写入该群当前人格的历史
        _mock_persona.append_message.assert_called_once()
        call = _mock_persona.append_message.call_args
        assert call.args[0] == "123"
        assert call.args[2] == "default"

    async def test_group_disabled_no_send(self):
        _mock_persona.get_group_proactive.return_value = False
        _mock_call_llm("大家今天怎么样？")
        await proactive.run_heartbeat("group", "123")
        _mock_llm.call_llm.assert_not_awaited()
        _mock_chunker.send_chunked_raw.assert_not_awaited()

    async def test_group_persona_missing_skips(self):
        _mock_persona.load_persona_prompt.return_value = None
        _mock_call_llm("大家今天怎么样？")
        await proactive.run_heartbeat("group", "123")
        _mock_llm.call_llm.assert_not_awaited()

    async def test_group_history_ending_with_user_still_runs(self):
        """群聊历史末条是 user 时心跳照常执行

        这是 issue #7 的核心：/listen on 下群消息全以 role=user 写入历史，
        旧实现的末条检查会让群聊主动会话永远触发不了。

        注：显式设置自身依赖的 mock 状态，避免被前面测试污染（同上一个测试）。
        """
        _mock_persona.get_group_proactive.return_value = True
        _mock_persona.load_persona_prompt.return_value = "群人格 prompt"
        _mock_persona.load_history.return_value = [{"role": "user", "content": "有人吗"}]
        _mock_call_llm("大家今天怎么样？都还好吗")
        await proactive.run_heartbeat("group", "123")
        _mock_llm.call_llm.assert_awaited_once()
        _mock_persona.append_message.assert_called_once()


# ══════════════════════════════════════════════════════
# key 解析
# ══════════════════════════════════════════════════════

class TestKeyParse:

    def test_private_key(self):
        assert proactive._parse_key("private") == ("private", "373900859")

    def test_group_key(self):
        assert proactive._parse_key("group:123") == ("group", "123")

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError):
            proactive._parse_key("bogus")


# ══════════════════════════════════════════════════════
# 心跳 OK 判定（issue #6：模型把思考过程吐在正文里）
# ══════════════════════════════════════════════════════

class TestStripThinking:

    def test_no_think_block_unchanged(self):
        assert proactive._strip_thinking("记得喝水哦") == "记得喝水哦"

    def test_removes_paired_think_block(self):
        text = "<think>让我想想现在该说点什么……\n好像没什么事</think>在吗大家"
        assert proactive._strip_thinking(text) == "在吗大家"

    def test_removes_thinking_variant(self):
        text = "<thinking>分析一下</thinking>今天天气不错"
        assert proactive._strip_thinking(text) == "今天天气不错"

    def test_removes_unclosed_think_block(self):
        """输出被截断时 <think> 没闭合，应把开标签之后全截掉"""
        assert proactive._strip_thinking("前面正常内容<think>然后开始思考") == "前面正常内容"

    def test_case_insensitive(self):
        assert proactive._strip_thinking("<THINK>思考</THINK>结论") == "结论"

    def test_empty(self):
        assert proactive._strip_thinking("") == ""


class TestIsHeartbeatOk:

    def test_plain_ok(self):
        assert proactive._is_heartbeat_ok("HEARTBEAT_OK") is True

    def test_lowercase_ok(self):
        assert proactive._is_heartbeat_ok("heartbeat_ok") is True

    def test_no(self):
        assert proactive._is_heartbeat_ok("NO") is True

    def test_empty(self):
        assert proactive._is_heartbeat_ok("") is True
        assert proactive._is_heartbeat_ok("   ") is True

    def test_normal_message_not_ok(self):
        assert proactive._is_heartbeat_ok("记得喝水哦，保重身体呀") is False

    def test_think_then_ok(self):
        """带标签的思考 + 结论 OK → 静默"""
        text = "<think>检查了一遍，没什么待办的</think>HEARTBEAT_OK"
        assert proactive._is_heartbeat_ok(text) is True

    def test_bare_thinking_then_ok(self):
        """issue #6 的真实场景：不带标签，思考直接混在正文里，最后给 OK"""
        text = (
            "让我看看现在是什么时候了。晚上十一点，用户应该已经睡了。"
            "翻了一遍最近的对话，也没什么特别需要跟进的事情。"
            "那就这样吧。\nHEARTBEAT_OK"
        )
        assert proactive._is_heartbeat_ok(text) is True

    def test_bare_thinking_without_ok_is_not_silent(self):
        """只是把思考吐出来、但没给结论 → 不该被静默吞掉"""
        text = "让我看看现在是什么时候了。晚上十一点，用户应该已经睡了。"
        assert proactive._is_heartbeat_ok(text) is False

    def test_short_real_message_not_swallowed(self):
        """旧的 len<=10 兜底会把正常短主动消息吞掉"""
        assert proactive._is_heartbeat_ok("在吗大家") is False
        assert proactive._is_heartbeat_ok("睡了吗") is False
