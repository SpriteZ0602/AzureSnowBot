"""
群聊插件包
────────
加载群聊相关的所有子模块。
"""

from nonebot import get_driver
from ..mcp import manager as mcp

# MCP 生命周期
driver = get_driver()


@driver.on_startup
async def _startup():
    await mcp.initialize()


@driver.on_shutdown
async def _shutdown():
    await mcp.shutdown()


# 加载子模块（触发 nonebot matcher 注册）
from . import handler as handler     # noqa: E402, F401
from . import commands as commands   # noqa: E402, F401
