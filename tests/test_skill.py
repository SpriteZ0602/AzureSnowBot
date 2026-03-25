"""
tests/test_skill.py
────────────────────
测试 skill 管理模块:
  - YAML frontmatter 解析
  - 技能扫描与加载
  - Level 2 / Level 3 按需加载
  - 路径穿越安全检查
  - OpenAI 工具定义生成
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))

import pytest

# 直接加载 manager.py 单文件，绕过 plugins.skill.__init__ 的 commands 导入链
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "plugins.skill.manager",
    os.path.join(os.path.dirname(__file__), "..", "plugins", "skill", "manager.py"),
)
_sm = _ilu.module_from_spec(_spec)
sys.modules["plugins.skill.manager"] = _sm
_spec.loader.exec_module(_sm)
_parse_frontmatter = _sm._parse_frontmatter


# ──────────────────── YAML Frontmatter 解析 ────────────────────

class TestParseFrontmatter:
    """测试 frontmatter 解析"""

    def test_standard_frontmatter(self):
        text = """---
name: web-search
description: 搜索网络信息。当用户需要查找实时信息时使用。
---

# Web Search

使用步骤..."""
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "web-search"
        assert "搜索网络信息" in meta["description"]
        assert body.strip().startswith("# Web Search")

    def test_quoted_values(self):
        """引号包裹的值应被去除"""
        text = """---
name: "my-skill"
description: '这是一个测试技能'
---

正文"""
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "my-skill"
        assert meta["description"] == "这是一个测试技能"

    def test_no_frontmatter(self):
        """没有 frontmatter 时返回空 dict 和原文"""
        text = "# Just a markdown file\n\nSome content"
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_empty_frontmatter(self):
        text = """---
---

Some body"""
        meta, body = _parse_frontmatter(text)
        assert meta == {}

    def test_extra_fields(self):
        """额外的字段也能解析"""
        text = """---
name: test
description: desc
version: 1.0
author: nobody
---

body"""
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "test"
        assert meta["version"] == "1.0"
        assert meta["author"] == "nobody"

    def test_multiline_body(self):
        text = """---
name: test
description: desc
---

Line 1
Line 2
Line 3"""
        meta, body = _parse_frontmatter(text)
        assert "Line 1" in body
        assert "Line 3" in body


# ──────────────────── 技能扫描与加载 ────────────────────

@pytest.fixture
def skill_env(tmp_path):
    """搭建临时技能目录"""
    sm = _sm
    original_dir = sm.SKILLS_DIR
    sm.SKILLS_DIR = tmp_path / "skills"
    sm.SKILLS_DIR.mkdir()
    sm._catalog.clear()

    yield sm, sm.SKILLS_DIR

    sm.SKILLS_DIR = original_dir
    sm._catalog.clear()


class TestScanSkills:
    """测试技能扫描"""

    def test_scan_empty(self, skill_env):
        sm, skills_dir = skill_env
        sm.scan_skills()
        assert sm.list_skill_names() == []

    def test_scan_one_skill(self, skill_env):
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "weather"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: weather
description: 查询天气预报
---

# Weather Skill
""", encoding="utf-8")

        sm.scan_skills()
        assert "weather" in sm.list_skill_names()
        meta = sm.get_skill_meta("weather")
        assert meta is not None
        assert meta.description == "查询天气预报"
        assert meta.references == []

    def test_scan_with_references(self, skill_env):
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "coder"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: coder
description: 代码审查
---

Review code.
""", encoding="utf-8")
        refs_dir = skill_dir / "references"
        refs_dir.mkdir()
        (refs_dir / "api.md").write_text("API 文档", encoding="utf-8")
        (refs_dir / "examples.md").write_text("示例", encoding="utf-8")

        sm.scan_skills()
        meta = sm.get_skill_meta("coder")
        assert sorted(meta.references) == ["api.md", "examples.md"]

    def test_skip_dir_without_skill_md(self, skill_env):
        """没有 SKILL.md 的目录应被跳过"""
        sm, skills_dir = skill_env
        (skills_dir / "broken").mkdir()
        (skills_dir / "broken" / "README.md").write_text("Not a skill", encoding="utf-8")

        sm.scan_skills()
        assert sm.list_skill_names() == []

    def test_skip_skill_without_description(self, skill_env):
        """没有 description 的技能应被跳过"""
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "empty"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: empty
---

Body without description.
""", encoding="utf-8")

        sm.scan_skills()
        assert "empty" not in sm.list_skill_names()

    def test_name_fallback_to_dirname(self, skill_env):
        """没有 name 字段时应用目录名"""
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "my-tool"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
description: 一个工具
---

Body.
""", encoding="utf-8")

        sm.scan_skills()
        assert "my-tool" in sm.list_skill_names()


# ──────────────────── Level 2: 加载正文 ────────────────────

class TestLoadSkillBody:

    def test_load_body(self, skill_env):
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "trans"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: trans
description: 翻译
---

# 翻译技能

详细工作流程...""", encoding="utf-8")

        sm.scan_skills()
        body = sm.load_skill_body("trans")
        assert body is not None
        assert "翻译技能" in body
        assert "name:" not in body  # frontmatter 已去除

    def test_load_nonexistent(self, skill_env):
        sm, _ = skill_env
        assert sm.load_skill_body("不存在") is None


# ──────────────────── Level 3: 加载参考文件 ────────────────────

class TestLoadReference:

    def test_load_reference(self, skill_env):
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "coder"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: coder
description: 代码审查
---

Review.""", encoding="utf-8")
        refs_dir = skill_dir / "references"
        refs_dir.mkdir()
        (refs_dir / "api.md").write_text("API 参考内容", encoding="utf-8")

        sm.scan_skills()
        content = sm.load_skill_reference("coder", "api.md")
        assert content == "API 参考内容"

    def test_reference_nonexistent_file(self, skill_env):
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "coder"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: coder
description: 代码审查
---

x""", encoding="utf-8")

        sm.scan_skills()
        assert sm.load_skill_reference("coder", "nope.md") is None

    def test_reference_nonexistent_skill(self, skill_env):
        sm, _ = skill_env
        assert sm.load_skill_reference("不存在", "x.md") is None

    def test_path_traversal_blocked(self, skill_env):
        """路径穿越攻击应被阻止"""
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "test"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: test
description: test
---

x""", encoding="utf-8")
        refs_dir = skill_dir / "references"
        refs_dir.mkdir()

        sm.scan_skills()
        # 尝试路径穿越
        result = sm.load_skill_reference("test", "../../pyproject.toml")
        assert result is None


# ──────────────────── OpenAI 工具定义 ────────────────────

class TestOpenAITools:

    def test_empty_catalog(self, skill_env):
        sm, _ = skill_env
        tools = sm.get_openai_tools()
        assert tools == []

    def test_has_load_skill_tool(self, skill_env):
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "test"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: test
description: test skill
---

body""", encoding="utf-8")

        sm.scan_skills()
        tools = sm.get_openai_tools()
        assert len(tools) >= 1
        names = [t["function"]["name"] for t in tools]
        assert "skill__load_skill" in names

    def test_has_load_reference_when_refs_exist(self, skill_env):
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "test"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: test
description: test
---

body""", encoding="utf-8")
        refs = skill_dir / "references"
        refs.mkdir()
        (refs / "doc.md").write_text("doc", encoding="utf-8")

        sm.scan_skills()
        tools = sm.get_openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "skill__load_reference" in names


# ──────────────────── 工具调用分发 ────────────────────

class TestToolCallDispatch:

    def test_load_skill_dispatch(self, skill_env):
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: demo
description: demo skill
---

# Demo 正文""", encoding="utf-8")

        sm.scan_skills()
        result = sm.handle_tool_call("skill__load_skill", {"name": "demo"})
        assert "Demo 正文" in result

    def test_load_skill_not_found(self, skill_env):
        sm, _ = skill_env
        result = sm.handle_tool_call("skill__load_skill", {"name": "不存在"})
        assert "错误" in result

    def test_unknown_tool_returns_none(self, skill_env):
        sm, _ = skill_env
        result = sm.handle_tool_call("other_tool", {})
        assert result is None


# ──────────────────── 摘要 ────────────────────

class TestSummary:

    def test_summary_format(self, skill_env):
        sm, skills_dir = skill_env
        skill_dir = skills_dir / "test"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("""---
name: test
description: 测试技能
---

body""", encoding="utf-8")

        sm.scan_skills()
        lines = sm.list_skills_summary()
        assert len(lines) == 1
        assert "test" in lines[0]
        assert "测试技能" in lines[0]
