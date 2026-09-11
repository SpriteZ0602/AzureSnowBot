"""
group/utils 单元测试
──────────────────
覆盖 extract_text 的 @ 段转写（@昵称(qq号)）与
fetch_quoted_text 的引用作者提取。
"""

import sys
import importlib
import importlib.util
import types
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── mock NoneBot（utils 模块加载时要 get_driver().config）──
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

# 构造 plugins / plugins.group 包（不触发 __init__.py 中的 handler 导入）
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
_mod = importlib.util.module_from_spec(_utils_spec)
sys.modules["plugins.group.utils"] = _mod
_utils_spec.loader.exec_module(_mod)

extract_text = _mod.extract_text
fetch_quoted_text = _mod.fetch_quoted_text


# ──────────────────── 测试替身 ────────────────────

class _Seg:
    def __init__(self, type: str, **data):
        self.type = type
        self.data = data


class _Event:
    def __init__(self, segments, self_id="10000", group_id=111):
        self.message = segments
        self.self_id = self_id
        self.group_id = group_id


class _Bot:
    """get_group_member_info 替身：members 提供昵称表，fail=True 模拟 API 故障"""

    def __init__(self, members: dict | None = None, fail: bool = False):
        self.members = members or {}
        self.fail = fail

    async def get_group_member_info(self, group_id, user_id):
        if self.fail:
            raise RuntimeError("api down")
        return {"nickname": self.members.get(str(user_id), "")}


class _MsgBot:
    """get_msg 替身"""

    def __init__(self, response=None, fail: bool = False):
        self.response = response
        self.fail = fail

    async def get_msg(self, message_id):
        if self.fail:
            raise RuntimeError("boom")
        return self.response


# ──────────────────── extract_text：@ 转写 ────────────────────

class TestExtractText:
    async def test_plain_text_unchanged(self):
        ev = _Event([_Seg("text", text="你好呀")])
        assert await extract_text(_Bot(), ev) == "你好呀"

    async def test_at_bot(self):
        ev = _Event([_Seg("at", qq="10000"), _Seg("text", text=" 在吗")])
        assert await extract_text(_Bot(), ev) == "@Bot 在吗"

    async def test_at_other_renders_nickname_and_qq(self):
        ev = _Event([_Seg("text", text="叫"), _Seg("at", qq="12345"), _Seg("text", text=" 来")])
        bot = _Bot(members={"12345": "大明"})
        assert await extract_text(bot, ev) == "叫@大明(12345) 来"

    async def test_at_unknown_falls_back_to_qq(self):
        ev = _Event([_Seg("at", qq="99999")])
        assert await extract_text(_Bot(members={}), ev) == "@99999"

    async def test_at_api_failure_falls_back_to_qq(self):
        ev = _Event([_Seg("at", qq="12345")])
        assert await extract_text(_Bot(fail=True), ev) == "@12345"

    async def test_at_all(self):
        ev = _Event([_Seg("at", qq="all")])
        assert await extract_text(_Bot(), ev) == "@全体成员"

    async def test_empty_nickname_falls_back_to_qq(self):
        ev = _Event([_Seg("at", qq="12345")])
        assert await extract_text(_Bot(members={"12345": ""}), ev) == "@12345"

    async def test_ignores_image_and_reply_segments(self):
        ev = _Event([
            _Seg("reply", id="5"),
            _Seg("text", text="看这个"),
            _Seg("image", url="http://x/img.jpg"),
        ])
        assert await extract_text(_Bot(), ev) == "看这个"


# ──────────────────── fetch_quoted_text：引用作者 ────────────────────

class TestFetchQuotedText:
    async def test_author_and_text(self):
        bot = _MsgBot({
            "sender": {"nickname": "大明", "user_id": 12345},
            "message": [
                {"type": "text", "data": {"text": "今天吃火锅"}},
                {"type": "image", "data": {"url": "http://x/img.jpg"}},
            ],
        })
        author, text = await fetch_quoted_text(bot, 1)
        assert author == "大明"
        assert text == "今天吃火锅"

    async def test_raw_string_message(self):
        bot = _MsgBot({"sender": {"nickname": "小明"}, "message": "纯字符串消息"})
        author, text = await fetch_quoted_text(bot, 1)
        assert (author, text) == ("小明", "纯字符串消息")

    async def test_missing_sender_falls_back(self):
        bot = _MsgBot({"message": [{"type": "text", "data": {"text": "hi"}}]})
        author, text = await fetch_quoted_text(bot, 1)
        assert author == "某人"
        assert text == "hi"

    async def test_api_failure_returns_empty_pair(self):
        author, text = await fetch_quoted_text(_MsgBot(fail=True), 1)
        assert (author, text) == ("", "")
