"""
本地工具插件包
────────────
注册和管理本地 Python 工具，与 MCP/Skill 工具统一注入 agentic loop。
"""

from .local_tools import manager as manager  # noqa: F401
from .local_tools import tools as tools      # noqa: F401  触发工具注册
from .local_tools import tarot as tarot      # noqa: F401  触发工具注册
from .local_tools import stickers as stickers  # noqa: F401
