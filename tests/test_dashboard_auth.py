"""
tests/test_dashboard_auth.py
────────────────────────────
测试 Dashboard 认证（authenticate）:
  - 正确凭据通过（bcrypt 哈希验证）
  - 错误密码 / 错误账号拒绝
  - 未配置 DASHBOARD_PASSWORD_HASH 时拒绝登录（不硬编码密码回退）

按 AGENTS.md 约定：importlib 直接加载目标文件，不启动 NoneBot。
"""

import sys
import os
import importlib.util
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 使用真实 fastapi / jwt，避免污染其他 HTTP 安全回归测试。

import bcrypt
import pytest

# 加载 config.py（从 .env 读取凭据）
_spec = importlib.util.spec_from_file_location(
    "plugins.dashboard.config",
    os.path.join(os.path.dirname(__file__), "..", "plugins", "dashboard", "config.py"),
)
_cfg = importlib.util.module_from_spec(_spec)
sys.modules["plugins.dashboard.config"] = _cfg
_spec.loader.exec_module(_cfg)

# 加载 auth.py
_spec = importlib.util.spec_from_file_location(
    "plugins.dashboard.auth",
    os.path.join(os.path.dirname(__file__), "..", "plugins", "dashboard", "auth.py"),
)
_auth = importlib.util.module_from_spec(_spec)
sys.modules["plugins.dashboard.auth"] = _auth
_spec.loader.exec_module(_auth)


@pytest.fixture(autouse=True)
def _restore_creds():
    """每个测试后恢复模块级凭据（测试会改写它们）"""
    user, hashed = _auth.DASHBOARD_USER, _auth.DASHBOARD_PASSWORD_HASH
    yield
    _auth.DASHBOARD_USER, _auth.DASHBOARD_PASSWORD_HASH = user, hashed


# ──────────────────── authenticate ────────────────────

class TestAuthenticate:

    def test_correct_credentials(self):
        _auth.DASHBOARD_USER = "373900859"
        _auth.DASHBOARD_PASSWORD_HASH = bcrypt.hashpw(b"Gdf123123", bcrypt.gensalt()).decode()
        assert _auth.authenticate("373900859", "Gdf123123") is True

    def test_wrong_password(self):
        _auth.DASHBOARD_USER = "373900859"
        _auth.DASHBOARD_PASSWORD_HASH = bcrypt.hashpw(b"Gdf123123", bcrypt.gensalt()).decode()
        assert _auth.authenticate("373900859", "wrong") is False

    def test_wrong_username(self):
        _auth.DASHBOARD_USER = "373900859"
        _auth.DASHBOARD_PASSWORD_HASH = bcrypt.hashpw(b"Gdf123123", bcrypt.gensalt()).decode()
        assert _auth.authenticate("admin", "Gdf123123") is False

    def test_no_hash_denies_login(self):
        """未配置哈希时拒绝登录，不提供任何明文密码回退"""
        _auth.DASHBOARD_USER = "373900859"
        _auth.DASHBOARD_PASSWORD_HASH = ""
        assert _auth.authenticate("373900859", "Gdf123123") is False
        assert _auth.authenticate("373900859", "admin") is False
