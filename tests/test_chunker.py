"""
tests/test_chunker.py
──────────────────────
测试 chunker 模块的文本拆分逻辑:
  - chunk_text: 短文本不拆、按换行拆、超长行硬切
"""

import sys
import os

# 将项目根目录加入 sys.path，使 plugins 可作为顶层包导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from plugins.chunker import chunk_text, CHUNK_THRESHOLD, MAX_CHUNK_CHARS


# ──────────────────── 短文本不拆 ────────────────────

class TestChunkTextShort:
    """短文本应整条返回，不拆分"""

    def test_empty_string(self):
        assert chunk_text("") == []

    def test_whitespace_only(self):
        assert chunk_text("   \n  ") == []

    def test_short_text(self):
        text = "你好呀"
        result = chunk_text(text)
        assert result == [text]

    def test_exactly_at_threshold(self):
        text = "a" * CHUNK_THRESHOLD
        result = chunk_text(text)
        assert result == [text]

    def test_one_char_below_threshold(self):
        text = "x" * (CHUNK_THRESHOLD - 1)
        result = chunk_text(text)
        assert result == [text]


# ──────────────────── 按换行拆分 ────────────────────

class TestChunkTextNewline:
    """超过阈值的文本应按换行符拆分成多条"""

    def test_two_lines(self):
        line1 = "这是第一行消息" * 5
        line2 = "这是第二行消息" * 5
        text = f"{line1}\n{line2}"
        assert len(text) > CHUNK_THRESHOLD  # 确保超过阈值
        result = chunk_text(text)
        assert result == [line1, line2]

    def test_multiple_lines(self):
        lines = [f"第{i}行消息内容" for i in range(10)]
        text = "\n".join(lines)
        assert len(text) > CHUNK_THRESHOLD
        result = chunk_text(text)
        assert result == lines

    def test_blank_lines_are_skipped(self):
        """空行应被忽略"""
        text = "a" * 40 + "\n\n\n" + "b" * 40
        assert len(text) > CHUNK_THRESHOLD
        result = chunk_text(text)
        assert result == ["a" * 40, "b" * 40]

    def test_lines_with_only_whitespace_are_skipped(self):
        """仅含空白的行应被忽略"""
        text = "a" * 40 + "\n   \n" + "b" * 40
        assert len(text) > CHUNK_THRESHOLD
        result = chunk_text(text)
        assert result == ["a" * 40, "b" * 40]

    def test_leading_trailing_whitespace_stripped(self):
        """每行的首尾空白应被去除"""
        text = "  前面有空格  \n  后面也有  " + "x" * 50
        result = chunk_text(text)
        assert all(line == line.strip() for line in result)


# ──────────────────── 超长行硬切 ────────────────────

class TestChunkTextLongLine:
    """超过 MAX_CHUNK_CHARS 的单行应被硬切"""

    def test_single_long_line(self):
        text = "字" * (MAX_CHUNK_CHARS + 50)
        result = chunk_text(text)
        assert len(result) == 2
        assert result[0] == "字" * MAX_CHUNK_CHARS
        assert result[1] == "字" * 50

    def test_very_long_line_multiple_chunks(self):
        text = "x" * (MAX_CHUNK_CHARS * 3 + 10)
        result = chunk_text(text)
        assert len(result) == 4
        for chunk in result[:-1]:
            assert len(chunk) == MAX_CHUNK_CHARS
        assert len(result[-1]) == 10

    def test_mixed_normal_and_long_lines(self):
        """正常行和超长行混合"""
        short_line = "正常行"
        long_line = "长" * (MAX_CHUNK_CHARS + 30)
        text = f"{short_line}\n{long_line}"
        result = chunk_text(text)
        assert result[0] == short_line
        assert result[1] == "长" * MAX_CHUNK_CHARS
        assert result[2] == "长" * 30


# ──────────────────── 边界情况 ────────────────────

class TestChunkTextEdge:
    """边界情况"""

    def test_single_newline_only(self):
        assert chunk_text("\n") == []

    def test_exactly_max_chars(self):
        """恰好 MAX_CHUNK_CHARS 长的行不应被切"""
        text = "y" * MAX_CHUNK_CHARS + "\n" + "z" * 10
        result = chunk_text(text)
        assert result[0] == "y" * MAX_CHUNK_CHARS

    def test_preserves_content_integrity(self):
        """验证拆分后拼起来等于原文（去空行后）"""
        lines = ["第一段落内容不短" * 3, "第二段落" * 5, "第三段" * 10]
        text = "\n".join(lines)
        result = chunk_text(text)
        reassembled = "\n".join(result)
        # 去除空行后内容应一致
        original_stripped = "\n".join(
            line.strip() for line in text.split("\n") if line.strip()
        )
        assert reassembled == original_stripped


# ══════════════════════════════════════════════════════
# issue #5：去掉字数限制，一个换行 = 一条消息
# ══════════════════════════════════════════════════════

class TestNoCharLimit:

    def test_long_line_not_split_below_safety_cap(self):
        """单段 1200 字（超过旧的 200 字限制）不应被切断"""
        result = chunk_text("字" * 1200)
        assert result == ["字" * 1200]

    def test_multiple_long_lines_keep_one_chunk_each(self):
        """每段都在安全上限内时，一条消息 = 一个换行段"""
        text = ("甲" * 800) + "\n" + ("乙" * 900) + "\n" + ("丙" * 700)
        result = chunk_text(text)
        assert len(result) == 3
        assert result[0] == "甲" * 800
        assert result[1] == "乙" * 900
        assert result[2] == "丙" * 700

    def test_realistic_reply_not_shredded(self):
        """模拟一段真实的多段回复，行数应与换行数一致"""
        reply = (
            "关于这个问题，我觉得可以从几个角度来看。\n"
            "首先是成本方面，现在的方案确实开销比较大，主要还是因为每一轮都要重新拉取全量数据，"
            "这部分如果加个缓存应该能省下来不少。\n"
            "其次是稳定性，之前遇到过的几次超时都集中在高峰期。\n"
            "所以我建议先做缓存，观察一段时间再决定要不要上更重的方案。"
        )
        result = chunk_text(reply)
        assert len(result) == 4

    def test_safety_cap_still_applies_to_absurd_line(self):
        """安全兜底：单段超过 MAX_CHUNK_CHARS（QQ 单条上限）仍要切，防发送失败"""
        result = chunk_text("长" * (MAX_CHUNK_CHARS * 2 + 50))
        assert len(result) == 3
        assert all(len(c) <= MAX_CHUNK_CHARS for c in result)


# ══════════════════════════════════════════════════════
# 合并转发（分段数达到 FORWARD_THRESHOLD 时）
# ══════════════════════════════════════════════════════

import asyncio as _asyncio
import types as _types
from unittest.mock import AsyncMock, MagicMock

import pytest as _pytest

from plugins.chunker import (
    FORWARD_THRESHOLD,
    build_forward_nodes,
    send_forward,
    send_chunked,
    send_chunked_raw,
    reset_self_nickname_cache,
)


def _fake_bot(nickname="测试Bot", self_id="10001", fail_forward=False):
    bot = MagicMock()
    bot.self_id = self_id
    bot.calls = []

    async def _call_api(api, **kwargs):
        bot.calls.append((api, kwargs))
        if api == "get_login_info":
            return {"user_id": int(self_id), "nickname": nickname}
        if api in ("send_group_forward_msg", "send_private_forward_msg"):
            if fail_forward:
                raise RuntimeError("实现端不支持合并转发")
            return {"message_id": 1}
        return {}

    bot.call_api = AsyncMock(side_effect=_call_api)
    bot.send_group_msg = AsyncMock()
    bot.send_private_msg = AsyncMock()
    return bot


@_pytest.fixture(autouse=True)
def _reset_cache(tmp_path, monkeypatch):
    from plugins import message_store
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "chatlog.db")
    reset_self_nickname_cache()
    yield
    reset_self_nickname_cache()


class TestBuildForwardNodes:

    def test_node_structure(self):
        """节点必须是标准格式：type=node + user_id/nickname/content"""
        nodes = build_forward_nodes(["甲", "乙"], user_id="12345", nickname="阿雪")
        assert len(nodes) == 2
        assert nodes[0] == {
            "type": "node",
            "data": {"user_id": 12345, "nickname": "阿雪", "content": "甲"},
        }
        assert nodes[1]["data"]["content"] == "乙"

    def test_user_id_is_int(self):
        """NapCat 会 .toString()，但标准类型应是数字"""
        nodes = build_forward_nodes(["内容"], user_id="999", nickname="n")
        assert nodes[0]["data"]["user_id"] == 999
        assert isinstance(nodes[0]["data"]["user_id"], int)

    def test_non_numeric_user_id_falls_back_to_zero(self):
        nodes = build_forward_nodes(["内容"], user_id="abc", nickname="n")
        assert nodes[0]["data"]["user_id"] == 0

    def test_empty_chunks(self):
        assert build_forward_nodes([], user_id=1, nickname="n") == []


class TestSendForward:

    def test_group_uses_group_api(self):
        bot = _fake_bot()
        ok = _asyncio.run(send_forward(bot, "group", 777, ["a", "b", "c"]))
        assert ok.status == "sent"
        api, kwargs = bot.calls[-1]
        assert api == "send_group_forward_msg"
        assert kwargs["group_id"] == 777
        assert len(kwargs["messages"]) == 3

    def test_private_uses_private_api(self):
        bot = _fake_bot()
        ok = _asyncio.run(send_forward(bot, "private", 555, ["a", "b"]))
        assert ok.status == "sent"
        api, kwargs = bot.calls[-1]
        assert api == "send_private_forward_msg"
        assert kwargs["user_id"] == 555

    def test_fetches_and_caches_nickname(self):
        bot = _fake_bot(nickname="阿雪")
        _asyncio.run(send_forward(bot, "group", 1, ["a", "b", "c"]))
        _asyncio.run(send_forward(bot, "group", 1, ["x", "y", "z"]))
        # get_login_info 只应被调用一次（第二次走缓存）
        logins = [c for c in bot.calls if c[0] == "get_login_info"]
        assert len(logins) == 1
        assert bot.calls[-1][1]["messages"][0]["data"]["nickname"] == "阿雪"

    def test_nickname_falls_back_to_self_id(self):
        bot = _fake_bot(nickname="")
        _asyncio.run(send_forward(bot, "group", 1, ["a", "b", "c"]))
        assert bot.calls[-1][1]["messages"][0]["data"]["nickname"] == "10001"

    def test_failure_returns_false(self):
        """扩展 API 不被支持时应返回 False，由调用方回退"""
        bot = _fake_bot(fail_forward=True)
        assert _asyncio.run(send_forward(bot, "group", 1, ["a", "b", "c"])).status == "failed"
        bot.send_group_msg.assert_not_awaited()

    def test_empty_chunks_no_call(self):
        bot = _fake_bot()
        assert _asyncio.run(send_forward(bot, "group", 1, [])).status == "sent"
        assert not bot.calls


class TestThresholdRouting:

    @_pytest.fixture(autouse=True)
    def _no_delay(self, monkeypatch):
        """去掉人类节奏延迟，避免测试变慢"""
        import plugins.chunker as ck
        monkeypatch.setattr(ck, "HUMAN_DELAY_MIN", 0)
        monkeypatch.setattr(ck, "HUMAN_DELAY_MAX", 0)

    def _event(self, group_id=None):
        ev = _types.SimpleNamespace(user_id=555, message_id=99)
        if group_id is not None:
            ev.group_id = group_id
        return ev

    def test_below_threshold_sends_one_by_one(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked(bot, self._event(group_id=7), ["a", "b"]))
        assert bot.send_group_msg.await_count == 2
        assert not any(c[0].endswith("forward_msg") for c in bot.calls)

    def test_at_threshold_uses_forward(self):
        bot = _fake_bot()
        chunks = ["a", "b", "c"]
        assert len(chunks) == FORWARD_THRESHOLD
        _asyncio.run(send_chunked(bot, self._event(group_id=7), chunks))
        assert bot.send_group_msg.await_count == 0
        assert bot.calls[-1][0] == "send_group_forward_msg"
        assert len(bot.calls[-1][1]["messages"]) == 3

    def test_fallback_to_sequential_when_forward_fails(self):
        bot = _fake_bot(fail_forward=True)
        _asyncio.run(send_chunked(bot, self._event(group_id=7), ["a", "b", "c", "d"]))
        assert bot.send_group_msg.await_count == 4

    def test_private_event_routes_to_private_forward(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked(bot, self._event(), ["a", "b", "c"]))
        assert bot.calls[-1][0] == "send_private_forward_msg"
        assert bot.calls[-1][1]["user_id"] == 555

    def test_raw_at_threshold_uses_forward(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked_raw(bot, "group", 7, "甲\n乙\n丙"))
        assert bot.send_group_msg.await_count == 0
        assert bot.calls[-1][0] == "send_group_forward_msg"

    def test_raw_below_threshold_sends_one_by_one(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked_raw(bot, "group", 7, "甲\n乙"))
        assert bot.send_group_msg.await_count == 2

    def test_empty_chunks_no_send(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked(bot, self._event(group_id=7), []))
        assert bot.send_group_msg.await_count == 0
        assert not bot.calls


# ══════════════════════════════════════════════════════
# 合并转发（分段数达到 FORWARD_THRESHOLD 时）
# ══════════════════════════════════════════════════════

import asyncio as _asyncio
import types as _types
from unittest.mock import AsyncMock, MagicMock

import pytest as _pytest

from plugins.chunker import (
    FORWARD_THRESHOLD,
    build_forward_nodes,
    send_forward,
    send_chunked,
    send_chunked_raw,
    reset_self_nickname_cache,
)


def _fake_bot(nickname="测试Bot", self_id="10001", fail_forward=False):
    bot = MagicMock()
    bot.self_id = self_id
    bot.calls = []

    async def _call_api(api, **kwargs):
        bot.calls.append((api, kwargs))
        if api == "get_login_info":
            return {"user_id": int(self_id), "nickname": nickname}
        if api in ("send_group_forward_msg", "send_private_forward_msg"):
            if fail_forward:
                raise RuntimeError("实现端不支持合并转发")
            return {"message_id": 1}
        return {}

    bot.call_api = AsyncMock(side_effect=_call_api)
    bot.send_group_msg = AsyncMock()
    bot.send_private_msg = AsyncMock()
    return bot


@_pytest.fixture(autouse=True)
def _reset_cache(tmp_path, monkeypatch):
    from plugins import message_store
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "chatlog.db")
    reset_self_nickname_cache()
    yield
    reset_self_nickname_cache()


class TestBuildForwardNodes:

    def test_node_structure(self):
        """节点必须是标准格式：type=node + user_id/nickname/content"""
        nodes = build_forward_nodes(["甲", "乙"], user_id="12345", nickname="阿雪")
        assert len(nodes) == 2
        assert nodes[0] == {
            "type": "node",
            "data": {"user_id": 12345, "nickname": "阿雪", "content": "甲"},
        }
        assert nodes[1]["data"]["content"] == "乙"

    def test_user_id_is_int(self):
        """NapCat 会 .toString()，但标准类型应是数字"""
        nodes = build_forward_nodes(["内容"], user_id="999", nickname="n")
        assert nodes[0]["data"]["user_id"] == 999
        assert isinstance(nodes[0]["data"]["user_id"], int)

    def test_non_numeric_user_id_falls_back_to_zero(self):
        nodes = build_forward_nodes(["内容"], user_id="abc", nickname="n")
        assert nodes[0]["data"]["user_id"] == 0

    def test_empty_chunks(self):
        assert build_forward_nodes([], user_id=1, nickname="n") == []


class TestSendForward:

    def test_group_uses_group_api(self):
        bot = _fake_bot()
        ok = _asyncio.run(send_forward(bot, "group", 777, ["a", "b", "c"]))
        assert ok.status == "sent"
        api, kwargs = bot.calls[-1]
        assert api == "send_group_forward_msg"
        assert kwargs["group_id"] == 777
        assert len(kwargs["messages"]) == 3

    def test_private_uses_private_api(self):
        bot = _fake_bot()
        ok = _asyncio.run(send_forward(bot, "private", 555, ["a", "b"]))
        assert ok.status == "sent"
        api, kwargs = bot.calls[-1]
        assert api == "send_private_forward_msg"
        assert kwargs["user_id"] == 555

    def test_fetches_and_caches_nickname(self):
        bot = _fake_bot(nickname="阿雪")
        _asyncio.run(send_forward(bot, "group", 1, ["a", "b", "c"]))
        _asyncio.run(send_forward(bot, "group", 1, ["x", "y", "z"]))
        # get_login_info 只应被调用一次（第二次走缓存）
        logins = [c for c in bot.calls if c[0] == "get_login_info"]
        assert len(logins) == 1
        assert bot.calls[-1][1]["messages"][0]["data"]["nickname"] == "阿雪"

    def test_nickname_falls_back_to_self_id(self):
        bot = _fake_bot(nickname="")
        _asyncio.run(send_forward(bot, "group", 1, ["a", "b", "c"]))
        assert bot.calls[-1][1]["messages"][0]["data"]["nickname"] == "10001"

    def test_failure_returns_false(self):
        """扩展 API 不被支持时应返回 False，由调用方回退"""
        bot = _fake_bot(fail_forward=True)
        assert _asyncio.run(send_forward(bot, "group", 1, ["a", "b", "c"])).status == "failed"
        bot.send_group_msg.assert_not_awaited()

    def test_empty_chunks_no_call(self):
        bot = _fake_bot()
        assert _asyncio.run(send_forward(bot, "group", 1, [])).status == "sent"
        assert not bot.calls


class TestThresholdRouting:

    @_pytest.fixture(autouse=True)
    def _no_delay(self, monkeypatch):
        """去掉人类节奏延迟，避免测试变慢"""
        import plugins.chunker as ck
        monkeypatch.setattr(ck, "HUMAN_DELAY_MIN", 0)
        monkeypatch.setattr(ck, "HUMAN_DELAY_MAX", 0)

    def _event(self, group_id=None):
        ev = _types.SimpleNamespace(user_id=555, message_id=99)
        if group_id is not None:
            ev.group_id = group_id
        return ev

    def test_below_threshold_sends_one_by_one(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked(bot, self._event(group_id=7), ["a", "b"]))
        assert bot.send_group_msg.await_count == 2
        assert not any(c[0].endswith("forward_msg") for c in bot.calls)

    def test_at_threshold_uses_forward(self):
        bot = _fake_bot()
        chunks = ["a", "b", "c"]
        assert len(chunks) == FORWARD_THRESHOLD
        _asyncio.run(send_chunked(bot, self._event(group_id=7), chunks))
        assert bot.send_group_msg.await_count == 0
        assert bot.calls[-1][0] == "send_group_forward_msg"
        assert len(bot.calls[-1][1]["messages"]) == 3

    def test_fallback_to_sequential_when_forward_fails(self):
        bot = _fake_bot(fail_forward=True)
        _asyncio.run(send_chunked(bot, self._event(group_id=7), ["a", "b", "c", "d"]))
        assert bot.send_group_msg.await_count == 4

    def test_private_event_routes_to_private_forward(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked(bot, self._event(), ["a", "b", "c"]))
        assert bot.calls[-1][0] == "send_private_forward_msg"
        assert bot.calls[-1][1]["user_id"] == 555

    def test_raw_at_threshold_uses_forward(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked_raw(bot, "group", 7, "甲\n乙\n丙"))
        assert bot.send_group_msg.await_count == 0
        assert bot.calls[-1][0] == "send_group_forward_msg"

    def test_raw_below_threshold_sends_one_by_one(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked_raw(bot, "group", 7, "甲\n乙"))
        assert bot.send_group_msg.await_count == 2

    def test_empty_chunks_no_send(self):
        bot = _fake_bot()
        _asyncio.run(send_chunked(bot, self._event(group_id=7), []))
        assert bot.send_group_msg.await_count == 0
        assert not bot.calls


class TestDeliveryRecords:
    def test_confirmed_forward_is_available_for_later_quote(self):
        from plugins import message_store
        bot = _fake_bot()
        result = _asyncio.run(send_forward(bot, "group", 777, ["a", "b", "c"]))
        assert result.message_ids == (1,)
        assert message_store.get_context("777", "qq:1")["text"] == "a\nb\nc"

    def test_timeout_does_not_fall_back_to_duplicate_messages(self):
        bot = _fake_bot()
        async def api(name, **kwargs):
            if name == "get_login_info":
                return {"nickname": "Bot"}
            raise TimeoutError("response was lost")
        bot.call_api.side_effect = api
        result = _asyncio.run(send_chunked_raw(bot, "group", 777, "a\nb\nc"))
        assert result.status == "unknown"
        bot.send_group_msg.assert_not_awaited()

    def test_index_failure_after_send_does_not_trigger_resend(self, monkeypatch):
        from plugins import message_store
        def broken(*args, **kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(message_store, "remember_sent_message", broken)
        bot = _fake_bot()
        result = _asyncio.run(send_chunked_raw(bot, "group", 777, "a\nb\nc"))
        assert result.status == "sent"
        bot.send_group_msg.assert_not_awaited()

    def test_plain_message_id_is_also_recorded(self):
        from plugins import message_store
        bot = _fake_bot()
        bot.send_group_msg.return_value = {"message_id": 9}
        result = _asyncio.run(send_chunked_raw(bot, "group", 777, "plain text"))
        assert result.message_ids == (9,)
        assert message_store.get_context("777", "qq:9")["text"] == "plain text"
