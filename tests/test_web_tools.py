"""
tests/test_web_tools.py
────────────────────────
测试网络工具（web_search 聚合 + web_read）:
  - _fetch_page_text 截断 / 失败处理
  - web_search 默认聚合抓取前几个链接正文
  - web_search auto_read=false 只返回结果列表
  - web_read 空 URL 校验

所有 httpx 请求均 mock，不发真实网络请求。
"""

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock nonebot
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

import pytest

from plugins.local_tools.tools import (
    _bing_search,
    _fetch_page_text,
    web_read_tool,
    web_search_tool,
)


# ──────────────────── httpx 假实现 ────────────────────

class _FakeResp:
    def __init__(self, text: str = "", status_code: int = 200, content: bytes | None = None):
        self.text = text
        self.status_code = status_code
        self.content = content if content is not None else text.encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClientCM:
    """模拟 httpx.AsyncClient 的 async with 用法"""

    def __init__(self, side_effect):
        self._side_effect = side_effect

    async def __aenter__(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=self._side_effect)
        return client

    async def __aexit__(self, *exc):
        return False


DDG_HTML = """<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F1&amp;rut=abc">First Result</a>
<a class="result__snippet">First description</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2F2&amp;rut=xyz">Second Result</a>
<a class="result__snippet"><span>Second description</span></a>
</body></html>"""


def _fake_get(url: str, **kwargs) -> _FakeResp:
    """按 URL 返回不同内容：DDG 搜索页 vs Jina 正文页"""
    if "duckduckgo" in url:
        return _FakeResp(DDG_HTML)
    return _FakeResp(f"页面正文 for {url}")


def _patch_httpx(side_effect):
    return patch("httpx.AsyncClient", return_value=_FakeAsyncClientCM(side_effect))


# ──────────────────── _fetch_page_text ────────────────────

class TestFetchPageText:

    async def test_truncates_long_content(self):
        with _patch_httpx(lambda url, **kw: _FakeResp("长" * 2000)):
            out = await _fetch_page_text("https://x.com/a", max_chars=100)
        assert "内容被截断" in out
        # 正文部分不超过 max_chars（截断消息额外附加）
        assert len(out) < 200

    async def test_short_content_untouched(self):
        with _patch_httpx(lambda url, **kw: _FakeResp("短文")):
            out = await _fetch_page_text("https://x.com/a", max_chars=100)
        assert out == "短文"

    async def test_http_error_returns_error_string(self):
        with _patch_httpx(lambda url, **kw: _FakeResp("", status_code=500)):
            out = await _fetch_page_text("https://x.com/a")
        assert "读取网页失败" in out

    async def test_empty_page_returns_error_string(self):
        with _patch_httpx(lambda url, **kw: _FakeResp("")):
            out = await _fetch_page_text("https://x.com/a")
        assert "页面内容为空" in out

    async def test_jina_403_warning_page_falls_back_to_direct(self):
        """Jina 返回 HTTP 200 的 403 告警页时，应视为失败并直连抓取"""
        def _side(url: str, **kw):
            if "r.jina.ai" in url:
                return _FakeResp(
                    "Title: Just a moment...\n\n"
                    "Warning: Target URL returned error 403: Forbidden"
                )
            return _FakeResp("直连抓到的真正文")

        with _patch_httpx(_side):
            out = await _fetch_page_text("https://x.com/a")

        assert "真正文" in out
        assert "Target URL returned error" not in out
        assert "Just a moment" not in out


# ──────────────────── web_read ────────────────────

class TestWebRead:

    async def test_empty_url(self):
        assert "[错误]" in await web_read_tool(url="")

    async def test_reads_page(self):
        with _patch_httpx(lambda url, **kw: _FakeResp("页面正文")):
            out = await web_read_tool(url="https://x.com/a")
        assert out == "页面正文"


# ──────────────────── _bing_search ────────────────────

BING_HTML = """<html><body><ol>
<li class="b_algo"><link rel="stylesheet" href="/rp/x.css"/><h2 class=""><a target="_blank" href="https://a.com/1" h="ID=SERP,1.1">First <strong>Result</strong></a></h2>
<div class="b_caption"><p class="b_lineclamp2">First description &amp; more</p></div></li>
<li class="b_algo"><h2 class=""><a href="https://example.org/2">Second Result</a></h2>
<div class="b_caption"><p class="b_lineclamp2">2025年8月18日&ensp;Second description</p></div></li>
</ol></body></html>"""


class TestBingSearch:
    """Bing 主引擎：解析 cn.bing.com HTML 结果（RSS 端点已废弃，返回随机垃圾），
    固定中文市场，绕过代理环境变量"""

    async def test_uses_cn_bing_with_zh_market_and_no_proxy(self):
        captured = {}

        class _RecordingClient:
            def __init__(self, **init_kwargs):
                captured["init"] = init_kwargs

            async def __aenter__(self):
                async def _get(url, **kw):
                    captured["url"] = url
                    captured["params"] = kw.get("params", {})
                    return _FakeResp(BING_HTML)

                self.get = _get
                return self

            async def __aexit__(self, *exc):
                return False

        with patch("httpx.AsyncClient", _RecordingClient):
            results = await _bing_search("facebook", 5)

        assert len(results) == 2
        assert results[0]["title"] == "First Result"  # <strong> 被剥离
        assert results[0]["url"] == "https://a.com/1"
        assert results[0]["desc"] == "First description & more"  # 实体被反转义
        assert results[1]["desc"] == "2025年8月18日 Second description"
        assert "cn.bing.com" in captured["url"]
        assert captured["params"]["mkt"] == "zh-CN"
        assert captured["params"]["q"] == "facebook"
        assert captured["init"].get("trust_env") is False

    async def test_junk_page_returns_empty(self):
        """页面没有 b_algo 块（验证页/异常页）时返回空列表，交给上层降级 DDG"""
        with _patch_httpx(lambda url, **kw: _FakeResp("<html>安全验证</html>")):
            results = await _bing_search("任意", 5)
        assert results == []


# ──────────────────── web_search ────────────────────

class TestWebSearch:

    async def test_auto_read_aggregates_pages(self):
        """默认 auto_read=true：结果列表 + 自动抓取的正文在一个返回里"""
        with _patch_httpx(_fake_get):
            out = await web_search_tool(query="测试 查询", num_results=5)

        # 搜索结果部分
        assert "搜索结果" in out
        assert "First Result" in out
        assert "https://example.com/1" in out
        assert "Second description" in out

        # 聚合正文部分（_fetch_page_text 会把 URL 拼成 r.jina.ai/<url>）
        assert "[自动读取的前几个链接正文]" in out
        assert "页面正文 for https://r.jina.ai/https://example.com/1" in out
        assert "页面正文 for https://r.jina.ai/https://example.org/2" in out

    async def test_auto_read_false_returns_list_only(self):
        with _patch_httpx(_fake_get):
            out = await web_search_tool(query="测试 查询", auto_read=False)
        assert "[自动读取的前几个链接正文]" not in out
        assert "First Result" in out

    async def test_search_failure(self):
        with _patch_httpx(lambda url, **kw: _FakeResp("", status_code=503)):
            out = await web_search_tool(query="测试 查询")
        assert "搜索失败" in out

    async def test_empty_query(self):
        out = await web_search_tool(query="")
        assert "[错误]" in out

    async def test_single_page_read_failure_does_not_kill_aggregation(self):
        """某个链接读取失败时，其余页面仍返回"""
        def _partial(url: str, **kw):
            if "example.com/1" in url:
                return _FakeResp("", status_code=500)
            return _fake_get(url, **kw)

        with _patch_httpx(_partial):
            out = await web_search_tool(query="测试 查询")

        assert "读取网页失败" in out  # 失败页的错误提示
        assert "页面正文 for https://r.jina.ai/https://example.org/2" in out  # 成功页正常
