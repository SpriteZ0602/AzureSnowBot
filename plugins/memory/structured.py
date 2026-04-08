"""
结构化记忆蒸馏
──────────────
从对话摘要/增量消息中提取事实性知识，以结构化 JSON 条目
追加写入 memories.jsonl。只追加不去重，写入前严格校验质量。

触发点：
  - Compaction 后：从对话摘要中蒸馏
  - 心跳时：从增量消息中蒸馏

使用方法：
    from plugins.memory.structured import distill_memories
    await distill_memories(text, memories_path)
"""

import json
from datetime import datetime
from pathlib import Path

import httpx
from nonebot.log import logger

from ..llm import API_KEY, BASE_URL, LLM_PROVIDER

# ──────────────────── 小模型配置 ────────────────────
# 蒸馏用小模型，按 provider 选择
_SMALL_MODELS: dict[str, str] = {
    "gemini": "gemini-2.0-flash-lite",
    "openai": "gpt-4o-mini",
    "qwen": "qwen-turbo",
}

DISTILL_TIMEOUT = 60

# ──────────────────── 蒸馏 Prompt ────────────────────

DISTILL_SYSTEM_PROMPT = """\
你是一个信息提取助手。从给定的对话内容中，提取高置信度的事实性知识，输出为 JSON 数组。

每条知识包含以下字段：
- "type": 类型，可选值：identity（身份信息）、preference（偏好）、fact（事实）、task（进行中的任务）、emotion（情感记录）
- "subject": 知识主题，简短描述（如"编程语言"、"工作"、"饮食偏好"）
- "value": 知识内容，一句话描述
- "confidence": 置信度，可选值：high、medium

提取规则：
- 只提取明确的、事实性的信息，不要推测或猜测
- 闲聊、寒暄、临时情绪不要提取
- 模棱两可的内容不要提取（宁缺毋滥）
- confidence 为 low 的不要输出
- 如果没有值得提取的信息，输出空数组 []

输出要求：
- 只输出 JSON 数组，不要有其他文字
- 每条尽量简短（value 不超过 50 字）"""


# ──────────────────── 核心函数 ────────────────────

async def distill_memories(text: str, memories_path: Path) -> int:
    """
    从文本中蒸馏结构化记忆条目，追加写入 memories.jsonl。

    参数:
        text: 待蒸馏的文本（对话摘要或增量消息）
        memories_path: memories.jsonl 的路径

    返回: 成功写入的条目数
    """
    if not text or not text.strip():
        return 0

    if not API_KEY:
        logger.debug("结构化蒸馏: 未配置 API Key，跳过")
        return 0

    # 调用小模型
    small_model = _SMALL_MODELS.get(LLM_PROVIDER, "gemini-2.0-flash-lite")
    raw = await _call_small_llm(small_model, text)
    if not raw:
        return 0

    # 解析 + 校验
    entries = _parse_and_validate(raw)
    if not entries:
        return 0

    # 追加写入
    memories_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    with memories_path.open("a", encoding="utf-8") as f:
        for entry in entries:
            entry["updated"] = today
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            count += 1

    logger.info(f"结构化蒸馏: 写入 {count} 条记忆到 {memories_path.name}")
    return count


# ──────────────────── 小模型调用 ────────────────────

async def _call_small_llm(model: str, text: str) -> str | None:
    """调用小模型进行蒸馏"""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=DISTILL_TIMEOUT) as client:
            resp = await client.post(
                f"{BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            from ..token_stats import record_usage
            record_usage("distill", data.get("usage"))
            return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        logger.warning(f"结构化蒸馏 LLM 调用失败: {e}")
        return None


# ──────────────────── 解析 + 校验 ────────────────────

def _parse_and_validate(raw: str) -> list[dict]:
    """解析 JSON 输出并严格校验每条记录"""
    # 尝试提取 JSON 数组（小模型可能包裹在 ```json ... ``` 中）
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # 去掉首尾的 ``` 行
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[start:end]).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"结构化蒸馏: JSON 解析失败，原文: {raw[:200]}")
        return []

    if not isinstance(data, list):
        logger.warning("结构化蒸馏: 输出不是 JSON 数组")
        return []

    valid_types = {"identity", "preference", "fact", "task", "emotion"}
    valid_confidence = {"high", "medium"}
    result: list[dict] = []

    for item in data:
        if not isinstance(item, dict):
            continue
        # 必填字段校验
        entry_type = item.get("type", "")
        subject = item.get("subject", "")
        value = item.get("value", "")
        confidence = item.get("confidence", "medium")

        if not entry_type or not subject or not value:
            continue
        if entry_type not in valid_types:
            continue
        if confidence not in valid_confidence:
            continue

        entry: dict = {
            "type": entry_type,
            "subject": subject,
            "value": value,
            "confidence": confidence,
        }
        # 可选字段
        if item.get("expires"):
            entry["expires"] = item["expires"]

        result.append(entry)

    return result


# ──────────────────── 读取工具 ────────────────────

def load_memories(memories_path: Path) -> list[dict]:
    """加载 memories.jsonl 的所有条目，跳过已过期的"""
    if not memories_path.exists():
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    entries: list[dict] = []

    for line in memories_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        # 跳过过期条目
        expires = entry.get("expires", "")
        if expires and expires < today:
            continue
        entries.append(entry)

    return entries


def load_identity_memories(memories_path: Path) -> str:
    """加载 type=identity 的条目，格式化为可注入 system prompt 的文本"""
    entries = load_memories(memories_path)
    identity = [e for e in entries if e.get("type") == "identity"]
    if not identity:
        return ""

    # 按 updated 排序，最新的在前，去重 subject（保留最新）
    identity.sort(key=lambda e: e.get("updated", ""), reverse=True)
    seen: set[str] = set()
    unique: list[dict] = []
    for e in identity:
        subj = e.get("subject", "")
        if subj not in seen:
            seen.add(subj)
            unique.append(e)

    lines = ["## 核心记忆"]
    for e in unique:
        lines.append(f"- {e['subject']}: {e['value']}")
    return "\n".join(lines)


def search_memories(
    memories_path: Path,
    *,
    type_filter: str = "",
    keyword: str = "",
    limit: int = 20,
) -> list[dict]:
    """按 type 和关键词过滤结构化记忆"""
    entries = load_memories(memories_path)

    if type_filter:
        entries = [e for e in entries if e.get("type") == type_filter]

    if keyword:
        kw = keyword.lower()
        entries = [
            e for e in entries
            if kw in e.get("subject", "").lower()
            or kw in e.get("value", "").lower()
        ]

    # 按 updated 排序，最新的在前
    entries.sort(key=lambda e: e.get("updated", ""), reverse=True)
    return entries[:limit]
