"""群聊工具执行凭证：只保存有界的文字记录，不保存完整工具返回值。"""

import json
import re
from collections.abc import Callable

from nonebot.log import logger
from botcore.tools import tool_result
from botcore.sessions import ensure_current

ARGUMENT_LIMIT = 200
RESULT_LIMIT = 600
_SECRET_KEYS = re.compile(r"token|password|secret|api.?key|authorization", re.I)


def shorten(text: str, limit: int) -> str:
    text = str(text)
    suffix = "…[已截断]"
    return text if len(text) <= limit else text[: limit - len(suffix)] + suffix


def _redact(value):
    if isinstance(value, dict):
        return {k: "[已隐藏]" if _SECRET_KEYS.search(str(k)) else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def summarize_web_search(arguments: dict, result: str) -> str:
    """丢弃自动抓取的正文，优先保留前三个标题和来源链接。"""
    listing = result.split("[自动读取的前几个链接正文]", 1)[0]
    lines = listing.splitlines()
    selected = [line.strip() for line in lines if re.match(r"^\d+\.\s", line) or line.strip().startswith(("http://", "https://"))]
    return "\n".join(selected[:6]) if selected else listing


def summarize_web_read(arguments: dict, result: str) -> str:
    return f"来源：{arguments.get('url', '')}\n{result}"


def make_receipt(name: str, arguments: dict, result: str, *, source: str = "group",
                 formatter: Callable[[dict, str], str] | None = None) -> str:
    status = "已执行" if tool_result(result).ok else "失败"
    summary = result
    if formatter is not None and status != "失败":
        try:
            summary = str(formatter(arguments, result))
        except Exception as exc:
            logger.warning(f"工具历史摘要失败 [{name}]: {type(exc).__name__}")
    args = shorten(json.dumps(_redact(arguments), ensure_ascii=False, default=str), ARGUMENT_LIMIT)
    return (f"【执行记录｜{source}】{name} 参数={args}；状态={status}；"
            f"结果：{shorten(summary, RESULT_LIMIT)}")


def record_tool_execution(name: str, arguments: dict, result: str, *, context: dict | None,
                          source: str = "group") -> None:
    """所有群聊执行入口共用；记录失败不影响工具执行或回复。"""
    if not context or context.get("_chat_type") != "group":
        return
    gid = str(context.get("_target_id", ""))
    persona = context.get("_persona")
    if not gid.isdigit() or not isinstance(persona, str) or not persona:
        return
    try:
        ensure_current()
        formatter = None
        if name.startswith("local__"):
            from .local_tools.manager import _registry
            tool = _registry.get(name.removeprefix("local__"))
            formatter = tool.history_formatter if tool else None
        from .persona.manager import append_message
        append_message(gid, {"role": "assistant", "content": make_receipt(
            name, arguments, result, source=source, formatter=formatter,
        )}, persona)
    except Exception as exc:
        logger.warning(f"保存工具执行记录失败 [{name}]: {type(exc).__name__}")
