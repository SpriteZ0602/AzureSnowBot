"""
Dashboard 配置
──────────────
从 .env 读取 Dashboard 专用配置项。
"""

import os
import secrets

# JWT 密钥（未配置时自动生成随机密钥，重启后旧 token 失效）
SECRET_KEY: str = os.getenv("DASHBOARD_SECRET_KEY", "").strip() or secrets.token_urlsafe(32)
if len(SECRET_KEY.encode("utf-8")) < 32:
    raise ValueError("DASHBOARD_SECRET_KEY 至少需要 32 字节；留空可自动生成随机密钥")

# Dashboard 登录凭据（全部来自 .env，代码中不硬编码密码）
# 默认账号 admin；使用 scripts/setup_dashboard.py 初始化随机密码。
DASHBOARD_USER: str = os.getenv("DASHBOARD_USER", "").strip() or "admin"
DASHBOARD_PASSWORD_HASH: str = os.getenv("DASHBOARD_PASSWORD_HASH", "")

# JWT 过期时间（秒）
ACCESS_TOKEN_EXPIRE = 30 * 60      # 30 分钟
REFRESH_TOKEN_EXPIRE = 7 * 24 * 3600  # 7 天
