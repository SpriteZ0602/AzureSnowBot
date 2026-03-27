"""
tests/test_file_tools.py
─────────────────────────
测试文件系统工具（read_file, write_file, list_files）:
  - 安全校验：admin_only + 路径白名单
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

import pytest
from plugins.local_tools.tools import (
    read_file_tool,
    write_file_tool,
    list_files_tool,
    _check_admin_only,
    _resolve_safe_path,
)


# ──────────────────── _check_admin_only ────────────────────

class TestCheckAdminOnly:

    def test_no_context(self):
        assert _check_admin_only(None) is not None
        assert "Admin" in _check_admin_only(None) or "仅限" in _check_admin_only(None)

    def test_group_rejected(self):
        err = _check_admin_only({"_chat_type": "group"})
        assert err is not None
        assert "仅限" in err

    def test_private_allowed(self):
        err = _check_admin_only({"_chat_type": "private"})
        assert err is None


# ──────────────────── _resolve_safe_path ────────────────────

class TestResolveSafePath:

    def test_valid_admin_path(self):
        _, err = _resolve_safe_path("data/admin/MEMORY.md")
        assert err is None

    def test_valid_personas_path(self):
        _, err = _resolve_safe_path("data/personas/default.txt")
        assert err is None

    def test_valid_skills_path(self):
        _, err = _resolve_safe_path("data/skills/web-search")
        assert err is None

    def test_blocked_outside_whitelist(self):
        _, err = _resolve_safe_path("plugins/llm.py")
        assert err is not None
        assert "不在允许范围" in err

    def test_blocked_root_path(self):
        _, err = _resolve_safe_path("pyproject.toml")
        assert err is not None

    def test_path_traversal_blocked(self):
        """尝试 .. 遍历逃出白名单"""
        _, err = _resolve_safe_path("data/admin/../../pyproject.toml")
        assert err is not None

    def test_absolute_path_outside(self):
        _, err = _resolve_safe_path("C:/Windows/System32/cmd.exe")
        assert err is not None


# ──────────────────── read_file_tool ────────────────────

class TestReadFileTool:

    @pytest.mark.asyncio
    async def test_rejected_without_context(self):
        result = await read_file_tool(path="data/admin/MEMORY.md")
        assert "仅限" in result

    @pytest.mark.asyncio
    async def test_rejected_in_group(self):
        result = await read_file_tool(
            path="data/admin/MEMORY.md",
            _context={"_chat_type": "group"},
        )
        assert "仅限" in result

    @pytest.mark.asyncio
    async def test_empty_path(self):
        result = await read_file_tool(
            path="",
            _context={"_chat_type": "private"},
        )
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
                _context={"_chat_type": "private"},
            )
            assert "hello world" in result
        finally:
            tools._ALLOWED_ROOTS = original

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self):
        result = await read_file_tool(
            path="data/admin/nonexistent_file_xyz.md",
            _context={"_chat_type": "private"},
        )
        assert "不存在" in result

    @pytest.mark.asyncio
    async def test_read_outside_whitelist(self):
        result = await read_file_tool(
            path="plugins/llm.py",
            _context={"_chat_type": "private"},
        )
        assert "不在允许范围" in result

    @pytest.mark.asyncio
    async def test_path_traversal(self):
        result = await read_file_tool(
            path="data/admin/../../.env",
            _context={"_chat_type": "private"},
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
                _context={"_chat_type": "private"},
            )
            assert "为空" in result
        finally:
            tools._ALLOWED_ROOTS = original


# ──────────────────── write_file_tool ────────────────────

class TestWriteFileTool:

    @pytest.mark.asyncio
    async def test_rejected_in_group(self):
        result = await write_file_tool(
            path="data/admin/MEMORY.md",
            content="test",
            _context={"_chat_type": "group"},
        )
        assert "仅限" in result

    @pytest.mark.asyncio
    async def test_empty_path(self):
        result = await write_file_tool(
            path="",
            content="test",
            _context={"_chat_type": "private"},
        )
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_write_outside_whitelist(self):
        result = await write_file_tool(
            path="plugins/llm.py",
            content="hacked",
            _context={"_chat_type": "private"},
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
                _context={"_chat_type": "private"},
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
                _context={"_chat_type": "private"},
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
                _context={"_chat_type": "private"},
            )
            assert "5" in result
        finally:
            tools._ALLOWED_ROOTS = original


# ──────────────────── list_files_tool ────────────────────

class TestListFilesTool:

    @pytest.mark.asyncio
    async def test_rejected_in_group(self):
        result = await list_files_tool(
            path="data/admin",
            _context={"_chat_type": "group"},
        )
        assert "仅限" in result

    @pytest.mark.asyncio
    async def test_empty_path(self):
        result = await list_files_tool(
            path="",
            _context={"_chat_type": "private"},
        )
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_outside_whitelist(self):
        result = await list_files_tool(
            path="plugins",
            _context={"_chat_type": "private"},
        )
        assert "不在允许范围" in result

    @pytest.mark.asyncio
    async def test_nonexistent_dir(self):
        result = await list_files_tool(
            path="data/admin/no_such_dir_xyz",
            _context={"_chat_type": "private"},
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
                _context={"_chat_type": "private"},
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
                _context={"_chat_type": "private"},
            )
            assert "为空" in result
        finally:
            tools._ALLOWED_ROOTS = original
