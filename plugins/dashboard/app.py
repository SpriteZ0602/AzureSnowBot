"""
Dashboard FastAPI 子应用
──────────────────────
创建 FastAPI 实例，注册所有路由和中间件。
"""

import time
from collections import defaultdict

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routes import (
    auth_router,
    overview_router,
    tokens_router,
    conversations_router,
    memory_router,
    personas_router,
    reminders_router,
    skills_router,
    config_router,
)

# ──────────────────── Rate Limiting ────────────────────

_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_RATE_LIMIT = 5       # 最多 5 次
LOGIN_RATE_WINDOW = 60     # 60 秒内


def create_dashboard_app() -> FastAPI:
    app = FastAPI(
        title="AzureSnowBot Dashboard API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )

    # CORS（开发时允许 Vite dev server）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 登录接口 Rate Limiting
    @app.middleware("http")
    async def rate_limit_login(request: Request, call_next):
        if request.url.path.endswith("/auth/login") and request.method == "POST":
            client_ip = request.client.host if request.client else "unknown"
            now = time.time()
            # 清理过期记录
            _login_attempts[client_ip] = [
                t for t in _login_attempts[client_ip]
                if now - t < LOGIN_RATE_WINDOW
            ]
            if len(_login_attempts[client_ip]) >= LOGIN_RATE_LIMIT:
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"detail": "登录尝试过于频繁，请稍后再试"},
                )
            _login_attempts[client_ip].append(now)
        return await call_next(request)

    # 注册路由
    app.include_router(auth_router, prefix="/auth", tags=["认证"])
    app.include_router(overview_router, prefix="/overview", tags=["总览"])
    app.include_router(tokens_router, prefix="/tokens", tags=["Token 统计"])
    app.include_router(conversations_router, prefix="/conversations", tags=["对话历史"])
    app.include_router(memory_router, prefix="/memory", tags=["记忆管理"])
    app.include_router(personas_router, prefix="/personas", tags=["人格管理"])
    app.include_router(reminders_router, prefix="/reminders", tags=["提醒管理"])
    app.include_router(skills_router, prefix="/skills", tags=["技能管理"])
    app.include_router(config_router, prefix="/config", tags=["配置编辑"])

    return app
