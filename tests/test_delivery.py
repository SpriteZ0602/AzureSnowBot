"""发送失败和部分送达不得被记录为完整成功。"""

from types import SimpleNamespace
from botcore.delivery import record_delivery


def test_failed_reply_is_not_claimed_as_said():
    history = []
    assert not record_delivery(SimpleNamespace(status="failed", delivered_text=""), "unsent", history.append)
    assert "unsent" not in str(history)
    assert "失败" in history[0]["content"]


def test_partial_reply_preserves_only_confirmed_prefix():
    history = []
    record_delivery(SimpleNamespace(status="unknown", delivered_text="first"), "first\nsecond", history.append)
    assert history[0]["content"] == "first"
    assert "second" not in str(history)
    assert "未知" in history[1]["content"]


def test_success_preserves_full_reply():
    history = []
    assert record_delivery(SimpleNamespace(status="sent"), "full", history.append)
    assert history == [{"role": "assistant", "content": "full"}]
