"""
群聊插件包
────────
加载群聊相关的所有子模块。
"""

from nonebot import get_driver
from nonebot.log import logger
from ..mcp import manager as mcp
from ..skill import manager as skill

# MCP + Skill 生命周期
driver = get_driver()


def _start_group_heartbeats() -> None:
    """为所有开启主动对话的群启动心跳计时器。"""
    from ..proactive import reset_idle_timer
    from ..persona.manager import GROUP_SESSION_DIR, get_group_proactive

    started = 0
    for cfg_path in GROUP_SESSION_DIR.glob("*/config.json"):
        gid = cfg_path.parent.name
        if get_group_proactive(gid):
            reset_idle_timer(f"group:{gid}")
            started += 1
    if started:
        logger.info(f"已启动 {started} 个群的主动对话心跳")


@driver.on_startup
async def _startup():
    skill.scan_skills()     # Skill 系统（同步，纯文件扫描）
    await mcp.initialize()  # MCP 服务器（异步，需要连接）
    _start_group_heartbeats()


@driver.on_shutdown
async def _shutdown():
    await mcp.shutdown()


# 加载子模块（触发 nonebot matcher 注册）
from . import handler as handler     # noqa: E402, F401
from . import commands as commands   # noqa: E402, F401
from . import chatlog as chatlog     # noqa: E402, F401
from . import repeater as repeater   # noqa: E402, F401
from . import chatter as chatter     # noqa: E402, F401
