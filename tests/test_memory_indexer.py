"""
tests/test_memory_indexer.py
─────────────────────────────
测试 memory indexer 模块:
  - 分块逻辑（chunk_text, chunk_markdown, chunk_jsonl）
  - 索引管理（sources_changed, ensure_index）
  - 搜索（search，mock embedding）
  - 余弦相似度
"""

import sys
import os
import json
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Mock nonebot ──
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

_mock_config = MagicMock()
_mock_config.llm_provider = "gemini"
_mock_config.gemini_api_key = "fake-key"
_mock_config.llm_base_url = ""
_mock_config.llm_model = "test-model"
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)

# ── 确保 plugins 包 ──
if "plugins" not in sys.modules:
    _plugins_pkg = types.ModuleType("plugins")
    _plugins_pkg.__path__ = [str(ROOT / "plugins")]
    sys.modules["plugins"] = _plugins_pkg

if "plugins.llm" not in sys.modules:
    import plugins.llm  # noqa

if "plugins.memory" not in sys.modules:
    _mem_pkg = types.ModuleType("plugins.memory")
    _mem_pkg.__path__ = [str(ROOT / "plugins" / "memory")]
    sys.modules["plugins.memory"] = _mem_pkg

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "plugins.memory.indexer",
    ROOT / "plugins" / "memory" / "indexer.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["plugins.memory.indexer"] = _mod
_spec.loader.exec_module(_mod)

import pytest
from plugins.memory.indexer import (
    chunk_text,
    chunk_markdown,
    chunk_jsonl,
    cosine_similarity,
    sources_changed,
    get_all_chunks,
    _estimate_tokens,
    _tokenize,
    bm25_score_chunks,
    time_decay,
    _extract_chunk_date,
    mmr_rerank,
    CHUNK_TOKENS,
)


# ──────────────────── chunk_text ────────────────────

class TestChunkText:

    def test_short_text_single_chunk(self):
        text = "这是一段短文本"
        chunks = chunk_text(text, "test.md")
        assert len(chunks) == 1
        assert chunks[0]["source"] == "test.md"
        assert chunks[0]["text"] == text
        assert chunks[0]["start_line"] == 1
        assert chunks[0]["end_line"] == 1

    def test_empty_text(self):
        assert chunk_text("", "test.md") == []

    def test_long_text_multiple_chunks(self):
        # 生成超过 CHUNK_TOKENS 的文本
        line = "这是一段需要被分块的较长文本内容" * 5
        text = "\n".join([line] * 30)
        chunks = chunk_text(text, "test.md")
        assert len(chunks) > 1
        # 每个 chunk 都有元数据
        for c in chunks:
            assert "source" in c
            assert "text" in c
            assert "start_line" in c
            assert "end_line" in c

    def test_chunks_have_overlap(self):
        """后一块的开头应包含前一块末尾的部分内容"""
        line = "这是测试重叠的文本行" * 5
        text = "\n".join([f"{i}: {line}" for i in range(50)])
        chunks = chunk_text(text, "test.md")
        if len(chunks) >= 2:
            # 第二块的起始行 <= 第一块的结束行（有重叠）
            assert chunks[1]["start_line"] <= chunks[0]["end_line"] + 1

    def test_whitespace_only_skipped(self):
        text = "   \n\n   \n"
        assert chunk_text(text, "test.md") == []


# ──────────────────── chunk_markdown ────────────────────

class TestChunkMarkdown:

    def test_existing_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Title\nSome content\n## Section\nMore content", encoding="utf-8")
        chunks = chunk_markdown(f)
        assert len(chunks) >= 1
        assert "Title" in chunks[0]["text"]

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nope.md"
        assert chunk_markdown(f) == []

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("", encoding="utf-8")
        assert chunk_markdown(f) == []


# ──────────────────── chunk_jsonl ────────────────────

class TestChunkJsonl:

    def test_normal_conversation(self, tmp_path):
        f = tmp_path / "history.jsonl"
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
            {"role": "user", "content": "今天吃什么"},
            {"role": "assistant", "content": "芝士蛋糕怎么样"},
        ]
        f.write_text(
            "\n".join(json.dumps(m, ensure_ascii=False) for m in messages),
            encoding="utf-8",
        )
        chunks = chunk_jsonl(f)
        assert len(chunks) >= 1
        text = chunks[0]["text"]
        assert "用户:" in text or "助手:" in text

    def test_skips_system_messages(self, tmp_path):
        f = tmp_path / "history.jsonl"
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ]
        f.write_text(
            "\n".join(json.dumps(m, ensure_ascii=False) for m in messages),
            encoding="utf-8",
        )
        chunks = chunk_jsonl(f)
        assert len(chunks) >= 1
        assert "你是助手" not in chunks[0]["text"]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        assert chunk_jsonl(f) == []

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nope.jsonl"
        assert chunk_jsonl(f) == []


# ──────────────────── cosine_similarity ────────────────────

class TestCosineSimilarity:

    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0


# ──────────────────── sources_changed ────────────────────

class TestSourcesChanged:

    def test_no_sources_no_change(self, tmp_path):
        index = {"sources": {}}
        assert sources_changed(index, [tmp_path / "nonexistent.md"]) is False

    def test_new_file_detected(self, tmp_path):
        f = tmp_path / "new.md"
        f.write_text("content", encoding="utf-8")
        index = {"sources": {}}
        assert sources_changed(index, [f]) is True

    def test_unchanged_file(self, tmp_path):
        f = tmp_path / "same.md"
        f.write_text("content", encoding="utf-8")
        stat = f.stat()
        index = {"sources": {str(f): {"mtime": stat.st_mtime, "size": stat.st_size}}}
        assert sources_changed(index, [f]) is False

    def test_modified_file(self, tmp_path):
        f = tmp_path / "mod.md"
        f.write_text("content", encoding="utf-8")
        stat = f.stat()
        index = {"sources": {str(f): {"mtime": stat.st_mtime - 1, "size": stat.st_size}}}
        assert sources_changed(index, [f]) is True

    def test_deleted_file(self, tmp_path):
        f = tmp_path / "deleted.md"
        index = {"sources": {str(f): {"mtime": 1234, "size": 100}}}
        assert sources_changed(index, [f]) is True


# ──────────────────── search (with mocked embedding) ────────────────────

class TestSearch:

    @pytest.mark.asyncio
    async def test_search_returns_results(self, tmp_path):
        """Mock embedding，验证搜索流程完整性"""
        from plugins.memory import indexer

        # 创建测试文件
        memory = tmp_path / "MEMORY.md"
        memory.write_text(
            "## 用户偏好\n- 碧碧喜欢芝士蛋糕\n- 碧碧常用 Python\n\n"
            "## 决定\n- 选择了 PostgreSQL 数据库\n",
            encoding="utf-8",
        )

        # Mock embedding: 返回固定向量（query 与第一个 chunk 接近）
        fake_query_vec = [1.0, 0.0, 0.0]
        fake_chunk_vec = [0.9, 0.1, 0.0]  # 高相似度

        call_count = [0]
        async def mock_embed(texts):
            call_count[0] += 1
            if call_count[0] == 1:
                # 索引阶段：每个 chunk 一个向量
                return [fake_chunk_vec for _ in texts]
            else:
                # 搜索阶段：query 向量
                return [fake_query_vec]

        # 临时修改索引路径和来源
        orig_file = indexer.INDEX_FILE
        orig_sources = indexer.MEMORY_SOURCES
        indexer.INDEX_FILE = tmp_path / ".memory_index.json"
        indexer.MEMORY_SOURCES = [memory]

        try:
            with patch.object(indexer, "embed_texts", side_effect=mock_embed):
                results = await indexer.search("碧碧喜欢什么", max_results=3)

            assert len(results) > 0
            assert results[0]["score"] > 0.3  # 混合分数经过归一化+加权，不会是原始余弦值
            assert "碧碧" in results[0]["text"] or "芝士蛋糕" in results[0]["text"]
        finally:
            indexer.INDEX_FILE = orig_file
            indexer.MEMORY_SOURCES = orig_sources

    @pytest.mark.asyncio
    async def test_search_empty_index(self, tmp_path):
        from plugins.memory import indexer

        orig_file = indexer.INDEX_FILE
        orig_sources = indexer.MEMORY_SOURCES
        indexer.INDEX_FILE = tmp_path / ".memory_index.json"
        indexer.MEMORY_SOURCES = []

        try:
            results = await indexer.search("anything")
            assert results == []
        finally:
            indexer.INDEX_FILE = orig_file
            indexer.MEMORY_SOURCES = orig_sources


# ──────────────────── BM25 Tokenizer ────────────────────

class TestTokenize:

    def test_chinese_bigrams(self):
        tokens = _tokenize("你好世界")
        assert "你好" in tokens
        assert "好世" in tokens
        assert "世界" in tokens

    def test_english_words(self):
        tokens = _tokenize("hello world")
        assert "hello" in tokens
        assert "world" in tokens

    def test_mixed(self):
        tokens = _tokenize("你好hello")
        assert any("你" in t for t in tokens)
        assert "hello" in tokens

    def test_empty(self):
        assert _tokenize("") == []

    def test_single_chinese_char(self):
        tokens = _tokenize("你")
        assert tokens == ["你"]


# ──────────────────── BM25 Score ────────────────────

class TestBM25:

    def test_matching_query_scores_higher(self):
        chunks = [
            {"text": "碧碧喜欢芝士蛋糕"},
            {"text": "今天天气很好"},
        ]
        query_tokens = _tokenize("芝士蛋糕")
        scores = bm25_score_chunks(query_tokens, chunks)
        assert scores[0] > scores[1]

    def test_no_match_zero_score(self):
        chunks = [{"text": "完全不相关的内容"}]
        query_tokens = _tokenize("芝士蛋糕")
        scores = bm25_score_chunks(query_tokens, chunks)
        assert scores[0] == pytest.approx(0.0)

    def test_empty_chunks(self):
        assert bm25_score_chunks(_tokenize("test"), []) == []


# ──────────────────── Time Decay ────────────────────

class TestTimeDecay:

    def test_recent_date_high_decay(self):
        today = datetime.now().strftime("%Y-%m-%d")
        chunk = {"text": f"- [{today}] 最近的事"}
        assert time_decay(chunk) == pytest.approx(1.0, abs=0.01)

    def test_old_date_low_decay(self):
        chunk = {"text": "- [2020-01-01] 很久以前的事"}
        d = time_decay(chunk)
        assert 0.0 < d < 0.5  # 5 年前应该衰减很多

    def test_no_date_no_decay(self):
        chunk = {"text": "没有日期标记的内容"}
        assert time_decay(chunk) == 1.0

    def test_extract_date(self):
        chunk = {"text": "- [2026-03-27] 某件事\n- [2026-04-01] 另一件事"}
        date = _extract_chunk_date(chunk)
        assert date is not None
        assert date.strftime("%Y-%m-%d") == "2026-04-01"  # 取最新的


# ──────────────────── MMR ────────────────────

class TestMMR:

    def test_diverse_selection(self):
        """MMR 应该优先选择跟已选结果不同的候选"""
        query_vec = [1.0, 1.0]  # query 在两个维度都有分量
        candidates = [
            {"text": "A", "score": 0.9, "vector": [1.0, 0.0]},       # 与 query 相关
            {"text": "A_dup", "score": 0.88, "vector": [1.0, 0.0]},   # 与 A 完全相同
            {"text": "B", "score": 0.85, "vector": [0.0, 1.0]},       # 与 query 相关但与 A 正交
        ]
        selected = mmr_rerank(candidates, query_vec, max_results=2, lambda_param=0.5)
        texts = [s["text"] for s in selected]
        # A_dup 跟 A 完全一样会被 MMR 惩罚，B 跟 A 完全不同会被 MMR 奖励
        assert "A" in texts
        assert "B" in texts

    def test_max_results_respected(self):
        query_vec = [1.0, 0.0]
        candidates = [
            {"text": f"item{i}", "score": 0.5, "vector": [0.5, 0.5]}
            for i in range(10)
        ]
        selected = mmr_rerank(candidates, query_vec, max_results=3)
        assert len(selected) == 3

    def test_fewer_than_max(self):
        query_vec = [1.0, 0.0]
        candidates = [
            {"text": "only", "score": 0.9, "vector": [0.9, 0.1]},
        ]
        selected = mmr_rerank(candidates, query_vec, max_results=5)
        assert len(selected) == 1
