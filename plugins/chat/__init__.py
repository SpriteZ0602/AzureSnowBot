"""
私聊对话插件包
────────────
加载私聊对话处理 + 心跳/主动发言。
"""

from nonebot import get_driver

from . import handler as handler  # noqa: F401
from . import proactive as proactive  # noqa: F401


@get_driver().on_startup
async def _start_heartbeat():
    """Bot 启动时开启心跳计时器 + 预热记忆索引。"""
    proactive.reset_idle_timer()

    try:
        from ..memory.indexer import ensure_index
        await ensure_index()
    except Exception:
        pass  # 索引预热失败不阻塞启动
