"""按需图片描述、跨轮复用、原子替换及真实群 handler 的集成测试。"""

import copy
import importlib.util
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx
from PIL import Image

sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))

from plugins import image_context as images, message_store as store
from plugins.history_io import append_jsonl_message, clear_jsonl_history, load_jsonl_history, rewrite_compacted_history
from plugins.message_parser import ImageRef, MessageResolver, image_marker, resolve_quoted_message

ROOT = Path(__file__).resolve().parents[1]
IID = "img_" + "a" * 20


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "chatlog.db")


def note(iid=IID, value="ABC-731"):
    return {"image_id": iid, "description": "白底编号截图", "visible_text": value, "uncertain": "底部小字模糊"}


def test_decode_validates_identity_types_and_limits():
    good = note()
    good["description"] = "画" * 1000
    good["visible_text"] = "字" * 5000
    raw = json.dumps({"reply": "正常回复", "image_notes": [
        {**note(), "image_id": ["bad"]}, note("other"), {**note(), "visible_text": []}, good, note(),
    ]})
    reply, notes = images.decode_visual_response(raw, [ImageRef(IID, "https://x/image")])
    assert reply == "正常回复" and len(notes) == 1
    assert len(notes[0]["description"]) <= 300 and len(notes[0]["visible_text"]) <= 1000
    assert "已截断" in notes[0]["visible_text"]


@pytest.mark.parametrize("raw", ['{"reply":', "", "[]", "null", '{"reply":42,"image_notes":{}}'])
def test_invalid_json_is_not_forwarded_as_a_reply(raw):
    assert images.decode_visual_response(raw, [ImageRef(IID, "https://x/image")]) == ("", [])


@pytest.mark.parametrize("raw,expected", [
    ("图片上写着 ABC-731，是一只兔子。", "图片上写着 ABC-731，是一只兔子。"),
    ('{"reply":"第一行\n第二行","image_notes":[]}', "第一行\n第二行"),
    ('{"reply":"已经看到了兔子","image_notes":[', "已经看到了兔子"),
    ('```json\n{"reply":"兔子","image_notes":[]}\n```', "兔子"),
    ('{"reply":"兔子","image_notes":[]}\n[[sticker:兔子]]', "兔子\n[[sticker:兔子]]"),
    ('```json\n{"reply":"兔子","image_notes":[]}\n```\n[[sticker:兔子]]', "兔子\n[[sticker:兔子]]"),
])
def test_visual_reply_format_deviations_keep_usable_answer(raw, expected):
    assert images.decode_visual_response(raw, [ImageRef(IID, "https://x/image")]) == (expected, [])


def test_atomic_replacement_preserves_new_messages_and_other_images(tmp_path):
    path = tmp_path / "history.jsonl"
    other = image_marker("img_" + "b" * 20)
    snapshot = [{"role": "user", "content": "Alice:文字" + image_marker(IID) + other}]
    append_jsonl_message(path, snapshot[0])
    append_jsonl_message(path, {"role": "user", "content": "期间新消息"})
    images.apply_image_notes("123", "cat", [note()], session_path=path)
    loaded = load_jsonl_history(path)
    assert "ABC-731" in loaded[0]["content"] and other in loaded[0]["content"]
    assert "Alice:文字" in loaded[0]["content"]
    assert loaded[-1]["content"] == "期间新消息"
    # 旧 compact 快照必须放弃覆盖已更新的图片描述。
    assert rewrite_compacted_history(path, snapshot, [{"role": "assistant", "content": "旧摘要"}]) is None
    assert all(set(msg) == {"role", "content"} for msg in loaded)


def test_replacement_operates_on_latest_compacted_history(tmp_path):
    path = tmp_path / "history.jsonl"
    snapshot = [{"role": "user", "content": "旧聊天"}, {"role": "user", "content": image_marker(IID)}]
    for msg in snapshot:
        append_jsonl_message(path, msg)
    rewrite_compacted_history(path, snapshot, [{"role": "assistant", "content": "摘要"}, snapshot[-1]])
    images.apply_image_notes("123", "cat", [note()], session_path=path)
    loaded = load_jsonl_history(path)
    assert loaded[0]["content"] == "摘要" and "ABC-731" in loaded[-1]["content"]


def test_reset_is_not_undone_and_personas_are_not_modified(tmp_path):
    path, other = tmp_path / "cat.jsonl", tmp_path / "other.jsonl"
    for target in (path, other):
        append_jsonl_message(target, {"role": "user", "content": image_marker(IID)})
    clear_jsonl_history(path)
    images.apply_image_notes("123", "cat", [note()], session_path=path)
    assert not path.exists()
    assert load_jsonl_history(other)[0]["content"] == image_marker(IID)
    assert images.cached_note("456", IID) is None


async def test_same_image_reuses_note_even_if_signed_url_changes(tmp_path):
    bot = MagicMock()
    raw = [{"type": "image", "data": {"file": "stable-image", "url": "https://x/img?token=old"}}]
    first = await MessageResolver(bot, "123").resolve(raw, origin="qq:77")
    iid = first.images[0].image_id
    images.apply_image_notes("123", "cat", [note(iid)], session_path=tmp_path / "absent.jsonl")
    raw[0]["data"]["url"] = "https://x/img?token=new"
    second = await MessageResolver(bot, "123").resolve(raw, origin="qq:77")
    assert second.images[0].image_id == iid
    assert "ABC-731" in second.text and "尚未识别" not in second.text
    assert store.get_context("123", f"image:{iid}")["url"].endswith("new")
    assert not list(tmp_path.rglob("*.png")) and not list(tmp_path.rglob("*.jpg"))


async def test_explicit_refresh_fetches_fresh_qq_message():
    reply = types.SimpleNamespace(message=[{"type": "text", "data": {"text": "旧内容"}}], sender={})
    bot = MagicMock(get_msg=AsyncMock(return_value={"sender": {"nickname": "A"}, "message": "新内容"}))
    value = await resolve_quoted_message(bot, "123", 77, reply=reply, refresh=True)
    assert value.text == "新内容"
    bot.get_msg.assert_awaited_once_with(message_id=77)


async def test_image_identity_survives_nonebot_at_segment_removal():
    bot = MagicMock(get_group_member_info=AsyncMock(return_value={"nickname": "Bot"}))
    pic = {"type": "image", "data": {"file": "stable", "url": "https://x/image"}}
    raw = [{"type": "at", "data": {"qq": "10000"}}, pic]
    original = await MessageResolver(bot, "123", "10000").resolve(raw, origin="qq:77")
    processed = await MessageResolver(bot, "123", "10000").resolve([pic], origin="qq:77")
    assert original.images[0].image_id == processed.images[0].image_id


def test_explicit_reread_updates_previous_description(tmp_path):
    path = tmp_path / "history.jsonl"
    append_jsonl_message(path, {"role": "user", "content": image_marker(IID)})
    images.apply_image_notes("123", "cat", [note()], session_path=path)
    images.apply_image_notes("123", "cat", [note(value="DEF-999")], session_path=path)
    content = load_jsonl_history(path)[0]["content"]
    assert "DEF-999" in content and "ABC-731" not in content


def test_original_chatlog_is_updated_only_in_same_group(tmp_path):
    import sqlite3
    with sqlite3.connect(store.DB_PATH) as conn:
        conn.execute("CREATE TABLE messages(group_id TEXT, text TEXT)")
        conn.executemany("INSERT INTO messages VALUES (?,?)", [(gid, image_marker(IID)) for gid in ("123", "456")])
    images.apply_image_notes("123", "cat", [note()], session_path=tmp_path / "absent.jsonl")
    with sqlite3.connect(store.DB_PATH) as conn:
        rows = dict(conn.execute("SELECT group_id,text FROM messages"))
    assert "ABC-731" in rows["123"] and rows["456"] == image_marker(IID)


@pytest.fixture
def group_handler(tmp_path, monkeypatch):
    """加载真实 handler，仅替换外部服务和 NoneBot 生命周期。"""
    bot = types.SimpleNamespace(get_msg=AsyncMock())
    matcher = types.SimpleNamespace(handle=lambda: lambda fn: fn, finish=AsyncMock())
    sent = AsyncMock(return_value=types.SimpleNamespace(status="sent", message_ids=(), delivered_text=""))
    llm = AsyncMock()
    def module(name, **attributes):
        value = types.ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)
        return value
    module("nonebot", on_message=lambda **kw: matcher, get_bot=lambda: bot)
    module("nonebot.adapters")
    module("nonebot.adapters.onebot")
    module("nonebot.adapters.onebot.v11", GroupMessageEvent=type("GroupMessageEvent", (), {}), MessageSegment=MagicMock())
    for package in ("group", "persona", "chat", "mcp", "skill", "local_tools"):
        if f"plugins.{package}" not in sys.modules:
            module(f"plugins.{package}", __path__=[str(ROOT / "plugins" / package)])
    module("nonebot.exception", FinishedException=type("FinishedException", (Exception,), {}))
    module("plugins.chunker", chunk_text=lambda text: [text], send_chunked=sent)
    module("plugins.runtime_context", build_runtime_context=lambda **kw: "静态运行环境", build_time_context=lambda *a: "动态时间")
    path = lambda gid, persona: tmp_path / gid / f"{persona}.jsonl"
    manager = module("plugins.persona.manager", get_active_persona=lambda gid: "cat",
                     load_persona_prompt=lambda *a: "人格提示", load_history=lambda gid, p: load_jsonl_history(path(gid, p)),
                     append_message=lambda gid, msg, p: append_jsonl_message(path(gid, p), msg),
                     get_group_config=lambda gid: {}, get_listen_all=lambda gid: False,
                     get_group_proactive=lambda gid: False, load_group_memory=lambda gid: "",
                     group_memory_path=lambda gid: tmp_path / gid / "MEMORY.md", _session_path=path)
    module("plugins.llm", API_KEY="test-key", BASE_URL="https://test.invalid", MODEL="deepseek-flash", SUPPORTS_VISION=True, call_llm=llm)
    module("plugins.proactive", reset_idle_timer=lambda *a: None)
    module("plugins.mcp.manager", get_openai_tools=lambda: [], call_tool=AsyncMock(), MAX_TOOL_ROUNDS=2)
    module("plugins.skill.manager", build_catalog_prompt=lambda **kw: "", get_openai_tools=lambda **kw: [], handle_tool_call=lambda *a, **kw: None)
    module("plugins.local_tools.manager", get_openai_tools=lambda **kw: [], handle_tool_call=AsyncMock(return_value="正位 太阳"), _registry={})
    module("plugins.tool_log", log_tool_call=lambda *a, **kw: None)
    module("plugins.chat.compaction", should_compact=lambda h: False, compact_history=AsyncMock())
    module("plugins.group.chatter", note_bot_reply=lambda gid: None)
    module("plugins.group.utils", in_whitelist=lambda gid: True, is_at_bot=lambda e: True,
           extract_text=AsyncMock(), is_group_event=lambda e: True,
           get_reply_id=lambda e: e.reply_id, trim_history=lambda h, p: h, get_session_lock=MagicMock())
    name = "plugins.group.handler_image_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "plugins/group/handler.py")
    handler = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, handler)
    spec.loader.exec_module(handler)
    raw = [{"type": "image", "data": {"file": "stable", "url": "https://x/image"}}]
    event = types.SimpleNamespace(self_id=10000, user_id=5, message_id=88, reply_id=77,
                                 message=[{"type": "text", "data": {"text": "看图"}}],
                                 reply=types.SimpleNamespace(message=raw, sender={"nickname": "Alice"}),
                                 sender=types.SimpleNamespace(nickname="Bob"))
    return handler, event, llm, sent, manager, path


async def test_failed_delivery_does_not_claim_generated_answer(group_handler):
    handler, event, llm, sent, manager, path = group_handler
    event.reply_id = None
    llm.return_value = {"choices": [{"message": {"content": "UNDELIVERED_REPLY"}}]}
    sent.return_value = types.SimpleNamespace(status="failed", message_ids=(), delivered_text="")
    await handler._handle_group_chat(event, "123", "cat", "hello")
    history = load_jsonl_history(path("123", "cat"))
    assert "UNDELIVERED_REPLY" not in str(history)
    assert "发送失败" in str(history)


async def test_real_handler_same_response_then_followup_without_image(group_handler):
    handler, event, llm, sent, manager, path = group_handler
    calls = []
    async def answer(messages, **kwargs):
        snapshot = copy.deepcopy(messages)
        calls.append((snapshot, kwargs))
        if len(calls) == 1:
            user = next(m["content"] for m in messages if isinstance(m.get("content"), list))
            iid = next(piece["text"].split("：", 1)[1] for piece in user if piece.get("type") == "text" and piece["text"].startswith("图片标识："))
            output = json.dumps({"reply": "图中编号是 ABC-731", "image_notes": [note(iid)]})
        else:
            assert "ABC-731" in str(messages)
            assert not any(isinstance(m.get("content"), list) for m in messages)
            assert "response_format" not in kwargs
            output = "刚才图片的编号是 ABC-731"
        return {"choices": [{"message": {"content": output}}]}
    llm.side_effect = answer
    await handler._handle_group_chat(event, "123", "cat", "这张图写了什么？")
    saved = load_jsonl_history(path("123", "cat"))
    assert "ABC-731" in saved[0]["content"] and "尚未识别" not in saved[0]["content"]
    assert all("https://x/image" not in msg["content"] for msg in saved)
    assert calls[0][1]["response_format"] == {"type": "json_object"}
    assert "动态时间" not in calls[0][0][0]["content"]
    event.reply_id, event.reply = None, None
    await handler._handle_group_chat(event, "123", "cat", "刚才的编号是什么？")
    assert llm.await_count == 2 and sent.await_count == 2
    assert sent.await_args_list[0].args[2] == ["图中编号是 ABC-731"]


async def test_real_handler_tool_round_does_not_send_raw_json(group_handler):
    handler, event, llm, sent, manager, path = group_handler
    count = 0
    async def answer(messages, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call1", "type": "function", "function": {"name": "local__tarot", "arguments": "{}"}}
            ]}}]}
        assert kwargs["response_format"] == {"type": "json_object"}
        assert any(m["role"] == "tool" for m in messages)
        user = next(m["content"] for m in messages if isinstance(m.get("content"), list))
        iid = next(p["text"].split("：", 1)[1] for p in user if p.get("type") == "text" and p["text"].startswith("图片标识："))
        return {"choices": [{"message": {"content": json.dumps({"reply": "看过图片，也确实抽了正位太阳", "image_notes": [note(iid)]})}}]}
    llm.side_effect = answer
    await handler._handle_group_chat(event, "123", "cat", "看图然后抽牌")
    assert sent.await_count == 1, handler.logger.error.call_args
    assert "image_notes" not in sent.await_args.args[2][0]
    saved = load_jsonl_history(path("123", "cat"))
    assert any("【执行记录" in m["content"] and "正位 太阳" in m["content"] for m in saved)
    assert "ABC-731" in saved[0]["content"]


@pytest.mark.parametrize("user_request,cached_reference,reply_format", [
    ("存一下这个表情包，标签是兔子", False, "json"),
    ("把这张图收入图库，叫兔子", False, "json"),
    ("帮我留着这张，标个兔子", False, "json"),
    ("存一下这个表情包，标签是兔子", True, "text"),
    ("存一下这个表情包，标签是兔子", False, "text"),
    ("存一下这个表情包，标签是兔子", False, "broken"),
    ("存一下这个表情包，标签是兔子", False, "empty"),
    ("存一下这个表情包，标签是兔子", False, "failure_reply"),
    ("存一下这个表情包，标签是兔子", False, "request_failed"),
    ("存一下这个表情包，标签是兔子", False, "budget_failed"),
    ("存一下这个表情包，标签是兔子", False, "outside_marker"),
])
async def test_real_handler_collects_quote_through_tool_without_keyword_gate(group_handler, monkeypatch, tmp_path, user_request, cached_reference, reply_format):
    handler, event, llm, sent, _, path = group_handler
    def load_module(name, relative):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        value = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, value)
        spec.loader.exec_module(value)
        return value
    dispatcher = load_module("plugins.local_tools.manager", "plugins/local_tools/manager.py")
    tools = load_module("plugins.local_tools.stickers_quote_test", "plugins/local_tools/stickers.py")
    library = tools.library
    monkeypatch.setattr(library, "ROOT", tmp_path / "stickers")
    monkeypatch.setattr(handler, "local_openai_tools", dispatcher.get_openai_tools)
    monkeypatch.setattr(handler, "local_handle_tool_call", dispatcher.handle_tool_call)
    if reply_format == "budget_failed":
        monkeypatch.setattr(handler, "MAX_TOOL_ROUNDS", 1)
    content = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(content, format="PNG")
    client_class = httpx.AsyncClient
    downloads = []
    def response(request):
        downloads.append(request.url)
        return httpx.Response(200, content=content.getvalue())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(response), **kw))
    bot = sys.modules["nonebot"].get_bot()
    if cached_reference:
        store.put_context("123", "qq:77", {"text": "原图文字笔记", "images": [], "author": "Alice"}, kind="outgoing")
        bot.get_msg.return_value = {"message": event.reply.message, "sender": {"nickname": "Alice"}}
    count = 0
    async def answer(messages, **kwargs):
        nonlocal count
        count += 1
        assert user_request in str(messages)
        if count == 1:
            assert any(isinstance(m.get("content"), list) for m in messages) is not cached_reference
            assert any(t["function"]["name"] == "local__save_quoted_sticker" for t in kwargs["tools"])
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "save1", "type": "function", "function": {"name": "local__save_quoted_sticker", "arguments": '{"tags":["兔子"]}'}}
            ]}}]}
        result = next(m["content"] for m in messages if m["role"] == "tool")
        assert "已收藏" in result, result
        final = "存好了，标签是兔子"
        if reply_format == "json":
            final = json.dumps({"reply": final, "image_notes": []})
        elif reply_format == "broken":
            final = '{"reply":'
        elif reply_format == "empty":
            final = '{"image_notes": []}'
        elif reply_format == "failure_reply":
            final = json.dumps({"reply": images.FAILURE_REPLY, "image_notes": []})
        elif reply_format in ("request_failed", "budget_failed"):
            raise httpx.ReadTimeout("final response timed out")
        elif reply_format == "outside_marker":
            final = json.dumps({"reply": final, "image_notes": []}) + "\n[[sticker:兔子]]"
        return {"choices": [{"message": {"content": final}}]}
    llm.side_effect = answer
    await handler._handle_group_chat(event, "123", "cat", user_request)
    assert llm.await_count == 2 and sent.await_count == 1
    if reply_format in ("json", "text", "outside_marker") or cached_reference:
        assert sent.await_args.args[2] == ["存好了，标签是兔子"]
    else:
        reply, = sent.await_args.args[2]
        assert reply.startswith("已收藏 #") and "兔子" in reply
        assert images.FAILURE_REPLY not in reply and "image_notes" not in reply
    item, = library.items("123")
    assert item["tags"] == ["兔子"] and item["uploader"] == "5"
    assert library.image_path("123", item).exists()
    assert library.items("456") == []
    assert bot.get_msg.await_count == int(cached_reference)
    assert len(downloads) == 1
    assert any("已收藏" in m["content"] and "执行记录" in m["content"] for m in load_jsonl_history(path("123", "cat")))


@pytest.mark.parametrize("tool_name,result", [
    ("save_quoted_sticker", "[错误] 收藏失败：图片格式不支持"),
    ("save_quoted_sticker", "[错误] 批量收藏未全部完成：新收藏 2 张，失败 1 张，未处理 0 张"),
    ("delete_quoted_sticker", "已删除这张表情包及其全部标签关联"),
    ("update_quoted_sticker_tags", "已修改表情 #1234 的标签：兔子（原收藏和图片保留）"),
])
async def test_invalid_visual_reply_preserves_sticker_failures_and_partial_results(group_handler, tool_name, result):
    handler, event, llm, sent, _, path = group_handler
    handler.local_handle_tool_call.return_value = result
    llm.side_effect = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "action1", "type": "function", "function": {"name": f"local__{tool_name}", "arguments": '{}'}}
        ]}}]},
        {"choices": [{"message": {"content": '{"reply":'}}]},
    ]
    await handler._handle_group_chat(event, "123", "cat", "处理这张表情包")
    assert sent.await_count == 1 and sent.await_args.args[2] == [result]
    assert handler.local_handle_tool_call.await_count == 1
    saved = load_jsonl_history(path("123", "cat"))
    assert saved[-1]["content"] == result
    assert images.FAILURE_REPLY not in str(saved)


async def test_real_handler_invalid_json_keeps_placeholder(group_handler):
    handler, event, llm, sent, manager, path = group_handler
    llm.return_value = {"choices": [{"message": {"content": '{"reply":'}}]}
    await handler._handle_group_chat(event, "123", "cat", "看图")
    assert sent.await_args.args[2] == [images.FAILURE_REPLY]
    assert "尚未识别" in load_jsonl_history(path("123", "cat"))[0]["content"]


async def test_plain_visual_answer_is_sent_without_false_failure_or_fake_note(group_handler):
    handler, event, llm, sent, _, path = group_handler
    llm.return_value = {"choices": [{"message": {"content": "图里是一只兔子，上面写着开心。"}}]}
    await handler._handle_group_chat(event, "123", "cat", "图里有什么？")
    assert sent.await_args.args[2] == ["图里是一只兔子，上面写着开心。"]
    saved = load_jsonl_history(path("123", "cat"))
    assert "尚未识别" in saved[0]["content"]
    assert images.FAILURE_REPLY not in str(saved)


async def test_empty_plain_reply_without_images_does_not_report_vision_failure(group_handler):
    handler, event, llm, sent, _, path = group_handler
    event.reply_id, event.reply = None, None
    llm.return_value = {"choices": [{"message": {"content": ""}}]}
    await handler._handle_group_chat(event, "123", "cat", "你好")
    sent.assert_not_awaited()
    assert images.FAILURE_REPLY not in str(load_jsonl_history(path("123", "cat")))


async def test_recent_sticker_tool_gets_current_message_boundary_without_a_quote(group_handler):
    handler, event, llm, sent, _, path = group_handler
    event.reply_id, event.reply = None, None
    result = "最近图片收藏完成：新收藏 8 张，标签：兔子、开心"
    handler.local_handle_tool_call.return_value = result
    llm.side_effect = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "recent1", "type": "function", "function": {"name": "local__save_recent_stickers", "arguments": '{}'}}
        ]}}]},
        {"choices": [{"message": {"content": ""}}]},
    ]
    await handler._handle_group_chat(event, "123", "cat", "把我刚才那一堆图片都存了，自动打标签")
    context = handler.local_handle_tool_call.await_args.kwargs["context"]
    assert context["_sticker_request_message_id"] == 88 and context["_user_id"] == "5"
    assert context["_quoted_message_id"] is None and context["_sticker_user_turn"] is True
    assert sent.await_args.args[2] == [result]
    assert load_jsonl_history(path("123", "cat"))[-1]["content"] == result


async def test_real_handler_strips_sticker_marker_after_visual_json(group_handler, monkeypatch):
    handler, event, llm, sent, manager, path = group_handler
    runtime = types.ModuleType("plugins.sticker_reply")
    runtime.prompt = lambda gid: "本群表情标签：开心（1）"
    runtime.prepare = lambda gid, text, **kw: (text.split("\n[[sticker:", 1)[0], {"id": "test"})
    runtime.split_marker = lambda text: (text, None)
    monkeypatch.setitem(sys.modules, "plugins.sticker_reply", runtime)
    llm.return_value = {"choices": [{"message": {"content": json.dumps({
        "reply": "真开心\n[[sticker:开心]]", "image_notes": [],
    })}}]}
    await handler._handle_group_chat(event, "123", "cat", "这张图写了什么？")
    assert sent.await_args.args[2] == ["真开心"]
    assert sent.await_args.kwargs == {"sticker": {"id": "test"}, "persona": "cat"}
    assert "本群表情标签" in llm.await_args.args[0][-1]["content"]
    assert "[[sticker:" not in str(load_jsonl_history(path("123", "cat")))


async def test_real_handler_repeated_quote_does_not_resend_image(group_handler):
    handler, event, llm, sent, manager, path = group_handler
    quoted = await MessageResolver(MagicMock(), "123").resolve(event.reply.message, origin="qq:77")
    iid = quoted.images[0].image_id
    images.apply_image_notes("123", "cat", [note(iid)], session_path=path("123", "cat"))
    async def answer(messages, **kwargs):
        assert "response_format" not in kwargs
        assert not any(isinstance(m.get("content"), list) for m in messages)
        assert "ABC-731" in str(messages)
        return {"choices": [{"message": {"content": "已有识别结果是 ABC-731"}}]}
    llm.side_effect = answer
    await handler._handle_group_chat(event, "123", "cat", "还是这张图")
    assert llm.await_count == 1


async def test_multimodal_input_is_not_lost_when_listen_appends_a_message(group_handler, monkeypatch):
    handler, event, llm, sent, manager, path = group_handler
    original = manager.append_message
    def append_with_concurrent_message(gid, msg, persona):
        original(gid, msg, persona)
        if msg["role"] == "user":
            original(gid, {"role": "user", "content": "[另一个群友]: 并发新消息"}, persona)
    monkeypatch.setattr(handler, "append_message", append_with_concurrent_message)
    async def answer(messages, **kwargs):
        assert any(isinstance(m.get("content"), list) for m in messages)
        assert any(m.get("content") == "[另一个群友]: 并发新消息" for m in messages)
        return {"choices": [{"message": {"content": json.dumps({"reply": "看到图片了", "image_notes": []})}}]}
    llm.side_effect = answer
    await handler._handle_group_chat(event, "123", "cat", "看图")
    assert sent.await_args.args[2] == ["看到图片了"]
