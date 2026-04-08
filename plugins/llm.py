"""
LLM 统一配置 + Fallback
──────────────────────
根据 .env 中的 LLM_PROVIDER 自动选择 API Key / Base URL / Model。
当主力模型失败时，自动降级到备用模型。

支持的 provider:
  - gemini  (默认) → gemini_api_key  / generativelanguage.googleapis.com
  - openai          → openai_api_key  / api.openai.com
  - qwen            → qwen_api_key    / dashscope.aliyuncs.com

三家都兼容 OpenAI /chat/completions 接口，调用方无需区分。

使用方法:
    from plugins.llm import API_KEY, BASE_URL, MODEL
    # 或使用带 fallback 的调用：
    from plugins.llm import call_llm
    data = await call_llm(messages, tools=None, source="chat")
"""

import os

import httpx
from nonebot import get_driver
from nonebot.log import logger

config = get_driver().config

# ──────────────────── Provider 选择 ────────────────────
LLM_PROVIDER: str = (
    getattr(config, "llm_provider", "")
    or os.environ.get("LLM_PROVIDER", "gemini")
).strip().lower()

# ──────────────────── Provider 配置表 ────────────────────
_PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "key_attr": "gemini_api_key",
        "key_env": "GEMINI_API_KEY",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-3-flash-preview",
    },
    "openai": {
        "key_attr": "openai_api_key",
        "key_env": "OPENAI_API_KEY",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5.4",
    },
    "qwen": {
        "key_attr": "qwen_api_key",
        "key_env": "QWEN_API_KEY",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen3.6-plus-2026-04-02",
    },
}


def _resolve_provider(provider_name: str) -> tuple[str, str, str] | None:
    """解析指定 provider 的 (api_key, base_url, model)，无 key 返回 None。"""
    prov = _PROVIDERS.get(provider_name)
    if not prov:
        return None
    api_key = (
        getattr(config, prov["key_attr"], "")
        or os.environ.get(prov["key_env"], "")
    )
    if not api_key:
        return None
    base_url = prov["default_base_url"]
    model = prov["default_model"]
    return api_key, base_url, model


def _resolve() -> tuple[str, str, str]:
    """根据 LLM_PROVIDER 解析出 (api_key, base_url, model)。"""
    prov = _PROVIDERS.get(LLM_PROVIDER)
    if prov is None:
        supported = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"不支持的 LLM_PROVIDER='{LLM_PROVIDER}'，可选值: {supported}"
        )

    api_key = (
        getattr(config, prov["key_attr"], "")
        or os.environ.get(prov["key_env"], "")
    )
    if not api_key:
        logger.warning(f"LLM_PROVIDER={LLM_PROVIDER} 但未配置 {prov['key_attr']}，LLM 调用将失败")

    base_url = (
        getattr(config, "llm_base_url", "")
        or os.environ.get("LLM_BASE_URL", "")
        or prov["default_base_url"]
    )

    model = (
        getattr(config, "llm_model", "")
        or os.environ.get("LLM_MODEL", "")
        or prov["default_model"]
    )

    return api_key, base_url, model


# ──────────────────── 公开常量（主力模型） ────────────────────
API_KEY, BASE_URL, MODEL = _resolve()

logger.info(f"LLM Provider: {LLM_PROVIDER} | Model: {MODEL} | Base URL: {BASE_URL[:50]}...")


# ──────────────────── Fallback 链 ────────────────────
# 主力 provider 之外的、有配置 key 的 provider 作为 fallback
_FALLBACK_CHAIN: list[tuple[str, str, str, str]] = []  # [(provider, key, base_url, model)]

for _pname in _PROVIDERS:
    if _pname == LLM_PROVIDER:
        continue
    _resolved = _resolve_provider(_pname)
    if _resolved:
        _FALLBACK_CHAIN.append((_pname, *_resolved))

if _FALLBACK_CHAIN:
    logger.info(f"LLM Fallback 链: {' → '.join(p[0] for p in _FALLBACK_CHAIN)}")


# ──────────────────── 可重试的错误码 ────────────────────
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ──────────────────── 带 Fallback 的 LLM 调用 ────────────────────

async def call_llm(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    source: str = "unknown",
    timeout: int = 120,
) -> dict:
    """
    调用 LLM API，失败时自动 fallback 到备用模型。

    返回完整的 API 响应 JSON（含 choices + usage）。
    所有 provider 都失败时抛出最后一个异常。

    参数:
        messages: OpenAI 格式的消息列表
        tools: 工具定义列表（可选）
        source: 来源标识，用于 token 统计
        timeout: 请求超时秒数
    """
    from .token_stats import record_usage

    # 构建尝试列表：主力 + fallback
    attempts = [
        (LLM_PROVIDER, API_KEY, BASE_URL, MODEL),
        *_FALLBACK_CHAIN,
    ]

    last_error: Exception | None = None

    for provider_name, api_key, base_url, model in attempts:
        if not api_key:
            continue

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                record_usage(source, data.get("usage"))

                # 标记实际使用的 provider（供调用方日志用）
                data["_provider"] = provider_name
                data["_model"] = model

                if provider_name != LLM_PROVIDER:
                    logger.info(f"LLM Fallback 成功: {LLM_PROVIDER} → {provider_name}/{model}")

                return data

        except httpx.HTTPStatusError as e:
            last_error = e
            status = e.response.status_code
            if status in _RETRYABLE_STATUS and _FALLBACK_CHAIN:
                logger.warning(
                    f"LLM {provider_name}/{model} 返回 {status}，尝试 fallback..."
                )
                continue
            raise  # 非可重试错误直接抛出

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            if _FALLBACK_CHAIN:
                logger.warning(
                    f"LLM {provider_name}/{model} 连接失败: {e}，尝试 fallback..."
                )
                continue
            raise

    # 所有 provider 都失败
    if last_error:
        raise last_error
    raise RuntimeError("没有可用的 LLM provider")
