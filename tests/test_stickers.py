"""真实图片、原子存储、配额、引用删除以及列表负载的行为测试。"""

import asyncio
import base64
import importlib.util
import io
import json
import os
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


def test_gif_over_limit_is_rejected_not_flattened(lib, monkeypatch):
    monkeypatch.setattr(lib, "MAX_IMAGE_BYTES", 1)
    with pytest.raises(lib.StickerError, match="GIF"):
        lib.add("123", picture(animated=True), ["猫"])


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


def test_permissions_come_from_current_request_only(lib):
    assert lib.request_permissions("把这张图片存成开心猫", 77)["_sticker_save_allowed"]
    assert lib.request_permissions("删除这张表情包", 77)["_sticker_delete_allowed"]
    assert not any(lib.request_permissions("不要删除这张表情包", 77).values())
    assert not any(lib.request_permissions("删除这张表情包", None).values())


@pytest.mark.asyncio
async def test_download_limit_stops_before_writing(lib, monkeypatch):
    client_class = httpx.AsyncClient
    monkeypatch.setattr(lib, "MAX_DOWNLOAD_BYTES", 10)
    monkeypatch.setattr(lib.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 11)), **kw))
    with pytest.raises(lib.StickerError, match="下载超过"):
        await lib.collect("123", "https://qq.example/image", ["猫"])
    assert not lib.ROOT.exists()


@pytest.fixture
def sticker_tools(lib, monkeypatch):
    manager = types.ModuleType("plugins.local_tools.manager")
    manager.register_tool = lambda **kw: lambda fn: fn
    monkeypatch.setitem(sys.modules, "plugins.local_tools.manager", manager)
    mod = load("plugins.local_tools.stickers_under_test", "plugins/local_tools/stickers.py")
    monkeypatch.setattr(mod, "library", lib)
    return mod


@pytest.mark.asyncio
async def test_tools_require_user_capability_and_single_quote(sticker_tools, lib):
    assert "[错误]" in await sticker_tools.save_quoted_sticker(["猫"], _context={"_chat_type": "group"})
    ctx = {"_chat_type": "group", "_target_id": "123", "_sticker_save_allowed": True,
           "_quoted_sticker_urls": ["https://a", "https://b"]}
    assert "一张" in await sticker_tools.save_quoted_sticker(["猫"], _context=ctx)
    assert lib.items("123") == []


@pytest.mark.asyncio
async def test_quoted_delete_uses_group_and_message_mapping(sticker_tools, lib, monkeypatch):
    item, _ = lib.add("123", picture(), ["猫"])
    message_store = types.ModuleType("plugins.message_store")
    message_store.get_context = lambda gid, key: {"kind": "sticker", "sticker_id": item["id"]} if gid == "123" and key == "qq:77" else None
    monkeypatch.setitem(sys.modules, "plugins.message_store", message_store)
    ctx = {"_chat_type": "group", "_target_id": "456", "_quoted_message_id": 77, "_sticker_delete_allowed": True}
    assert "无法唯一定位" in await sticker_tools.delete_quoted_sticker(_context=ctx)
    ctx["_target_id"] = "123"
    assert "已删除" in await sticker_tools.delete_quoted_sticker(_context=ctx)
    new, _ = lib.add("123", picture(), ["猫"])
    assert "已经删除" in await sticker_tools.delete_quoted_sticker(_context=ctx)
    assert lib.get("123", new["id"])


@pytest.fixture
def commands(lib, monkeypatch):
    matcher = types.SimpleNamespace(handle=lambda: lambda fn: fn, finish=AsyncMock())
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
    return mod


def test_list_repeats_multitag_images_and_preserves_gif(commands, lib, monkeypatch):
    gif = picture(animated=True)
    lib.add("123", gif, ["猫", "开心"])
    lib.add("123", picture(), ["猫"])
    monkeypatch.setattr(commands, "MAX_BATCH_IMAGES", 1)
    batches = list(commands.list_batches("123", 99, "Bot"))
    assert len(batches) == 3
    for batch in batches:
        assert len(json.dumps(batch).encode()) < commands.MAX_BATCH_BYTES
    raw_images = [base64.b64decode(node["data"]["content"][-1]["data"]["file"].split("//", 1)[1]) for batch in batches for node in batch]
    assert raw_images.count(gif) == 2


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
    assert await commands.send_list(bot, "123") == ("unknown", 0)
    assert bot.call_api.await_count == 1


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


def test_auto_toggle_cooldown_and_explicit_selection(runtime, lib, monkeypatch):
    item, _ = lib.add("123", picture(), ["猫"])
    assert lib.reserve_auto("123")
    assert runtime.prepare("123", "[[sticker:猫]]")[1] is None
    assert not lib.reserve_auto("123")
    monkeypatch.setattr(lib, "enabled", lambda gid: False)
    assert runtime.prepare("123", "[[sticker:猫]]")[1] is None
    assert runtime.prepare("123", "", requested_id=item["id"])[1]["id"] == item["id"]


@pytest.fixture
def sender(lib, tmp_path, monkeypatch):
    import plugins
    monkeypatch.setattr(plugins, "stickers", lib, raising=False)
    monkeypatch.setitem(sys.modules, "plugins.stickers", lib)
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
    ctx = {"_chat_type": "group", "_target_id": "123", "_quoted_message_id": 501, "_sticker_delete_allowed": True}
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
