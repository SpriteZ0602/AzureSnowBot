"""
本地工具注册与分发
──────────────────
参考 OpenClaw 的工具架构：
  - 工厂模式：每个工具是一个 (name, description, parameters, execute) 四元组
  - 统一注册：通过 @register_tool 装饰器注册
  - 名称分发：agentic loop 按 "local__<name>" 前缀匹配
  - OpenAI 格式：自动转换为 function calling schema

使用方法:
    from .manager import register_tool

    @register_tool(
        name="current_time",
        description="获取当前日期和时间",
        parameters={},  # 无参数
    )
    async def current_time(**kwargs) -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from nonebot.log import logger

# 工具名称前缀
TOOL_PREFIX = "local"


@dataclass
class LocalTool:
    """本地工具定义"""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema 格式
    execute: Callable[..., Awaitable[str] | str]


# ──────────────────── 全局注册表 ────────────────────
_registry: dict[str, LocalTool] = {}


def register_tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> Callable:
    """
    工具注册装饰器。

    参数:
        name: 工具名称（不含前缀）
        description: 工具描述（LLM 可见）
        parameters: JSON Schema 格式的参数定义，None 表示无参数

    示例:
        @register_tool(
            name="get_time",
            description="获取当前时间",
        )
        async def get_time(**kwargs) -> str:
            ...
    """
    if parameters is None:
        parameters = {"type": "object", "properties": {}}

    # 确保顶层是 object 类型（OpenAI 要求）
    if "type" not in parameters:
        parameters["type"] = "object"
    if "properties" not in parameters:
        parameters["properties"] = {}

    def decorator(func: Callable) -> Callable:
        tool = LocalTool(
            name=name,
            description=description,
            parameters=parameters,
            execute=func,
        )
        _registry[name] = tool
        logger.info(f"已注册本地工具: {TOOL_PREFIX}__{name} — {description[:50]}")
        return func

    return decorator


# ──────────────────── OpenAI 格式转换 ────────────────────

def get_openai_tools() -> list[dict]:
    """将所有本地工具转换为 OpenAI function calling 的 tools 格式"""
    tools = []
    for name, tool in _registry.items():
        tools.append({
            "type": "function",
            "function": {
                "name": f"{TOOL_PREFIX}__{name}",
                "description": tool.description,
                "parameters": tool.parameters,
            },
        })
    return tools


# ──────────────────── 工具调用分发 ────────────────────

async def handle_tool_call(
    full_name: str,
    arguments: dict,
    *,
    context: dict | None = None,
) -> str | None:
    """
    处理本地工具调用。
    full_name 格式: "local__<tool_name>"
    context: 可选的调用上下文（_chat_type, _target_id, _user_id, _sender_name）
    返回结果字符串，如果不属于本地工具返回 None。
    """
    if not full_name.startswith(f"{TOOL_PREFIX}__"):
        return None

    tool_name = full_name[len(f"{TOOL_PREFIX}__"):]
    tool = _registry.get(tool_name)
    if not tool:
        return f"[错误] 本地工具 '{tool_name}' 不存在"

    try:
        call_args = {**arguments}
        if context:
            call_args["_context"] = context
        result = tool.execute(**call_args)
        # 支持同步和异步函数
        if inspect.isawaitable(result):
            result = await result
        return str(result)
    except Exception as e:
        logger.error(f"本地工具调用失败 [{full_name}]: {e}")
        return f"[工具调用出错] {e}"


# ──────────────────── 摘要 ────────────────────

def list_tools_summary() -> list[str]:
    """返回所有本地工具的摘要列表"""
    lines = []
    for name, tool in _registry.items():
        desc = tool.description[:60]
        lines.append(f"  • {TOOL_PREFIX}__{name} — {desc}")
    return lines
