"""
Web Dashboard 插件
──────────────────
在 NoneBot2 启动时挂载 FastAPI 子应用，提供 REST API 和 Vue SPA 静态文件。
"""

from nonebot import get_driver
from nonebot.log import logger

from .app import create_dashboard_app

driver = get_driver()


@driver.on_startup
async def _mount_dashboard():
    """NoneBot 启动后挂载 Dashboard 子应用"""
    app = create_dashboard_app()
    driver.server_app.mount("/api/v1", app)
    logger.info("Dashboard API 已挂载到 /api/v1")

    # 尝试挂载前端静态文件（生产模式）
    from pathlib import Path
    dist = Path("web/dist")
    if dist.is_dir():
        from starlette.staticfiles import StaticFiles
        driver.server_app.mount("/dashboard", StaticFiles(directory=str(dist), html=True))
        logger.info("Dashboard 前端已挂载到 /dashboard")
