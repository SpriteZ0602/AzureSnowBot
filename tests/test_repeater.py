"""
tests/test_repeater.py
────────────────────
测试群聊复读判定：
  - 概率随连续句数指数上升（2 句 10%，6 句 100%）
  - 同一人连发 → 不复读
  - 复读后第三人再说同样的 → 不再跟
  - 换内容后重新计数
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
    "plugins.group.repeater",
    ROOT / "plugins" / "group" / "repeater.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["plugins.group.repeater"] = _mod
_spec.loader.exec_module(_mod)

should_repeat = _mod.should_repeat
reset_group = _mod.reset_group
repeat_probability = _mod.repeat_probability
MAX_REPEAT_LEN = _mod.MAX_REPEAT_LEN

GID = "111"
BOT = "999"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_group(GID)
    yield
    reset_group(GID)


def _always() -> float:
    return 0.0


def _never() -> float:
    return 0.999


def _feed(n: int, *, rng):
    """n 个不同的人连续说「草」。"""
    results = []
    for i in range(n):
        results.append(should_repeat(GID, str(i + 1), "草", BOT, rng=rng))
    return results


class TestRepeatProbability:
    def test_anchors(self):
        assert repeat_probability(1) == 0.0
        assert repeat_probability(2) == pytest.approx(0.10)
        assert repeat_probability(6) == pytest.approx(1.0)
        assert repeat_probability(7) == pytest.approx(1.0)

    def test_exponential_between(self):
        p3 = repeat_probability(3)
        p4 = repeat_probability(4)
        p5 = repeat_probability(5)
        assert 0.10 < p3 < p4 < p5 < 1.0


class TestShouldRepeat:
    def test_two_different_users_can_repeat(self):
        assert should_repeat(GID, "1", "草", BOT, rng=_always) is False
        assert should_repeat(GID, "2", "草", BOT, rng=_always) is True

    def test_two_users_can_miss(self):
        should_repeat(GID, "1", "草", BOT, rng=_never)
        assert should_repeat(GID, "2", "草", BOT, rng=_never) is False

    def test_sixth_is_certain(self):
        hits = _feed(6, rng=_never)
        assert hits[:5] == [False] * 5
        assert hits[5] is True

    def test_miss_then_hit_later(self):
        should_repeat(GID, "1", "草", BOT, rng=_never)
        assert should_repeat(GID, "2", "草", BOT, rng=_never) is False
        assert should_repeat(GID, "3", "草", BOT, rng=_never) is False
        assert should_repeat(GID, "4", "草", BOT, rng=_never) is False
        assert should_repeat(GID, "5", "草", BOT, rng=_never) is False
        assert should_repeat(GID, "6", "草", BOT, rng=_never) is True

    def test_same_user_twice_does_not_repeat(self):
        assert should_repeat(GID, "1", "草", BOT, rng=_always) is False
        assert should_repeat(GID, "1", "草", BOT, rng=_always) is False

    def test_only_once_per_streak(self):
        should_repeat(GID, "1", "草", BOT, rng=_always)
        assert should_repeat(GID, "2", "草", BOT, rng=_always) is True
        assert should_repeat(GID, "3", "草", BOT, rng=_always) is False

    def test_new_text_resets_streak(self):
        should_repeat(GID, "1", "草", BOT, rng=_always)
        should_repeat(GID, "2", "草", BOT, rng=_always)
        assert should_repeat(GID, "1", "哈", BOT, rng=_always) is False
        assert should_repeat(GID, "2", "哈", BOT, rng=_always) is True

    def test_bot_own_message_ignored(self):
        should_repeat(GID, "1", "草", BOT, rng=_always)
        assert should_repeat(GID, BOT, "草", BOT, rng=_always) is False
        assert should_repeat(GID, "2", "草", BOT, rng=_always) is True

    def test_command_breaks_streak(self):
        should_repeat(GID, "1", "草", BOT, rng=_always)
        assert should_repeat(GID, "2", "/help", BOT, rng=_always) is False
        assert should_repeat(GID, "3", "草", BOT, rng=_always) is False

    def test_empty_ignored(self):
        assert should_repeat(GID, "1", "   ", BOT, rng=_always) is False
        assert should_repeat(GID, "2", "   ", BOT, rng=_always) is False

    def test_too_long_ignored(self):
        long_text = "哈" * (MAX_REPEAT_LEN + 1)
        should_repeat(GID, "1", long_text, BOT, rng=_always)
        assert should_repeat(GID, "2", long_text, BOT, rng=_always) is False

    def test_groups_are_isolated(self):
        other = "222"
        reset_group(other)
        should_repeat(GID, "1", "草", BOT, rng=_always)
        should_repeat(other, "1", "草", BOT, rng=_always)
        assert should_repeat(GID, "2", "草", BOT, rng=_always) is True
        assert should_repeat(other, "2", "草", BOT, rng=_always) is True
        reset_group(other)
