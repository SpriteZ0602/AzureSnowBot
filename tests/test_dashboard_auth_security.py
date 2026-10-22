"""真实 bcrypt/JWT 安全回归，不启动 NoneBot。"""

import importlib.util
import sys
import time
import types
from pathlib import Path

import bcrypt
import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def auth(monkeypatch):
    package = types.ModuleType("dashboard_auth_test")
    package.__path__ = [str(ROOT / "plugins/dashboard")]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret-with-at-least-32-bytes")
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", "")
    load("dashboard_auth_test.config", "plugins/dashboard/config.py")
    mod = load("dashboard_auth_test.auth", "plugins/dashboard/auth.py")
    mod.DASHBOARD_PASSWORD_HASH = bcrypt.hashpw(b"test-password", bcrypt.gensalt(rounds=4)).decode()
    return mod


def test_password_validation(auth):
    assert auth.authenticate("admin", "test-password")
    assert not auth.authenticate("admin", "wrong")
    assert not auth.authenticate("other", "test-password")


@pytest.mark.parametrize("hashed", ["", "malformed-hash"])
def test_missing_or_malformed_hash_fails_closed(auth, hashed):
    auth.DASHBOARD_PASSWORD_HASH = hashed
    assert not auth.authenticate("admin", "test-password")


@pytest.mark.parametrize("password", ["", "x" * 73, "猫" * 25])
def test_invalid_bcrypt_length_denied(auth, password):
    assert not auth.authenticate("admin", password)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_key_is_random(auth, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("DASHBOARD_SECRET_KEY")
    else:
        monkeypatch.setenv("DASHBOARD_SECRET_KEY", value)
    first = load("dashboard_auth_test.config", "plugins/dashboard/config.py")
    second = load("dashboard_auth_test.config", "plugins/dashboard/config.py")
    assert len(first.SECRET_KEY) >= 32
    assert first.SECRET_KEY != second.SECRET_KEY


def test_short_configured_key_rejected(auth, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "weak")
    with pytest.raises(ValueError, match="32"):
        load("dashboard_auth_test.config", "plugins/dashboard/config.py")


def test_default_username(auth, monkeypatch):
    monkeypatch.delenv("DASHBOARD_USER")
    assert load("dashboard_auth_test.config", "plugins/dashboard/config.py").DASHBOARD_USER == "admin"


def test_access_and_refresh_tokens(auth):
    assert auth.decode_token(auth.create_access_token("admin"))["type"] == "access"
    assert auth.decode_token(auth.create_refresh_token("admin"))["type"] == "refresh"


@pytest.mark.parametrize("change", [{"sub": "other"}, {"type": "unknown"}, {"exp": 1}, {"exp": None}])
def test_invalid_claims_denied(auth, change):
    payload = {"sub": "admin", "type": "access", "exp": int(time.time()) + 60, "iat": int(time.time())}
    payload.update(change)
    if payload["exp"] is None:
        payload.pop("exp")
    token = jwt.encode(payload, auth.SECRET_KEY, algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        auth.decode_token(token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_access_dependency_rejects_refresh_or_missing_token(auth):
    with pytest.raises(HTTPException):
        await auth.get_current_user(None)
    cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=auth.create_refresh_token("admin"))
    with pytest.raises(HTTPException):
        await auth.get_current_user(cred)
    cred.credentials = auth.create_access_token("admin")
    assert await auth.get_current_user(cred) == "admin"
