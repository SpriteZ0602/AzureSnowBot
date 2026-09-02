"""
tests/test_chatter.py
────────────────────
测试群聊热闹插话判定：
  - 5 分钟条数指数概率（1 条 1%，10 条 50%，20 条 100%）
  - 回复后概率减半
  - 5 分钟没回复则重置衰减
  - 群之间隔离
"""

import sys
import os
import importlib.util
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

_mock_config = MagicMock()
_mock_config.group_whitelist = []
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)
_matcher = MagicMock()
_matcher.handle = lambda: lambda f: f
sys.modules["nonebot"].on_message = MagicMock(return_value=_matcher)

for name in (
    "plugins.chunker",
    "plugins.llm",
    "plugins.persona.manager",
    "plugins.proactive",
    "plugins.runtime_context",
    "plugins.group.chatlog",
):
    sys.modules.setdefault(name, MagicMock())

_plugins_pkg = types.ModuleType("plugins")
_plugins_pkg.__path__ = [str(ROOT / "plugins")]
sys.modules.setdefault("plugins", _plugins_pkg)

_group_pkg = types.ModuleType("plugins.group")
_group_pkg.__path__ = [str(ROOT / "plugins" / "group")]
sys.modules["plugins.group"] = _group_pkg

_utils_spec = importlib.util.spec_from_file_location(
    "plugins.group.utils",
    ROOT / "plugins" / "group" / "utils.py",
)
_utils_mod = importlib.util.module_from_spec(_utils_spec)
sys.modules["plugins.group.utils"] = _utils_mod
_utils_spec.loader.exec_module(_utils_mod)

_spec = importlib.util.spec_from_file_location(
    "plugins.group.chatter",
    ROOT / "plugins" / "group" / "chatter.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["plugins.group.chatter"] = _mod
_spec.loader.exec_module(_mod)

should_chime_in = _mod.should_chime_in
reset_group = _mod.reset_group
note_bot_reply = _mod.note_bot_reply
chime_base_probability = _mod.chime_base_probability
WINDOW_SECONDS = _mod.WINDOW_SECONDS
RESET_SECONDS = _mod.RESET_SECONDS

GID = "111"


@pytest.fixture(autouse=True)
def _clean():
    reset_group(GID)
    yield
    reset_group(GID)


def _always() -> float:
    return 0.0


def _never() -> float:
    return 0.999


class TestChimeBaseProbability:
    def test_anchors(self):
        assert chime_base_probability(0) == 0.0
        assert chime_base_probability(1) == pytest.approx(0.01)
        assert chime_base_probability(10) == pytest.approx(0.50)
        assert chime_base_probability(20) == pytest.approx(1.0)
        assert chime_base_probability(30) == pytest.approx(1.0)

    def test_monotonic(self):
        prev = 0.0
        for n in range(1, 21):
            p = chime_base_probability(n)
            assert p >= prev
            prev = p


class TestShouldChimeIn:
    def test_one_message_rarely(self):
        t0 = 1000.0
        assert should_chime_in(GID, now=t0, rng=lambda: 0.02) is False
        reset_group(GID)
        assert should_chime_in(GID, now=t0, rng=lambda: 0.005) is True

    def test_ten_messages_fifty_percent(self):
        t0 = 1000.0
        for i in range(9):
            should_chime_in(GID, now=t0 + i, rng=_never)
        # 第 10 条，基础 50%
        assert should_chime_in(GID, now=t0 + 9, rng=lambda: 0.51) is False
        reset_group(GID)
        for i in range(9):
            should_chime_in(GID, now=t0 + i, rng=_never)
        assert should_chime_in(GID, now=t0 + 9, rng=lambda: 0.49) is True

    def test_twenty_messages_certain(self):
        t0 = 1000.0
        for i in range(19):
            should_chime_in(GID, now=t0 + i, rng=_never)
        assert should_chime_in(GID, now=t0 + 19, rng=_never) is True

    def test_reply_halves_probability(self):
        t0 = 1000.0
        for i in range(20):
            should_chime_in(GID, now=t0 + i, rng=_never)
        note_bot_reply(GID, now=t0 + 19)
        # 20 条基础 100%，衰减后 50%
        assert should_chime_in(GID, now=t0 + 20, rng=lambda: 0.51) is False
        assert should_chime_in(GID, now=t0 + 21, rng=lambda: 0.49) is True

    def test_second_reply_quarters(self):
        t0 = 1000.0
        for i in range(20):
            should_chime_in(GID, now=t0 + i, rng=_never)
        note_bot_reply(GID, now=t0 + 19)
        note_bot_reply(GID, now=t0 + 20)
        # 100% * 0.5 * 0.5 = 25%
        assert should_chime_in(GID, now=t0 + 21, rng=lambda: 0.26) is False
        assert should_chime_in(GID, now=t0 + 22, rng=lambda: 0.24) is True

    def test_reset_after_five_minutes_without_reply(self):
        t0 = 1000.0
        should_chime_in(GID, now=t0, rng=_never)
        note_bot_reply(GID, now=t0)
        later = t0 + RESET_SECONDS
        for i in range(19):
            should_chime_in(GID, now=later + i, rng=_never)
        # 衰减已重置为 1，凑满 20 条应 100%
        assert should_chime_in(GID, now=later + 19, rng=_never) is True

    def test_old_messages_fall_out_of_window(self):
        t0 = 1000.0
        for i in range(20):
            should_chime_in(GID, now=t0 + i, rng=_never)
        # 窗口滑空后再来一条，只剩这一条（1%）
        assert should_chime_in(
            GID, now=t0 + 19 + WINDOW_SECONDS + 1, rng=lambda: 0.02,
        ) is False

    def test_at_messages_can_count_without_rolling(self):
        t0 = 1000.0
        for i in range(20):
            assert should_chime_in(
                GID, now=t0 + i, rng=_always, roll=False,
            ) is False
        # 已有 20 条在窗口里，下一条真掷骰应 100%
        assert should_chime_in(GID, now=t0 + 20, rng=_never, roll=True) is True

    def test_groups_isolated(self):
        other = "222"
        reset_group(other)
        t0 = 1000.0
        for i in range(20):
            should_chime_in(GID, now=t0 + i, rng=_never)
        for i in range(19):
            should_chime_in(other, now=t0 + i, rng=_never)
        assert should_chime_in(other, now=t0 + 19, rng=_never) is True
        reset_group(other)
