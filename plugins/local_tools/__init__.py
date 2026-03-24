"""
本地工具插件包
────────────
注册和管理本地 Python 工具，与 MCP/Skill 工具统一注入 agentic loop。
"""

from . import manager as manager  # noqa: F401
from . import tools as tools      # noqa: F401  触发工具注册
