"""
对话历史压缩（Compaction）
───────────────────────
当对话历史 token 超过阈值时，调用 LLM 对旧消息生成摘要，
替换原始消息。同时提取重要信息写入 MEMORY.md。

参考 OpenClaw 的 compaction 策略：
  - 保留最近的消息完整（tail）
  - 将旧消息分块调用 LLM 生成摘要
  - 摘要中必须保留：当前任务状态、重要决定、承诺、标识符
  - 提取值得长期记忆的事实写入 MEMORY.md
"""

import json
from datetime import datetime
from pathlib import Path

import httpx
from nonebot.log import logger

from ..llm import API_KEY, BASE_URL, MODEL

# ──────────────────── 阈值配置 ────────────────────

# 当历史 token 超过此值时触发 compaction
COMPACTION_THRESHOLD = 80_000

# 压缩后目标：保留最近消息的占比（最近 40% 的 token 保留完整）
TAIL_RATIO = 0.4

# 最少保留的消息条数（即使 token 很少也不会压缩掉最后 N 条）
MIN_TAIL_MESSAGES = 10

# Compaction LLM 调用超时
COMPACTION_TIMEOUT = 120

# 摘要系统 prompt — 生成对话摘要
SUMMARY_SYSTEM_PROMPT = """\
你是一个对话摘要助手。请将以下对话历史压缩成一段简洁的摘要。

必须保留：
- 当前正在进行的任务及其状态（进行中、已完成、待定）
- 用户最后提出的请求和你正在做的事
- 做出的决定及其理由
- 未完成的待办事项、承诺、约定
- 所有具体的标识符（ID、文件名、URL、数字等）原样保留
- 用户透露的个人信息、偏好

优先保留近期内容，远期内容可以更概括。
输出纯文本摘要，不要用 markdown 格式，不要加标题。"""

# 记忆提取系统 prompt — 从被压缩的对话中提取值得长期记忆的信息
MEMORY_EXTRACT_SYSTEM_PROMPT = """\
你是一个记忆提取助手。从以下即将被压缩的对话中，提取值得长期记忆的信息。

需要提取的类型：
- 用户的个人信息、偏好、习惯
- 重要的决定和约定
- 用户提到的人名、项目名等持久性信息
- 你做出的承诺或待办事项

不需要提取的：
- 临时性的闲聊内容
- 已经在 MEMORY.md 里记录过的信息
- 临时情绪状态

输出格式（每条一行，带分区标签）：
[用户信息与偏好] 具体内容
[重要决定与约定] 具体内容
[承诺与待办] 具体内容
[对话备忘] 具体内容

如果没有值得提取的信息，只输出：无"""


# ──────────────────── Token 估算 ────────────────────

def _estimate_tokens(text: str) -> int:
    """中文约 1 字 ≈ 1.5 token，英文/数字约 4 字符 ≈ 1 token"""
    cn_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - cn_chars
    return int(cn_chars * 1.5 + other_chars / 4)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """估算消息列表的总 token 数"""
    return sum(_estimate_tokens(m.get("content", "")) + 4 for m in messages)


# ──────────────────── 分割点计算 ────────────────────

def find_split_point(messages: list[dict], tail_ratio: float = TAIL_RATIO) -> int:
    """
    找到旧消息和保留消息的分割点。

    返回分割索引 split_idx：
      - messages[:split_idx] → 旧消息（待压缩）
      - messages[split_idx:] → 保留消息（完整保留）

    返回 0 表示消息太少，不值得压缩。
    """
    total_tokens = _estimate_messages_tokens(messages)

    # 目标：保留尾部 tail_ratio 的 token
    tail_budget = int(total_tokens * tail_ratio)

    # 从最新往前数，找到保留范围
    tail_tokens = 0
    tail_start = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        cost = _estimate_tokens(messages[i].get("content", "")) + 4
        if tail_tokens + cost > tail_budget:
            break
        tail_tokens += cost
        tail_start = i

    # 至少保留 MIN_TAIL_MESSAGES 条
    tail_start = min(tail_start, max(0, len(messages) - MIN_TAIL_MESSAGES))

    # 如果旧消息太少（< 4 条），不值得压缩
    if tail_start < 4:
        return 0

    return tail_start


def should_compact(messages: list[dict]) -> bool:
    """检查对话历史是否超过压缩阈值"""
    return _estimate_messages_tokens(messages) >= COMPACTION_THRESHOLD


# ──────────────────── LLM 调用 ────────────────────

async def _call_llm(system_prompt: str, user_content: str) -> str | None:
    """调用 LLM 获取响应，失败返回 None"""
    if not API_KEY:
        logger.warning("Compaction: 未配置 API Key，跳过")
        return None

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=COMPACTION_TIMEOUT) as client:
            resp = await client.post(
                f"{BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        logger.error(f"Compaction LLM 调用失败: {e}")
        return None


# ──────────────────── 摘要生成 ────────────────────

def _format_messages_for_summary(messages: list[dict]) -> str:
    """将消息列表格式化为文本，供 LLM 摘要"""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            lines.append(f"用户: {content}")
        elif role == "assistant":
            lines.append(f"助手: {content}")
        elif role == "system":
            # 跳过 system 消息（摘要不需要包含 system prompt）
            continue
        else:
            lines.append(f"[{role}]: {content}")
    return "\n".join(lines)


async def generate_summary(messages: list[dict]) -> str | None:
    """对消息列表生成摘要，失败返回 None"""
    text = _format_messages_for_summary(messages)
    if not text.strip():
        return None

    return await _call_llm(SUMMARY_SYSTEM_PROMPT, text)


# ──────────────────── 记忆提取 ────────────────────

def _parse_memory_extractions(raw: str) -> dict[str, list[str]]:
    """
    解析 LLM 返回的记忆提取结果。
    返回 {分区名: [条目列表]}
    """
    if not raw or raw.strip() == "无":
        return {}

    section_map = {
        "用户信息与偏好": "用户信息与偏好",
        "重要决定与约定": "重要决定与约定",
        "承诺与待办": "承诺与待办",
        "对话备忘": "对话备忘",
    }

    result: dict[str, list[str]] = {}
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # 解析 [分区名] 内容 格式
        for key_tag, section_name in section_map.items():
            prefix = f"[{key_tag}]"
            if line.startswith(prefix):
                entry = line[len(prefix):].strip()
                if entry:
                    result.setdefault(section_name, []).append(entry)
                break
    return result


async def extract_memories(messages: list[dict], existing_memory: str) -> dict[str, list[str]]:
    """
    从即将被压缩的对话中提取值得长期记忆的信息。
    existing_memory: 当前 MEMORY.md 的内容，供 LLM 避免重复。
    返回 {分区名: [条目列表]}，空字典表示无需提取。
    """
    text = _format_messages_for_summary(messages)
    if not text.strip():
        return {}

    prompt = MEMORY_EXTRACT_SYSTEM_PROMPT
    if existing_memory.strip():
        prompt += f"\n\n当前 MEMORY.md 内容（避免重复提取）:\n{existing_memory}"

    raw = await _call_llm(prompt, text)
    if not raw:
        return {}

    return _parse_memory_extractions(raw)


def merge_memories_into_file(memory_path: Path, extractions: dict[str, list[str]]) -> None:
    """
    将提取的记忆条目合并到 MEMORY.md 文件中。
    在对应分区下追加新条目。
    """
    if not extractions:
        return

    today = datetime.now().strftime("%Y-%m-%d")

    # 读取现有内容
    if memory_path.exists():
        content = memory_path.read_text(encoding="utf-8")
    else:
        content = "# MEMORY.md — 长期记忆\n"

    lines = content.splitlines()
    new_lines: list[str] = []
    processed_sections: set[str] = set()
    i = 0

    while i < len(lines):
        line = lines[i]
        new_lines.append(line)

        # 检查是否匹配某个分区标题
        for section_name, entries in extractions.items():
            if section_name in processed_sections:
                continue
            if line.strip() == f"## {section_name}":
                processed_sections.add(section_name)
                # 跳过分区标题后的占位符行（以 _ 开头的斜体说明）
                if i + 1 < len(lines) and lines[i + 1].strip().startswith("_"):
                    i += 1
                    new_lines.append(lines[i])
                # 追加新条目
                for entry in entries:
                    new_lines.append(f"- [{today}] {entry}")
                break
        i += 1

    # 处理未找到对应分区的提取内容（追加到文件末尾）
    for section_name, entries in extractions.items():
        if section_name not in processed_sections:
            new_lines.append("")
            new_lines.append(f"## {section_name}")
            for entry in entries:
                new_lines.append(f"- [{today}] {entry}")

    memory_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    logger.info(f"Compaction: 已将 {sum(len(v) for v in extractions.values())} 条记忆写入 MEMORY.md")


# ──────────────────── 核心 Compaction ────────────────────

def _rewrite_history(session_path: Path, messages: list[dict]) -> None:
    """用新的消息列表覆盖 JSONL 文件"""
    with session_path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


async def compact_history(
    user_id: str,
    session_path: Path,
    memory_path: Path,
) -> bool:
    """
    对对话历史执行 compaction。

    流程：
    1. 加载完整历史
    2. 计算分割点（旧消息 vs 保留消息）
    3. 从旧消息中提取记忆 → 写入 MEMORY.md
    4. 对旧消息生成摘要
    5. 用 [摘要消息] + [保留消息] 重写 JSONL 文件

    返回 True 表示执行了压缩，False 表示未触发。
    """
    # 读取历史
    if not session_path.exists():
        return False

    messages: list[dict] = []
    for line in session_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not messages:
        return False

    # 计算分割点
    split_idx = find_split_point(messages)
    if split_idx == 0:
        return False

    old_messages = messages[:split_idx]
    tail_messages = messages[split_idx:]

    old_tokens = _estimate_messages_tokens(old_messages)
    tail_tokens = _estimate_messages_tokens(tail_messages)
    logger.info(
        f"Compaction: 触发压缩 — 总消息 {len(messages)} 条, "
        f"旧消息 {len(old_messages)} 条 (~{old_tokens} tokens), "
        f"保留 {len(tail_messages)} 条 (~{tail_tokens} tokens)"
    )

    # Step 1: 从旧消息中提取记忆
    existing_memory = ""
    if memory_path.exists():
        existing_memory = memory_path.read_text(encoding="utf-8")

    extractions = await extract_memories(old_messages, existing_memory)
    if extractions:
        merge_memories_into_file(memory_path, extractions)

    # Step 2: 生成摘要
    summary = await generate_summary(old_messages)
    if not summary:
        logger.warning("Compaction: 摘要生成失败，跳过压缩")
        return False

    # Step 3: 构建新历史 = [摘要] + [保留消息]
    summary_msg = {
        "role": "assistant",
        "content": f"[前置会话摘要]\n{summary}",
    }
    new_messages = [summary_msg] + tail_messages

    # Step 4: 重写 JSONL
    _rewrite_history(session_path, new_messages)

    new_tokens = _estimate_messages_tokens(new_messages)
    logger.info(
        f"Compaction: 完成 — "
        f"压缩前 {len(messages)} 条 (~{old_tokens + tail_tokens} tokens) → "
        f"压缩后 {len(new_messages)} 条 (~{new_tokens} tokens), "
        f"提取记忆 {sum(len(v) for v in extractions.values())} 条"
    )

    return True
