"""
tests/test_llm_fallback.py
──────────────────────────
测试 call_llm 的 fallback 机制:
  - 主力成功 → 直接返回
  - 主力 429 → fallback 到备用
  - 主力超时 → fallback 到备用
  - 所有 provider 都失败 → 抛出异常
  - 非可重试错误 → 直接抛出
  - token 统计被调用
"""

import sys
import os
import types
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Mock nonebot ──
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

_mock_config = MagicMock()
_mock_config.llm_provider = "gemini"
_mock_config.gemini_api_key = "fake-gemini-key"
_mock_config.openai_api_key = "fake-openai-key"
_mock_config.qwen_api_key = ""
_mock_config.llm_base_url = ""
_mock_config.llm_model = ""
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)

# ── Mock token_stats ──
if "plugins" not in sys.modules:
    _plugins_pkg = types.ModuleType("plugins")
    _plugins_pkg.__path__ = [str(ROOT / "plugins")]
    sys.modules["plugins"] = _plugins_pkg

_mock_token_stats = types.ModuleType("plugins.token_stats")
_mock_token_stats.record_usage = MagicMock()
sys.modules["plugins.token_stats"] = _mock_token_stats

# ── 加载 llm 模块 ──
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "plugins.llm", ROOT / "plugins" / "llm.py"
)
_llm = importlib.util.module_from_spec(_spec)
sys.modules["plugins.llm"] = _llm
_spec.loader.exec_module(_llm)

call_llm = _llm.call_llm


# ──────────────────── 辅助 ────────────────────

def _make_success_response(content: str = "hello") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return resp


def _make_error_response(status_code: int) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = f"Error {status_code}"
    request = MagicMock()
    return httpx.HTTPStatusError(
        f"{status_code}", request=request, response=resp
    )


# ──────────────────── 测试 ────────────────────

class TestCallLlmFallback:

    @pytest.mark.asyncio
    async def test_primary_success(self):
        """主力成功时直接返回，不触发 fallback。"""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_make_success_response("ok"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await call_llm([{"role": "user", "content": "hi"}], source="test")

        assert result["choices"][0]["message"]["content"] == "ok"
        # 只调了一次（主力）
        assert mock_client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_fallback_on_429(self):
        """主力 429 时 fallback 到备用。"""
        call_count = [0]
        async def _mock_post(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise _make_error_response(429)
            return _make_success_response("from fallback")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _mock_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await call_llm([{"role": "user", "content": "hi"}], source="test")

        assert result["choices"][0]["message"]["content"] == "from fallback"
        assert call_count[0] == 2  # 主力 + fallback

    @pytest.mark.asyncio
    async def test_fallback_on_500(self):
        """主力 500 时 fallback。"""
        call_count = [0]
        async def _mock_post(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise _make_error_response(500)
            return _make_success_response("recovered")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _mock_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await call_llm([{"role": "user", "content": "hi"}], source="test")

        assert result["choices"][0]["message"]["content"] == "recovered"

    @pytest.mark.asyncio
    async def test_fallback_on_timeout(self):
        """主力超时时 fallback。"""
        call_count = [0]
        async def _mock_post(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise httpx.TimeoutException("timed out")
            return _make_success_response("after timeout")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _mock_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await call_llm([{"role": "user", "content": "hi"}], source="test")

        assert result["choices"][0]["message"]["content"] == "after timeout"

    @pytest.mark.asyncio
    async def test_all_fail_raises(self):
        """所有 provider 都失败时抛出异常。"""
        async def _mock_post(url, **kwargs):
            raise _make_error_response(429)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _mock_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await call_llm([{"role": "user", "content": "hi"}], source="test")

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self):
        """400 等非可重试错误直接抛出，不 fallback。"""
        call_count = [0]
        async def _mock_post(url, **kwargs):
            call_count[0] += 1
            raise _make_error_response(400)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = _mock_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                await call_llm([{"role": "user", "content": "hi"}], source="test")

        assert call_count[0] == 1  # 只尝试了一次

    @pytest.mark.asyncio
    async def test_tools_passed_to_payload(self):
        """tools 参数应被传入 payload。"""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_make_success_response())

        tools = [{"type": "function", "function": {"name": "test"}}]
        with patch("httpx.AsyncClient", return_value=mock_client):
            await call_llm([{"role": "user", "content": "hi"}], tools=tools, source="test")

        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "tools" in payload

    @pytest.mark.asyncio
    async def test_token_stats_recorded(self):
        """成功调用后应记录 token 统计。"""
        _mock_token_stats.record_usage.reset_mock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_make_success_response())

        with patch("httpx.AsyncClient", return_value=mock_client):
            await call_llm([{"role": "user", "content": "hi"}], source="test_source")

        _mock_token_stats.record_usage.assert_called_once()
        args = _mock_token_stats.record_usage.call_args[0]
        assert args[0] == "test_source"
