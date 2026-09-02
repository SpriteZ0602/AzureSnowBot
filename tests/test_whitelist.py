"""
tests/test_whitelist.py
────────────────────────
测试群白名单管理（list/add/delete + .env 持久化 + 指令解析）:
  - add / delete / list 运行时行为
  - .env 持久化（重启后仍生效）
  - handle_whitelist_command 解析
"""

import sys
import os
import types
import json
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Mock nonebot
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

# mock nonebot.get_driver 返回带 group_whitelist 的 config
_mock_config = MagicMock()
_mock_config.group_whitelist = ["100", "200"]
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)

# 构造 plugins / plugins.group 包（绕过 __init__.py）
_plugins_pkg = types.ModuleType("plugins")
_plugins_pkg.__path__ = [str(ROOT / "plugins")]
sys.modules.setdefault("plugins", _plugins_pkg)

_group_pkg = types.ModuleType("plugins.group")
_group_pkg.__path__ = [str(ROOT / "plugins" / "group")]
sys.modules["plugins.group"] = _group_pkg

# 加载 utils.py
_spec = importlib.util.spec_from_file_location(
    "plugins.group.utils",
    ROOT / "plugins" / "group" / "utils.py",
)
utils = importlib.util.module_from_spec(_spec)
sys.modules["plugins.group.utils"] = utils
_spec.loader.exec_module(utils)

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    """把 ENV_PATH 指向临时目录，恢复 GROUP_WHITELIST 初始状态"""
    saved_list = list(utils.GROUP_WHITELIST)
    saved_env = utils.ENV_PATH
    utils.ENV_PATH = tmp_path / ".env"
    yield
    utils.GROUP_WHITELIST[:] = saved_list
    utils.ENV_PATH = saved_env


# ──────────────────── list ────────────────────

class TestList:

    def test_returns_copy(self):
        result = utils.list_whitelist()
        result.append("999")  # 修改返回的列表不应影响内部状态
        assert "999" not in utils.GROUP_WHITELIST

    def test_initial_from_config(self):
        assert utils.list_whitelist() == ["100", "200"]


# ──────────────────── add / remove ────────────────────

class TestAddRemove:

    def test_add_new_group(self):
        assert utils.add_to_whitelist("300") is True
        assert "300" in utils.list_whitelist()

    def test_add_duplicate(self):
        assert utils.add_to_whitelist("100") is False

    def test_remove_existing(self):
        assert utils.remove_from_whitelist("100") is True
        assert "100" not in utils.list_whitelist()

    def test_remove_nonexistent(self):
        assert utils.remove_from_whitelist("999") is False

    def test_in_whitelist_reflects_changes(self):
        utils.add_to_whitelist("300")
        assert utils.in_whitelist(300) is True
        utils.remove_from_whitelist("300")
        assert utils.in_whitelist(300) is False


# ──────────────────── .env 持久化 ────────────────────

class TestPersistence:

    def test_add_persists_to_env(self):
        utils.add_to_whitelist("300")
        content = utils.ENV_PATH.read_text(encoding="utf-8")
        assert "GROUP_WHITELIST=" in content
        # 行内容应为 JSON 数组且包含新群
        line = next(l for l in content.splitlines() if l.startswith("GROUP_WHITELIST="))
        parsed = json.loads(line.split("=", 1)[1])
        assert "300" in parsed

    def test_remove_persists_to_env(self):
        utils.remove_from_whitelist("100")
        line = next(l for l in utils.ENV_PATH.read_text(encoding="utf-8").splitlines() if l.startswith("GROUP_WHITELIST="))
        parsed = json.loads(line.split("=", 1)[1])
        assert "100" not in parsed
        assert "200" in parsed

    def test_preserves_other_env_lines(self):
        utils.ENV_PATH.write_text("PORT=8082\nGROUP_WHITELIST=[\"100\"]\nADMIN_NUMBER=373900859\n", encoding="utf-8")
        utils.add_to_whitelist("300")
        content = utils.ENV_PATH.read_text(encoding="utf-8")
        assert "PORT=8082" in content
        assert "ADMIN_NUMBER=373900859" in content


# ──────────────────── 指令解析 ────────────────────

class TestCommandParsing:

    def test_no_args_usage(self):
        assert "用法" in utils.handle_whitelist_command("/白名单")

    def test_list(self):
        reply = utils.handle_whitelist_command("/白名单 list")
        assert "100" in reply and "200" in reply

    def test_list_empty(self):
        utils.remove_from_whitelist("100")
        utils.remove_from_whitelist("200")
        assert "为空" in utils.handle_whitelist_command("/白名单 list")

    def test_add(self):
        reply = utils.handle_whitelist_command("/白名单 add 300")
        assert "加入" in reply
        assert "300" in utils.list_whitelist()

    def test_add_duplicate(self):
        reply = utils.handle_whitelist_command("/白名单 add 100")
        assert "已在白名单" in reply

    def test_add_invalid_group_id(self):
        reply = utils.handle_whitelist_command("/白名单 add abc")
        assert "无效" in reply
        assert "abc" not in utils.list_whitelist()

    def test_delete(self):
        reply = utils.handle_whitelist_command("/白名单 delete 100")
        assert "移出" in reply
        assert "100" not in utils.list_whitelist()

    def test_delete_nonexistent(self):
        reply = utils.handle_whitelist_command("/白名单 delete 999")
        assert "不在白名单" in reply

    def test_unknown_command(self):
        reply = utils.handle_whitelist_command("/白名单 foo")
        assert "未知指令" in reply

    def test_add_missing_group_id(self):
        reply = utils.handle_whitelist_command("/白名单 add")
        assert "用法" in reply


# ──────────────────── list 增强：带全量/主动对话开关 ────────────────────

class TestListWithFlags:
    """list 每行应带「全量对话」「主动对话」两个开关状态"""

    @pytest.fixture
    def persona_flags(self, monkeypatch):
        """注入假的 persona.manager，避免触发真实包并控制开关返回值"""
        pkg = types.ModuleType("plugins.persona")
        pkg.__path__ = [str(ROOT / "plugins" / "persona")]
        monkeypatch.setitem(sys.modules, "plugins.persona", pkg)

        mgr = types.ModuleType("plugins.persona.manager")
        mgr.get_listen_all = lambda gid: gid == "100"
        mgr.get_group_proactive = lambda gid: gid == "200"
        mgr.get_auto_trigger = lambda gid: False
        monkeypatch.setitem(sys.modules, "plugins.persona.manager", mgr)
        return mgr

    def test_shows_both_flags_per_group(self, persona_flags):
        reply = utils.handle_whitelist_command("/白名单 list")
        lines = [l for l in reply.splitlines() if l.startswith("- ")]
        assert len(lines) == 2
        # 100：全量开、主动关
        assert lines[0].startswith("- 100")
        assert "全量对话: 开" in lines[0]
        assert "主动对话: 关" in lines[0]
        # 200：全量关、主动开
        assert lines[1].startswith("- 200")
        assert "全量对话: 关" in lines[1]
        assert "主动对话: 开" in lines[1]

    def test_all_flags_off(self, persona_flags):
        persona_flags.get_listen_all = lambda gid: False
        persona_flags.get_group_proactive = lambda gid: False
        reply = utils.handle_whitelist_command("/白名单 list")
        assert reply.count("全量对话: 关") == 2
        assert reply.count("主动对话: 关") == 2

    def test_all_flags_on(self, persona_flags):
        persona_flags.get_listen_all = lambda gid: True
        persona_flags.get_group_proactive = lambda gid: True
        reply = utils.handle_whitelist_command("/白名单 list")
        assert reply.count("全量对话: 开") == 2
        assert reply.count("主动对话: 开") == 2

    def test_count_header_unchanged(self, persona_flags):
        reply = utils.handle_whitelist_command("/白名单 list")
        assert reply.startswith("白名单中的群（2 个）:")

    def test_empty_whitelist_still_short_circuits(self, persona_flags):
        utils.GROUP_WHITELIST[:] = []
        assert utils.handle_whitelist_command("/白名单 list") == "白名单为空。"
