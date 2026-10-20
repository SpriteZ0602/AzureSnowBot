"""工具凭证的有界保存、来源和群/人格隔离。"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
spec = importlib.util.spec_from_file_location("plugins.tool_history", ROOT / "plugins/tool_history.py")
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)


def test_tarot_preserves_real_result_and_source():
    result = "你抽到了 2 张牌：\n1. 正位 太阳\n2. 逆位 魔术师"
    receipt = history.make_receipt("local__tarot", {"num": 2}, result)
    assert result in receipt
    assert "local__tarot" in receipt and "状态=已执行" in receipt
    command = history.make_receipt("command:/塔罗", {"num": 2}, result, source="command")
    assert "command:/塔罗" in command and "command" in command


def test_long_results_and_parameters_have_fixed_budget():
    receipt = history.make_receipt("remote__read", {"query": "参" * 5000}, "文" * 9000)
    assert len(receipt) < 900
    assert receipt.count("[已截断]") == 2


@pytest.mark.parametrize("prefix", ["[错误]", "[工具调用出错]", "[Sub-Agent 调用失败]"])
def test_failures_are_not_reformatted_as_success(prefix):
    receipt = history.make_receipt("test", {}, prefix + "执行失败", formatter=lambda a, r: "成功")
    assert "状态=失败" in receipt
    assert prefix in receipt
    assert "结果：成功" not in receipt


def test_custom_formatter_error_falls_back_to_bounded_result():
    def broken(args, result):
        raise ValueError("bad formatter")
    receipt = history.make_receipt("test", {}, "x" * 5000, formatter=broken)
    assert "已截断" in receipt and len(receipt) < 900


def test_search_drops_web_body_but_keeps_sources():
    result = ("搜索结果：\n\n1. 文档\n   https://example.com/docs\n   摘要\n"
              "2. API\n   https://example.com/api\n   摘要\n"
              "[自动读取的前几个链接正文]\n不应保存的正文" * 1)
    receipt = history.make_receipt("local__web_search", {"query": "测试"}, result,
                                   formatter=history.summarize_web_search)
    assert "https://example.com/docs" in receipt
    assert "https://example.com/api" in receipt
    assert "不应保存的正文" not in receipt


def test_sensitive_arguments_redacted_recursively():
    receipt = history.make_receipt("test", {"api_key": "secret-value", "nested": {"token": "token-value"}}, "ok")
    assert "secret-value" not in receipt and "token-value" not in receipt


def test_record_survives_reload_and_stays_in_captured_group_persona(tmp_path, monkeypatch):
    from plugins.history_io import append_jsonl_message, load_jsonl_history
    manager = types.ModuleType("plugins.persona.manager")
    def append(gid, message, persona):
        append_jsonl_message(tmp_path / gid / f"{persona}.jsonl", message)
    manager.append_message = append
    monkeypatch.setitem(sys.modules, "plugins.persona.manager", manager)
    ctx = {"_chat_type": "group", "_target_id": "123", "_persona": "cat"}
    history.record_tool_execution("server__draw", {}, "正位 太阳", context=ctx)
    loaded = load_jsonl_history(tmp_path / "123/cat.jsonl")
    assert len(loaded) == 1
    assert set(loaded[0]) == {"role", "content"}
    assert "正位 太阳" in loaded[0]["content"]
    assert not (tmp_path / "123/default.jsonl").exists()
    history.record_tool_execution("test", {}, "ok", context={**ctx, "_chat_type": "private"})
    history.record_tool_execution("test", {}, "ok", context={**ctx, "_target_id": "../456"})
    assert len(load_jsonl_history(tmp_path / "123/cat.jsonl")) == 1


def test_storage_error_does_not_break_tool_result(monkeypatch):
    manager = types.ModuleType("plugins.persona.manager")
    manager.append_message = MagicMock(side_effect=OSError("disk full"))
    monkeypatch.setitem(sys.modules, "plugins.persona.manager", manager)
    history.record_tool_execution("test", {}, "ok", context={
        "_chat_type": "group", "_target_id": "123", "_persona": "cat"})


def test_registered_formatter_is_used(monkeypatch):
    manager = types.ModuleType("plugins.persona.manager")
    manager.append_message = MagicMock()
    local = types.ModuleType("plugins.local_tools.manager")
    local._registry = {"read": types.SimpleNamespace(history_formatter=lambda a, r: "指定摘要")}
    monkeypatch.setitem(sys.modules, "plugins.persona.manager", manager)
    monkeypatch.setitem(sys.modules, "plugins.local_tools.manager", local)
    history.record_tool_execution("local__read", {}, "原始长结果", context={
        "_chat_type": "group", "_target_id": "123", "_persona": "cat"})
    args = manager.append_message.call_args.args
    assert "指定摘要" in args[1]["content"]
    assert "原始长结果" not in args[1]["content"]
