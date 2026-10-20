"""跨重启的消息引用、转发解析边界及群间隔离。"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))

from plugins import message_store as store
from plugins.message_parser import MessageResolver, resolve_quoted_message, segments


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "chatlog.db")


def text(value):
    return {"type": "text", "data": {"text": value}}


def forward(fid, **data):
    return {"type": "forward", "data": {"id": fid, **data}}


def test_store_alias_reload_and_group_isolation():
    store.remember_sent_message("123", {"message_id": 7, "res_id": "abc"}, "原始回复", "Bot")
    assert store.get_context("123", "qq:7")["text"] == "原始回复"
    assert store.get_context("123", "forward:abc")["text"] == "原始回复"
    assert store.get_context("456", "qq:7") is None
    assert store.get_context("../123", "qq:7") is None


def test_expiration_and_quota_remove_aliases(monkeypatch):
    monkeypatch.setattr(store.time, "time", lambda: 1000)
    store.put_context("123", "old", {"text": "old"}, kind="forward", ttl=10, aliases=["alias"])
    monkeypatch.setattr(store.time, "time", lambda: 1011)
    assert store.get_context("123", "alias") is None
    store.cleanup_context()
    monkeypatch.setattr(store, "MAX_BYTES", 120)
    store.put_context("123", "new", {"text": "n" * 80}, kind="forward")
    store.put_context("123", "newer", {"text": "x" * 80}, kind="forward")
    assert store.get_context("123", "new") is None
    assert store.get_context("123", "newer") is not None


def test_cq_strings_preserve_escaped_text():
    result = segments("hi[CQ:image,url=https://x/a?x=1&#44;y=2]&#91;CQ:at,qq=1&#93;")
    assert result[1]["data"]["url"] == "https://x/a?x=1,y=2"
    assert result[2]["type"] == "text"
    assert result[2]["data"]["text"] == "[CQ:at,qq=1]"


@pytest.mark.parametrize("shape", ["messages", "message"])
async def test_forward_supports_both_wire_shapes(shape):
    node = ({"sender": {"nickname": "Alice", "user_id": 7}, "message": [text("内容")]} if shape == "messages"
            else {"type": "node", "data": {"nickname": "Alice", "user_id": 7, "content": [text("内容")]}})
    bot = MagicMock(get_forward_msg=AsyncMock(return_value={shape: [node]}))
    value = await MessageResolver(bot, "123").resolve([forward("abc")])
    assert "Alice(7)" in value.text and "内容" in value.text
    await MessageResolver(bot, "123").resolve([forward("abc")])
    bot.get_forward_msg.assert_awaited_once_with(id="abc")


async def test_inline_nested_forward_and_image():
    bot = MagicMock(get_forward_msg=AsyncMock())
    nested = forward("inner", content=[{"sender": {"nickname": "B"}, "message": [
        text("内层"), {"type": "image", "data": {"url": "https://x/image.png"}}]}])
    value = await MessageResolver(bot, "123").resolve([forward("outer", content=[
        {"sender": {"nickname": "A"}, "message": [nested]}])])
    assert "内层" in value.text and "转发中的作者 A" in value.text
    assert len(value.images) == 1 and value.images[0].url == "https://x/image.png"
    bot.get_forward_msg.assert_not_called()


async def test_forward_cycle_node_limit_and_preview():
    bot = MagicMock(get_forward_msg=AsyncMock(return_value={"messages": [
        {"sender": {}, "message": [forward("loop")]}]}))
    cyclic = await MessageResolver(bot, "123").resolve([forward("loop")])
    assert "循环引用" in cyclic.text
    nodes = [{"sender": {"nickname": str(i)}, "message": [text("x" * 100)]} for i in range(105)]
    value = await MessageResolver(bot, "123").resolve([forward("long", content=nodes)])
    assert len(value.text) < 6500 and "read_message_content" in value.text
    saved = store.get_context("123", "forward:long")["text"]
    assert "作者 99" in saved and "作者 100" not in saved


async def test_own_message_reference_works_without_qq_api():
    store.remember_sent_message("123", {"message_id": 8}, "Bot 原文", "Bot")
    bot = MagicMock(get_msg=AsyncMock(side_effect=RuntimeError("offline")))
    value = await resolve_quoted_message(bot, "123", 8)
    assert value.author == "Bot" and value.text == "Bot 原文"
    bot.get_msg.assert_not_awaited()


async def test_missing_reference_is_visible_not_silent():
    bot = MagicMock(get_msg=AsyncMock(side_effect=RuntimeError("expired")))
    value = await resolve_quoted_message(bot, "123", 8)
    assert "读取失败" in value.text


async def test_pagination_cannot_access_another_group():
    from plugins.local_tools.tools import read_message_content
    store.put_context("123", "forward:abc", {"text": "0123456789"}, kind="forward")
    result = await read_message_content("forward:abc", 3, 4, _context={"_chat_type": "group", "_target_id": "123"})
    assert "3456" in result and "offset=7" in result
    other = await read_message_content("forward:abc", _context={"_chat_type": "group", "_target_id": "456"})
    assert other.startswith("[错误]")
