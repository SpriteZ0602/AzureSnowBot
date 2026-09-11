"""
tests/test_llm_vision.py
────────────────────────
测试 llm.py 的多模态能力标志（SUPPORTS_VISION）:
  - deepseek 2026-09 起已支持视觉 → 开启识图
  - gemini / openai 等其他 provider → 开启识图
  - 不支持视觉的 provider 应加进 _NON_VISION_PROVIDERS

每个 provider 用独立模块名加载 llm.py，避免与 test_llm_fallback 的模块缓存冲突。
"""

import sys
import os
import types
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Mock nonebot
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))

import pytest


def _load_llm(provider: str):
    """以指定 provider 加载 llm.py，返回模块对象"""
    mock_config = MagicMock()
    mock_config.llm_provider = provider
    mock_config.deepseek_api_key = "fake-deepseek-key" if provider == "deepseek" else ""
    mock_config.gemini_api_key = "fake-gemini-key" if provider == "gemini" else ""
    mock_config.openai_api_key = ""
    mock_config.qwen_api_key = ""
    mock_config.llm_base_url = ""
    mock_config.llm_model = ""
    mock_driver = MagicMock()
    mock_driver.config = mock_config
    sys.modules["nonebot"].get_driver = MagicMock(return_value=mock_driver)

    # 构造 plugins 包（llm.py 内无相对导入，但保持与项目其他测试一致）
    _plugins_pkg = types.ModuleType("plugins")
    _plugins_pkg.__path__ = [str(ROOT / "plugins")]
    sys.modules.setdefault("plugins", _plugins_pkg)

    mod_name = f"plugins.llm_vision_{provider}"
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / "plugins" / "llm.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSupportsVision:

    def test_deepseek_enables_vision(self):
        """deepseek 已支持视觉，识图应开启；默认模型为 deepseek-flash"""
        mod = _load_llm("deepseek")
        assert mod.LLM_PROVIDER == "deepseek"
        assert mod.SUPPORTS_VISION is True
        assert mod.MODEL == "deepseek-flash"

    def test_gemini_enables_vision(self):
        mod = _load_llm("gemini")
        assert mod.SUPPORTS_VISION is True

    def test_openai_enables_vision(self):
        """openai 支持视觉（gpt 系列多模态）"""
        mod = _load_llm("openai")
        assert mod.SUPPORTS_VISION is True
