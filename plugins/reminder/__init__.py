"""
定时提醒插件
────────────
Bot 启动时重新加载持久化的提醒，实际工具注册在 local_tools/tools.py。
"""

from nonebot import get_driver
from nonebot.log import logger

from .scheduler import reload_reminders

driver = get_driver()
_reloaded = False


@driver.on_bot_connect
async def _on_connect(bot):
    global _reloaded
    if _reloaded:
        return
    _reloaded = True
    await reload_reminders()
    logger.info("定时提醒模块已初始化")
