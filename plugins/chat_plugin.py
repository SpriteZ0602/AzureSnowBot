"""
私聊对话插件包
────────────
加载私聊对话处理 + 心跳/主动发言。
"""

from nonebot import get_driver

from .chat import handler as handler  # noqa: F401


@get_driver().on_startup
async def _start_heartbeat():
    """Bot 启动时开启私聊心跳计时器（若开启）+ 预热记忆索引。"""
    from .proactive import reset_idle_timer
    if handler.get_proactive_enabled():
        reset_idle_timer("private")

    try:
        from .memory.indexer import ensure_index
        await ensure_index()
    except Exception:
        pass  # 索引预热失败不阻塞启动
