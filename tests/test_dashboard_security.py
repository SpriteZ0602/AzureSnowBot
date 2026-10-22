"""HTTP 级别验证认证、敏感配置、目录穿越和正常管理流程。"""

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import bcrypt
import httpx
import pytest
from dotenv import dotenv_values
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "dashboard-security-test-key-32-bytes")
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", bcrypt.hashpw(b"test-password", bcrypt.gensalt(rounds=4)).decode())
    prefix = "dashboard_security_test"
    for name, rel in [(prefix, "plugins"), (prefix + ".dashboard", "plugins/dashboard"),
                      (prefix + ".dashboard.routes", "plugins/dashboard/routes"),
                      (prefix + ".persona", "plugins/persona"), (prefix + ".skill", "plugins/skill")]:
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(ROOT / rel)]
        monkeypatch.setitem(sys.modules, name, pkg)
    monkeypatch.setitem(sys.modules, "nonebot.log", types.SimpleNamespace(logger=MagicMock()))

    def load(name, rel):
        spec = importlib.util.spec_from_file_location(prefix + "." + name, ROOT / rel)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, mod)
        spec.loader.exec_module(mod)
        return mod

    load("dashboard.config", "plugins/dashboard/config.py")
    auth = load("dashboard.auth", "plugins/dashboard/auth.py")
    load("persona.manager", "plugins/persona/manager.py")
    load("skill.manager", "plugins/skill/manager.py")
    app = FastAPI()
    mods = {}
    for name in ("auth", "config_routes", "personas", "skills", "memory", "conversations"):
        mod = load("dashboard.routes." + name, "plugins/dashboard/routes/" + name + ".py")
        mods[name] = mod
        app.include_router(mod.router, prefix="/" + ("config" if name == "config_routes" else name))
    return app, auth, mods


def client(api, authenticated=True):
    app, auth, _ = api
    headers = {"Authorization": "Bearer " + auth.create_access_token("admin")} if authenticated else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


@pytest.mark.asyncio
async def test_endpoints_require_login_and_real_login_works(api):
    async with client(api, False) as c:
        for path in ("/config/env", "/personas", "/skills", "/memory/content", "/conversations/groups"):
            assert (await c.get(path)).status_code == 401
        assert (await c.post("/auth/login", json={"username": "admin", "password": "wrong"})).status_code == 401
        response = await c.post("/auth/login", json={"username": "admin", "password": "test-password"})
        assert response.status_code == 200
        tokens = response.json()
        assert (await c.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})).status_code == 200
        assert (await c.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})).status_code == 401


@pytest.mark.asyncio
async def test_all_secret_fields_and_multiline_values_masked(api):
    Path(".env").write_text('# comment\nPORT=8082\nAPI_KEY="dummy-secret-123" # keep private\nONEBOT_ACCESS_TOKEN=abc123\nPRIVATE_KEY="first-line\nsecond-line"\n', encoding="utf-8")
    async with client(api) as c:
        response = await c.get("/config/env")
        assert response.status_code == 200
        for secret in ("dummy-secret-123", "abc123", "first-line", "second-line", "keep private"):
            assert secret not in response.text
        assert "8082" in response.text and "# comment" in response.text


@pytest.mark.asyncio
async def test_env_write_rejects_injection_and_quotes_literal_values(api):
    Path(".env").write_text('PORT=8082\nUNTOUCHED=original\n', encoding="utf-8")
    async with client(api) as c:
        for body in ({"key": "X\nHOST", "value": "bad"}, {"key": "X", "value": "bad\nHOST=0.0.0.0"}, {"key": "X", "value": "bad\x00"}):
            assert (await c.put("/config/env", json=body)).status_code == 422
        value = "literal # quoted ' text"
        assert (await c.put("/config/env", json={"key": "X", "value": value})).status_code == 200
        assert dotenv_values(".env")["X"] == value
        assert dotenv_values(".env")["UNTOUCHED"] == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../outside", "..\\outside", "/tmp/outside", "C:\\outside", "..", "file:stream", "CON", "猫\nname", "trailing."])
async def test_create_rejects_path_like_names(api, name):
    async with client(api) as c:
        assert (await c.post("/personas", json={"name": name, "prompt": "bad"})).status_code == 400
        assert (await c.post("/skills", json={"name": name, "description": "bad", "body": "bad"})).status_code == 400


@pytest.mark.asyncio
async def test_query_traversal_blocked_in_all_file_routes(api):
    async with client(api) as c:
        assert (await c.post("/personas", json={"name": "safe", "group_id": "../../outside", "prompt": "bad"})).status_code == 400
        for method_name in ("get", "put", "delete"):
            kwargs = {"json": {"prompt": "bad"}} if method_name == "put" else {}
            assert (await getattr(c, method_name)("/personas/default", params={"group_id": "../outside"}, **kwargs)).status_code == 400
        assert (await c.get("/memory/content", params={"scope": "../../outside"})).status_code == 400
        assert (await c.put("/memory/content", params={"scope": "/tmp/outside"}, json={"content": "bad"})).status_code == 400
        assert (await c.get("/conversations/groups/123", params={"persona": "../../admin/history"})).status_code == 400


@pytest.mark.asyncio
async def test_legitimate_chinese_persona_skill_memory_and_history(api):
    async with client(api) as c:
        assert (await c.post("/personas", json={"name": "猫猫", "prompt": "test"})).status_code == 200
        assert (await c.get("/personas/猫猫")).json()["prompt"] == "test"
        assert (await c.put("/personas/猫猫", json={"prompt": "updated"})).status_code == 200
        assert (await c.delete("/personas/猫猫")).status_code == 200
        assert (await c.post("/personas", json={"name": "群猫", "group_id": "123", "prompt": "test"})).status_code == 200
        assert (await c.put("/personas/groups/123/active", params={"name": "群猫"})).status_code == 200
        assert (await c.post("/skills", json={"name": "测试", "description": "test", "body": "body"})).status_code == 200
        assert (await c.get("/skills/测试")).json()["body"] == "body"
        assert (await c.put("/skills/测试", json={"body": "updated"})).status_code == 200
        assert (await c.delete("/skills/测试")).status_code == 200
        assert (await c.put("/memory/content", params={"scope": "123"}, json={"content": "memory"})).status_code == 200
        assert (await c.get("/memory/content", params={"scope": "123"})).json()["content"] == "memory"
        Path("data/sessions/groups/123/群猫.jsonl").write_text(json.dumps({"role": "user", "content": "test"}), encoding="utf-8")
        response = await c.get("/conversations/groups/123", params={"persona": "群猫"})
        assert response.status_code == 200 and response.json()["total"] == 1


@pytest.mark.asyncio
async def test_symlink_to_outside_is_rejected(api, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "MEMORY.md").write_text("private", encoding="utf-8")
    try:
        Path("data/groups/123").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    async with client(api) as c:
        assert (await c.get("/memory/content", params={"scope": "123"})).status_code == 400
        assert (await c.put("/memory/content", params={"scope": "123"}, json={"content": "bad"})).status_code == 400
    assert (outside / "MEMORY.md").read_text(encoding="utf-8") == "private"
