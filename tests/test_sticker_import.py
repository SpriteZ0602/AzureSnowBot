"""无需引用的十分钟图片收藏：消息归属、自动标签、批量保存和失败反馈。"""

import importlib.util
import io
import json
import sqlite3
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, value)
    spec.loader.exec_module(value)
    return value


def picture(index=0):
    out = io.BytesIO()
    image = Image.new("RGB", (32, 32), (index * 17 % 256, 20, 100))
    if index == 0:
        image.save(out, format="GIF", save_all=True, append_images=[Image.new("RGB", (32, 32), "blue")], duration=80, loop=0)
    else:
        image.save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def system(tmp_path, monkeypatch):
    import plugins
    from plugins import image_context
    monkeypatch.setitem(sys.modules, "nonebot.log", types.SimpleNamespace(logger=MagicMock()))
    lib = load("plugins.stickers_import_test", "plugins/stickers.py", monkeypatch)
    lib.ROOT = tmp_path / "stickers"
    monkeypatch.setattr(plugins, "stickers", lib, raising=False)
    db = load("plugins.group.chatlog_db_import_test", "plugins/group/chatlog_db.py", monkeypatch)
    db.DB_PATH = tmp_path / "chatlog.db"
    monkeypatch.setitem(sys.modules, "plugins.group.chatlog_db", db)
    parser = load("plugins.message_parser_import_test", "plugins/message_parser.py", monkeypatch)
    monkeypatch.setattr(image_context, "register_image", lambda gid, image, origin: parser.image_marker(image.image_id))
    monkeypatch.setitem(sys.modules, "plugins.message_parser", parser)
    service = load("plugins.sticker_import", "plugins/sticker_import.py", monkeypatch)
    calls = AsyncMock()
    async def classify(messages, **kwargs):
        assert kwargs["source"] == "sticker_tagging" and kwargs["response_format"] == {"type": "json_object"}
        previews = [item["image_url"]["url"] for item in messages[1]["content"] if item["type"] == "image_url"]
        assert 1 <= len(previews) <= 4
        return {"choices": [{"message": {"content": json.dumps({"images": [
            {"index": i, "recognized": True, "tags": ["兔子", "开心"], "description": "兔子表情图片"}
            for i in range(len(previews))
        ]})}}]}
    calls.side_effect = classify
    llm = types.ModuleType("plugins.llm")
    llm.SUPPORTS_VISION, llm.call_llm = True, calls
    monkeypatch.setitem(sys.modules, "plugins.llm", llm)
    messages, bodies, downloads = {}, {}, []
    async def get_msg(message_id):
        return messages[str(message_id)]
    bot = types.SimpleNamespace(get_msg=AsyncMock(side_effect=get_msg), get_forward_msg=AsyncMock())
    client_class = httpx.AsyncClient
    def respond(request):
        index = request.url.path.lstrip("/")
        downloads.append(index)
        return httpx.Response(200, content=bodies[index])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw))
    now = int(time.time())
    def record(mid, indices=(), *, gid="123", uid="9", at=None):
        text = " ".join(f"[图片 img_{i:020x}，尚未识别]" for i in indices) if indices else "把我刚才的图片自动存了"
        with db._connect() as conn:
            conn.execute("INSERT INTO messages(group_id,ts,uid,name,text,message_id) VALUES (?,?,?,?,?,?)",
                         (gid, now if at is None else at, uid, "Alice", text, str(mid)))
        raw = []
        for index in indices:
            bodies[str(index)] = picture(index)
            raw.append({"type": "image", "data": {"file": str(index), "url": f"https://qq.example/{index}"}})
        messages[str(mid)] = {"group_id": gid, "sender": {"user_id": uid}, "message": raw}
    nonebot = types.ModuleType("nonebot")
    nonebot.get_bot = lambda: bot
    monkeypatch.setitem(sys.modules, "nonebot", nonebot)
    manager = load("plugins.local_tools.manager", "plugins/local_tools/manager.py", monkeypatch)
    tool = load("plugins.local_tools.stickers_import_test", "plugins/local_tools/stickers.py", monkeypatch)
    return types.SimpleNamespace(lib=lib, db=db, service=service, calls=calls, llm=llm, bot=bot,
                                 bodies=bodies, downloads=downloads, record=record, now=now, tool=tool, manager=manager)


def test_old_database_upgrades_without_losing_history(system):
    s = system
    with sqlite3.connect(s.db.DB_PATH) as conn:
        conn.executescript("CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT, ts INTEGER, uid TEXT, name TEXT, text TEXT);"
                           "CREATE UNIQUE INDEX idx_messages_dedupe ON messages(group_id,ts,uid,text);"
                           "INSERT INTO messages(group_id,ts,uid,name,text) VALUES ('123',1,'9','Alice','old');")
    s.db.append_chatlog("123", "9", "Alice", "same", message_id=2)
    s.db.append_chatlog("123", "9", "Alice", "same", message_id=3)
    s.db.append_chatlog("123", "9", "Alice", "same", message_id=3)
    with s.db._connect() as conn:
        rows = conn.execute("SELECT text,message_id FROM messages ORDER BY id").fetchall()
    assert rows == [("old", ""), ("same", "2"), ("same", "3")]


@pytest.mark.asyncio
async def test_real_tool_collects_only_own_group_past_ten_minutes_before_request(system):
    s = system
    s.record(1, [0, 1], at=s.now - 30)
    s.record(2, [2], uid="8", at=s.now - 20)
    s.record(3, [3], gid="456", at=s.now - 10)
    s.record(4, [4], at=s.now - 601)
    s.record(500)
    s.record(6, [6])  # 同一秒但在请求之后到达，也不能混入。
    ctx = {"_chat_type": "group", "_target_id": "123", "_user_id": "9", "_sticker_user_turn": True,
           "_sticker_request_message_id": 500, "_quoted_message_id": None}
    result = await s.manager.handle_tool_call("local__save_recent_stickers", {}, context=ctx)
    assert result.startswith("最近图片收藏完成") and "新收藏 2 张" in result
    assert s.downloads == ["0", "1"] and s.bot.get_msg.await_count == 1
    assert s.calls.await_count == 1
    assert s.lib.items("456") == []
    assert all(item["tags"] == ["兔子", "开心"] and item["uploader"] == "9" and item["description"] for item in s.lib.items("123"))
    gif = next(item for item in s.lib.items("123") if item["mime"] == "image/gif")
    assert s.lib.read_image("123", gif["id"])[1] == s.bodies["0"]
    assert not list(s.lib.ROOT.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_many_messages_and_multiphoto_message_use_four_image_vision_batches(system):
    s = system
    s.record(1, list(range(6)))
    s.record(2, [6, 7])
    s.record(500)
    result = await s.service.import_recent(s.bot, "123", "9", 500)
    assert "新收藏 8 张" in result and len(s.lib.items("123")) == 8
    assert s.calls.await_count == 2 and s.bot.get_msg.await_count == 2
    assert not list(s.lib.ROOT.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_repeat_import_reuses_files_without_another_vision_request(system):
    s = system
    s.record(1, [0, 1])
    s.record(500)
    await s.service.import_recent(s.bot, "123", "9", 500)
    ids = {i["id"] for i in s.lib.items("123")}
    s.record(501)
    result = await s.service.import_recent(s.bot, "123", "9", 501)
    assert "新收藏 0 张" in result and "复用已有 2 张" in result
    assert s.calls.await_count == 1 and {i["id"] for i in s.lib.items("123")} == ids


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unrecognized", "invalid_json", "bad_index"])
async def test_invalid_labels_are_skipped_and_never_reported_as_saved(system, failure):
    s = system
    s.record(1, [0, 1])
    s.record(500)
    if failure == "invalid_json":
        raw = '{"images":'
    else:
        raw = json.dumps({"images": [
            {"index": 0, "recognized": True, "tags": ["兔子"], "description": "可识别的兔子"},
            {"index": 0 if failure == "bad_index" else 1, "recognized": failure != "unrecognized", "tags": ["兔子"], "description": "不确定"},
        ]})
    s.calls.side_effect = None
    s.calls.return_value = {"choices": [{"message": {"content": raw}}]}
    result = await s.service.import_recent(s.bot, "123", "9", 500)
    expected = 1 if failure == "unrecognized" else 0
    assert result.startswith("[错误]") and f"新收藏 {expected} 张" in result
    assert len(s.lib.items("123")) == expected and not list(s.lib.ROOT.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_broken_image_does_not_prevent_other_images_from_being_saved(system):
    s = system
    s.record(1, [0, 1])
    s.record(500)
    s.bodies["0"] = b"broken image"
    result = await s.service.import_recent(s.bot, "123", "9", 500)
    assert "新收藏 1 张" in result and "失败 1 张" in result
    assert len(s.lib.items("123")) == 1 and not list(s.lib.ROOT.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_quota_stops_and_keeps_completed_imports(system, monkeypatch):
    s = system
    for index in range(20, 23):
        s.lib.add("123", picture(index), ["已有"])
    monkeypatch.setattr(s.lib, "MAX_COUNT", 4)
    s.record(1, [0, 1, 2, 3])
    s.record(500)
    result = await s.service.import_recent(s.bot, "123", "9", 500)
    assert "新收藏 1 张" in result and "失败 1 张" in result and "未处理 2 张" in result
    assert len(s.lib.items("123")) == 4 and not list(s.lib.ROOT.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_vision_service_failure_stops_without_guessing_labels(system):
    s = system
    s.record(1, list(range(6)))
    s.record(500)
    s.calls.side_effect = httpx.ReadTimeout("offline")
    result = await s.service.import_recent(s.bot, "123", "9", 500)
    assert "新收藏 0 张" in result and "失败 4 张" in result and "未处理 2 张" in result
    assert s.calls.await_count == 1 and not s.lib.items("123") and not list(s.lib.ROOT.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_unknown_anchor_and_background_calls_do_not_access_other_users_images(system):
    s = system
    with pytest.raises(ValueError):
        await s.service.import_recent(s.bot, "123", "9", 500)
    result = await s.tool.save_recent_stickers(_context={"_chat_type": "group", "_target_id": "123", "_user_id": "9"})
    assert result.startswith("[错误]")
    s.bot.get_msg.assert_not_awaited()
    s.calls.assert_not_awaited()
