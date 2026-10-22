"""
JWT 认证模块
───────────
提供 JWT 签发、验证和 FastAPI 依赖项。
"""

import logging
import time
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

from .config import (
    SECRET_KEY,
    DASHBOARD_USER,
    DASHBOARD_PASSWORD_HASH,
    ACCESS_TOKEN_EXPIRE,
    REFRESH_TOKEN_EXPIRE,
)

_security = HTTPBearer(auto_error=False)

# ──────────────────── 密码验证 ────────────────────

def _verify_password(plain: str, hashed: str) -> bool:
    """bcrypt 验证密码"""
    import bcrypt
    # bcrypt 最多接收 72 字节；异常配置和超长输入均按认证失败处理。
    if not plain or len(plain.encode("utf-8")) > 72:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def authenticate(username: str, password: str) -> bool:
    """验证用户名和密码（凭据来自 .env，不硬编码在代码中）"""
    if not DASHBOARD_PASSWORD_HASH:
        logger.warning("未配置 DASHBOARD_PASSWORD_HASH，拒绝登录（请在 .env 中设置）")
        return False
    if username != DASHBOARD_USER:
        return False
    return _verify_password(password, DASHBOARD_PASSWORD_HASH)


# ──────────────────── Token 签发 ────────────────────

def create_access_token(username: str) -> str:
    payload = {
        "sub": username,
        "type": "access",
        "exp": int(time.time()) + ACCESS_TOKEN_EXPIRE,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def create_refresh_token(username: str) -> str:
    payload = {
        "sub": username,
        "type": "refresh",
        "exp": int(time.time()) + REFRESH_TOKEN_EXPIRE,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


# ──────────────────── Token 验证 ────────────────────

def decode_token(token: str) -> dict:
    """解码并验证 JWT，返回 payload"""
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=["HS256"],
            options={"require": ["sub", "type", "exp", "iat"]},
        )
        if payload.get("sub") != DASHBOARD_USER or payload.get("type") not in ("access", "refresh"):
            raise jwt.InvalidTokenError("无效的账号或 Token 类型")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效的 Token")


# ──────────────────── FastAPI 依赖 ────────────────────

async def get_current_user(
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)] = None,
) -> str:
    """FastAPI 依赖项：从 Bearer token 中提取当前用户名"""
    if not cred:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未提供认证信息")
    payload = decode_token(cred.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token 类型错误")
    username = payload.get("sub")
    if not username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token 无效")
    return username
