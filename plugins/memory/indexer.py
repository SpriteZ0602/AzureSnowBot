"""
记忆向量索引与语义搜索
──────────────────────
对 MEMORY.md 和 history.jsonl 做 Embedding 索引，
提供 memory_search 语义搜索能力。

架构：
  - 分块：Markdown 按段落切块，JSONL 按消息组切块
  - Embedding：调用 LLM Provider 的 Embedding API
  - 存储：JSON 文件（向量 + 元数据）
  - 检索：混合搜索（向量 + BM25）+ MMR 去重 + 时间衰减
  - 同步：文件 mtime 变化时自动重建索引
"""

import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
from nonebot.log import logger

from ..llm import API_KEY, BASE_URL, LLM_PROVIDER

# ──────────────────── Embedding 模型配置 ────────────────────

_EMBEDDING_MODELS: dict[str, str] = {
    "gemini": "text-embedding-004",
    "openai": "text-embedding-3-small",
    "qwen": "text-embedding-v3",
}
EMBEDDING_MODEL = _EMBEDDING_MODELS.get(LLM_PROVIDER, "text-embedding-004")

# ──────────────────── 分块配置 ────────────────────

CHUNK_TOKENS = 400      # 每块最大 token 数
CHUNK_OVERLAP = 80       # 块间重叠 token 数
EMBEDDING_TIMEOUT = 60   # API 超时
BATCH_SIZE = 100         # 每批最多发送文本数

# ──────────────────── 混合搜索配置 ────────────────────

VECTOR_WEIGHT = 0.7      # 向量搜索权重
BM25_WEIGHT = 0.3        # BM25 文本搜索权重
BM25_K1 = 1.5            # BM25 参数：词频饱和度
BM25_B = 0.75            # BM25 参数：文档长度归一化
MMR_LAMBDA = 0.7         # MMR 多样性参数（1.0=纯相关，0.0=纯多样性）
TIME_DECAY_HALF_LIFE = 30  # 时间衰减半衰期（天）

# ──────────────────── 索引路径 ────────────────────

INDEX_DIR = Path("data/admin")
INDEX_FILE = INDEX_DIR / ".memory_index.json"

# 索引来源（仅 Admin 私聊）
MEMORY_SOURCES = [
    INDEX_DIR / "MEMORY.md",
    INDEX_DIR / "history.jsonl",
]


# ──────────────────── Token 估算 ────────────────────

def _estimate_tokens(text: str) -> int:
    cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cn
    return int(cn * 1.5 + other / 4)


# ──────────────────── 分块 ────────────────────

def chunk_text(text: str, source: str) -> list[dict]:
    """将文本按 token 限制切成块，带重叠"""
    lines = text.splitlines()
    if not lines:
        return []

    chunks: list[dict] = []
    current_lines: list[str] = []
    current_tokens = 0
    start_line = 1

    for i, line in enumerate(lines, 1):
        line_tokens = _estimate_tokens(line) + 1  # +1 for newline
        if current_tokens + line_tokens > CHUNK_TOKENS and current_lines:
            chunk_text_str = "\n".join(current_lines)
            if chunk_text_str.strip():
                chunks.append({
                    "source": source,
                    "text": chunk_text_str,
                    "start_line": start_line,
                    "end_line": i - 1,
                })
            # 保留尾部作为重叠
            overlap_tokens = 0
            overlap_start = len(current_lines)
            for j in range(len(current_lines) - 1, -1, -1):
                lt = _estimate_tokens(current_lines[j]) + 1
                if overlap_tokens + lt > CHUNK_OVERLAP:
                    break
                overlap_tokens += lt
                overlap_start = j
            current_lines = current_lines[overlap_start:]
            current_tokens = sum(_estimate_tokens(l) + 1 for l in current_lines)
            start_line = i - len(current_lines)

        current_lines.append(line)
        current_tokens += line_tokens

    # 最后一块
    if current_lines:
        chunk_text_str = "\n".join(current_lines)
        if chunk_text_str.strip():
            chunks.append({
                "source": source,
                "text": chunk_text_str,
                "start_line": start_line,
                "end_line": len(lines),
            })

    return chunks


def chunk_markdown(path: Path) -> list[dict]:
    """对 Markdown 文件分块"""
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []
    return chunk_text(content, str(path))


def chunk_jsonl(path: Path) -> list[dict]:
    """对 JSONL 对话文件分块（合并 user/assistant 消息后再切块）"""
    if not path.exists():
        return []

    lines: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            label = "用户" if role == "user" else "助手"
            lines.append(f"{label}: {content}")

    if not lines:
        return []
    return chunk_text("\n".join(lines), str(path))


def get_all_chunks(sources: list[Path] | None = None) -> list[dict]:
    """从所有来源获取分块"""
    if sources is None:
        sources = MEMORY_SOURCES
    all_chunks: list[dict] = []
    for src in sources:
        if not src.exists():
            continue
        if src.suffix == ".jsonl":
            all_chunks.extend(chunk_jsonl(src))
        else:
            all_chunks.extend(chunk_markdown(src))
    return all_chunks


# ──────────────────── Embedding API ────────────────────

async def embed_texts(texts: list[str]) -> list[list[float]]:
    """调用 Embedding API 获取向量"""
    if not texts:
        return []
    if not API_KEY:
        raise RuntimeError("未配置 API Key，无法调用 Embedding API")

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    all_vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        async with httpx.AsyncClient(timeout=EMBEDDING_TIMEOUT) as client:
            resp = await client.post(
                f"{BASE_URL}/embeddings",
                headers=headers,
                json={"model": EMBEDDING_MODEL, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            from ..token_stats import record_usage
            record_usage("embedding", data.get("usage"))
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            all_vectors.extend(
                [round(v, 6) for v in d["embedding"]]
                for d in sorted_data
            )

    return all_vectors


# ──────────────────── 余弦相似度 ────────────────────

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ──────────────────── 索引管理 ────────────────────

def _load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": 2, "sources": {}, "chunks": []}


def _save_index(index: dict) -> None:
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False),
        encoding="utf-8",
    )


def sources_changed(index: dict, sources: list[Path] | None = None) -> bool:
    """检查源文件是否有变化（通过 mtime + size）"""
    if sources is None:
        sources = MEMORY_SOURCES
    stored = index.get("sources", {})

    for src in sources:
        key = str(src)
        if not src.exists():
            if key in stored:
                return True  # 文件被删除
            continue
        stat = src.stat()
        info = stored.get(key)
        if not info:
            return True  # 新文件
        if stat.st_mtime != info.get("mtime") or stat.st_size != info.get("size"):
            return True  # 文件已修改
    return False


def _chunk_fingerprint(chunk: dict) -> str:
    """生成 chunk 的指纹，用于去重"""
    # 用 source + text 的前 200 字符作为指纹（足够区分不同 chunk）
    text = chunk.get("text", "")[:200]
    source = chunk.get("source", "")
    return f"{source}::{text}"


async def ensure_index(sources: list[Path] | None = None) -> dict:
    """
    确保索引是最新的。增量追加模式：
    - 保留旧索引中所有已有的 chunk（包括被 compaction 删掉的对话内容）
    - 只对新增/变化的 chunk 调用 Embedding API
    - 源文件里已不存在的 chunk 仍然保留在索引中（可被搜索到）
    """
    if sources is None:
        sources = MEMORY_SOURCES

    index = _load_index()

    if not sources_changed(index, sources):
        return index

    logger.info("Memory 索引需要更新，开始增量更新...")

    # 获取当前源文件的所有 chunk
    new_chunks = get_all_chunks(sources)

    # 构建已有 chunk 的指纹集合
    existing_fingerprints: set[str] = set()
    for chunk in index.get("chunks", []):
        existing_fingerprints.add(_chunk_fingerprint(chunk))

    # 找出需要新建 embedding 的 chunk（不在已有索引中的）
    chunks_to_embed: list[dict] = []
    for chunk in new_chunks:
        fp = _chunk_fingerprint(chunk)
        if fp not in existing_fingerprints:
            chunks_to_embed.append(chunk)

    if chunks_to_embed:
        # 只对新 chunk 调用 Embedding API
        texts = [c["text"] for c in chunks_to_embed]
        try:
            vectors = await embed_texts(texts)
            for chunk, vector in zip(chunks_to_embed, vectors):
                chunk["vector"] = vector
            logger.info(f"Memory 索引: 新增 {len(chunks_to_embed)} 个块的 embedding")
        except Exception as e:
            logger.error(f"Embedding 调用失败: {e}")
            return index  # 返回旧索引，不崩溃

    # 合并：保留旧索引 + 追加新 chunk
    merged_chunks = list(index.get("chunks", []))
    merged_fingerprints = set(existing_fingerprints)
    for chunk in chunks_to_embed:
        fp = _chunk_fingerprint(chunk)
        if fp not in merged_fingerprints:
            merged_chunks.append(chunk)
            merged_fingerprints.add(fp)

    # 更新源文件状态
    new_sources: dict = {}
    for src in sources:
        if src.exists():
            stat = src.stat()
            new_sources[str(src)] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }

    index = {"version": 2, "sources": new_sources, "chunks": merged_chunks}
    _save_index(index)
    logger.info(
        f"Memory 索引更新完成: 总计 {len(merged_chunks)} 个块 "
        f"(新增 {len(chunks_to_embed)}, 保留 {len(merged_chunks) - len(chunks_to_embed)})"
    )
    return index


# ──────────────────── BM25 文本搜索 ────────────────────

def _tokenize(text: str) -> list[str]:
    """中文按 bigram 切分，英文按空格/标点切分（免分词库）"""
    tokens: list[str] = []
    cn_buf: list[str] = []
    en_buf: list[str] = []

    def _flush_cn():
        nonlocal cn_buf
        if cn_buf:
            for k in range(len(cn_buf) - 1):
                tokens.append(cn_buf[k] + cn_buf[k + 1])
            if len(cn_buf) == 1:
                tokens.append(cn_buf[0])
            cn_buf = []

    def _flush_en():
        nonlocal en_buf
        if en_buf:
            tokens.append("".join(en_buf))
            en_buf = []

    for ch in text.lower():
        if "\u4e00" <= ch <= "\u9fff":
            _flush_en()
            cn_buf.append(ch)
        elif ch.isalnum():
            _flush_cn()
            en_buf.append(ch)
        else:
            _flush_cn()
            _flush_en()

    _flush_cn()
    _flush_en()
    return tokens


def bm25_score_chunks(
    query_tokens: list[str],
    chunks: list[dict],
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> list[float]:
    """计算所有 chunk 的 BM25 分数"""
    # 预计算文档频率
    n = len(chunks)
    if n == 0:
        return []

    doc_tokens_list = [_tokenize(c["text"]) for c in chunks]
    doc_freqs: Counter[str] = Counter()
    for dt in doc_tokens_list:
        for t in set(dt):
            doc_freqs[t] += 1

    avg_dl = sum(len(dt) for dt in doc_tokens_list) / n if n > 0 else 1

    scores: list[float] = []
    for dt in doc_tokens_list:
        dl = len(dt)
        tf_map = Counter(dt)
        score = 0.0
        for qt in query_tokens:
            if qt not in doc_freqs:
                continue
            tf = tf_map.get(qt, 0)
            df = doc_freqs[qt]
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1)
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
            score += idf * tf_norm
        scores.append(score)

    return scores


# ──────────────────── 时间衰减 ────────────────────

_DATE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")


def _extract_chunk_date(chunk: dict) -> datetime | None:
    """从 chunk 文本中提取最新的日期标记 [YYYY-MM-DD]"""
    matches = _DATE_RE.findall(chunk.get("text", ""))
    if not matches:
        return None
    try:
        return max(datetime.strptime(d, "%Y-%m-%d") for d in matches)
    except ValueError:
        return None


def time_decay(chunk: dict, half_life_days: float = TIME_DECAY_HALF_LIFE) -> float:
    """
    计算时间衰减系数 (0, 1]。
    有日期标记的 chunk 越老分数越低，无日期的返回 1.0（不衰减）。
    """
    date = _extract_chunk_date(chunk)
    if date is None:
        return 1.0
    age_days = (datetime.now() - date).days
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


# ──────────────────── MMR 去重 ────────────────────

def mmr_rerank(
    candidates: list[dict],
    query_vector: list[float],
    max_results: int,
    lambda_param: float = MMR_LAMBDA,
) -> list[dict]:
    """
    Maximal Marginal Relevance 重排序。
    在相关性和多样性之间取平衡，避免返回内容相似的结果。
    """
    if len(candidates) <= max_results:
        return candidates

    selected: list[dict] = []
    remaining = list(candidates)

    while len(selected) < max_results and remaining:
        best_idx = -1
        best_mmr = -float("inf")

        for i, cand in enumerate(remaining):
            cand_vec = cand.get("vector", [])
            if not cand_vec:
                continue

            # 与 query 的相关性
            relevance = cosine_similarity(cand_vec, query_vector)

            # 与已选结果的最大相似度
            max_sim = 0.0
            for sel in selected:
                sel_vec = sel.get("vector", [])
                if sel_vec:
                    sim = cosine_similarity(cand_vec, sel_vec)
                    if sim > max_sim:
                        max_sim = sim

            # MMR = λ * relevance - (1-λ) * max_similarity_to_selected
            mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim

            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = i

        if best_idx < 0:
            break

        selected.append(remaining.pop(best_idx))

    return selected


# ──────────────────── 混合搜索 ────────────────────

async def search(query: str, max_results: int = 5) -> list[dict]:
    """
    混合搜索 MEMORY.md + history.jsonl。

    流程：
    1. 向量搜索：query embedding × chunk embedding → 余弦相似度
    2. BM25 搜索：query tokens × chunk tokens → BM25 分数
    3. 混合：归一化后加权合并（0.7 向量 + 0.3 BM25）
    4. 时间衰减：有日期标记的 chunk 越老分数越低
    5. MMR 去重：避免返回内容相似的结果

    返回 [{source, text, start_line, end_line, score}, ...]
    """
    index = await ensure_index()
    chunks = index.get("chunks", [])
    if not chunks:
        return []

    # 1. 向量搜索
    try:
        query_vectors = await embed_texts([query])
        query_vector = query_vectors[0]
    except Exception as e:
        logger.error(f"Query embedding 失败: {e}")
        query_vector = None

    if query_vector:
        vector_scores = [
            cosine_similarity(query_vector, c.get("vector", []))
            for c in chunks
        ]
    else:
        vector_scores = [0.0] * len(chunks)

    # 2. BM25 搜索
    query_tokens = _tokenize(query)
    bm25_scores = bm25_score_chunks(query_tokens, chunks)

    # 3. 归一化 + 加权合并
    def _normalize(scores: list[float]) -> list[float]:
        if not scores:
            return scores
        max_s = max(scores)
        min_s = min(scores)
        r = max_s - min_s
        if r == 0:
            return [0.5] * len(scores)
        return [(s - min_s) / r for s in scores]

    norm_vector = _normalize(vector_scores)
    norm_bm25 = _normalize(bm25_scores)

    hybrid_scores = [
        VECTOR_WEIGHT * v + BM25_WEIGHT * b
        for v, b in zip(norm_vector, norm_bm25)
    ]

    # 4. 时间衰减
    for i, chunk in enumerate(chunks):
        decay = time_decay(chunk)
        hybrid_scores[i] *= decay

    # 构建候选列表（带向量，供 MMR 用）
    candidates: list[dict] = []
    for i, chunk in enumerate(chunks):
        candidates.append({
            "source": chunk["source"],
            "text": chunk["text"],
            "start_line": chunk.get("start_line", 0),
            "end_line": chunk.get("end_line", 0),
            "score": round(hybrid_scores[i], 4),
            "vector": chunk.get("vector", []),
        })

    # 按分数排序，取 top 候选（MMR 的输入池）
    candidates.sort(key=lambda x: x["score"], reverse=True)
    pool_size = min(max_results * 3, len(candidates))  # MMR 从 3x 候选中选
    pool = candidates[:pool_size]

    # 5. MMR 去重
    if query_vector:
        selected = mmr_rerank(pool, query_vector, max_results)
    else:
        selected = pool[:max_results]

    # 清理输出（不返回向量）
    for item in selected:
        item.pop("vector", None)

    # 按分数重新排序（MMR 可能打乱顺序）
    selected.sort(key=lambda x: x["score"], reverse=True)
    return selected
