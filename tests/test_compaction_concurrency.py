"""Compaction 与会话历史并发写入的回归测试"""

import json
from unittest.mock import patch

import pytest

from tests.test_compaction import (
    COMPACTION_THRESHOLD,
    _mod,
    _make_large_messages,
    compact_history,
)


def _write_messages(path, messages):
    with path.open("w", encoding="utf-8") as file:
        for message in messages:
            file.write(json.dumps(message, ensure_ascii=False) + "\n")


@pytest.mark.asyncio
async def test_preserves_messages_appended_while_compacting(tmp_path):
    """LLM 压缩期间追加的新上下文不能被旧快照覆盖。"""
    session = tmp_path / "history.jsonl"
    memory = tmp_path / "MEMORY.md"
    _write_messages(session, _make_large_messages(COMPACTION_THRESHOLD + 20_000))

    late_message = {"role": "user", "content": "压缩期间到达的新群聊消息"}
    call_count = 0

    async def fake_llm(system_prompt, user_content):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "无"
        with session.open("a", encoding="utf-8") as file:
            file.write(json.dumps(late_message, ensure_ascii=False) + "\n")
        return "压缩后的对话摘要"

    with patch.object(_mod, "_call_llm", side_effect=fake_llm):
        result = await compact_history("test", session, memory)

    assert result is True
    rewritten = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()]
    assert rewritten[-1] == late_message
    assert rewritten.count(late_message) == 1


@pytest.mark.asyncio
async def test_does_not_restore_history_reset_while_compacting(tmp_path):
    """压缩期间发生 reset 时，旧快照不得把已清空的历史恢复回来。"""
    session = tmp_path / "history.jsonl"
    memory = tmp_path / "MEMORY.md"
    _write_messages(session, _make_large_messages(COMPACTION_THRESHOLD + 20_000))

    call_count = 0

    async def fake_llm(system_prompt, user_content):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "无"
        session.unlink()
        return "不应写回的摘要"

    with patch.object(_mod, "_call_llm", side_effect=fake_llm):
        result = await compact_history("test", session, memory)

    assert result is False
    assert not session.exists()
