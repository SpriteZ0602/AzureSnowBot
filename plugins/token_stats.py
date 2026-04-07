"""
Token 使用量统计
──────────────
记录每次 LLM 调用的 token 消耗，按来源分类统计。
支持查询当日/累计用量，持久化到 JSON 文件。

使用方法:
    from plugins.token_stats import record_usage, get_stats_summary

    data = resp.json()
    record_usage("chat", data.get("usage"))
"""

import json
from datetime import datetime
from pathlib import Path
from threading import Lock

from nonebot.log import logger

# ──────────────────── 持久化路径 ────────────────────
STATS_FILE = Path("data/admin/token_stats.json")
STATS_FILE.parent.mkdir(parents=True, exist_ok=True)

# ──────────────────── 内存状态 ────────────────────
_lock = Lock()
_stats: dict = {}


def _load_stats() -> dict:
    global _stats
    if STATS_FILE.exists():
        try:
            _stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _stats = {}
    return _stats


def _save_stats() -> None:
    try:
        STATS_FILE.write_text(
            json.dumps(_stats, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logger.debug(f"Token stats 保存失败: {e}")


# 启动时加载
_load_stats()


# ──────────────────── 记录 ────────────────────

def record_usage(source: str, usage: dict | None) -> None:
    """
    记录一次 LLM 调用的 token 用量。

    参数:
        source: 来源标识（如 "chat", "group", "heartbeat", "compaction", "sub_agent", "reminder", "embedding"）
        usage: OpenAI 兼容格式的 usage 字典，如 {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    """
    if not usage:
        return

    prompt = usage.get("prompt_tokens", 0) or 0
    completion = usage.get("completion_tokens", 0) or 0
    total = usage.get("total_tokens", 0) or (prompt + completion)
    today = datetime.now().strftime("%Y-%m-%d")

    with _lock:
        # 按日期分桶
        if today not in _stats:
            _stats[today] = {}
        day = _stats[today]

        if source not in day:
            day[source] = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
        entry = day[source]
        entry["prompt"] += prompt
        entry["completion"] += completion
        entry["total"] += total
        entry["calls"] += 1

        _save_stats()


# ──────────────────── 查询 ────────────────────

def get_today_stats() -> dict:
    """返回今日各来源的 token 用量"""
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        return dict(_stats.get(today, {}))


def get_stats_summary() -> str:
    """生成可读的统计摘要"""
    today = datetime.now().strftime("%Y-%m-%d")

    with _lock:
        day_stats = _stats.get(today, {})

    if not day_stats:
        return f"今日 ({today}) 暂无 LLM 调用记录"

    lines = [f"📊 Token 用量统计 — {today}"]
    total_prompt = 0
    total_completion = 0
    total_total = 0
    total_calls = 0

    for source, data in sorted(day_stats.items()):
        p = data.get("prompt", 0)
        c = data.get("completion", 0)
        t = data.get("total", 0)
        n = data.get("calls", 0)
        lines.append(f"  {source}: {t:,} tokens ({p:,} in / {c:,} out) — {n} 次调用")
        total_prompt += p
        total_completion += c
        total_total += t
        total_calls += n

    lines.append(f"  ──────")
    lines.append(f"  合计: {total_total:,} tokens ({total_prompt:,} in / {total_completion:,} out) — {total_calls} 次调用")

    # 估算费用（按 Gemini 3 Flash 价格）
    input_cost = total_prompt / 1_000_000 * 0.5
    output_cost = total_completion / 1_000_000 * 3.0
    lines.append(f"  预估费用: ${input_cost + output_cost:.4f} (in ${input_cost:.4f} + out ${output_cost:.4f})")

    return "\n".join(lines)
