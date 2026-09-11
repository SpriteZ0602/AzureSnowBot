"""
tests/test_file_tools.py
─────────────────────────
测试文件系统工具（read_file, write_file, list_files）:
  - 安全校验：场景校验（私聊/群聊）+ 路径白名单
  - 私聊：data/admin/、data/skills/、data/personas/
  - 群聊：仅 data/groups/<群号>/（本群记忆）
  - 正常读写功能
  - 路径遍历防护
  - 边界情况
"""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock nonebot
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

# 本文件会 import 真实 plugins.llm（经 memory_search → indexer），其 import 期
# 校验 provider，需给 mock driver 配合法值，否则单跑本文件时报错
_mock_config = MagicMock()
_mock_config.group_whitelist = []
_mock_config.llm_provider = "deepseek"
_mock_config.deepseek_api_key = ""
_mock_config.llm_base_url = ""
_mock_config.llm_model = ""
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)

import pytest
from plugins.local_tools.tools import (
    read_file_tool,
    write_file_tool,
    list_files_tool,
    memory_search_tool,
    _check_scope,
    _resolve_safe_path,
)


PRIVATE_CTX = {"_chat_type": "private", "_target_id": "373900859"}
GROUP_CTX = {"_chat_type": "group", "_target_id": "123456"}
GROUP_ID = "123456"


# ──────────────────── _check_scope ────────────────────

class TestCheckScope:

    def test_no_context(self):
        _, err = _check_scope(None)
        assert err is not None
        assert "仅限" in err

    def test_unknown_chat_type(self):
        _, err = _check_scope({"_chat_type": "other"})
        assert err is not None

    def test_private_allowed(self):
        chat_type, err = _check_scope({"_chat_type": "private"})
        assert err is None
        assert chat_type == "private"

    def test_group_allowed(self):
        """群聊现在也可以使用文件工具（范围限定本群目录）"""
        chat_type, err = _check_scope(GROUP_CTX)
        assert err is None
        assert chat_type == "group"


# ──────────────────── _resolve_safe_path ────────────────────

class TestResolveSafePathPrivate:

    def test_valid_admin_path(self):
        _, err = _resolve_safe_path("data/admin/MEMORY.md", PRIVATE_CTX)
        assert err is None

    def test_valid_personas_path(self):
        _, err = _resolve_safe_path("data/personas/default.txt", PRIVATE_CTX)
        assert err is None

    def test_valid_skills_path(self):
        _, err = _resolve_safe_path("data/skills/web-search", PRIVATE_CTX)
        assert err is None

    def test_blocked_outside_whitelist(self):
        _, err = _resolve_safe_path("plugins/llm.py", PRIVATE_CTX)
        assert err is not None
        assert "不在允许范围" in err

    def test_blocked_root_path(self):
        _, err = _resolve_safe_path("pyproject.toml", PRIVATE_CTX)
        assert err is not None

    def test_path_traversal_blocked(self):
        """尝试 .. 遍历逃出白名单"""
        _, err = _resolve_safe_path("data/admin/../../pyproject.toml", PRIVATE_CTX)
        assert err is not None

    def test_absolute_path_outside(self):
        _, err = _resolve_safe_path("C:/Windows/System32/cmd.exe", PRIVATE_CTX)
        assert err is not None


class TestResolveSafePathGroup:

    def test_valid_group_memory_path(self):
        target, err = _resolve_safe_path(f"data/groups/{GROUP_ID}/MEMORY.md", GROUP_CTX)
        assert err is None
        assert str(target).replace("\\", "/").endswith(f"data/groups/{GROUP_ID}/MEMORY.md")

    def test_group_cannot_access_admin(self):
        """群聊不能读 Admin 记忆"""
        _, err = _resolve_safe_path("data/admin/MEMORY.md", GROUP_CTX)
        assert err is not None
        assert "不在允许范围" in err

    def test_group_cannot_access_other_group(self):
        """群聊不能访问其他群的记忆目录"""
        _, err = _resolve_safe_path(f"data/groups/{GROUP_ID}2/MEMORY.md", GROUP_CTX)
        assert err is not None

    def test_group_path_traversal_blocked(self):
        _, err = _resolve_safe_path(f"data/groups/{GROUP_ID}/../../admin/MEMORY.md", GROUP_CTX)
        assert err is not None

    def test_group_invalid_group_id(self):
        """非纯数字群号拒绝（防止路径注入）"""
        ctx = {"_chat_type": "group", "_target_id": "abc/../admin"}
        _, err = _resolve_safe_path("data/groups/abc/MEMORY.md", ctx)
        assert err is not None

    def test_group_missing_target_id(self):
        ctx = {"_chat_type": "group"}
        _, err = _resolve_safe_path("data/groups/1/MEMORY.md", ctx)
        assert err is not None

    def test_no_context_rejected(self):
        _, err = _resolve_safe_path("data/admin/MEMORY.md")
        assert err is not None


class TestResolveSafePathGroupRelative:
    """群聊相对路径：LLM 无需知道群号，传相对路径即可命中本群目录"""

    def test_relative_memory_md_resolves_into_group_dir(self):
        target, err = _resolve_safe_path("MEMORY.md", GROUP_CTX)
        assert err is None
        assert str(target).replace("\\", "/").endswith(f"data/groups/{GROUP_ID}/MEMORY.md")

    def test_relative_dot_resolves_to_group_dir(self):
        target, err = _resolve_safe_path(".", GROUP_CTX)
        assert err is None
        assert str(target).replace("\\", "/").endswith(f"data/groups/{GROUP_ID}")

    def test_relative_subpath_resolves_into_group_dir(self):
        target, err = _resolve_safe_path("notes/abc.md", GROUP_CTX)
        assert err is None
        assert str(target).replace("\\", "/").endswith(f"data/groups/{GROUP_ID}/notes/abc.md")

    def test_data_prefixed_other_group_still_rejected(self):
        """以 data/ 开头的路径严格按项目根校验，不被嵌套解析进本群目录"""
        _, err = _resolve_safe_path(f"data/groups/{GROUP_ID}9/MEMORY.md", GROUP_CTX)
        assert err is not None

    def test_relative_traversal_still_blocked(self):
        _, err = _resolve_safe_path("../admin/MEMORY.md", GROUP_CTX)
        assert err is not None

    def test_relative_deep_traversal_blocked(self):
        _, err = _resolve_safe_path("sub/../../../admin/MEMORY.md", GROUP_CTX)
        assert err is not None

    def test_absolute_path_outside_group_rejected(self):
        _, err = _resolve_safe_path("C:/Windows/System32/cmd.exe", GROUP_CTX)
        assert err is not None


# ──────────────────── read_file_tool ────────────────────

class TestReadFileTool:

    @pytest.mark.asyncio
    async def test_rejected_without_context(self):
        result = await read_file_tool(path="data/admin/MEMORY.md")
        assert "仅限" in result

    @pytest.mark.asyncio
    async def test_group_cannot_read_admin(self):
        result = await read_file_tool(
            path="data/admin/MEMORY.md",
            _context=GROUP_CTX,
        )
        assert "不在允许范围" in result

    @pytest.mark.asyncio
    async def test_empty_path(self):
        result = await read_file_tool(path="", _context=PRIVATE_CTX)
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_read_existing_file(self, tmp_path):
        """读取一个真实存在的文件（通过 monkeypatch 白名单）"""
        test_file = tmp_path / "test.md"
        test_file.write_text("hello world", encoding="utf-8")

        from plugins.local_tools import tools
        original = tools._ALLOWED_ROOTS
        tools._ALLOWED_ROOTS = [tmp_path]
        try:
            result = await read_file_tool(
                path=str(test_file),
                _context=PRIVATE_CTX,
            )
            assert "hello world" in result
        finally:
            tools._ALLOWED_ROOTS = original

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self):
        result = await read_file_tool(
            path="data/admin/nonexistent_file_xyz.md",
            _context=PRIVATE_CTX,
        )
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_read_outside_whitelist(self):
        result = await read_file_tool(
            path="plugins/llm.py",
            _context=PRIVATE_CTX,
        )
        assert "不在允许范围" in result

    @pytest.mark.asyncio
    async def test_path_traversal(self):
        result = await read_file_tool(
            path="data/admin/../../.env",
            _context=PRIVATE_CTX,
        )
        assert "不在允许范围" in result

    @pytest.mark.asyncio
    async def test_read_empty_file(self, tmp_path):
        test_file = tmp_path / "empty.md"
        test_file.write_text("", encoding="utf-8")

        from plugins.local_tools import tools
        original = tools._ALLOWED_ROOTS
        tools._ALLOWED_ROOTS = [tmp_path]
        try:
            result = await read_file_tool(
                path=str(test_file),
                _context=PRIVATE_CTX,
            )
            assert "为空" in result
        finally:
            tools._ALLOWED_ROOTS = original


# ──────────────────── read_file_tool（群聊） ────────────────────

class TestReadFileToolGroup:

    @pytest.mark.asyncio
    async def test_read_group_memory(self, tmp_path):
        """群聊读取本群 MEMORY.md"""
        from plugins.local_tools import tools
        original_root = tools.GROUP_MEMORY_ROOT
        original_roots = tools._ALLOWED_ROOTS
        tools.GROUP_MEMORY_ROOT = tmp_path
        tools._ALLOWED_ROOTS = []
        try:
            mem = tmp_path / GROUP_ID / "MEMORY.md"
            mem.parent.mkdir(parents=True)
            mem.write_text("群记忆内容", encoding="utf-8")
            result = await read_file_tool(
                path=str(mem),
                _context=GROUP_CTX,
            )
            assert "群记忆内容" in result
        finally:
            tools.GROUP_MEMORY_ROOT = original_root
            tools._ALLOWED_ROOTS = original_roots

    @pytest.mark.asyncio
    async def test_read_group_memory_nonexistent(self):
        result = await read_file_tool(
            path=f"data/groups/{GROUP_ID}/MEMORY.md",
            _context=GROUP_CTX,
        )
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_read_group_memory_relative_path(self, tmp_path):
        """群聊传相对路径 MEMORY.md 即可读到本群记忆（无需知道群号）"""
        from plugins.local_tools import tools
        original_root = tools.GROUP_MEMORY_ROOT
        original_roots = tools._ALLOWED_ROOTS
        tools.GROUP_MEMORY_ROOT = tmp_path
        tools._ALLOWED_ROOTS = []
        try:
            mem = tmp_path / GROUP_ID / "MEMORY.md"
            mem.parent.mkdir(parents=True)
            mem.write_text("相对路径群记忆", encoding="utf-8")
            result = await read_file_tool(path="MEMORY.md", _context=GROUP_CTX)
            assert "相对路径群记忆" in result
        finally:
            tools.GROUP_MEMORY_ROOT = original_root
            tools._ALLOWED_ROOTS = original_roots


# ──────────────────── write_file_tool ────────────────────

class TestWriteFileTool:

    @pytest.mark.asyncio
    async def test_group_cannot_write_admin(self):
        result = await write_file_tool(
            path="data/admin/MEMORY.md",
            content="test",
            _context=GROUP_CTX,
        )
        assert "不在允许范围" in result

    @pytest.mark.asyncio
    async def test_empty_path(self):
        result = await write_file_tool(
            path="",
            content="test",
            _context=PRIVATE_CTX,
        )
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_write_outside_whitelist(self):
        result = await write_file_tool(
            path="plugins/llm.py",
            content="hacked",
            _context=PRIVATE_CTX,
        )
        assert "不在允许范围" in result

    @pytest.mark.asyncio
    async def test_write_and_verify(self, tmp_path):
        test_file = tmp_path / "write_test.md"

        from plugins.local_tools import tools
        original = tools._ALLOWED_ROOTS
        tools._ALLOWED_ROOTS = [tmp_path]
        try:
            result = await write_file_tool(
                path=str(test_file),
                content="写入测试内容",
                _context=PRIVATE_CTX,
            )
            assert "已写入" in result
            assert test_file.read_text(encoding="utf-8") == "写入测试内容"
        finally:
            tools._ALLOWED_ROOTS = original

    @pytest.mark.asyncio
    async def test_write_creates_subdirectories(self, tmp_path):
        test_file = tmp_path / "sub" / "dir" / "file.md"

        from plugins.local_tools import tools
        original = tools._ALLOWED_ROOTS
        tools._ALLOWED_ROOTS = [tmp_path]
        try:
            result = await write_file_tool(
                path=str(test_file),
                content="nested content",
                _context=PRIVATE_CTX,
            )
            assert "已写入" in result
            assert test_file.exists()
        finally:
            tools._ALLOWED_ROOTS = original

    @pytest.mark.asyncio
    async def test_write_reports_character_count(self, tmp_path):
        test_file = tmp_path / "count.md"
        content = "12345"

        from plugins.local_tools import tools
        original = tools._ALLOWED_ROOTS
        tools._ALLOWED_ROOTS = [tmp_path]
        try:
            result = await write_file_tool(
                path=str(test_file),
                content=content,
                _context=PRIVATE_CTX,
            )
            assert "5" in result
        finally:
            tools._ALLOWED_ROOTS = original


# ──────────────────── write_file_tool（群聊） ────────────────────

class TestWriteFileToolGroup:

    @pytest.mark.asyncio
    async def test_write_group_memory(self, tmp_path):
        """群聊写入本群 MEMORY.md"""
        from plugins.local_tools import tools
        original_root = tools.GROUP_MEMORY_ROOT
        original_roots = tools._ALLOWED_ROOTS
        tools.GROUP_MEMORY_ROOT = tmp_path
        tools._ALLOWED_ROOTS = []
        try:
            mem = tmp_path / GROUP_ID / "MEMORY.md"
            result = await write_file_tool(
                path=str(mem),
                content="群聊记忆测试",
                _context=GROUP_CTX,
            )
            assert "已写入" in result
            assert mem.read_text(encoding="utf-8") == "群聊记忆测试"
        finally:
            tools.GROUP_MEMORY_ROOT = original_root
            tools._ALLOWED_ROOTS = original_roots

    @pytest.mark.asyncio
    async def test_write_group_cannot_write_other_group(self, tmp_path):
        from plugins.local_tools import tools
        original_root = tools.GROUP_MEMORY_ROOT
        original_roots = tools._ALLOWED_ROOTS
        tools.GROUP_MEMORY_ROOT = tmp_path
        tools._ALLOWED_ROOTS = []
        try:
            result = await write_file_tool(
                path=str(tmp_path / f"{GROUP_ID}9" / "MEMORY.md"),
                content="越权写入",
                _context=GROUP_CTX,
            )
            assert "不在允许范围" in result
            assert not (tmp_path / f"{GROUP_ID}9" / "MEMORY.md").exists()
        finally:
            tools.GROUP_MEMORY_ROOT = original_root
            tools._ALLOWED_ROOTS = original_roots

    @pytest.mark.asyncio
    async def test_write_group_invalid_group_id(self):
        ctx = {"_chat_type": "group", "_target_id": "../admin"}
        result = await write_file_tool(
            path="data/groups/../admin/evil.md",
            content="x",
            _context=ctx,
        )
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_write_group_memory_relative_path(self, tmp_path):
        """群聊传相对路径 MEMORY.md 写入本群记忆"""
        from plugins.local_tools import tools
        original_root = tools.GROUP_MEMORY_ROOT
        original_roots = tools._ALLOWED_ROOTS
        tools.GROUP_MEMORY_ROOT = tmp_path
        tools._ALLOWED_ROOTS = []
        try:
            result = await write_file_tool(
                path="MEMORY.md",
                content="相对路径写入",
                _context=GROUP_CTX,
            )
            assert "已写入" in result
            written = (tmp_path / GROUP_ID / "MEMORY.md").read_text(encoding="utf-8")
            assert written == "相对路径写入"
        finally:
            tools.GROUP_MEMORY_ROOT = original_root
            tools._ALLOWED_ROOTS = original_roots

    @pytest.mark.asyncio
    async def test_group_write_size_cap(self, tmp_path):
        """群聊单次写入超上限应拒绝且不落盘"""
        from plugins.local_tools import tools
        original_root = tools.GROUP_MEMORY_ROOT
        original_roots = tools._ALLOWED_ROOTS
        tools.GROUP_MEMORY_ROOT = tmp_path
        tools._ALLOWED_ROOTS = []
        try:
            result = await write_file_tool(
                path="MEMORY.md",
                content="x" * (tools.GROUP_WRITE_MAX_CHARS + 1),
                _context=GROUP_CTX,
            )
            assert "上限" in result
            assert not (tmp_path / GROUP_ID / "MEMORY.md").exists()
        finally:
            tools.GROUP_MEMORY_ROOT = original_root
            tools._ALLOWED_ROOTS = original_roots

    @pytest.mark.asyncio
    async def test_private_write_not_capped(self, tmp_path):
        """私聊 Admin 写入不受群聊上限限制"""
        from plugins.local_tools import tools
        original = tools._ALLOWED_ROOTS
        tools._ALLOWED_ROOTS = [tmp_path]
        try:
            result = await write_file_tool(
                path=str(tmp_path / "big.md"),
                content="x" * (tools.GROUP_WRITE_MAX_CHARS + 1),
                _context=PRIVATE_CTX,
            )
            assert "已写入" in result
        finally:
            tools._ALLOWED_ROOTS = original


# ──────────────────── list_files_tool ────────────────────

class TestListFilesTool:

    @pytest.mark.asyncio
    async def test_group_cannot_list_admin(self):
        result = await list_files_tool(
            path="data/admin",
            _context=GROUP_CTX,
        )
        assert "不在允许范围" in result

    @pytest.mark.asyncio
    async def test_empty_path(self):
        result = await list_files_tool(
            path="",
            _context=PRIVATE_CTX,
        )
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_outside_whitelist(self):
        result = await list_files_tool(
            path="plugins",
            _context=PRIVATE_CTX,
        )
        assert "不在允许范围" in result

    @pytest.mark.asyncio
    async def test_nonexistent_dir(self):
        result = await list_files_tool(
            path="data/admin/no_such_dir_xyz",
            _context=PRIVATE_CTX,
        )
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_list_directory(self, tmp_path):
        (tmp_path / "file1.txt").write_text("a", encoding="utf-8")
        (tmp_path / "file2.md").write_text("b", encoding="utf-8")
        (tmp_path / "subdir").mkdir()

        from plugins.local_tools import tools
        original = tools._ALLOWED_ROOTS
        tools._ALLOWED_ROOTS = [tmp_path]
        try:
            result = await list_files_tool(
                path=str(tmp_path),
                _context=PRIVATE_CTX,
            )
            assert "file1.txt" in result
            assert "file2.md" in result
            assert "subdir" in result
            assert "3 项" in result
        finally:
            tools._ALLOWED_ROOTS = original

    @pytest.mark.asyncio
    async def test_list_empty_directory(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        from plugins.local_tools import tools
        original = tools._ALLOWED_ROOTS
        tools._ALLOWED_ROOTS = [tmp_path]
        try:
            result = await list_files_tool(
                path=str(empty_dir),
                _context=PRIVATE_CTX,
            )
            assert "为空" in result
        finally:
            tools._ALLOWED_ROOTS = original


# ──────────────────── list_files_tool（群聊） ────────────────────

class TestListFilesToolGroup:

    @pytest.mark.asyncio
    async def test_list_group_memory_dir(self, tmp_path):
        from plugins.local_tools import tools
        original_root = tools.GROUP_MEMORY_ROOT
        original_roots = tools._ALLOWED_ROOTS
        tools.GROUP_MEMORY_ROOT = tmp_path
        tools._ALLOWED_ROOTS = []
        try:
            gdir = tmp_path / GROUP_ID
            gdir.mkdir(parents=True)
            (gdir / "MEMORY.md").write_text("x", encoding="utf-8")
            result = await list_files_tool(
                path=str(gdir),
                _context=GROUP_CTX,
            )
            assert "MEMORY.md" in result
        finally:
            tools.GROUP_MEMORY_ROOT = original_root
            tools._ALLOWED_ROOTS = original_roots


# ──────────────────── memory_search_tool（群聊场景） ────────────────────

class TestMemorySearchGroup:

    @pytest.mark.asyncio
    async def test_group_search_no_memory(self):
        """群聊搜索无记忆时返回未找到（不崩溃）"""
        result = await memory_search_tool(
            query="测试",
            _context=GROUP_CTX,
        )
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_group_invalid_group_id(self):
        ctx = {"_chat_type": "group", "_target_id": "abc"}
        result = await memory_search_tool(query="测试", _context=ctx)
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_private_no_context_rejected(self):
        result = await memory_search_tool(query="测试")
        assert "仅限" in result
