"""
tests/test_chunker.py
──────────────────────
测试 chunker 模块的文本拆分逻辑:
  - chunk_text: 短文本不拆、按换行拆、超长行硬切
"""

import sys
import os

# 将项目根目录加入 sys.path，使 plugins 可作为顶层包导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from plugins.chunker import chunk_text, CHUNK_THRESHOLD, MAX_CHUNK_CHARS


# ──────────────────── 短文本不拆 ────────────────────

class TestChunkTextShort:
    """短文本应整条返回，不拆分"""

    def test_empty_string(self):
        assert chunk_text("") == []

    def test_whitespace_only(self):
        assert chunk_text("   \n  ") == []

    def test_short_text(self):
        text = "你好呀"
        result = chunk_text(text)
        assert result == [text]

    def test_exactly_at_threshold(self):
        text = "a" * CHUNK_THRESHOLD
        result = chunk_text(text)
        assert result == [text]

    def test_one_char_below_threshold(self):
        text = "x" * (CHUNK_THRESHOLD - 1)
        result = chunk_text(text)
        assert result == [text]


# ──────────────────── 按换行拆分 ────────────────────

class TestChunkTextNewline:
    """超过阈值的文本应按换行符拆分成多条"""

    def test_two_lines(self):
        line1 = "这是第一行消息" * 5
        line2 = "这是第二行消息" * 5
        text = f"{line1}\n{line2}"
        assert len(text) > CHUNK_THRESHOLD  # 确保超过阈值
        result = chunk_text(text)
        assert result == [line1, line2]

    def test_multiple_lines(self):
        lines = [f"第{i}行消息内容" for i in range(10)]
        text = "\n".join(lines)
        assert len(text) > CHUNK_THRESHOLD
        result = chunk_text(text)
        assert result == lines

    def test_blank_lines_are_skipped(self):
        """空行应被忽略"""
        text = "a" * 40 + "\n\n\n" + "b" * 40
        assert len(text) > CHUNK_THRESHOLD
        result = chunk_text(text)
        assert result == ["a" * 40, "b" * 40]

    def test_lines_with_only_whitespace_are_skipped(self):
        """仅含空白的行应被忽略"""
        text = "a" * 40 + "\n   \n" + "b" * 40
        assert len(text) > CHUNK_THRESHOLD
        result = chunk_text(text)
        assert result == ["a" * 40, "b" * 40]

    def test_leading_trailing_whitespace_stripped(self):
        """每行的首尾空白应被去除"""
        text = "  前面有空格  \n  后面也有  " + "x" * 50
        result = chunk_text(text)
        assert all(line == line.strip() for line in result)


# ──────────────────── 超长行硬切 ────────────────────

class TestChunkTextLongLine:
    """超过 MAX_CHUNK_CHARS 的单行应被硬切"""

    def test_single_long_line(self):
        text = "字" * (MAX_CHUNK_CHARS + 50)
        result = chunk_text(text)
        assert len(result) == 2
        assert result[0] == "字" * MAX_CHUNK_CHARS
        assert result[1] == "字" * 50

    def test_very_long_line_multiple_chunks(self):
        text = "x" * (MAX_CHUNK_CHARS * 3 + 10)
        result = chunk_text(text)
        assert len(result) == 4
        for chunk in result[:-1]:
            assert len(chunk) == MAX_CHUNK_CHARS
        assert len(result[-1]) == 10

    def test_mixed_normal_and_long_lines(self):
        """正常行和超长行混合"""
        short_line = "正常行"
        long_line = "长" * (MAX_CHUNK_CHARS + 30)
        text = f"{short_line}\n{long_line}"
        result = chunk_text(text)
        assert result[0] == short_line
        assert result[1] == "长" * MAX_CHUNK_CHARS
        assert result[2] == "长" * 30


# ──────────────────── 边界情况 ────────────────────

class TestChunkTextEdge:
    """边界情况"""

    def test_single_newline_only(self):
        assert chunk_text("\n") == []

    def test_exactly_max_chars(self):
        """恰好 MAX_CHUNK_CHARS 长的行不应被切"""
        text = "y" * MAX_CHUNK_CHARS + "\n" + "z" * 10
        result = chunk_text(text)
        assert result[0] == "y" * MAX_CHUNK_CHARS

    def test_preserves_content_integrity(self):
        """验证拆分后拼起来等于原文（去空行后）"""
        lines = ["第一段落内容不短" * 3, "第二段落" * 5, "第三段" * 10]
        text = "\n".join(lines)
        result = chunk_text(text)
        reassembled = "\n".join(result)
        # 去除空行后内容应一致
        original_stripped = "\n".join(
            line.strip() for line in text.split("\n") if line.strip()
        )
        assert reassembled == original_stripped
