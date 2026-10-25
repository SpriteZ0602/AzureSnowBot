"""真实图片、原子存储、配额、引用删除以及列表负载的行为测试。"""

import asyncio
import base64
import importlib.util
import io
import json
import os
import random
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def picture(color="red", animated=False):
    out = io.BytesIO()
    first = Image.new("RGB", (32, 32), color)
    if animated:
        first.save(out, format="GIF", save_all=True, append_images=[Image.new("RGB", (32, 32), "blue")], duration=100, loop=0)
    else:
        first.save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def lib(tmp_path, monkeypatch):
    mod = load("isolated_stickers", "plugins/stickers.py")
    monkeypatch.setattr(mod, "ROOT", tmp_path / "stickers")
    return mod


def test_gif_bytes_and_animation_survive_reload(lib):
    raw = picture(animated=True)
    item, created = lib.add("123", raw, ["猫", "开心"], "99")
    assert created
    saved, content = lib.read_image("123", item["id"])
    assert content == raw and saved["mime"] == "image/gif"
    assert Image.open(io.BytesIO(content)).n_frames == 2
    reloaded = load("isolated_stickers_reload", "plugins/stickers.py")
    reloaded.ROOT = lib.ROOT
    assert reloaded.get("123", item["id"])["tags"] == ["猫", "开心"]


def test_recollection_merges_but_tag_edit_replaces_and_keeps_id_and_file(lib):
    raw = picture()
    item, _ = lib.add("123", raw, ["猫", "开心"])
    again, created = lib.add("123", raw, ["兔子"])
    assert not created and again["id"] == item["id"]
    assert again["tags"] == ["猫", "开心", "兔子"]
    path = lib.image_path("123", item)
    before = path.read_bytes()
    changed = lib.update_tags("123", item["id"], ["狗"])
    assert changed["id"] == item["id"] and changed["tags"] == ["狗"]
    assert len(lib.items("123")) == 1 and path.read_bytes() == before
    assert lib.tag_counts("123") == {"狗": 1}
    assert lib.select("123", ["猫"]) is None


def test_replace_one_tag_preserves_others_and_enforces_limits(lib, monkeypatch):
    item, _ = lib.add("123", picture(), ["猫", "开心"])
    assert lib.update_tags("123", item["id"], ["兔子"], old_tag="猫")["tags"] == ["兔子", "开心"]
    with pytest.raises(lib.StickerError):
        lib.update_tags("123", item["id"], ["狗"], old_tag="不存在")
    monkeypatch.setattr(lib, "MAX_TAGS", 2)
    with pytest.raises(lib.StickerError):
        lib.update_tags("123", item["id"], ["狗", "伤心"], old_tag="兔子")
    assert lib.get("123", item["id"])["tags"] == ["兔子", "开心"]


@pytest.mark.parametrize("legacy", [False, True])
def test_find_by_original_and_sent_bytes_without_cross_group_or_guessing(lib, legacy):
    raw = picture()
    item, _ = lib.add("123", raw, ["猫"])
    saved = lib.read_image("123", item["id"])[1]
    if legacy:
        index = lib._load("123")
        index["items"][0].pop("file_hash")
        lib._save("123", index)
    assert lib.find_by_content("123", raw)["id"] == item["id"]
    assert lib.find_by_content("123", saved)["id"] == item["id"]
    with pytest.raises(lib.StickerError):
        lib.find_by_content("456", raw)


def test_duplicate_merges_tags_without_copy_and_other_group_isolated(lib):
    item, _ = lib.add("123", picture(), ["猫"])
    merged, created = lib.add("123", picture(), ["开心"])
    assert not created and merged["id"] == item["id"]
    assert lib.tag_counts("123") == {"开心": 1, "猫": 1}
    assert len(list(lib.group_dir("123").glob("images/*"))) == 1
    assert lib.get("456", item["id"]) is None
    other, _ = lib.add("456", picture(), ["狗"])
    assert other["id"] != item["id"]


def test_delete_then_recollect_has_new_identity(lib):
    old, _ = lib.add("123", picture(), ["猫"])
    assert lib.delete("123", old["id"])
    assert not lib.image_path("123", old).exists()
    new, _ = lib.add("123", picture(), ["猫"])
    assert new["id"] != old["id"]
    assert not lib.delete("123", old["id"])
    assert lib.get("123", new["id"])


def test_count_limit_allows_duplicate_update(lib, monkeypatch):
    monkeypatch.setattr(lib, "MAX_COUNT", 1)
    lib.add("123", picture(), ["猫"])
    with pytest.raises(lib.StickerError, match="先删除"):
        lib.add("123", picture("green"), ["猫"])
    assert not lib.add("123", picture(), ["开心"])[1]


def test_global_capacity_is_shared_and_concurrent(lib, monkeypatch):
    prepared = lib._prepare(picture())[0]
    monkeypatch.setattr(lib, "MAX_TOTAL_BYTES", len(prepared))
    def add(gid):
        try:
            lib.add(gid, picture(), ["猫"])
            return True
        except lib.StickerError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, ["123", "456"]))
    assert sum(results) == 1
    assert lib.total_bytes() <= lib.MAX_TOTAL_BYTES


def test_failed_index_commit_rolls_back_new_file(lib, monkeypatch):
    monkeypatch.setattr(lib, "_save", MagicMock(side_effect=OSError("full")))
    with pytest.raises(OSError):
        lib.add("123", picture(), ["猫"])
    assert list(lib.group_dir("123").glob("images/*")) == []


def test_tag_limits_and_update_refresh_catalog(lib, monkeypatch):
    monkeypatch.setattr(lib, "MAX_TAGS", 2)
    item, _ = lib.add("123", picture(), ["猫", "开心"])
    with pytest.raises(lib.StickerError):
        lib.add("123", picture("green"), ["狗"])
    lib.update_tags("123", item["id"], ["狗"])
    assert lib.tag_counts("123") == {"狗": 1}


@pytest.mark.parametrize("raw", [b"not an image", b"GIF89a"])
def test_bad_images_leave_no_files(lib, raw):
    with pytest.raises(lib.StickerError):
        lib.add("123", raw, ["猫"])
    assert not lib.ROOT.exists()


@pytest.fixture(scope="module")
def large_gif():
    rng = random.Random(42)
    frames = []
    for _ in range(7):
        frame = Image.frombytes("P", (1024, 1024), rng.randbytes(1024 * 1024))
        frame.putpalette([value for value in range(256) for _ in range(3)])
        frames.append(frame)
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0, optimize=False)
    raw = out.getvalue()
    assert len(raw) > 8 * 1024 * 1024
    return raw


@pytest.mark.asyncio
async def test_large_gif_download_preserves_original_and_cleans_temp_files(lib, monkeypatch, large_gif):
    client_class = httpx.AsyncClient
    monkeypatch.setattr(lib.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=large_gif)), **kw))
    item, created = await lib.collect("123", "https://qq.example/large.gif", ["兔子"])
    assert created and item["size"] == len(large_gif)
    assert lib.read_image("123", item["id"])[1] == large_gif
    assert Image.open(lib.image_path("123", item)).n_frames == 7
    assert not list(lib.ROOT.rglob("*.tmp"))
    duplicate, created = lib.add("123", large_gif, ["开心"])
    assert not created and duplicate["id"] == item["id"]
    assert lib.total_bytes() == len(large_gif)


def test_cleanup_keeps_old_collection_but_removes_orphans(lib):
    item, _ = lib.add("123", picture(), ["猫"])
    path = lib.image_path("123", item)
    orphan = path.with_name("orphan.jpg")
    orphan.write_bytes(b"unused")
    temp = path.with_name(".interrupted.tmp")
    temp.write_bytes(b"temp")
    for file in [path, orphan, temp]:
        os.utime(file, (time.time() - 90000,) * 2)
    lib.cleanup()
    assert path.exists() and not orphan.exists() and not temp.exists()


@pytest.mark.asyncio
async def test_download_stops_at_global_budget_and_cleans_temp_files(lib, monkeypatch):
    client_class = httpx.AsyncClient
    monkeypatch.setattr(lib, "MAX_TOTAL_BYTES", 10)
    monkeypatch.setattr(lib.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 11)), **kw))
    with pytest.raises(lib.StickerLimitError, match="全库容量上限"):
        await lib.collect("123", "https://qq.example/image", ["猫"])
    assert not list(lib.ROOT.rglob("*.tmp"))
    assert lib.items("123") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "invalid", "index"])
async def test_download_failures_leave_no_temp_or_unindexed_images(lib, monkeypatch, failure):
    client_class = httpx.AsyncClient
    def response(request):
        if failure == "network":
            raise httpx.ReadError("connection lost")
        return httpx.Response(200, content=b"not an image" if failure == "invalid" else picture(animated=True))
    monkeypatch.setattr(lib.httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(response), **kw))
    if failure == "index":
        monkeypatch.setattr(lib, "_save", MagicMock(side_effect=OSError("full")))
    with pytest.raises((httpx.ReadError, lib.StickerError, OSError)):
        await lib.collect("123", "https://qq.example/image", ["猫"])
    assert not list(lib.ROOT.rglob("*.tmp"))
    assert not list(lib.ROOT.rglob("*.gif"))
    assert lib.items("123") == []


@pytest.fixture
def sticker_tools(lib, monkeypatch):
    manager = types.ModuleType("plugins.local_tools.manager")
    manager.register_tool = lambda **kw: lambda fn: fn
    monkeypatch.setitem(sys.modules, "plugins.local_tools.manager", manager)
    mod = load("plugins.local_tools.stickers_under_test", "plugins/local_tools/stickers.py")
    monkeypatch.setattr(mod, "library", lib)
    return mod


@pytest.mark.asyncio
async def test_update_quoted_bot_sticker_replaces_tags_without_downloading(sticker_tools, lib, monkeypatch):
    item, _ = lib.add("123", picture(), ["猫", "开心"])
    store = types.SimpleNamespace(get_context=lambda gid, ref: {"kind": "sticker", "sticker_id": item["id"]})
    monkeypatch.setitem(sys.modules, "plugins.message_store", store)
    ctx = {"_chat_type": "group", "_target_id": "123", "_sticker_user_turn": True, "_quoted_message_id": 77}
    result = await sticker_tools.update_quoted_sticker_tags(["兔子"], _context=ctx)
    assert result.startswith("已修改表情") and lib.get("123", item["id"])["tags"] == ["兔子"]
    result = await sticker_tools.update_quoted_sticker_tags(["狗"], old_tag="兔子", _context=ctx)
    assert result.startswith("已修改表情") and lib.get("123", item["id"])["tags"] == ["狗"]
    assert len(lib.items("123")) == 1


@pytest.mark.asyncio
async def test_old_bot_reference_never_edits_recollected_image(sticker_tools, lib, monkeypatch):
    raw = picture()
    old, _ = lib.add("123", raw, ["猫"])
    lib.delete("123", old["id"])
    new, _ = lib.add("123", raw, ["兔子"])
    monkeypatch.setitem(sys.modules, "plugins.message_store", types.SimpleNamespace(
        get_context=lambda gid, ref: {"kind": "sticker", "sticker_id": old["id"]}))
    ctx = {"_chat_type": "group", "_target_id": "123", "_sticker_user_turn": True,
           "_quoted_message_id": 77, "_quoted_sticker_urls": ["https://example/original"]}
    result = await sticker_tools.update_quoted_sticker_tags(["狗"], _context=ctx)
    assert "已经删除" in result and lib.get("123", new["id"])["tags"] == ["兔子"]


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [
    {"_chat_type": "private", "_sticker_user_turn": True},
    {"_chat_type": "group"},
    {"_chat_type": "group", "_sticker_user_turn": True},
])
async def test_update_tags_requires_current_group_turn_and_quote(sticker_tools, context):
    assert (await sticker_tools.update_quoted_sticker_tags(["兔子"], _context=context)).startswith("[错误]")


@pytest.mark.asyncio
async def test_update_tags_rejects_cross_group_sticker_id(sticker_tools, lib, monkeypatch):
    item, _ = lib.add("456", picture(), ["猫"])
    monkeypatch.setitem(sys.modules, "plugins.message_store", types.SimpleNamespace(
        get_context=lambda gid, ref: {"kind": "sticker", "sticker_id": item["id"]}))
    ctx = {"_chat_type": "group", "_target_id": "123", "_sticker_user_turn": True, "_quoted_message_id": 77}
    assert (await sticker_tools.update_quoted_sticker_tags(["兔子"], _context=ctx)).startswith("[错误]")
    assert lib.get("456", item["id"])["tags"] == ["猫"]


@pytest.fixture
def batch_quote(sticker_tools, lib, monkeypatch, tmp_path):
    parser = load("plugins.message_parser_sticker_batch_test", "plugins/message_parser.py")
    store = load("plugins.message_store_sticker_batch_test", "plugins/message_store.py")
    store.DB_PATH = tmp_path / "batch-chatlog.db"
    parser.store = store
    monkeypatch.setitem(sys.modules, "plugins.message_parser", parser)
    from plugins import image_context
    monkeypatch.setattr(image_context, "register_image", lambda gid, image, origin: parser.image_marker(image.image_id))
    raw = [{"type": "image", "data": {"file": f"image-{i}", "url": f"https://qq.example/{i}"}} for i in range(8)]
    bot = types.SimpleNamespace(get_msg=AsyncMock(return_value={"message": raw}), get_forward_msg=AsyncMock())
    monkeypatch.setattr(sys.modules["nonebot"], "get_bot", lambda: bot, raising=False)
    bodies = {str(i): picture((i * 25, 10, 30)) for i in range(8)}
    bodies["1"] = picture(animated=True)
    downloads = []
    def respond(request):
        key = request.url.path.lstrip("/")
        downloads.append(key)
        return httpx.Response(200, content=bodies[key])
    client_class = httpx.AsyncClient
    monkeypatch.setattr(lib.httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw))
    ctx = {"_chat_type": "group", "_target_id": "123", "_user_id": "99", "_sticker_user_turn": True,
           "_quoted_message_id": 77, "_quoted_sticker_urls": [seg["data"]["url"] for seg in raw[:4]]}
    return bot, raw, bodies, downloads, ctx


@pytest.mark.asyncio
async def test_update_original_quote_matches_hash_cleans_temp_and_does_not_add(sticker_tools, lib, batch_quote):
    bot, raw, bodies, downloads, ctx = batch_quote
    bot.get_msg.return_value = {"message": raw[:1], "group_id": 123, "sender": {"user_id": 5}}
    item, _ = lib.add("123", bodies["0"], ["猫", "开心"])
    ctx["_quoted_sticker_urls"] = ["https://qq.example/0"]
    result = await sticker_tools.update_quoted_sticker_tags(["兔子"], old_tag="猫", _context=ctx)
    assert result.startswith("已修改表情") and downloads == ["0"]
    assert lib.get("123", item["id"])["tags"] == ["兔子", "开心"]
    assert len(lib.items("123")) == 1 and not list(lib.ROOT.rglob(".download-*.tmp"))


@pytest.mark.asyncio
async def test_update_quote_rejects_multiple_images_and_unknown_image(sticker_tools, lib, batch_quote):
    bot, raw, _, downloads, ctx = batch_quote
    assert (await sticker_tools.update_quoted_sticker_tags(["兔子"], _context=ctx)).startswith("[错误]")
    assert downloads == []
    bot.get_msg.return_value = {"message": raw[:1]}
    ctx["_quoted_sticker_urls"] = ["https://qq.example/0"]
    assert (await sticker_tools.update_quoted_sticker_tags(["兔子"], _context=ctx)).startswith("[错误]")
    assert lib.items("123") == []


@pytest.mark.asyncio
async def test_unmapped_old_bot_quote_never_falls_back_to_same_image_hash(sticker_tools, lib, batch_quote):
    bot, raw, bodies, downloads, ctx = batch_quote
    bot.self_id = 100
    bot.get_msg.return_value = {"message": raw[:1], "sender": {"user_id": 100}}
    item, _ = lib.add("123", bodies["0"], ["兔子"])
    result = await sticker_tools.update_quoted_sticker_tags(["狗"], _context=ctx)
    assert "关联已失效" in result and downloads == []
    assert lib.get("123", item["id"])["tags"] == ["兔子"]


@pytest.mark.asyncio
async def test_batch_collects_more_than_four_deduplicates_and_preserves_gif(sticker_tools, lib, batch_quote):
    bot, _, bodies, downloads, ctx = batch_quote
    bodies["7"] = bodies["0"]
    result = await sticker_tools.save_quoted_sticker(["兔子", "开心"], all_images=True, _context=ctx)
    assert result.startswith("批量收藏完成")
    assert "新收藏 7 张" in result and "合并标签 1 张" in result and "失败 0 张" in result
    assert downloads == [str(i) for i in range(8)]
    assert len(lib.items("123")) == 7 and lib.items("456") == []
    assert all(item["tags"] == ["兔子", "开心"] and item["uploader"] == "99" for item in lib.items("123"))
    gif = next(item for item in lib.items("123") if item["mime"] == "image/gif")
    assert lib.read_image("123", gif["id"])[1] == bodies["1"]
    bot.get_msg.assert_awaited_once_with(message_id=77)


@pytest.mark.asyncio
async def test_batch_reports_partial_failure_and_retry_reuses_successes(sticker_tools, lib, batch_quote):
    bot, raw, bodies, downloads, ctx = batch_quote
    bot.get_msg.return_value = {"message": raw[:3]}
    bodies["1"] = b"broken image"
    first = await sticker_tools.save_quoted_sticker(["兔子"], all_images=True, _context=ctx)
    assert first.startswith("[错误]") and "新收藏 2 张" in first and "失败 1 张" in first and "第 2 张" in first
    assert len(lib.items("123")) == 2 and downloads == ["0", "1", "2"]
    second = await sticker_tools.save_quoted_sticker(["兔子"], all_images=True, _context=ctx)
    assert "新收藏 0 张" in second and "合并标签 2 张" in second and len(lib.items("123")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["count", "bytes"])
async def test_batch_stops_at_quota_preserving_completed_images(sticker_tools, lib, batch_quote, monkeypatch, limit):
    bot, raw, bodies, downloads, ctx = batch_quote
    bot.get_msg.return_value = {"message": raw[:3]}
    if limit == "count":
        monkeypatch.setattr(lib, "MAX_COUNT", 3)
        for color in ("green", "yellow"):
            lib.add("123", picture(color), ["已有"])
    else:
        monkeypatch.setattr(lib, "MAX_TOTAL_BYTES", len(lib._prepare(bodies["0"])[0]))
    result = await sticker_tools.save_quoted_sticker(["兔子"], all_images=True, _context=ctx)
    assert result.startswith("[错误]") and "新收藏 1 张" in result and "失败 1 张" in result and "未处理 1 张" in result
    assert downloads == ["0", "1"]
    assert lib.tag_counts("123")["兔子"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["missing_url", "too_many", "forward_limit", "forward_failed"])
async def test_incomplete_batch_does_not_download_or_claim_all_saved(sticker_tools, lib, batch_quote, source):
    bot, raw, _, downloads, ctx = batch_quote
    if source == "missing_url":
        bot.get_msg.return_value = {"message": [raw[0], {"type": "image", "data": {"file": "missing"}}]}
    elif source == "too_many":
        bot.get_msg.return_value = {"message": [{"type": "image", "data": {
            "file": str(i), "url": f"https://qq.example/{i}"}} for i in range(201)]}
    else:
        bot.get_msg.return_value = {"message": [{"type": "forward", "data": {"id": "many"}}]}
        if source == "forward_limit":
            bot.get_forward_msg.return_value = {"messages": [{"sender": {}, "message": [raw[0]]} for _ in range(101)]}
        else:
            bot.get_forward_msg.side_effect = RuntimeError("expired")
    result = await sticker_tools.save_quoted_sticker(["兔子"], all_images=True, _context=ctx)
    assert result.startswith("[错误]") and "未开始收藏" in result
    assert downloads == [] and lib.items("123") == []


@pytest.mark.asyncio
async def test_batch_can_collect_all_images_in_forward(sticker_tools, lib, batch_quote):
    bot, raw, _, downloads, ctx = batch_quote
    bot.get_msg.return_value = {"message": [{"type": "forward", "data": {"id": "pictures"}}]}
    bot.get_forward_msg.return_value = {"messages": [{"sender": {}, "message": [seg]} for seg in raw]}
    result = await sticker_tools.save_quoted_sticker(["兔子"], all_images=True, _context=ctx)
    assert result.startswith("批量收藏完成") and len(downloads) == len(lib.items("123")) == 8


@pytest.mark.asyncio
async def test_tools_require_user_capability_and_single_quote(sticker_tools, lib):
    background = {"_chat_type": "group", "_target_id": "123", "_quoted_message_id": 77,
                  "_quoted_sticker_urls": ["https://a"]}
    assert "当前群用户对话" in await sticker_tools.save_quoted_sticker(["猫"], _context=background)
    assert "当前群用户对话" in await sticker_tools.delete_quoted_sticker(_context=background)
    assert "当前群用户对话" in await sticker_tools.request_sticker(["猫"], _context=background)
    no_quote = {"_chat_type": "group", "_target_id": "123", "_sticker_user_turn": True}
    assert "未取得本轮引用消息 ID" in await sticker_tools.save_quoted_sticker(["猫"], _context=no_quote)
    assert "未取得本轮引用消息 ID" in await sticker_tools.delete_quoted_sticker(_context=no_quote)
    ctx = {**no_quote, "_quoted_message_id": 77,
           "_quoted_sticker_urls": ["https://a", "https://b"]}
    assert "一张" in await sticker_tools.save_quoted_sticker(["猫"], _context=ctx)
    assert lib.items("123") == []


@pytest.mark.asyncio
async def test_quoted_delete_uses_group_and_message_mapping(sticker_tools, lib, monkeypatch):
    item, _ = lib.add("123", picture(), ["猫"])
    message_store = types.ModuleType("plugins.message_store")
    message_store.get_context = lambda gid, key: {"kind": "sticker", "sticker_id": item["id"]} if gid == "123" and key == "qq:77" else None
    monkeypatch.setitem(sys.modules, "plugins.message_store", message_store)
    ctx = {"_chat_type": "group", "_target_id": "456", "_quoted_message_id": 77, "_sticker_user_turn": True}
    assert "无法唯一定位" in await sticker_tools.delete_quoted_sticker(_context=ctx)
    ctx["_target_id"] = "123"
    assert "已删除" in await sticker_tools.delete_quoted_sticker(_context=ctx)
    new, _ = lib.add("123", picture(), ["猫"])
    assert "已经删除" in await sticker_tools.delete_quoted_sticker(_context=ctx)
    assert lib.get("123", new["id"])


@pytest.mark.asyncio
async def test_user_requested_sticker_is_queued_when_automatic_is_off(sticker_tools, lib, monkeypatch):
    item, _ = lib.add("123", picture(), ["兔子"])
    monkeypatch.setattr(lib, "enabled", lambda gid: False)
    ctx = {"_chat_type": "group", "_target_id": "123", "_sticker_user_turn": True}
    result = await sticker_tools.request_sticker(["兔子"], _context=ctx)
    assert "尚未发送" in result
    assert ctx["_requested_sticker"] == item["id"]


@pytest.fixture
def commands(lib, monkeypatch):
    matcher = types.SimpleNamespace(handle=lambda: lambda fn: fn, finish=AsyncMock(), send=AsyncMock())
    nonebot = types.ModuleType("nonebot")
    nonebot.on_message = lambda **kw: matcher
    nonebot.get_driver = lambda: types.SimpleNamespace(config=types.SimpleNamespace(admin_number="999"))
    monkeypatch.setitem(sys.modules, "nonebot", nonebot)
    monkeypatch.setitem(sys.modules, "nonebot.log", types.SimpleNamespace(logger=MagicMock()))
    rule = MagicMock()
    monkeypatch.setitem(sys.modules, "nonebot.rule", types.SimpleNamespace(Rule=rule, startswith=rule))
    adapter = types.ModuleType("nonebot.adapters.onebot.v11")
    adapter.Bot, adapter.GroupMessageEvent, adapter.PrivateMessageEvent = object, type("Group", (), {}), type("Private", (), {})
    monkeypatch.setitem(sys.modules, "nonebot.adapters.onebot.v11", adapter)
    monkeypatch.setitem(sys.modules, "plugins.group.utils", types.SimpleNamespace(in_whitelist=lambda gid: True))
    mod = load("plugins.sticker_commands_under_test", "plugins/sticker_commands.py")
    mod.library = lib
    mod.SEND_INTERVAL = 0
    return mod


@pytest.mark.asyncio
async def test_list_repeats_multitag_images_and_preserves_gif(commands, lib, monkeypatch):
    gif = picture(animated=True)
    lib.add("123", gif, ["猫", "开心"])
    lib.add("123", picture(), ["猫"])
    monkeypatch.setattr(commands, "MAX_BATCH_IMAGES", 1)
    batches = [batch async for batch in commands.transport_batches(object(), "123", 99, "Bot", entries=lib.items("123"), missing=set())]
    assert len(batches) == 3
    for batch in batches:
        assert len(json.dumps(batch).encode()) < commands.MAX_BATCH_BYTES
    raw_images = [base64.b64decode(node["data"]["content"][-1]["data"]["file"].split("//", 1)[1]) for batch in batches for node in batch]
    assert raw_images.count(gif) == 2


@pytest.mark.asyncio
async def test_list_places_large_gif_in_its_own_batch(commands, lib, large_gif, monkeypatch):
    from plugins import sticker_transport as transport
    lib.add("123", picture(), ["兔子"])
    large, _ = lib.add("123", large_gif, ["兔子"])
    lib.add("123", picture("green"), ["兔子"])
    async def source(bot, gid, sticker_id, **kwargs):
        item = lib.get(gid, sticker_id)
        if sticker_id == large["id"]:
            return item, "/napcat/fixture.gif"
        return item, "base64://" + base64.b64encode(lib.read_image(gid, sticker_id)[1]).decode("ascii")
    monkeypatch.setattr(transport, "image_source", source)
    batches = [batch async for batch in commands.transport_batches(object(), "123", 99, "Bot", entries=lib.items("123"), missing=set())]
    assert len(batches) == 3 and all(len(batch) == 1 for batch in batches)
    assert all(len(json.dumps(batch).encode()) < commands.MAX_BATCH_BYTES for batch in batches)
    assert batches[1][0]["data"]["content"][-1]["data"]["file"] == "/napcat/fixture.gif"


@pytest.mark.asyncio
async def test_list_unknown_stops_after_first_batch(commands, lib, monkeypatch):
    lib.add("123", picture(), ["猫", "开心"])
    monkeypatch.setattr(commands, "MAX_BATCH_IMAGES", 1)
    bot = types.SimpleNamespace(self_id=99, call_api=AsyncMock(side_effect=TimeoutError()))
    chunker = types.ModuleType("plugins.chunker")
    chunker._session_locks = {"g:123": asyncio.Lock()}
    chunker._get_self_nickname = AsyncMock(return_value="Bot")
    chunker._delivery_error = lambda exc: types.SimpleNamespace(status="unknown")
    chunker._remember_delivery = MagicMock()
    monkeypatch.setitem(sys.modules, "plugins.chunker", chunker)
    result = await commands.send_list(bot, "123")
    assert result.status == "unknown" and result.batches == 0 and result.displayed == 0
    assert bot.call_api.await_count == 1


@pytest.fixture
def listing_sender(commands, monkeypatch):
    chunker = types.ModuleType("plugins.chunker")
    chunker._session_locks = {"g:123": asyncio.Lock()}
    chunker._get_self_nickname = AsyncMock(return_value="Bot")
    chunker._delivery_error = lambda exc: types.SimpleNamespace(status="unknown" if isinstance(exc, TimeoutError) else "failed")
    chunker._remember_delivery = lambda kind, gid, response, *args: (response["message_id"],) if response.get("message_id") else ()
    monkeypatch.setitem(sys.modules, "plugins.chunker", chunker)
    return chunker


def many_stickers(lib, count=73):
    return [lib.add("123", picture(f"#{index:06x}"), ["兔子", "开心"] if index % 10 == 0 else ["兔子"])[0]
            for index in range(count)]


@pytest.mark.asyncio
async def test_list_sends_all_73_images_across_tags_and_batches(commands, lib, listing_sender):
    many_stickers(lib)
    delivered = {}
    async def call(action, **params):
        if action == "send_group_forward_msg":
            mid = len(delivered) + 1
            delivered[str(mid)] = params["messages"]
            return {"message_id": mid}
        assert action == "get_forward_msg"
        # 标准节点回包；图片不经过普通引用的四图上限。
        return {"messages": delivered[params["message_id"]]}
    bot = types.SimpleNamespace(self_id=99, call_api=AsyncMock(side_effect=call))
    result = await commands.send_list(bot, "123")
    assert result.status == "sent" and result.unique == 73
    assert result.displayed == result.total == 81
    assert result.batches == 5 and all(len(nodes) <= 20 for nodes in delivered.values())
    assert {commands._node_ordinal(node) for nodes in delivered.values() for node in nodes} == set(range(1, 82))


@pytest.mark.asyncio
async def test_list_repairs_only_confirmed_missing_nodes_after_silent_truncation(commands, lib, listing_sender):
    many_stickers(lib, 20)
    sent, delivered = [], {}
    async def call(action, **params):
        if action == "send_group_forward_msg":
            nodes = params["messages"]
            sent.append(nodes)
            mid = str(len(sent))
            delivered[mid] = nodes[:7] if len(sent) == 1 else nodes
            return {"message_id": int(mid), "forward_id": mid}
        # NapCat 回包形式，不是 node 包装形式。
        return {"messages": [{"message": n["data"]["content"]} for n in delivered[params["message_id"]]]}
    bot = types.SimpleNamespace(self_id=99, call_api=AsyncMock(side_effect=call))
    result = await commands.send_list(bot, "123")
    assert result.status == "sent" and result.displayed == result.total == 22
    original = {commands._node_ordinal(n) for n in sent[0][:7]}
    repaired = {commands._node_ordinal(n[0]) for n in sent[1:14]}
    assert not original & repaired and len(repaired) == 13


@pytest.mark.asyncio
async def test_missing_local_file_does_not_hide_remaining_items(commands, lib, listing_sender):
    entries = many_stickers(lib, 30)
    lib.image_path("123", entries[7]).unlink()
    delivered = {}
    async def call(action, **params):
        if action == "send_group_forward_msg":
            mid = str(len(delivered) + 1)
            delivered[mid] = params["messages"]
            return {"message_id": int(mid)}
        return {"messages": delivered[params["message_id"]]}
    result = await commands.send_list(types.SimpleNamespace(self_id=99, call_api=AsyncMock(side_effect=call)), "123")
    assert result.status == "failed" and result.missing == 1
    assert result.displayed == result.total - 1 and result.batches == 2
    assert any(commands._node_ordinal(n) > 20 for batch in delivered.values() for n in batch)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["verification_error", "missing_id", "empty_verification"])
async def test_unverifiable_delivery_never_resends(commands, lib, listing_sender, mode):
    many_stickers(lib, 30)
    async def call(action, **params):
        if action == "send_group_forward_msg":
            return {} if mode == "missing_id" else {"message_id": 1}
        if mode == "verification_error":
            raise TimeoutError()
        return {"messages": []}
    bot = types.SimpleNamespace(self_id=99, call_api=AsyncMock(side_effect=call))
    result = await commands.send_list(bot, "123")
    assert result.status == "unknown" and result.displayed == 0
    assert sum(call.args[0] == "send_group_forward_msg" for call in bot.call_api.await_args_list) == 1


@pytest.fixture
def runtime(lib, monkeypatch):
    mod = load("plugins.sticker_reply_test", "plugins/sticker_reply.py")
    monkeypatch.setattr(mod, "library", lib)
    monkeypatch.setattr(lib, "enabled", lambda gid: True)
    return mod


def test_catalog_is_live_and_selection_requires_all_tags(runtime, lib):
    cat, _ = lib.add("123", picture(), ["猫", "开心"])
    lib.add("123", picture("green"), ["狗", "开心"])
    assert "猫（1）" in runtime.prompt("123")
    text, selected = runtime.prepare("123", "好耶\n[[sticker:猫,开心]]")
    assert text == "好耶" and selected["id"] == cat["id"]
    assert runtime.prepare("123", "好耶\n[[sticker:猫,伤心]]")[1] is None
    lib.update_tags("123", cat["id"], ["无语"])
    assert "猫（" not in runtime.prompt("123") and "无语（1）" in runtime.prompt("123")
    assert runtime.prepare("456", "[[sticker:无语]]")[1] is None


@pytest.mark.parametrize("text", ["```\n[[sticker:猫]]\n```", "> [[sticker:猫]]", "示例 `[[sticker:猫]]`"])
def test_examples_never_trigger_selection(runtime, text):
    clean, tags = runtime.split_marker(text)
    assert clean == text and tags is None


def test_invalid_and_prelude_markers_do_not_leak_or_execute(runtime):
    assert runtime.split_marker("你好 [[sticker:猫]]") == ("你好", None)
    assert runtime.split_marker("你好\n[[sticker:未完成") == ("你好", None)
    assert runtime.split_marker("[[sticker:猫]]\n还有文字") == ("还有文字", None)


def test_auto_has_no_cooldown_and_still_respects_toggle(runtime, lib, monkeypatch):
    item, _ = lib.add("123", picture(), ["猫"])
    data = lib._load("123")
    data["last_auto_at"] = time.time()
    lib._save("123", data)
    assert runtime.prepare("123", "[[sticker:猫]]")[1]["id"] == item["id"]
    lib.mark_used("123", item["id"])
    assert runtime.prepare("123", "[[sticker:猫]]")[1]["id"] == item["id"]
    monkeypatch.setattr(lib, "enabled", lambda gid: False)
    assert runtime.prepare("123", "[[sticker:猫]]")[1] is None
    assert runtime.prepare("123", "", requested_id=item["id"])[1]["id"] == item["id"]


@pytest.fixture
def sender(lib, tmp_path, monkeypatch):
    import plugins
    monkeypatch.setattr(plugins, "stickers", lib, raising=False)
    monkeypatch.setitem(sys.modules, "plugins.stickers", lib)
    adapter = types.ModuleType("nonebot.adapters.onebot.v11")
    adapter.Bot, adapter.Message, adapter.MessageSegment = object, MagicMock(), MagicMock()
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
    mod = load("plugins.chunker_sticker_test", "plugins/chunker.py")
    monkeypatch.setattr(mod, "Message", lambda value: value)
    monkeypatch.setattr(mod, "MessageSegment", types.SimpleNamespace(
        reply=lambda ident: f"[reply:{ident}]", image=lambda value: {"image": value}))
    monkeypatch.setattr(mod, "_self_nickname", "Bot")
    monkeypatch.setattr(lib, "enabled", lambda gid: True)
    records = []
    manager = types.ModuleType("plugins.persona.manager")
    manager.append_message = lambda gid, message, persona=None: records.append((gid, message, persona))
    monkeypatch.setitem(sys.modules, "plugins.persona.manager", manager)
    store = load("plugins.message_store_sticker_test", "plugins/message_store.py")
    store.DB_PATH = tmp_path / "chatlog.db"
    monkeypatch.setitem(sys.modules, "plugins.message_store", store)
    bot = types.SimpleNamespace(self_id=99, send_group_msg=AsyncMock(return_value={"message_id": 501}),
                                call_api=AsyncMock(return_value={"message_id": 500}))
    event = types.SimpleNamespace(group_id=123, user_id=1, message_id=100)
    return mod, bot, event, records, store


@pytest.mark.asyncio
async def test_forward_then_gif_records_mapping_and_supports_quoted_delete(sender, lib, sticker_tools):
    mod, bot, event, records, store = sender
    gif = picture(animated=True)
    item, _ = lib.add("123", gif, ["猫"])
    result = await mod.send_chunked(bot, event, ["a", "b", "c"], sticker=item, persona="cat")
    assert result.message_ids == (500,) and result.sticker_result.message_ids == (501,)
    encoded = bot.send_group_msg.await_args.kwargs["message"]["image"].split("//", 1)[1]
    assert base64.b64decode(encoded) == gif
    mapping = store.get_context("123", "qq:501")
    assert mapping["sticker_id"] == item["id"] and mapping["kind"] == "sticker"
    assert store.get_context("456", "qq:501") is None
    assert "已发送" in records[0][1]["content"] and records[0][2] == "cat"
    ctx = {"_chat_type": "group", "_target_id": "123", "_quoted_message_id": 501, "_sticker_user_turn": True}
    assert "已删除" in await sticker_tools.delete_quoted_sticker(_context=ctx)


@pytest.mark.asyncio
async def test_unknown_image_delivery_never_retries_or_claims_success(sender, lib):
    mod, bot, event, records, store = sender
    item, _ = lib.add("123", picture(), ["猫"])
    bot.send_group_msg.side_effect = TimeoutError()
    result = await mod.send_chunked(bot, event, [], sticker=item, persona="cat")
    assert result.sticker_result.status == "unknown"
    assert bot.send_group_msg.await_count == 1
    assert "状态未知" in records[0][1]["content"]
    assert store.get_context("123", "qq:501") is None


@pytest.mark.asyncio
async def test_consecutive_automatic_stickers_are_both_sent(sender, lib):
    mod, bot, event, records, _ = sender
    lib.add("123", picture(), ["猫"])
    for _ in range(2):
        selected = lib.select("123", ["猫"], automatic=True)
        result = await mod.send_chunked(bot, event, [], sticker=selected, persona="cat")
        assert result.sticker_result.status == "sent"
    assert bot.send_group_msg.await_count == 2 and len(records) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [True, False])
async def test_send_rechecks_auto_switch_but_allows_explicit_requests(sender, lib, monkeypatch, automatic):
    mod, bot, event, _, _ = sender
    lib.add("123", picture(), ["猫"])
    selected = lib.select("123", ["猫"], automatic=automatic)
    monkeypatch.setattr(lib, "enabled", lambda gid: False)
    result = await mod.send_chunked(bot, event, [], sticker=selected, persona="cat")
    if automatic:
        assert result.sticker_result is None
        bot.send_group_msg.assert_not_awaited()
    else:
        assert result.sticker_result.status == "sent"
        bot.send_group_msg.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_forward_does_not_send_sticker(sender, lib):
    mod, bot, event, records, _ = sender
    item, _ = lib.add("123", picture(), ["猫"])
    bot.call_api.side_effect = TimeoutError()
    result = await mod.send_chunked(bot, event, ["a", "b", "c"], sticker=item, persona="cat")
    assert result.status == "unknown" and result.sticker_result is None
    bot.send_group_msg.assert_not_awaited()
    assert records == []


@pytest.mark.asyncio
async def test_deleted_attachment_fails_without_sending_wrong_image(sender, lib):
    mod, bot, event, records, _ = sender
    item, _ = lib.add("123", picture(), ["猫"])
    lib.delete("123", item["id"])
    result = await mod.send_chunked(bot, event, [], sticker=item, persona="cat")
    assert result.sticker_result.status == "failed"
    bot.send_group_msg.assert_not_awaited()
    assert "发送失败" in records[0][1]["content"]


@pytest.mark.asyncio
async def test_large_gif_uploads_bounded_chunks_and_sends_remote_path(sender, lib, monkeypatch):
    from plugins import sticker_transport as transport
    mod, bot, event, records, _ = sender
    raw = picture(animated=True)
    item, _ = lib.add("123", raw, ["猫"])
    monkeypatch.setattr(transport, "INLINE_BYTES", 1)
    monkeypatch.setattr(transport, "CHUNK_BYTES", 17)
    chunks = []
    async def api(action, **params):
        assert action == "upload_file_stream"
        if params.get("is_complete"):
            assert b"".join(chunks) == raw
            return {"status": "file_complete", "file_path": "/napcat/temp/fixture.gif",
                    "file_size": len(raw), "sha256": lib.content_hash(raw)}
        chunk = base64.b64decode(params["chunk_data"])
        assert len(chunk) <= 17
        assert params["expected_sha256"] == lib.content_hash(raw)
        assert params["file_retention"] == 300_000
        chunks.append(chunk)
        return {"status": "chunk_received", "received_chunks": len(chunks)}
    bot.call_api.side_effect = api
    def forbid_full_read(*args):
        raise AssertionError("large file must not be read into one bytes object")
    monkeypatch.setattr(lib, "read_image", forbid_full_read)
    result = await mod.send_chunked(bot, event, [], sticker=item, persona="cat")
    assert result.sticker_result.status == "sent"
    assert bot.send_group_msg.await_args.kwargs["message"] == {"image": "/napcat/temp/fixture.gif"}
    assert len(chunks) > 1


@pytest.mark.asyncio
async def test_unconfirmed_chunk_upload_never_sends_image(sender, lib, monkeypatch):
    from plugins import sticker_transport as transport
    mod, bot, event, _, _ = sender
    item, _ = lib.add("123", picture(animated=True), ["猫"])
    monkeypatch.setattr(transport, "INLINE_BYTES", 1)
    bot.call_api.return_value = {"status": "unexpected"}
    result = await mod.send_chunked(bot, event, [], sticker=item, persona="cat")
    assert result.sticker_result.status == "failed"
    bot.send_group_msg.assert_not_awaited()
    assert lib.get("123", item["id"]) is not None


@pytest.mark.asyncio
async def test_concurrent_text_image_pairs_do_not_interleave(sender, lib):
    mod, bot, event, _, _ = sender
    item, _ = lib.add("123", picture(), ["猫"])
    order = []
    async def send(**kwargs):
        order.append("image" if isinstance(kwargs["message"], dict) else kwargs["message"])
        await asyncio.sleep(0)
        return {"message_id": len(order)}
    bot.send_group_msg.side_effect = send
    await asyncio.gather(mod.send_chunked(bot, event, ["a"], reply_first=False, sticker=item, persona="cat"),
                         mod.send_chunked(bot, event, ["b"], reply_first=False, sticker=item, persona="cat"))
    assert order in (["a", "image", "b", "image"], ["b", "image", "a", "image"])
