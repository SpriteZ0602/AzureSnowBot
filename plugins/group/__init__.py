"""
群聊插件包
────────
加载群聊相关的所有子模块。
"""

from nonebot import get_driver
from ..mcp import manager as mcp
from ..skill import manager as skill

# MCP + Skill 生命周期
driver = get_driver()


@driver.on_startup
async def _startup():
    skill.scan_skills()     # Skill 系统（同步，纯文件扫描）
    await mcp.initialize()  # MCP 服务器（异步，需要连接）


@driver.on_shutdown
async def _shutdown():
    await mcp.shutdown()


# 加载子模块（触发 nonebot matcher 注册）
from . import handler as handler     # noqa: E402, F401
from . import commands as commands   # noqa: E402, F401
