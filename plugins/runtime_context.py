"""
运行时上下文
──────────
每次 LLM 请求时注入到 system prompt 末尾的环境信息。
参考 OpenClaw 的 Runtime 行设计，提供 LLM 对运行环境的感知。

使用方法:
    from plugins.runtime_context import build_runtime_context
    system_prompt += build_runtime_context(chat_type="private", last_message_at="...")
"""

import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from nonebot.log import logger

from .llm import MODEL as LLM_MODEL


# ──────────────────── 静态信息（启动时计算一次） ────────────────────

_OS_INFO = f"{platform.system()} {platform.release()} ({platform.machine()})"
_PYTHON_VERSION = f"Python {sys.version.split()[0]}"
_MACHINE_NAME = platform.node() or "unknown"
_WORKSPACE = str(Path.cwd())

# Git root（启动时探测一次）
def _detect_git_root() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""

_GIT_ROOT = _detect_git_root()

# Shell 类型
def _detect_shell() -> str:
    if platform.system() == "Windows":
        return "powershell"
    return os.environ.get("SHELL", "/bin/sh").rsplit("/", 1)[-1]

_SHELL = _detect_shell()


# ──────────────────── 工具摘要（每次调用时动态获取） ────────────────────

def _build_tools_summary(chat_type: str = "private") -> str:
    """收集所有可用工具的摘要（Skill + 本地 + MCP），按 chat_type 过滤"""
    from .local_tools.manager import list_tools_summary as local_summary
    from .mcp.manager import list_tools_summary as mcp_summary
    from .skill.manager import list_skills_summary as skill_summary

    lines: list[str] = []

    # Skill 工具
    skill_lines = skill_summary()
    if skill_lines:
        lines.append("Skills:")
        lines.extend(skill_lines)

    # 本地工具
    local_lines = local_summary(chat_type=chat_type)
    if local_lines:
        lines.append("本地工具:")
        lines.extend(local_lines)

    # MCP 工具
    mcp_lines = mcp_summary()
    if mcp_lines:
        lines.append("MCP 工具:")
        lines.extend(mcp_lines)

    return "\n".join(lines) if lines else ""


# ──────────────────── 星期映射 ────────────────────

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ──────────────────── 公共 API ────────────────────

def build_runtime_context(
    *,
    chat_type: str = "private",
    last_message_at: str = "",
) -> str:
    """
    构建运行时上下文字符串，追加到 system prompt 末尾。

    包含：
    - 当前时间 + 星期
    - 上次对话时间
    - Runtime 行（模型、OS、机器名、Python、Shell、Workspace、Git）
    - 消息渠道 + 能力
    - 可用工具摘要

    参数:
        chat_type: "private" 或 "group"
        last_message_at: 上次对话时间字符串（可空）
    """
    now = datetime.now()
    now_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')}（{_WEEKDAYS[now.weekday()]}）"

    sections: list[str] = []

    # 1. 时间上下文
    time_line = f"当前时间: {now_str}"
    if last_message_at:
        time_line += f"，上次对话: {last_message_at}"
    sections.append(time_line)

    # 2. Runtime 环境行
    #    私聊（Admin）: 完整环境信息（OS/Shell/Workspace/Git），用于电脑操控
    #    群聊: 仅模型名，不需要环境信息
    if chat_type == "private":
        runtime_parts = [
            f"model={LLM_MODEL}",
            f"os={_OS_INFO}",
            f"host={_MACHINE_NAME}",
            f"python={_PYTHON_VERSION}",
            f"shell={_SHELL}",
            f"workspace={_WORKSPACE}",
        ]
        if _GIT_ROOT:
            runtime_parts.append(f"git_root={_GIT_ROOT}")
        sections.append(f"Runtime: {' | '.join(runtime_parts)}")
    else:
        sections.append(f"Runtime: model={LLM_MODEL}")

    # 3. 消息渠道
    channel_type = "QQ私聊" if chat_type == "private" else "QQ群聊"
    capabilities = "文字, 引用回复, 表情, 图片"
    sections.append(f"Channel: {channel_type} | capabilities=[{capabilities}]")

    # 4. 可用工具摘要
    try:
        tools_text = _build_tools_summary(chat_type=chat_type)
        if tools_text:
            sections.append(f"可用工具:\n{tools_text}")
    except Exception as e:
        logger.debug(f"构建工具摘要失败: {e}")

    return "\n" + "\n".join(sections)
