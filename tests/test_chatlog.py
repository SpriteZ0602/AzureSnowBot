"""
chatlog 模块单元测试
──────────────────
测试 append_chatlog / load_chatlog / purge_old_entries
"""

import sys
import os
import importlib
import importlib.util
import types
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── 设置路径 & mock NoneBot ──
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

# mock nonebot.get_driver 返回 config 对象
_mock_config = MagicMock()
_mock_config.group_whitelist = []
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)
_matcher = MagicMock()
_matcher.handle = lambda: lambda f: f
sys.modules["nonebot"].on_message = MagicMock(return_value=_matcher)

# 构造 plugins / plugins.group 包（不触发 __init__.py 中的 handler 导入）
_plugins_pkg = types.ModuleType("plugins")
_plugins_pkg.__path__ = [str(ROOT / "plugins")]
sys.modules.setdefault("plugins", _plugins_pkg)

_group_pkg = types.ModuleType("plugins.group")
_group_pkg.__path__ = [str(ROOT / "plugins" / "group")]
sys.modules["plugins.group"] = _group_pkg

# 先加载 utils（chatlog 依赖它）
_utils_spec = importlib.util.spec_from_file_location(
    "plugins.group.utils",
    ROOT / "plugins" / "group" / "utils.py",
)
_utils_mod = importlib.util.module_from_spec(_utils_spec)
sys.modules["plugins.group.utils"] = _utils_mod
_utils_spec.loader.exec_module(_utils_mod)

# 加载 chatlog
_chatlog_spec = importlib.util.spec_from_file_location(
    "plugins.group.chatlog",
    ROOT / "plugins" / "group" / "chatlog.py",
)
_mod = importlib.util.module_from_spec(_chatlog_spec)
sys.modules["plugins.group.chatlog"] = _mod
_chatlog_spec.loader.exec_module(_mod)

append_chatlog = _mod.append_chatlog
load_chatlog = _mod.load_chatlog
purge_old_entries = _mod.purge_old_entries


@pytest.fixture(autouse=True)
def tmp_chatlog_dir(tmp_path, monkeypatch):
    """将 CHATLOG_DIR 指向临时目录"""
    monkeypatch.setattr(_mod, "CHATLOG_DIR", tmp_path)
    return tmp_path


# ──────────────────── append / load ────────────────────

class TestAppendAndLoad:
    def test_append_creates_file(self, tmp_chatlog_dir):
        append_chatlog("111", "u1", "Alice", "hello")
        path = tmp_chatlog_dir / "111" / "_chatlog.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["uid"] == "u1"
        assert entry["name"] == "Alice"
        assert entry["text"] == "hello"
        assert "ts" in entry

    def test_append_multiple(self, tmp_chatlog_dir):
        append_chatlog("111", "u1", "Alice", "msg1")
        append_chatlog("111", "u2", "Bob", "msg2")
        append_chatlog("111", "u1", "Alice", "msg3")
        records = load_chatlog("111", hours=1)
        assert len(records) == 3
        assert records[0]["text"] == "msg1"
        assert records[1]["name"] == "Bob"

    def test_load_empty_group(self, tmp_chatlog_dir):
        records = load_chatlog("999", hours=24)
        assert records == []


# ──────────────────── 时间过滤 ────────────────────

class TestTimeFilter:
    def test_excludes_old_entries(self, tmp_chatlog_dir, monkeypatch):
        # 写入一条 2 小时前的记录
        old_ts = int(time.time()) - 7200
        path = tmp_chatlog_dir / "222"
        path.mkdir(parents=True, exist_ok=True)
        with (path / "_chatlog.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": old_ts, "uid": "u1", "name": "Old", "text": "old msg"}) + "\n")

        # 再追加一条当前时间的
        append_chatlog("222", "u2", "New", "new msg")

        # hours=1 应该只返回新记录
        records = load_chatlog("222", hours=1)
        assert len(records) == 1
        assert records[0]["name"] == "New"

        # hours=3 应该返回两条
        records = load_chatlog("222", hours=3)
        assert len(records) == 2


# ──────────────────── 发送者过滤 ────────────────────

class TestUserNameFilter:
    def test_filter_by_name(self, tmp_chatlog_dir):
        append_chatlog("333", "u1", "Alice", "hi")
        append_chatlog("333", "u2", "Bob", "hey")
        append_chatlog("333", "u1", "Alice", "bye")

        records = load_chatlog("333", hours=1, user_name="alice")
        assert len(records) == 2
        assert all(r["name"] == "Alice" for r in records)

    def test_filter_partial_match(self, tmp_chatlog_dir):
        append_chatlog("333", "u1", "小明同学", "test")
        append_chatlog("333", "u2", "小红", "test2")

        records = load_chatlog("333", hours=1, user_name="小明")
        assert len(records) == 1
        assert records[0]["name"] == "小明同学"


# ──────────────────── 关键词过滤 ────────────────────

class TestKeywordFilter:
    def test_filter_by_keyword(self, tmp_chatlog_dir):
        append_chatlog("444", "u1", "A", "今天天气不错")
        append_chatlog("444", "u2", "B", "明天去旅游")
        append_chatlog("444", "u1", "A", "旅游攻略分享")

        records = load_chatlog("444", hours=1, keyword="旅游")
        assert len(records) == 2

    def test_keyword_case_insensitive(self, tmp_chatlog_dir):
        append_chatlog("444", "u1", "A", "Hello World")
        append_chatlog("444", "u2", "B", "something else")

        records = load_chatlog("444", hours=1, keyword="hello")
        assert len(records) == 1


# ──────────────────── limit ────────────────────

class TestLimit:
    def test_limit_returns_newest(self, tmp_chatlog_dir):
        for i in range(10):
            append_chatlog("555", "u1", "A", f"msg{i}")

        records = load_chatlog("555", hours=1, limit=3)
        assert len(records) == 3
        assert records[0]["text"] == "msg7"
        assert records[2]["text"] == "msg9"


# ──────────────────── 组合过滤 ────────────────────

class TestCombinedFilter:
    def test_name_and_keyword(self, tmp_chatlog_dir):
        append_chatlog("666", "u1", "Alice", "去旅游")
        append_chatlog("666", "u2", "Bob", "旅游很棒")
        append_chatlog("666", "u1", "Alice", "吃饭了")

        records = load_chatlog("666", hours=1, user_name="Alice", keyword="旅游")
        assert len(records) == 1
        assert records[0]["text"] == "去旅游"


# ──────────────────── purge ────────────────────

class TestPurge:
    def test_purge_removes_old(self, tmp_chatlog_dir, monkeypatch):
        monkeypatch.setattr(_mod, "RETENTION_DAYS", 1)
        old_ts = int(time.time()) - 2 * 86400  # 2 天前
        new_ts = int(time.time())

        path = tmp_chatlog_dir / "777"
        path.mkdir(parents=True, exist_ok=True)
        with (path / "_chatlog.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": old_ts, "uid": "u1", "name": "Old", "text": "old"}) + "\n")
            f.write(json.dumps({"ts": new_ts, "uid": "u2", "name": "New", "text": "new"}) + "\n")

        removed = purge_old_entries("777")
        assert removed == 1

        # 验证只剩新记录
        records = load_chatlog("777", hours=24 * 30)
        assert len(records) == 1
        assert records[0]["name"] == "New"

    def test_purge_nonexistent_group(self, tmp_chatlog_dir):
        removed = purge_old_entries("nonexistent")
        assert removed == 0

    def test_purge_keeps_all_recent(self, tmp_chatlog_dir, monkeypatch):
        monkeypatch.setattr(_mod, "RETENTION_DAYS", 7)
        append_chatlog("888", "u1", "A", "recent msg")
        removed = purge_old_entries("888")
        assert removed == 0
