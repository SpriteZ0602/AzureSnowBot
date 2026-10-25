"""七天管理日志：群归属、滚动过期、旧格式兼容和并发清理。"""

import asyncio
import importlib.util
import json
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone(timedelta(hours=8)))


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.astimezone().replace(tzinfo=None)


@pytest.fixture
def tool_log(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("isolated_tool_log", ROOT / "plugins/tool_log.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LOG_DIR", tmp_path / "tool_calls")
    monkeypatch.setattr(module, "LEGACY_LOG_FILE", tmp_path / "tool_calls.jsonl")
    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    return module


def write_entries(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), encoding="utf-8")


def read_entries(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def entry(name, age=timedelta()):
    return {"ts": (NOW - age).isoformat(), "tool": name}


@pytest.mark.parametrize("source", ["group", "heartbeat", "task", "sub_agent"])
def test_group_context_routes_all_execution_sources(tool_log, source):
    tool_log.log_tool_call(source, "local__tarot", {"num": 1}, "正位 太阳", context={
        "_chat_type": "group", "_target_id": "123", "_user_id": "456",
    })
    records = read_entries(tool_log.LOG_DIR / "groups/123.jsonl")
    assert len(records) == 1
    assert records[0]["group_id"] == "123" and records[0]["user_id"] == "456"
    assert records[0]["source"] == source
    assert records[0]["result_len"] == len("正位 太阳")
    assert "正位 太阳" not in json.dumps(records, ensure_ascii=False)
    assert not tool_log.LEGACY_LOG_FILE.exists()


def test_groups_private_and_unknown_are_separate(tool_log):
    tool_log.log_tool_call("group", "g1", {}, "ok", group_id="123")
    tool_log.log_tool_call("group", "g2", {}, "ok", group_id="456")
    tool_log.log_tool_call("chat", "admin", {}, "ok")
    # 私聊任务即使查询某群，也应按执行所在会话归属 Admin。
    tool_log.log_tool_call("task", "private_task", {}, "ok", group_id="123", context={
        "_chat_type": "private", "_target_id": "999",
    })
    tool_log.log_tool_call("sub_agent", "unknown", {}, "ok")
    assert [r["tool"] for r in read_entries(tool_log.LOG_DIR / "groups/123.jsonl")] == ["g1"]
    assert [r["tool"] for r in read_entries(tool_log.LOG_DIR / "groups/456.jsonl")] == ["g2"]
    assert [r["tool"] for r in read_entries(tool_log.LOG_DIR / "admin.jsonl")] == ["admin", "private_task"]
    assert read_entries(tool_log.LOG_DIR / "admin.jsonl")[1]["group_id"] == ""
    assert read_entries(tool_log.LOG_DIR / "unscoped.jsonl")[0]["tool"] == "unknown"


@pytest.mark.parametrize("gid", ["../escape", "123/../../escape", "１２３", ""])
def test_invalid_group_id_cannot_escape_log_directory(tool_log, gid):
    tool_log.log_tool_call("group", "test", {}, "ok", context={
        "_chat_type": "group", "_target_id": gid,
    })
    assert (tool_log.LOG_DIR / "unscoped.jsonl").exists()
    assert not list(tool_log.LOG_DIR.parent.glob("escape*"))


def test_retention_is_rolling_seven_days_and_preserves_boundary(tool_log):
    path = tool_log.LOG_DIR / "groups/123.jsonl"
    records = [entry("expired", timedelta(days=7, microseconds=1)),
               entry("boundary", timedelta(days=7)), entry("recent", timedelta(hours=1))]
    write_entries(path, records)
    tool_log.cleanup_tool_logs()
    assert [r["tool"] for r in read_entries(path)] == ["boundary", "recent"]
    assert not list(path.parent.glob("*.tmp"))


def test_expired_files_removed_even_without_new_calls_and_legacy_is_pruned(tool_log):
    expired = entry("expired", timedelta(days=8))
    for path in [tool_log.LOG_DIR / "groups/123.jsonl", tool_log.LOG_DIR / "admin.jsonl",
                 tool_log.LOG_DIR / "unscoped.jsonl"]:
        write_entries(path, [expired])
    write_entries(tool_log.LEGACY_LOG_FILE, [expired, entry("legacy_recent", timedelta(days=2))])
    unrelated = tool_log.LOG_DIR / "groups/config.json"
    unrelated.write_text('{"enabled": true}', encoding="utf-8")
    tool_log.cleanup_tool_logs()
    assert list((tool_log.LOG_DIR / "groups").glob("*.jsonl")) == []
    assert not (tool_log.LOG_DIR / "admin.jsonl").exists()
    assert not (tool_log.LOG_DIR / "unscoped.jsonl").exists()
    assert read_entries(tool_log.LEGACY_LOG_FILE)[0]["tool"] == "legacy_recent"
    assert unrelated.exists()
    write_entries(tool_log.LEGACY_LOG_FILE, [expired])
    tool_log.cleanup_tool_logs()
    assert not tool_log.LEGACY_LOG_FILE.exists()


def test_malformed_rows_and_old_naive_timestamps_do_not_block_cleanup(tool_log):
    path = tool_log.LOG_DIR / "groups/123.jsonl"
    naive_ts = (NOW - timedelta(days=1)).astimezone().replace(tzinfo=None).isoformat()
    write_entries(path, [entry("expired", timedelta(days=10)), [], {"ts": 123},
                         {"ts": "invalid"}, {"ts": naive_ts, "tool": "valid_naive"}])
    with path.open("ab") as file:
        file.write(b'not json\n\xff\n')
    tool_log.cleanup_tool_logs()
    assert [r["tool"] for r in read_entries(path)] == ["valid_naive"]


def test_dashboard_aggregates_recent_records_in_timestamp_order(tool_log, monkeypatch):
    write_entries(tool_log.LOG_DIR / "groups/123.jsonl", [entry("group_old", timedelta(days=6))])
    write_entries(tool_log.LOG_DIR / "groups/456.jsonl", [entry("group_new", timedelta(minutes=1))])
    write_entries(tool_log.LOG_DIR / "admin.jsonl", [entry("admin", timedelta(hours=1))])
    write_entries(tool_log.LEGACY_LOG_FILE, [entry("expired", timedelta(days=8)),
                                           entry("legacy", timedelta(minutes=30))])
    # 验证时区偏移的排序按实际时间，不按 ISO 字符串。
    write_entries(tool_log.LOG_DIR / "unscoped.jsonl", [
        {"ts": NOW.astimezone(timezone.utc).isoformat(), "tool": "latest_utc"},
    ])
    monkeypatch.setitem(sys.modules, "plugins.tool_log", tool_log)
    auth = types.ModuleType("plugins.dashboard.auth")
    auth.get_current_user = lambda: "admin"
    monkeypatch.setitem(sys.modules, "plugins.dashboard.auth", auth)
    spec = importlib.util.spec_from_file_location("plugins.dashboard.routes.overview", ROOT / "plugins/dashboard/routes/overview.py")
    overview = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(overview)
    assert [r["tool"] for r in overview._load_recent_tool_calls(3)] == ["latest_utc", "group_new", "legacy"]
    assert len(overview._load_recent_tool_calls(100)) == 5
    assert overview._load_recent_tool_calls(0) == []


def test_reader_excludes_expired_rows_between_scheduled_cleanups(tool_log):
    path = tool_log.LOG_DIR / "groups/123.jsonl"
    tool_log._last_cleanup = NOW.timestamp()
    write_entries(path, [entry("expired", timedelta(days=7, seconds=1)), entry("recent")])
    assert [r["tool"] for r in tool_log.load_recent_tool_calls()] == ["recent"]


def test_failed_atomic_rewrite_preserves_original_file(tool_log, monkeypatch):
    path = tool_log.LOG_DIR / "groups/123.jsonl"
    records = [entry("expired", timedelta(days=9)), entry("recent")]
    write_entries(path, records)
    monkeypatch.setattr(tool_log.os, "replace", MagicMock(side_effect=OSError("disk full")))
    tool_log.cleanup_tool_logs()
    assert read_entries(path) == records
    assert not list(path.parent.glob("*.tmp"))


def test_concurrent_cleanup_does_not_drop_appends(tool_log):
    def work(i):
        if i % 5 == 0:
            tool_log.cleanup_tool_logs()
        tool_log.log_tool_call("group", f"tool_{i}", {}, "ok", group_id="123")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(60)))
    records = read_entries(tool_log.LOG_DIR / "groups/123.jsonl")
    assert {r["tool"] for r in records} == {f"tool_{i}" for i in range(60)}
    assert len(records) == 60


def test_write_failure_does_not_break_tool_execution(tool_log, monkeypatch):
    monkeypatch.setattr(Path, "open", MagicMock(side_effect=OSError("disk full")))
    tool_log.log_tool_call("group", "test", {}, "ok", group_id="123")


@pytest.mark.parametrize("result", ["[工具调用出错] fixture", "[错误] fixture", "[Sub-Agent 调用失败] fixture"])
def test_all_error_forms_are_logged_as_failure(tool_log, result):
    tool_log.log_tool_call("group", "test", {}, result, group_id="123")
    assert read_entries(tool_log.LOG_DIR / "groups/123.jsonl")[0]["ok"] is False


@pytest.mark.asyncio
async def test_lifecycle_cleans_idle_logs_and_stops_background_task(tool_log, monkeypatch):
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    driver = types.SimpleNamespace(on_startup=lambda fn: fn, on_shutdown=lambda fn: fn)
    module("nonebot", get_driver=lambda: driver)
    module("nonebot.log", logger=MagicMock())
    mcp = module("plugins.mcp.manager", initialize=AsyncMock(), shutdown=AsyncMock())
    module("plugins.mcp", manager=mcp)
    skill = module("plugins.skill.manager", scan_skills=MagicMock())
    module("plugins.skill", manager=skill)
    module("plugins.group", __path__=[str(ROOT / "plugins/group")])
    for name in ["handler", "commands", "chatlog", "repeater", "chatter"]:
        module(f"plugins.group.{name}")
    monkeypatch.setitem(sys.modules, "plugins.tool_log", tool_log)
    spec = importlib.util.spec_from_file_location("plugins.group_plugin", ROOT / "plugins/group_plugin.py")
    group = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "plugins.group_plugin", group)
    spec.loader.exec_module(group)
    monkeypatch.setattr(group, "_start_group_heartbeats", lambda: None)
    path = tool_log.LOG_DIR / "groups/123.jsonl"
    write_entries(path, [entry("startup_expired", timedelta(days=8))])
    await group._startup()
    assert not path.exists()
    # 缩短测试调度周期；无需新调用，下一次定时清理也会删除过期记录。
    monkeypatch.setattr(tool_log, "CLEANUP_INTERVAL_SECONDS", 0.01)
    try:
        write_entries(path, [entry("idle_expired", timedelta(days=8))])
        async def wait_for_cleanup():
            while path.exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_cleanup(), timeout=2)
        task = group._tool_log_cleanup_task
    finally:
        await group._shutdown()
    assert task.cancelled()
    assert group._tool_log_cleanup_task is None
    mcp.shutdown.assert_awaited_once()
