"""有明确状态的工具结果，以及旧字符串工具的唯一兼容入口。"""

_ERROR_PREFIXES = ("[错误]", "[工具调用出错]", "[Sub-Agent 调用失败]", "[Sub-Agent 工具调用轮次达上限]")


class ToolResult(str):
    def __new__(cls, text: str, *, ok: bool = True):
        value = super().__new__(cls, text)
        value.ok = ok
        return value


def tool_result(value) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    text = str(value)
    return ToolResult(text, ok=not text.lstrip().startswith(_ERROR_PREFIXES))
