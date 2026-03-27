"""
tests/test_runtime_context.py
──────────────────────────────
测试 runtime_context 模块:
  - build_runtime_context() 私聊 vs 群聊差异
  - 时间上下文注入
  - 工具摘要按 chat_type 过滤
"""

import sys
import os
import types
import importlib
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Mock nonebot 及其子模块 ──
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

_mock_config = MagicMock()
_mock_config.group_whitelist = []
_mock_config.llm_provider = "gemini"
_mock_config.gemini_api_key = "fake-key"
_mock_config.llm_base_url = ""
_mock_config.llm_model = "test-model"
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)

# ── Mock nonebot.exception（reminder 导入链需要） ──
_mock_exc = types.ModuleType("nonebot.exception")
_mock_exc.MatcherException = type("MatcherException", (Exception,), {})
sys.modules.setdefault("nonebot.exception", _mock_exc)

# ── 确保 plugins 包存在 ──
if "plugins" not in sys.modules:
    _plugins_pkg = types.ModuleType("plugins")
    _plugins_pkg.__path__ = [str(ROOT / "plugins")]
    sys.modules["plugins"] = _plugins_pkg

# ── 确保 plugins.llm 可用（可能被其他测试先加载了） ──
if "plugins.llm" not in sys.modules:
    # 直接加载 llm.py（需要正确的 mock config 已设置好）
    _llm_spec = importlib.util.spec_from_file_location(
        "plugins.llm", ROOT / "plugins" / "llm.py"
    )
    _llm_mod = importlib.util.module_from_spec(_llm_spec)
    sys.modules["plugins.llm"] = _llm_mod
    _llm_spec.loader.exec_module(_llm_mod)

# ── 确保 local_tools 已加载（工具摘要测试需要注册表） ──
# 强制重载 manager（因为 test_proactive 可能已注入 mock）
_lt_pkg = types.ModuleType("plugins.local_tools")
_lt_pkg.__path__ = [str(ROOT / "plugins" / "local_tools")]
sys.modules["plugins.local_tools"] = _lt_pkg

_lt_mgr_spec = importlib.util.spec_from_file_location(
    "plugins.local_tools.manager",
    ROOT / "plugins" / "local_tools" / "manager.py",
)
_lt_mgr_mod = importlib.util.module_from_spec(_lt_mgr_spec)
sys.modules["plugins.local_tools.manager"] = _lt_mgr_mod
_lt_mgr_spec.loader.exec_module(_lt_mgr_mod)

# tools.py 注册工具到 manager._registry。
# 不能 force-reload（会破坏 test_file_tools 的 monkeypatch），
# 所以用 exec_module 在已有模块上重新执行，让 @register_tool 写入新 _registry。
_lt_tools_current = sys.modules.get("plugins.local_tools.tools")
if _lt_tools_current and hasattr(_lt_tools_current, "__spec__") and _lt_tools_current.__spec__:
    _lt_tools_current.__spec__.loader.exec_module(_lt_tools_current)
else:
    _lt_tools_spec = importlib.util.spec_from_file_location(
        "plugins.local_tools.tools",
        ROOT / "plugins" / "local_tools" / "tools.py",
    )
    _lt_tools_mod = importlib.util.module_from_spec(_lt_tools_spec)
    sys.modules["plugins.local_tools.tools"] = _lt_tools_mod
    _lt_tools_spec.loader.exec_module(_lt_tools_mod)

# ── 加载 runtime_context 模块（强制替换，因为 test_proactive 可能已注入 mock） ──
_rc_spec = importlib.util.spec_from_file_location(
    "plugins.runtime_context", ROOT / "plugins" / "runtime_context.py"
)
_rc_mod = importlib.util.module_from_spec(_rc_spec)
sys.modules["plugins.runtime_context"] = _rc_mod
_rc_spec.loader.exec_module(_rc_mod)

import pytest
from plugins.runtime_context import build_runtime_context, _build_tools_summary


# ──────────────────── 时间上下文 ────────────────────

class TestTimeContext:

    def test_includes_current_time(self):
        result = build_runtime_context(chat_type="private")
        assert "当前时间:" in result

    def test_includes_weekday(self):
        result = build_runtime_context(chat_type="private")
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        assert any(w in result for w in weekdays)

    def test_includes_last_message_at(self):
        result = build_runtime_context(
            chat_type="private",
            last_message_at="2026-03-26 12:00:00",
        )
        assert "上次对话: 2026-03-26 12:00:00" in result

    def test_omits_last_message_when_empty(self):
        result = build_runtime_context(chat_type="private", last_message_at="")
        assert "上次对话" not in result


# ──────────────────── Runtime 行差异 ────────────────────

class TestRuntimeLine:

    def test_private_has_full_runtime(self):
        """私聊应包含 OS/host/python/shell/workspace 等完整环境信息"""
        result = build_runtime_context(chat_type="private")
        assert "os=" in result
        assert "host=" in result
        assert "python=" in result
        assert "shell=" in result
        assert "workspace=" in result

    def test_group_has_minimal_runtime(self):
        """群聊只包含 model=，不应包含系统环境信息"""
        result = build_runtime_context(chat_type="group")
        assert "model=" in result
        # 群聊不应暴露系统信息
        assert "os=" not in result
        assert "host=" not in result
        assert "shell=" not in result
        assert "workspace=" not in result

    def test_private_has_model(self):
        result = build_runtime_context(chat_type="private")
        assert "model=" in result


# ──────────────────── 消息渠道 ────────────────────

class TestChannel:

    def test_private_channel(self):
        result = build_runtime_context(chat_type="private")
        assert "QQ私聊" in result

    def test_group_channel(self):
        result = build_runtime_context(chat_type="group")
        assert "QQ群聊" in result

    def test_capabilities_present(self):
        result = build_runtime_context(chat_type="private")
        assert "capabilities=" in result


# ──────────────────── 工具摘要过滤 ────────────────────

class TestToolsSummaryIntegration:
    """确保 build_runtime_context 将 chat_type 传递给工具摘要"""

    def test_private_tools_summary_includes_admin_tools(self):
        """私聊应能看到 admin_only 工具（如 read_file）"""
        result = build_runtime_context(chat_type="private")
        # read_file 是 admin_only 工具，私聊应可见
        assert "read_file" in result

    def test_group_tools_summary_excludes_admin_tools(self):
        """群聊不应看到 admin_only 工具"""
        result = build_runtime_context(chat_type="group")
        # admin_only 工具不应出现在群聊工具摘要中
        assert "read_file" not in result
        assert "write_file" not in result
        assert "list_files" not in result

    def test_group_tools_summary_includes_public_tools(self):
        """群聊仍应看到公共工具"""
        result = build_runtime_context(chat_type="group")
        # 公共工具（如 calculate）应对群聊可见
        assert "calculate" in result


class TestBuildToolsSummary:
    """直接测试 _build_tools_summary"""

    def test_private_returns_admin_tools(self):
        summary = _build_tools_summary(chat_type="private")
        assert "read_file" in summary
        assert "write_file" in summary
        assert "list_files" in summary

    def test_group_excludes_admin_tools(self):
        summary = _build_tools_summary(chat_type="group")
        assert "read_file" not in summary
        assert "write_file" not in summary
        assert "list_files" not in summary

    def test_default_is_private(self):
        summary_default = _build_tools_summary()
        summary_private = _build_tools_summary(chat_type="private")
        assert summary_default == summary_private

    def test_public_tools_in_both(self):
        summary_private = _build_tools_summary(chat_type="private")
        summary_group = _build_tools_summary(chat_type="group")
        # calculate 是公共工具
        assert "calculate" in summary_private
        assert "calculate" in summary_group
