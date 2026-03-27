"""
tests/test_compaction.py
─────────────────────────
测试对话历史压缩（Compaction）模块:
  - find_split_point: 分割点计算
  - _format_messages_for_summary: 消息格式化
  - _parse_memory_extractions: 记忆提取解析
  - merge_memories_into_file: 记忆合并到文件
  - compact_history: 完整压缩流程
"""

import sys
import os
import types
import json
import importlib
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Mock nonebot ──
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

_mock_config = MagicMock()
_mock_config.admin_number = "123456"
_mock_config.llm_provider = "gemini"
_mock_config.gemini_api_key = "fake-key"
_mock_config.llm_base_url = ""
_mock_config.llm_model = "test-model"
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)

# ── 构造 plugins 包 ──
if "plugins" not in sys.modules:
    _plugins_pkg = types.ModuleType("plugins")
    _plugins_pkg.__path__ = [str(ROOT / "plugins")]
    sys.modules["plugins"] = _plugins_pkg

if "plugins.chat" not in sys.modules:
    _chat_pkg = types.ModuleType("plugins.chat")
    _chat_pkg.__path__ = [str(ROOT / "plugins" / "chat")]
    sys.modules["plugins.chat"] = _chat_pkg

if "plugins.llm" not in sys.modules:
    import plugins.llm  # noqa

# ── 加载 compaction 模块 ──
_spec = importlib.util.spec_from_file_location(
    "plugins.chat.compaction",
    ROOT / "plugins" / "chat" / "compaction.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["plugins.chat.compaction"] = _mod
_spec.loader.exec_module(_mod)

from plugins.chat.compaction import (
    find_split_point,
    _estimate_tokens,
    _estimate_messages_tokens,
    _format_messages_for_summary,
    _parse_memory_extractions,
    merge_memories_into_file,
    compact_history,
    generate_summary,
    extract_memories,
    COMPACTION_THRESHOLD,
    MIN_TAIL_MESSAGES,
)


# ──────────────────── 辅助 ────────────────────

def _make_messages(count: int, content_size: int = 100) -> list[dict]:
    """生成指定数量的测试消息"""
    msgs = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        # 用中文字填充（每个中文字 ≈ 1.5 token）
        content = f"消息{i}：" + "测试内容" * (content_size // 4)
        msgs.append({"role": role, "content": content})
    return msgs


def _make_large_messages(target_tokens: int) -> list[dict]:
    """生成总 token 约为 target_tokens 的消息列表"""
    msgs = []
    current = 0
    i = 0
    while current < target_tokens:
        role = "user" if i % 2 == 0 else "assistant"
        content = f"第{i}条：" + "这是一段较长的对话内容用来测试压缩" * 10
        msg = {"role": role, "content": content}
        tokens = _estimate_tokens(content) + 4
        msgs.append(msg)
        current += tokens
        i += 1
    return msgs


# ──────────────────── find_split_point ────────────────────

class TestFindSplitPoint:

    def test_below_threshold_returns_zero(self):
        """token 未达阈值时不压缩"""
        msgs = _make_messages(5, content_size=20)
        assert find_split_point(msgs) == 0

    def test_above_threshold_returns_nonzero(self):
        """token 超过阈值时返回分割点"""
        msgs = _make_large_messages(COMPACTION_THRESHOLD + 10000)
        split = find_split_point(msgs)
        assert split > 0
        assert split < len(msgs)

    def test_tail_preserved(self):
        """分割后保留的尾部至少 MIN_TAIL_MESSAGES 条"""
        msgs = _make_large_messages(COMPACTION_THRESHOLD + 50000)
        split = find_split_point(msgs)
        tail_count = len(msgs) - split
        assert tail_count >= MIN_TAIL_MESSAGES

    def test_empty_returns_zero(self):
        assert find_split_point([]) == 0

    def test_too_few_old_returns_zero(self):
        """旧消息少于 4 条时不压缩"""
        # 制造 3 条旧消息 + 多条尾部的情况
        msgs = _make_messages(3, content_size=20)
        assert find_split_point(msgs) == 0


# ──────────────────── _format_messages_for_summary ────────────────────

class TestFormatMessages:

    def test_user_assistant_format(self):
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ]
        result = _format_messages_for_summary(msgs)
        assert "用户: 你好" in result
        assert "助手: 你好呀" in result

    def test_skips_system(self):
        msgs = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "你好"},
        ]
        result = _format_messages_for_summary(msgs)
        assert "系统提示" not in result
        assert "用户: 你好" in result

    def test_empty_list(self):
        result = _format_messages_for_summary([])
        assert result == ""


# ──────────────────── _parse_memory_extractions ────────────────────

class TestParseMemoryExtractions:

    def test_normal_extraction(self):
        raw = (
            "[用户信息与偏好] 碧碧喜欢芝士蛋糕\n"
            "[重要决定与约定] 决定用 PostgreSQL\n"
            "[承诺与待办] 下次帮他查快递\n"
        )
        result = _parse_memory_extractions(raw)
        assert "用户信息与偏好" in result
        assert result["用户信息与偏好"] == ["碧碧喜欢芝士蛋糕"]
        assert result["重要决定与约定"] == ["决定用 PostgreSQL"]
        assert result["承诺与待办"] == ["下次帮他查快递"]

    def test_no_extractions(self):
        assert _parse_memory_extractions("无") == {}
        assert _parse_memory_extractions("") == {}
        assert _parse_memory_extractions(None) == {}

    def test_multiple_in_same_section(self):
        raw = (
            "[对话备忘] 第一件事\n"
            "[对话备忘] 第二件事\n"
        )
        result = _parse_memory_extractions(raw)
        assert len(result["对话备忘"]) == 2

    def test_ignores_unknown_sections(self):
        raw = "[未知分区] 不应该被解析"
        result = _parse_memory_extractions(raw)
        assert len(result) == 0

    def test_empty_entry_ignored(self):
        raw = "[用户信息与偏好] \n[用户信息与偏好] 有效内容"
        result = _parse_memory_extractions(raw)
        assert result["用户信息与偏好"] == ["有效内容"]


# ──────────────────── merge_memories_into_file ────────────────────

class TestMergeMemories:

    def test_append_to_existing_section(self, tmp_path):
        memory_file = tmp_path / "MEMORY.md"
        memory_file.write_text(
            "# MEMORY.md — 长期记忆\n\n"
            "## 用户信息与偏好\n"
            "- [2026-03-20] 已有条目\n\n"
            "## 重要决定与约定\n"
            "_（空）_\n",
            encoding="utf-8",
        )

        extractions = {
            "用户信息与偏好": ["新增偏好"],
            "重要决定与约定": ["新增决定"],
        }
        merge_memories_into_file(memory_file, extractions)

        content = memory_file.read_text(encoding="utf-8")
        assert "已有条目" in content
        assert "新增偏好" in content
        assert "新增决定" in content

    def test_create_new_section(self, tmp_path):
        memory_file = tmp_path / "MEMORY.md"
        memory_file.write_text("# MEMORY.md — 长期记忆\n", encoding="utf-8")

        extractions = {"对话备忘": ["值得记住的事"]}
        merge_memories_into_file(memory_file, extractions)

        content = memory_file.read_text(encoding="utf-8")
        assert "## 对话备忘" in content
        assert "值得记住的事" in content

    def test_empty_extractions(self, tmp_path):
        memory_file = tmp_path / "MEMORY.md"
        memory_file.write_text("# 原始内容\n", encoding="utf-8")
        merge_memories_into_file(memory_file, {})
        assert memory_file.read_text(encoding="utf-8") == "# 原始内容\n"

    def test_nonexistent_file(self, tmp_path):
        memory_file = tmp_path / "NEW_MEMORY.md"
        extractions = {"用户信息与偏好": ["新条目"]}
        merge_memories_into_file(memory_file, extractions)
        content = memory_file.read_text(encoding="utf-8")
        assert "新条目" in content

    def test_date_format(self, tmp_path):
        memory_file = tmp_path / "MEMORY.md"
        memory_file.write_text(
            "# MEMORY.md\n\n## 用户信息与偏好\n",
            encoding="utf-8",
        )
        merge_memories_into_file(memory_file, {"用户信息与偏好": ["测试"]})
        content = memory_file.read_text(encoding="utf-8")
        # 应包含 [YYYY-MM-DD] 格式
        import re
        assert re.search(r"\[\d{4}-\d{2}-\d{2}\]", content)


# ──────────────────── compact_history 完整流程 ────────────────────

class TestCompactHistory:

    @pytest.mark.asyncio
    async def test_no_compaction_below_threshold(self, tmp_path):
        """token 未达阈值时不压缩"""
        session = tmp_path / "history.jsonl"
        memory = tmp_path / "MEMORY.md"
        msgs = _make_messages(5, content_size=20)
        with session.open("w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

        result = await compact_history("test", session, memory)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_file_returns_false(self, tmp_path):
        session = tmp_path / "nonexistent.jsonl"
        memory = tmp_path / "MEMORY.md"
        result = await compact_history("test", session, memory)
        assert result is False

    @pytest.mark.asyncio
    async def test_compaction_rewrites_file(self, tmp_path):
        """压缩后文件应包含摘要消息 + 尾部消息"""
        session = tmp_path / "history.jsonl"
        memory = tmp_path / "MEMORY.md"
        memory.write_text("# MEMORY.md\n\n## 对话备忘\n", encoding="utf-8")

        msgs = _make_large_messages(COMPACTION_THRESHOLD + 30000)
        with session.open("w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

        original_count = len(msgs)

        # Mock LLM 调用
        with patch("plugins.chat.compaction._call_llm", new_callable=AsyncMock) as mock_llm:
            # 第一次调用：记忆提取
            # 第二次调用：摘要生成
            mock_llm.side_effect = [
                "[对话备忘] 用户讨论了测试内容",  # extract_memories
                "这是一段对话摘要，讨论了很多测试内容。",  # generate_summary
            ]

            result = await compact_history("test", session, memory)

        assert result is True

        # 验证文件已重写
        new_messages = []
        for line in session.read_text(encoding="utf-8").splitlines():
            if line.strip():
                new_messages.append(json.loads(line))

        assert len(new_messages) < original_count
        # 第一条应该是摘要
        assert "[前置会话摘要]" in new_messages[0]["content"]

        # 验证记忆已写入
        mem_content = memory.read_text(encoding="utf-8")
        assert "用户讨论了测试内容" in mem_content

    @pytest.mark.asyncio
    async def test_compaction_summary_failure_skips(self, tmp_path):
        """摘要生成失败时不压缩"""
        session = tmp_path / "history.jsonl"
        memory = tmp_path / "MEMORY.md"

        msgs = _make_large_messages(COMPACTION_THRESHOLD + 30000)
        with session.open("w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

        with patch("plugins.chat.compaction._call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [
                "无",   # extract_memories: nothing
                None,   # generate_summary: failure
            ]

            result = await compact_history("test", session, memory)

        assert result is False

        # 文件未被修改
        new_lines = [l for l in session.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(new_lines) == len(msgs)

    @pytest.mark.asyncio
    async def test_compaction_no_memory_extraction(self, tmp_path):
        """记忆提取为空时仍正常压缩"""
        session = tmp_path / "history.jsonl"
        memory = tmp_path / "MEMORY.md"

        msgs = _make_large_messages(COMPACTION_THRESHOLD + 20000)
        with session.open("w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

        with patch("plugins.chat.compaction._call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [
                "无",                              # extract_memories
                "对话摘要：讨论了各种内容。",       # generate_summary
            ]

            result = await compact_history("test", session, memory)

        assert result is True
        # MEMORY.md 不应被创建（没有提取内容）
        assert not memory.exists()


# ──────────────────── Token 估算 ────────────────────

class TestTokenEstimation:

    def test_chinese_text(self):
        tokens = _estimate_tokens("你好世界")  # 4 中文字 ≈ 6 tokens
        assert tokens == 6

    def test_english_text(self):
        tokens = _estimate_tokens("hello world")  # 11 chars / 4 ≈ 2
        assert tokens >= 2

    def test_mixed(self):
        tokens = _estimate_tokens("你好 hello")
        assert tokens > 0

    def test_empty(self):
        assert _estimate_tokens("") == 0

    def test_messages_tokens(self):
        msgs = [{"role": "user", "content": "你好"}]
        tokens = _estimate_messages_tokens(msgs)
        assert tokens == _estimate_tokens("你好") + 4
