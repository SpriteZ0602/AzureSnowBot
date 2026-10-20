"""
tests/test_web_tools.py
────────────────────────
测试网络工具（DeepSeek Search / Fetch API）:
  - _deepseek_search 请求契约与结果解析
  - _deepseek_fetch 正文返回 / 截断 / 失败处理
  - web_search 默认携带正文，auto_read=false 只返回结果列表
  - web_read 空 URL 校验与正文返回

所有 httpx 请求均 mock，不发真实网络请求。
"""

import sys
import os
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock nonebot
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

# plugins.llm 需要 NoneBot 运行时配置；这里用轻量模块代替，
# 具体返回值由 _config fixture 逐个测试 patch。
if "plugins.llm" not in sys.modules:
    _llm_stub = types.ModuleType("plugins.llm")
    _llm_stub.get_deepseek_tools_config = lambda: ("", "")
    sys.modules["plugins.llm"] = _llm_stub

import httpx
import pytest

from plugins.local_tools.tools import (
    _deepseek_fetch,
    _deepseek_search,
    web_read_tool,
    web_search_tool,
)

_API_BASE = "https://api.deepseek.com/v1"


# ──────────────────── httpx 假实现 ────────────────────

class _FakeResp:
    def __init__(
        self,
        json_data: dict | None = None,
        status_code: int = 200,
        text: str = "",
    ):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", f"{_API_BASE}/tools/search")
            resp = httpx.Response(
                self.status_code, request=req, text=self.text
            )
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req, response=resp
            )


class _FakeClient:
    """模拟 httpx.AsyncClient 的 async with + post"""

    def __init__(self, handler, captured: list[dict]):
        self._handler = handler
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self._captured.append({"url": url, **kwargs})
        return self._handler(url, **kwargs)


def _patch_httpx(handler):
    """handler(url, **kwargs) -> _FakeResp；返回 (patcher, captured)"""
    captured: list[dict] = []
    patcher = patch(
        "httpx.AsyncClient",
        side_effect=lambda **kw: _FakeClient(handler, captured),
    )
    return patcher, captured


@pytest.fixture(autouse=True)
def _config():
    with patch(
        "plugins.llm.get_deepseek_tools_config",
        return_value=("test-key", _API_BASE),
    ):
        yield


def _search_handler(results: list[dict]):
    return lambda url, **kw: _FakeResp({"results": results})


# ──────────────────── _deepseek_search ────────────────────

class TestDeepSeekSearch:

    async def test_request_contract(self):
        patcher, captured = _patch_httpx(_search_handler([]))
        with patcher:
            await _deepseek_search("测试 查询", 7, need_content=True)

        assert len(captured) == 1
        req = captured[0]
        assert req["url"] == f"{_API_BASE}/tools/search"
        assert req["headers"]["Authorization"] == "Bearer test-key"
        assert req["json"] == {
            "query": "测试 查询",
            "count": 7,
            "need_content": True,
        }

    async def test_need_content_omitted_by_default(self):
        patcher, captured = _patch_httpx(_search_handler([]))
        with patcher:
            await _deepseek_search("q", 5)

        assert "need_content" not in captured[0]["json"]

    async def test_query_truncated_to_128_chars(self):
        patcher, captured = _patch_httpx(_search_handler([]))
        with patcher:
            await _deepseek_search("长" * 200, 5)

        assert len(captured[0]["json"]["query"]) == 128

    async def test_parses_results(self):
        results = [
            {
                "title": "标题一",
                "url": "https://example.com/1",
                "snippet": "摘要一",
                "summary": "长摘要一",
                "site_name": "示例站",
                "date": "2026-10-20",
            },
        ]
        patcher, _ = _patch_httpx(_search_handler(results))
        with patcher:
            out = await _deepseek_search("q", 5)

        assert out == results

    async def test_missing_results_returns_empty(self):
        patcher, _ = _patch_httpx(lambda url, **kw: _FakeResp({}))
        with patcher:
            out = await _deepseek_search("q", 5)
        assert out == []

    async def test_http_error_propagates(self):
        patcher, _ = _patch_httpx(
            lambda url, **kw: _FakeResp({}, status_code=503)
        )
        with patcher, pytest.raises(httpx.HTTPStatusError):
            await _deepseek_search("q", 5)

    async def test_missing_key_raises(self):
        with patch("plugins.llm.get_deepseek_tools_config", return_value=("", "")):
            with pytest.raises(RuntimeError):
                await _deepseek_search("q", 5)


# ──────────────────── _deepseek_fetch ────────────────────

class TestDeepSeekFetch:

    async def test_returns_markdown_with_title(self):
        patcher, captured = _patch_httpx(
            lambda url, **kw: _FakeResp(
                {"title": "文章标题", "content": "正文内容"}
            )
        )
        with patcher:
            out = await _deepseek_fetch("https://example.com/a")

        assert out == "# 文章标题\n\n正文内容"
        assert captured[0]["url"] == f"{_API_BASE}/tools/fetch"
        assert captured[0]["json"] == {"url": "https://example.com/a"}
        assert captured[0]["headers"]["Authorization"] == "Bearer test-key"

    async def test_truncates_long_content(self):
        patcher, _ = _patch_httpx(
            lambda url, **kw: _FakeResp({"title": "T", "content": "长" * 2000})
        )
        with patcher:
            out = await _deepseek_fetch("https://example.com/a", max_chars=100)

        assert "内容被截断" in out
        assert len(out) < 250

    async def test_empty_content_returns_error(self):
        patcher, _ = _patch_httpx(
            lambda url, **kw: _FakeResp({"title": "T", "content": ""})
        )
        with patcher:
            out = await _deepseek_fetch("https://example.com/a")

        assert "页面正文为空" in out

    async def test_http_403_returns_error_string(self):
        patcher, _ = _patch_httpx(
            lambda url, **kw: _FakeResp({}, status_code=403, text="blocked")
        )
        with patcher:
            out = await _deepseek_fetch("https://example.com/a")

        assert "网页抓取失败" in out
        assert "403" in out

    async def test_network_error_returns_error_string(self):
        def _boom(url, **kw):
            raise OSError("connection reset")

        patcher, _ = _patch_httpx(_boom)
        with patcher:
            out = await _deepseek_fetch("https://example.com/a")

        assert "读取网页失败" in out

    async def test_missing_key_returns_error(self):
        with patch("plugins.llm.get_deepseek_tools_config", return_value=("", "")):
            out = await _deepseek_fetch("https://example.com/a")
        assert "未配置 DeepSeek API Key" in out


# ──────────────────── web_read ────────────────────

class TestWebRead:

    async def test_empty_url(self):
        assert "[错误]" in await web_read_tool(url="")

    async def test_reads_page(self):
        patcher, _ = _patch_httpx(
            lambda url, **kw: _FakeResp({"title": "T", "content": "页面正文"})
        )
        with patcher:
            out = await web_read_tool(url="https://x.com/a")
        assert out == "# T\n\n页面正文"


# ──────────────────── web_search ────────────────────

def _sample_results(n: int = 5) -> list[dict]:
    return [
        {
            "title": f"结果{i}",
            "url": f"https://example.com/{i}",
            "summary": f"摘要{i}",
            "content": f"正文{i}",
            "site_name": "示例站",
            "date": "2026-10-20T08:00:00+08:00",
        }
        for i in range(1, n + 1)
    ]


class TestWebSearch:

    async def test_auto_read_requests_content_inline(self):
        """auto_read=true：单次搜索请求带 need_content=true，正文直接来自搜索结果"""
        patcher, captured = _patch_httpx(_search_handler(_sample_results()))
        with patcher:
            out = await web_search_tool(query="测试 查询", num_results=5)

        # 只发一次 POST，不再逐页 fetch
        assert len(captured) == 1
        assert captured[0]["json"]["need_content"] is True

        assert "搜索结果" in out
        assert "结果1" in out
        assert "https://example.com/1" in out
        assert "摘要1" in out
        assert "示例站" in out
        assert "2026-10-20" in out

        assert "[自动读取的前几个链接正文]" in out
        # 只展示前 3 条正文
        assert "正文1" in out
        assert "正文2" in out
        assert "正文3" in out
        assert "正文4" not in out
        assert "正文5" not in out

    async def test_auto_read_false_returns_list_only(self):
        patcher, captured = _patch_httpx(_search_handler(_sample_results()))
        with patcher:
            out = await web_search_tool(query="测试 查询", auto_read=False)

        assert "need_content" not in captured[0]["json"]
        assert "[自动读取的前几个链接正文]" not in out
        assert "结果1" in out
        assert "正文1" not in out

    async def test_long_summary_truncated(self):
        """summary 可能很长，列表里只保留 300 字"""
        results = [
            {
                "title": "T",
                "url": "https://example.com/a",
                "summary": "长" * 1000,
            }
        ]
        patcher, _ = _patch_httpx(_search_handler(results))
        with patcher:
            out = await web_search_tool(query="q", auto_read=False)

        assert "长" * 301 not in out
        assert "长" * 300 + "…" in out

    async def test_summary_falls_back_to_snippet(self):
        results = [
            {
                "title": "T",
                "url": "https://example.com/a",
                "snippet": "短摘要",
                "content": "",
            }
        ]
        patcher, _ = _patch_httpx(_search_handler(results))
        with patcher:
            out = await web_search_tool(query="q", auto_read=False)

        assert "短摘要" in out

    async def test_missing_content_skips_section(self):
        results = [
            {"title": "T", "url": "https://example.com/a", "summary": "S"},
        ]
        patcher, _ = _patch_httpx(_search_handler(results))
        with patcher:
            out = await web_search_tool(query="q")

        assert "T" in out
        assert "[自动读取的前几个链接正文]" not in out

    async def test_num_results_clamped(self):
        patcher, captured = _patch_httpx(_search_handler([]))
        with patcher:
            await web_search_tool(query="q", num_results=99)
        assert captured[0]["json"]["count"] == 10

        patcher, captured = _patch_httpx(_search_handler([]))
        with patcher:
            await web_search_tool(query="q", num_results=0)
        assert captured[0]["json"]["count"] == 1

    async def test_search_failure(self):
        patcher, _ = _patch_httpx(
            lambda url, **kw: _FakeResp({}, status_code=503)
        )
        with patcher:
            out = await web_search_tool(query="测试 查询")
        assert "搜索失败" in out

    async def test_api_error_message(self):
        def _boom(url, **kw):
            raise OSError("connection reset")

        patcher, _ = _patch_httpx(_boom)
        with patcher:
            out = await web_search_tool(query="测试 查询")
        assert "搜索失败" in out

    async def test_empty_query(self):
        out = await web_search_tool(query="")
        assert "[错误]" in out

    async def test_empty_results(self):
        patcher, _ = _patch_httpx(_search_handler([]))
        with patcher:
            out = await web_search_tool(query="不存在的内容")
        assert "未找到" in out
