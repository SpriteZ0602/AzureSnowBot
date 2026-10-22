"""图库 HTTP 接口：鉴权媒体、分页、群隔离、共享管理和设置持久化。"""

import importlib.util
import io
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def picture(animated=False):
    data = io.BytesIO()
    image = Image.new("RGB", (20, 20), "red")
    if animated:
        image.save(data, format="GIF", save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")], duration=80, loop=0)
    else:
        image.save(data, format="PNG")
    return data.getvalue()


@pytest.fixture
def api(tmp_path, monkeypatch):
    for name in ("fastapi", "fastapi.security", "fastapi.responses"):
        if isinstance(sys.modules.get(name), MagicMock):
            monkeypatch.delitem(sys.modules, name)
    from fastapi import FastAPI, Depends, HTTPException
    from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
    bearer = HTTPBearer(auto_error=False)
    async def authenticated(credential: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credential is None or credential.credentials != "test-session":
            raise HTTPException(401, "需要登录")
        return "admin"
    auth = types.ModuleType("plugins.dashboard.auth")
    auth.get_current_user = authenticated
    monkeypatch.setitem(sys.modules, "plugins.dashboard.auth", auth)
    lib = load("plugins.stickers_dashboard_test", "plugins/stickers.py")
    lib.ROOT = tmp_path / "data/stickers"
    manager = load("plugins.persona.manager_stickers_test", "plugins/persona/manager.py")
    manager.GROUP_SESSION_DIR = tmp_path / "data/sessions/groups"
    monkeypatch.setitem(sys.modules, "plugins.persona.manager", manager)
    mod = load("plugins.dashboard.routes.stickers_test", "plugins/dashboard/routes/stickers.py")
    mod.library = lib
    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/v1/stickers")
    return app, lib, manager


@pytest.mark.asyncio
async def test_all_routes_including_image_require_auth(api):
    app, lib, _ = api
    item, _ = lib.add("123", picture(), ["猫"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for path in ("groups", "123", f"123/{item['id']}/image"):
            response = await client.get(f"/api/v1/stickers/{path}")
            assert response.status_code == 401
        response = await client.get(f"/api/v1/stickers/123/{item['id']}/image", headers={"Authorization": "Bearer bad"})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_gif_response_preserves_frames_and_blocks_cross_group(api):
    app, lib, _ = api
    raw = picture(animated=True)
    item, _ = lib.add("123", raw, ["猫"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-session"}) as client:
        response = await client.get(f"/api/v1/stickers/123/{item['id']}/image")
        assert response.status_code == 200 and response.content == raw
        assert response.headers["content-type"] == "image/gif"
        assert response.headers["cache-control"] == "private, no-store"
        assert Image.open(io.BytesIO(response.content)).n_frames == 2
        assert (await client.get(f"/api/v1/stickers/456/{item['id']}/image")).status_code == 404


@pytest.mark.asyncio
async def test_filter_pagination_edit_delete_and_live_catalog(api):
    app, lib, _ = api
    first, _ = lib.add("123", picture(), ["猫", "开心"], "99")
    second, _ = lib.add("123", picture(animated=True), ["猫"], "100")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-session"}) as client:
        a = (await client.get("/api/v1/stickers/123?page_size=1")).json()
        b = (await client.get("/api/v1/stickers/123?page_size=1&page=2")).json()
        assert a["total"] == 2 and a["items"][0]["id"] != b["items"][0]["id"]
        assert "file" not in a["items"][0] and "hash" not in a["items"][0]
        filtered = (await client.get("/api/v1/stickers/123?tag=开心")).json()
        assert filtered["total"] == 1 and filtered["items"][0]["id"] == first["id"]
        changed = await client.patch(f"/api/v1/stickers/123/{first['id']}", json={"tags": ["无语"]})
        assert changed.status_code == 200 and lib.tag_counts("123") == {"无语": 1, "猫": 1}
        assert (await client.patch(f"/api/v1/stickers/123/{first['id']}", json={"tags": []})).status_code == 422
        assert (await client.patch(f"/api/v1/stickers/123/{first['id']}", json={"tags": ["x" * 17]})).status_code == 400
        removed = await client.delete(f"/api/v1/stickers/123/{second['id']}")
        assert removed.json() == {"deleted": True}
        assert not lib.image_path("123", second).exists()
        assert (await client.get(f"/api/v1/stickers/123/{second['id']}/image")).status_code == 404
        assert lib.tag_counts("123") == {"无语": 1}


@pytest.mark.asyncio
async def test_group_settings_persist_and_global_summary_has_no_group_byte_quota(api):
    app, lib, manager = api
    lib.add("123", picture(), ["猫"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer test-session"}) as client:
        before = (await client.get("/api/v1/stickers/groups")).json()
        assert before["groups"][0]["enabled"] is True
        assert before["max_count"] == 200 and before["max_total_bytes"] == 5_000_000_000
        response = await client.put("/api/v1/stickers/123/settings", json={"enabled": False})
        assert response.status_code == 200 and response.json() == {"enabled": False}
        assert manager.get_group_config("123")["auto_stickers"] is False
        assert (await client.get("/api/v1/stickers/123")).json()["enabled"] is False
        assert (await client.put("/api/v1/stickers/bad/settings", json={"enabled": True})).status_code == 400
        assert (await client.get("/api/v1/stickers/123?page=0")).status_code == 422
