"""
chatlog 模块单元测试
──────────────────
测试 append_chatlog / load_chatlog / purge_old_entries / JSONL 迁移
（存储层 plugins/group/chatlog_db.py，SQLite）
"""

import sys
import importlib
import importlib.util
import json
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── 设置路径 & mock NoneBot ──
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 先加载纯 SQLite 存储层（不依赖 nonebot）
_db_spec = importlib.util.spec_from_file_location(
    "plugins.group.chatlog_db",
    ROOT / "plugins" / "group" / "chatlog_db.py",
)
_db = importlib.util.module_from_spec(_db_spec)
sys.modules["plugins.group.chatlog_db"] = _db
_db_spec.loader.exec_module(_db)

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

# 加载 chatlog（nonebot 旁路记录器）
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
migrate_jsonl_to_db = _db.migrate_jsonl_to_db


@pytest.fixture(autouse=True)
def tmp_chatlog_dir(tmp_path, monkeypatch):
    """将 DB_PATH 指向临时文件"""
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "chatlog.db")
    return tmp_path


def _insert_raw(group_id: str, ts: int, uid: str, name: str, text: str) -> None:
    """直接插入指定时间戳的行（测试时间过滤 / purge 用）"""
    conn = _db._connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO messages(group_id, ts, uid, name, text) VALUES (?,?,?,?,?)",
            (str(group_id), int(ts), str(uid), str(name), str(text)),
        )
        conn.commit()
    finally:
        conn.close()


# ──────────────────── append / load ────────────────────

class TestAppendAndLoad:
    def test_append_inserts_row(self, tmp_chatlog_dir):
        append_chatlog("111", "u1", "Alice", "hello")
        records = load_chatlog("111", hours=1)
        assert len(records) == 1
        assert records[0]["uid"] == "u1"
        assert records[0]["name"] == "Alice"
        assert records[0]["text"] == "hello"
        assert "ts" in records[0]

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
    def test_excludes_old_entries(self, tmp_chatlog_dir):
        _insert_raw("222", int(time.time()) - 7200, "u1", "Old", "old msg")
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

    def test_filter_by_user_id(self, tmp_chatlog_dir):
        append_chatlog("334", "10001", "Alice", "hi")
        append_chatlog("334", "10002", "Bob", "hey")

        records = load_chatlog("334", hours=1, user_id="10002")
        assert len(records) == 1
        assert records[0]["name"] == "Bob"


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


# ──────────────────── 幂等写入（唯一索引） ────────────────────

class TestDedupe:
    def test_exact_duplicate_same_second_collapses(self, tmp_chatlog_dir):
        ts = int(time.time())
        for _ in range(3):
            _insert_raw("777", ts, "u1", "A", "same")
        records = load_chatlog("777", hours=1)
        assert len(records) == 1


# ──────────────────── purge ────────────────────

class TestPurge:
    def test_purge_removes_old(self, tmp_chatlog_dir, monkeypatch):
        monkeypatch.setattr(_db, "RETENTION_DAYS", 1)
        _insert_raw("777", int(time.time()) - 2 * 86400, "u1", "Old", "old")
        append_chatlog("777", "u2", "New", "new")

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
        monkeypatch.setattr(_db, "RETENTION_DAYS", 7)
        append_chatlog("888", "u1", "A", "recent msg")
        removed = purge_old_entries("888")
        assert removed == 0

    def test_purge_all_old_entries(self, tmp_chatlog_dir, monkeypatch):
        monkeypatch.setattr(_db, "RETENTION_DAYS", 1)
        _insert_raw("a1", int(time.time()) - 2 * 86400, "u1", "Old", "old1")
        _insert_raw("a2", int(time.time()) - 2 * 86400, "u1", "Old", "old2")
        append_chatlog("a1", "u2", "New", "new")

        removed = _db.purge_all_old_entries()
        assert removed == 2
        assert len(load_chatlog("a1", hours=48)) == 1
        assert len(load_chatlog("a2", hours=48)) == 0


# ──────────────────── JSONL 迁移 ────────────────────

class TestMigration:
    def _make_jsonl(self, root: Path, gid: str, lines: list[str]) -> None:
        d = root / "data" / "sessions" / "groups" / gid
        d.mkdir(parents=True, exist_ok=True)
        with (d / "_chatlog.jsonl").open("w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def test_migrates_jsonl_to_db(self, tmp_chatlog_dir):
        self._make_jsonl(tmp_chatlog_dir, "111", [
            json.dumps({"ts": 1700000000, "uid": "u1", "name": "A", "text": "hello"}),
            "不是json的坏行",
            json.dumps({"ts": 1700000100, "uid": "u2", "name": "B", "text": "world"}),
        ])

        report = migrate_jsonl_to_db(tmp_chatlog_dir)
        assert report["111"]["read"] == 2
        assert report["111"]["imported"] == 2

        # 10 年窗口足够覆盖 2023 年的 ts
        records = load_chatlog("111", hours=24 * 365 * 10)
        assert [r["text"] for r in records] == ["hello", "world"]
        assert [r["uid"] for r in records] == ["u1", "u2"]

    def test_migration_idempotent(self, tmp_chatlog_dir):
        self._make_jsonl(tmp_chatlog_dir, "222", [
            json.dumps({"ts": 1700000000, "uid": "u1", "name": "A", "text": "hi"}),
        ])

        first = migrate_jsonl_to_db(tmp_chatlog_dir)
        second = migrate_jsonl_to_db(tmp_chatlog_dir)

        assert first["222"]["imported"] == 1
        assert second["222"]["imported"] == 0
        records = load_chatlog("222", hours=24 * 365 * 10)
        assert len(records) == 1

    def test_migration_multiple_groups(self, tmp_chatlog_dir):
        self._make_jsonl(tmp_chatlog_dir, "a1", [
            json.dumps({"ts": 1700000000, "uid": "u1", "name": "A", "text": "one"}),
        ])
        self._make_jsonl(tmp_chatlog_dir, "a2", [
            json.dumps({"ts": 1700000001, "uid": "u2", "name": "B", "text": "two"}),
        ])

        report = migrate_jsonl_to_db(tmp_chatlog_dir)
        assert set(report.keys()) == {"a1", "a2"}
        assert all(r["imported"] == 1 for r in report.values())

    def test_migration_no_groups_dir(self, tmp_chatlog_dir):
        assert migrate_jsonl_to_db(tmp_chatlog_dir) == {}
