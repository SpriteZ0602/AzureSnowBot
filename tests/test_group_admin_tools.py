"""
tests/test_group_admin_tools.py
───────────────────────────────
测试群管理工具 get_group_members / group_mute:
  - 场景校验（仅群聊）
  - 成员列表格式化（昵称+群名片、角色中文、超长截断）
  - group_mute 硬护栏：调用者须管理员、目标非群主/管理员/Bot、时长封顶
  - 正常禁言执行与失败降级
"""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock nonebot
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

# tools.py 经 memory_search → indexer 拉起真实 plugins.llm，import 期校验 provider，
# 需给 mock driver 配合法值
_mock_config = MagicMock()
_mock_config.group_whitelist = []
_mock_config.llm_provider = "deepseek"
_mock_config.deepseek_api_key = ""
_mock_config.llm_base_url = ""
_mock_config.llm_model = ""
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)

import pytest
from plugins.local_tools.tools import get_group_members, group_mute


BOT_SELF_ID = 99999

MEMBERS = [
    {"user_id": 100, "nickname": "chaos", "card": "", "role": "member"},
    {"user_id": 200, "nickname": "张三", "card": "超哥", "role": "admin"},
    {"user_id": 300, "nickname": "老板", "card": "", "role": "owner"},
    {"user_id": BOT_SELF_ID, "nickname": "SnowBot", "card": "", "role": "member"},
]


class FakeBot:
    def __init__(self, members, self_id=BOT_SELF_ID, fail_ban=False, fail_list=False):
        self.self_id = self_id
        self._members = {str(m["user_id"]): m for m in members}
        self.bans = []
        self.fail_ban = fail_ban
        self.fail_list = fail_list

    async def get_group_member_list(self, group_id):
        if self.fail_list:
            raise RuntimeError("list api down")
        return list(self._members.values())

    async def get_group_member_info(self, group_id, user_id):
        m = self._members.get(str(user_id))
        if m is None:
            raise RuntimeError("member not found")
        return m

    async def set_group_ban(self, group_id, user_id, duration):
        if self.fail_ban:
            raise RuntimeError("ban api failed")
        self.bans.append((group_id, user_id, duration))


def ctx(user_id) -> dict:
    return {"_chat_type": "group", "_target_id": "123456", "_user_id": str(user_id)}


@pytest.fixture
def fake_bot(monkeypatch):
    bot = FakeBot(MEMBERS)
    monkeypatch.setattr(sys.modules["nonebot"], "get_bot", MagicMock(return_value=bot))
    return bot


# ──────────────────── get_group_members ────────────────────

class TestGetGroupMembers:

    async def test_formats_list(self, fake_bot):
        out = await get_group_members(_context=ctx(100))
        assert "100 chaos 群员" in out
        assert "200 张三(超哥) 管理员" in out
        assert "300 老板 群主" in out

    async def test_private_context_rejected(self, fake_bot):
        out = await get_group_members(_context={"_chat_type": "private", "_target_id": "373900859"})
        assert out.startswith("[错误]")
        assert "仅限群聊" in out

    async def test_none_context_rejected(self, fake_bot):
        assert (await get_group_members(_context=None)).startswith("[错误]")

    async def test_api_failure(self, monkeypatch):
        monkeypatch.setattr(
            sys.modules["nonebot"], "get_bot",
            MagicMock(return_value=FakeBot(MEMBERS, fail_list=True)),
        )
        out = await get_group_members(_context=ctx(100))
        assert out.startswith("[错误] 获取群成员列表失败")

    async def test_long_list_truncated(self, monkeypatch):
        many = [{"user_id": i, "nickname": f"u{i}", "card": "", "role": "member"} for i in range(250)]
        monkeypatch.setattr(
            sys.modules["nonebot"], "get_bot",
            MagicMock(return_value=FakeBot(many)),
        )
        out = await get_group_members(_context=ctx(100))
        assert "共 250 人" in out
        assert "u249" not in out


# ──────────────────── group_mute 护栏 ────────────────────

class TestGroupMuteGuardrails:

    async def test_non_admin_caller_rejected(self, fake_bot):
        out = await group_mute(user_id="100", duration_minutes=5, _context=ctx(100))
        assert "只有群管理员" in out
        assert fake_bot.bans == []

    async def test_missing_caller_rejected(self, fake_bot):
        out = await group_mute(user_id="100", _context={"_chat_type": "group", "_target_id": "123456"})
        assert "无法确认调用者身份" in out
        assert fake_bot.bans == []

    async def test_cannot_mute_owner(self, fake_bot):
        out = await group_mute(user_id="300", _context=ctx(200))
        assert "不能禁言群主" in out
        assert fake_bot.bans == []

    async def test_cannot_mute_admin(self, fake_bot):
        out = await group_mute(user_id="200", _context=ctx(200))
        assert "不能禁言管理员" in out
        assert fake_bot.bans == []

    async def test_cannot_mute_bot_self(self, fake_bot):
        out = await group_mute(user_id=str(BOT_SELF_ID), _context=ctx(200))
        assert "不能禁言 Bot 自己" in out
        assert fake_bot.bans == []

    async def test_non_numeric_user_id_rejected(self, fake_bot):
        out = await group_mute(user_id="chaos", _context=ctx(200))
        assert "user_id 必须是QQ号" in out
        assert fake_bot.bans == []

    async def test_non_group_context_rejected(self, fake_bot):
        out = await group_mute(
            user_id="100",
            _context={"_chat_type": "private", "_target_id": "373900859", "_user_id": "200"},
        )
        assert "仅限群聊" in out
        assert fake_bot.bans == []


# ──────────────────── group_mute 正常路径 ────────────────────

class TestGroupMuteExecute:

    async def test_admin_mutes_member(self, fake_bot):
        out = await group_mute(user_id="100", duration_minutes=5, _context=ctx(200))
        assert "chaos(100)" in out
        assert "5 分钟" in out
        assert fake_bot.bans == [(123456, 100, 300)]

    async def test_duration_capped(self, fake_bot):
        out = await group_mute(user_id="100", duration_minutes=60, _context=ctx(200))
        assert "超上限" in out
        assert fake_bot.bans == [(123456, 100, 600)]

    async def test_duration_floor(self, fake_bot):
        out = await group_mute(user_id="100", duration_minutes=0, _context=ctx(200))
        assert fake_bot.bans == [(123456, 100, 60)]

    async def test_bad_duration_defaults(self, fake_bot):
        out = await group_mute(user_id="100", duration_minutes="abc", _context=ctx(200))
        assert fake_bot.bans == [(123456, 100, 600)]

    async def test_ban_api_failure_returns_error(self, monkeypatch):
        bot = FakeBot(MEMBERS, fail_ban=True)
        monkeypatch.setattr(sys.modules["nonebot"], "get_bot", MagicMock(return_value=bot))
        out = await group_mute(user_id="100", _context=ctx(200))
        assert out.startswith("[错误] 禁言失败")
        assert bot.bans == []
