"""
LLM 统一配置
────────────
根据 .env 中的 LLM_PROVIDER 自动选择 API Key / Base URL / Model。

支持的 provider:
  - gemini  (默认) → gemini_api_key  / generativelanguage.googleapis.com
  - openai          → openai_api_key  / api.openai.com
  - qwen            → qwen_api_key    / dashscope.aliyuncs.com

三家都兼容 OpenAI /chat/completions 接口，调用方无需区分。

使用方法:
    from plugins.llm import API_KEY, BASE_URL, MODEL
"""

import os

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
        "default_model": "qwen-plus",
    },
}


def _resolve() -> tuple[str, str, str]:
    """根据 LLM_PROVIDER 解析出 (api_key, base_url, model)。"""
    prov = _PROVIDERS.get(LLM_PROVIDER)
    if prov is None:
        supported = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"不支持的 LLM_PROVIDER='{LLM_PROVIDER}'，可选值: {supported}"
        )

    # API Key: 优先 .env 属性 → 环境变量
    api_key = (
        getattr(config, prov["key_attr"], "")
        or os.environ.get(prov["key_env"], "")
    )
    if not api_key:
        logger.warning(f"LLM_PROVIDER={LLM_PROVIDER} 但未配置 {prov['key_attr']}，LLM 调用将失败")

    # Base URL: 可通过 llm_base_url 覆盖，否则用 provider 默认值
    base_url = (
        getattr(config, "llm_base_url", "")
        or os.environ.get("LLM_BASE_URL", "")
        or prov["default_base_url"]
    )

    # Model: 可通过 llm_model 覆盖，否则用 provider 默认值
    model = (
        getattr(config, "llm_model", "")
        or os.environ.get("LLM_MODEL", "")
        or prov["default_model"]
    )

    return api_key, base_url, model


# ──────────────────── 公开常量 ────────────────────
API_KEY, BASE_URL, MODEL = _resolve()

logger.info(f"LLM Provider: {LLM_PROVIDER} | Model: {MODEL} | Base URL: {BASE_URL[:50]}...")
