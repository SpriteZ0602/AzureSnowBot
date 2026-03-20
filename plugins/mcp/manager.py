"""
MCP (Model Context Protocol) 管理模块
──────────────────────────────────────
负责：
  1. 加载 MCP 服务器配置 (data/mcp_servers.json)
  2. 启动/连接 MCP 服务器（stdio 传输）
  3. 发现工具并转换为 OpenAI function calling 格式
  4. 执行工具调用
  5. 生命周期管理（启动/关闭）

架构说明：
  MCP 的 stdio_client 内部使用 anyio TaskGroup，与 NoneBot (uvicorn)
  的 event loop 存在兼容性问题。因此：
  - 在独立的守护线程中运行专用 asyncio event loop
  - 所有 MCP 操作（连接/工具调用/关闭）都在该线程完成
  - 主线程通过 run_coroutine_threadsafe 跨线程调度
"""

import json
import asyncio
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from nonebot.log import logger

# ──────────────────── 配置路径 ────────────────────
MCP_CONFIG_PATH = Path("data/mcp_servers.json")

# ──────────────────── 全局状态 ────────────────────
# server_name → { session, tools }
_servers: dict[str, dict] = {}
_initialized = False
_shutdown_event: asyncio.Event | None = None

# 独立线程 + event loop
_mcp_loop: asyncio.AbstractEventLoop | None = None
_mcp_thread: threading.Thread | None = None

# 工具调用最大轮次，防止无限循环
MAX_TOOL_ROUNDS = 10


# ──────────────────── 配置加载 ────────────────────

def load_config() -> dict[str, dict]:
    """
    加载 MCP 服务器配置。

    格式示例 (data/mcp_servers.json):
    {
      "servers": {
        "playwright": {
          "command": "cmd.exe",
          "args": ["/c", "npx", "-y", "@playwright/mcp", "--headless"]
        }
      }
    }
    """
    if not MCP_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
        return data.get("servers", {})
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"MCP 配置文件解析失败: {e}")
        return {}


# ──────────────────── 环境构建 ────────────────────

# Windows 上常见的工具路径
_COMMON_TOOL_PATHS = [
    r"C:\Program Files\nodejs",
    os.path.expandvars(r"%APPDATA%\npm"),
]


def _build_env(config_env: dict | None = None) -> dict[str, str]:
    """
    构建子进程环境变量。
    基于当前进程环境 + 配置中指定的 env + 自动补全常见工具路径。
    确保 node/npx 等工具可被找到。
    """
    env = dict(os.environ)
    # 合并配置中指定的环境变量
    if config_env:
        env.update(config_env)
    # 自动检测并补全 PATH
    if os.name == "nt":
        current_path = env.get("PATH", "")
        for tool_dir in _COMMON_TOOL_PATHS:
            if os.path.isdir(tool_dir) and tool_dir not in current_path:
                current_path = tool_dir + ";" + current_path
        env["PATH"] = current_path
    return env


# ──────────────────── MCP 线程内部逻辑 ────────────────────

async def _run_all_servers(config: dict[str, dict]) -> None:
    """
    在独立 event loop 中运行所有 MCP 服务器。
    每个服务器在独立 task 中运行，共享同一个 shutdown event。
    """
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    async def _run_one(name: str, srv_config: dict, ready: asyncio.Event) -> None:
        try:
            server_params = StdioServerParameters(
                command=srv_config["command"],
                args=srv_config.get("args", []),
                env=_build_env(srv_config.get("env")),
            )
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    tools = tools_result.tools
                    logger.info(
                        f"MCP 服务器 [{name}] 已连接，发现 {len(tools)} 个工具: "
                        f"{[t.name for t in tools]}"
                    )
                    _servers[name] = {"session": session, "tools": tools}
                    ready.set()
                    # 保持连接存活直到收到关闭信号
                    await _shutdown_event.wait()
        except BaseException as e:
            # 提取 ExceptionGroup 内层异常的详细信息
            detail = str(e)
            if hasattr(e, 'exceptions'):
                subs = []
                for sub in e.exceptions:
                    subs.append(f"{type(sub).__name__}: {sub}")
                detail += " | 子异常: " + "; ".join(subs)
            import traceback
            logger.error(f"MCP 服务器 [{name}] 连接失败: {detail}")
            logger.error(traceback.format_exc())
        finally:
            _servers.pop(name, None)
            ready.set()

    # 启动所有服务器 task
    ready_events: list[tuple[str, asyncio.Event]] = []
    tasks: list[asyncio.Task] = []
    for name, srv_config in config.items():
        ready = asyncio.Event()
        task = asyncio.create_task(_run_one(name, srv_config, ready))
        tasks.append(task)
        ready_events.append((name, ready))

    # 等待所有就绪
    for name, ready in ready_events:
        try:
            await asyncio.wait_for(ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(f"MCP 服务器 [{name}] 初始化超时 (30s)")

    total_tools = sum(len(s["tools"]) for s in _servers.values())
    logger.info(f"MCP 初始化完成：{len(_servers)} 个服务器，共 {total_tools} 个工具")

    # 阻塞等待关闭信号
    await _shutdown_event.wait()

    # 等所有 task 自然退出
    for t in tasks:
        try:
            await asyncio.wait_for(t, timeout=10)
        except (asyncio.TimeoutError, Exception):
            t.cancel()


def _thread_entry(config: dict[str, dict], started: threading.Event) -> None:
    """MCP 专用线程的入口函数"""
    global _mcp_loop
    _mcp_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_mcp_loop)
    started.set()
    try:
        _mcp_loop.run_until_complete(_run_all_servers(config))
    except Exception as e:
        logger.error(f"MCP 线程异常: {e}")
    finally:
        _mcp_loop.close()
        _mcp_loop = None


# ──────────────────── 公共接口 ────────────────────

async def initialize() -> None:
    """启动 MCP 专用线程，连接所有服务器"""
    global _initialized, _mcp_thread
    if _initialized:
        return

    config = load_config()
    if not config:
        logger.info("未配置 MCP 服务器，跳过初始化")
        _initialized = True
        return

    started = threading.Event()
    _mcp_thread = threading.Thread(
        target=_thread_entry, args=(config, started),
        name="mcp-worker", daemon=True,
    )
    _mcp_thread.start()
    started.wait()  # 等待 event loop 创建完成

    # 等待所有服务器初始化完成（MCP 线程内部每个有 30s 超时）
    # 这里轮询等到线程内的 ready event 全部 set 后 _servers 就不再变化
    expected = len(config)
    for _ in range(80):  # 最多 40s
        await asyncio.sleep(0.5)
        if len(_servers) >= expected:
            break
        # 如果线程已经退出（全部失败），也不用再等了
        if not _mcp_thread.is_alive():
            break

    _initialized = True


async def shutdown() -> None:
    """关闭所有 MCP 服务器连接"""
    global _initialized, _mcp_thread
    if _shutdown_event and _mcp_loop and _mcp_loop.is_running():
        _mcp_loop.call_soon_threadsafe(_shutdown_event.set)

    if _mcp_thread and _mcp_thread.is_alive():
        _mcp_thread.join(timeout=15)
        logger.info("MCP 工作线程已退出")

    _mcp_thread = None
    _servers.clear()
    _initialized = False


# ──────────────────── 工具格式转换 ────────────────────

def get_openai_tools() -> list[dict]:
    """
    将所有 MCP 工具转换为 OpenAI function calling 的 tools 格式。
    返回空列表表示没有可用工具。
    """
    tools = []
    for server_name, server in _servers.items():
        for tool in server["tools"]:
            # MCP Tool → OpenAI tool definition
            tools.append({
                "type": "function",
                "function": {
                    "name": f"{server_name}__{tool.name}",
                    "description": tool.description or "",
                    "parameters": tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}},
                },
            })
    return tools


def _resolve_tool(full_name: str) -> tuple[str, str, ClientSession] | None:
    """
    从 "server__tool_name" 格式解析出 (server_name, tool_name, session)。
    """
    if "__" not in full_name:
        return None
    server_name, tool_name = full_name.split("__", 1)
    server = _servers.get(server_name)
    if not server:
        return None
    return server_name, tool_name, server["session"]


async def call_tool(full_name: str, arguments: dict) -> str:
    """
    执行工具调用，返回结果文本。
    full_name 格式: "server_name__tool_name"
    调用会被调度到 MCP 专用线程执行。
    """
    resolved = _resolve_tool(full_name)
    if not resolved:
        return f"[错误] 工具 {full_name} 不存在"

    server_name, tool_name, session = resolved

    async def _do_call() -> str:
        try:
            result = await session.call_tool(tool_name, arguments=arguments)
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "data"):
                    parts.append(f"[binary data: {block.mimeType}]")
                else:
                    parts.append(str(block))
            return "\n".join(parts) if parts else "[工具无返回内容]"
        except Exception as e:
            logger.error(f"MCP 工具调用失败 [{full_name}]: {e}")
            return f"[工具调用出错] {e}"

    # 调度到 MCP 线程执行
    if _mcp_loop and _mcp_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_do_call(), _mcp_loop)
        return await asyncio.get_event_loop().run_in_executor(
            None, future.result, 120  # 120s 超时
        )
    return "[错误] MCP 服务未运行"


def has_tools() -> bool:
    """是否有可用的 MCP 工具"""
    return any(len(s["tools"]) > 0 for s in _servers.values())


def list_tools_summary() -> list[str]:
    """返回所有工具的摘要列表（供 /help 等使用）"""
    lines = []
    for server_name, server in _servers.items():
        for tool in server["tools"]:
            desc = (tool.description or "无描述")[:60]
            lines.append(f"  • {server_name}__{tool.name} — {desc}")
    return lines
