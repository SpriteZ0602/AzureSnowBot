"""各 Agent 入口共用的权限分发和执行记录；导入时不加载其他插件。"""

from botcore.tools import ToolResult, tool_result

_MAIN_ONLY = {"local__run_sub_agent", "local__save_quoted_sticker", "local__delete_quoted_sticker",
              "local__request_sticker", "local__save_recent_stickers", "local__update_quoted_sticker_tags"}


async def execute_tool(name: str, arguments: dict, *, context: dict, source: str) -> ToolResult:
    from .skill.manager import handle_tool_call as skill_call
    from .local_tools.manager import handle_tool_call as local_call
    from .mcp.manager import call_tool as mcp_call
    from .tool_log import log_tool_call
    from .tool_history import record_tool_execution

    if source == "sub_agent" and (name in _MAIN_ONLY or name.startswith("skill__")):
        result = ToolResult("[错误] 此工具不能由 Sub-Agent 调用", ok=False)
    else:
        result = skill_call(name, arguments, context=context)
        if result is None:
            result = await local_call(name, arguments, context=context)
        if result is None:
            result = await mcp_call(name, arguments)
        result = tool_result(result)
    log_tool_call(source, name, arguments, result, context=context)
    record_tool_execution(name, arguments, result, context=context, source=source)
    return result
