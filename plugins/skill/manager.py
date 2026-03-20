"""
Skill 管理模块
──────────────
渐进式披露 (Progressive Disclosure) 的三层加载体系：

  Level 1 — 元数据 (name + description)
    始终注入 system prompt，让 LLM 知道有哪些技能可用。
    约 ~100 词/skill，不占太多上下文。

  Level 2 — SKILL.md 正文
    LLM 判断需要某技能时，通过 load_skill 工具加载完整指令。
    只在触发时才消耗上下文窗口。

  Level 3 — references/ 参考文件
    技能附带的详细文档，LLM 按需通过 load_skill_reference 加载。
    最大限度节省 token。

目录结构：
  data/skills/<skill-name>/
  ├── SKILL.md              (必需) YAML frontmatter + Markdown 正文
  └── references/           (可选) 详细参考文档
      ├── api-docs.md
      └── examples.md

SKILL.md 格式：
  ---
  name: weather
  description: 查询天气预报。当用户询问天气、温度、是否下雨时使用。
  ---

  # Weather Skill

  具体的使用指令和工作流程...
"""

import re
from pathlib import Path
from dataclasses import dataclass, field
from nonebot.log import logger

# ──────────────────── 路径常量 ────────────────────
SKILLS_DIR = Path("data/skills")
SKILLS_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────── 数据结构 ────────────────────

@dataclass
class SkillMeta:
    """技能元数据（Level 1）"""
    name: str
    description: str
    path: Path                              # SKILL.md 所在目录
    references: list[str] = field(default_factory=list)  # references/ 中的文件名


# ──────────────────── 全局状态 ────────────────────
_catalog: dict[str, SkillMeta] = {}


# ──────────────────── YAML frontmatter 解析 ────────────────────

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)

_FIELD_RE = re.compile(r"^(\w+)\s*:\s*(.+)$", re.MULTILINE)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """
    简易 YAML frontmatter 解析，返回 (metadata_dict, body)。
    只提取顶层 key: value 字符串字段，足够处理 name/description。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw_yaml = m.group(1)
    body = text[m.end():]
    meta = {}
    for fm in _FIELD_RE.finditer(raw_yaml):
        key = fm.group(1).strip()
        val = fm.group(2).strip().strip("'\"")
        meta[key] = val
    return meta, body


# ──────────────────── 扫描与加载 ────────────────────

def scan_skills() -> None:
    """
    扫描 data/skills/ 下所有技能目录，解析 SKILL.md 的 frontmatter
    构建技能目录 (Level 1 catalog)。
    """
    _catalog.clear()
    if not SKILLS_DIR.exists():
        return

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            logger.warning(f"Skill 目录 {skill_dir.name} 缺少 SKILL.md，跳过")
            continue

        try:
            content = skill_md.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(content)
            name = meta.get("name", skill_dir.name)
            description = meta.get("description", "")
            if not description:
                logger.warning(f"Skill {name} 没有 description，跳过")
                continue

            # 扫描 references/
            refs_dir = skill_dir / "references"
            refs = []
            if refs_dir.is_dir():
                refs = sorted(f.name for f in refs_dir.iterdir() if f.is_file())

            _catalog[name] = SkillMeta(
                name=name,
                description=description,
                path=skill_dir,
                references=refs,
            )
            ref_info = f"，references: {refs}" if refs else ""
            logger.info(f"已加载 Skill: {name} — {description[:50]}...{ref_info}")
        except Exception as e:
            logger.error(f"解析 Skill {skill_dir.name} 失败: {e}")

    logger.info(f"共加载 {len(_catalog)} 个 Skill")


# ──────────────────── Level 1: 元数据查询 ────────────────────

def get_catalog() -> dict[str, SkillMeta]:
    """返回完整的技能目录"""
    return dict(_catalog)


def list_skill_names() -> list[str]:
    """返回所有已加载的技能名称"""
    return sorted(_catalog.keys())


def get_skill_meta(name: str) -> SkillMeta | None:
    """获取指定技能的元数据"""
    return _catalog.get(name)


def build_catalog_prompt() -> str:
    """
    构建技能目录提示词，注入到 system prompt 中。
    这是 Level 1 — LLM 始终看到所有技能的 name + description。
    如果没有技能，返回空字符串。
    """
    if not _catalog:
        return ""

    lines = [
        "\n## 可用技能 (Skills)",
        "以下技能提供专业知识和工作流程。当用户的请求匹配某个技能时，"
        "使用 load_skill 工具加载该技能的完整指令后再回答。",
        "",
    ]
    for name, meta in sorted(_catalog.items()):
        ref_hint = ""
        if meta.references:
            ref_hint = f" (附带参考文档: {', '.join(meta.references)})"
        lines.append(f"- **{name}**: {meta.description}{ref_hint}")

    return "\n".join(lines)


# ──────────────────── Level 2: 加载 SKILL.md 正文 ────────────────────

def load_skill_body(name: str) -> str | None:
    """
    加载指定技能的 SKILL.md 正文（去掉 frontmatter）。
    Level 2 — 只在 LLM 决定触发技能时调用。
    """
    meta = _catalog.get(name)
    if not meta:
        return None
    skill_md = meta.path / "SKILL.md"
    if not skill_md.exists():
        return None
    content = skill_md.read_text(encoding="utf-8")
    _, body = _parse_frontmatter(content)
    return body.strip()


# ──────────────────── Level 3: 加载参考文件 ────────────────────

def load_skill_reference(name: str, filename: str) -> str | None:
    """
    加载指定技能的参考文件内容。
    Level 3 — 只在 LLM 需要深入了解时调用。
    """
    meta = _catalog.get(name)
    if not meta:
        return None
    ref_path = meta.path / "references" / filename
    if not ref_path.exists():
        return None
    # 安全检查：不允许路径穿越
    try:
        ref_path.resolve().relative_to(meta.path.resolve())
    except ValueError:
        return None
    return ref_path.read_text(encoding="utf-8").strip()


def list_skill_references(name: str) -> list[str]:
    """列出指定技能的参考文件"""
    meta = _catalog.get(name)
    if not meta:
        return []
    return meta.references


# ──────────────────── OpenAI 工具定义 ────────────────────

def get_openai_tools() -> list[dict]:
    """
    返回 Skill 系统提供的工具定义（OpenAI function calling 格式）。
    这些工具会和 MCP 工具一起注入到 agentic loop 中。
    """
    if not _catalog:
        return []

    tools = [
        {
            "type": "function",
            "function": {
                "name": "skill__load_skill",
                "description": (
                    "加载指定技能的完整指令。当用户的请求匹配某个技能时调用此工具，"
                    "获取该技能的详细工作流程和指导。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": f"技能名称，可选值: {', '.join(sorted(_catalog.keys()))}",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
    ]

    # 只有存在带 references 的技能时才注册引用加载工具
    has_refs = any(meta.references for meta in _catalog.values())
    if has_refs:
        # 构建所有可用引用的描述
        ref_examples = []
        for sname, meta in sorted(_catalog.items()):
            for ref in meta.references:
                ref_examples.append(f"{sname}/{ref}")

        tools.append({
            "type": "function",
            "function": {
                "name": "skill__load_reference",
                "description": (
                    "加载技能附带的详细参考文档，获取更深入的信息。"
                    "需要先通过 load_skill 了解技能后，再按需加载参考文档。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "技能名称",
                        },
                        "filename": {
                            "type": "string",
                            "description": f"参考文件名，例如: {', '.join(ref_examples[:5])}",
                        },
                    },
                    "required": ["skill_name", "filename"],
                },
            },
        })

    return tools


# ──────────────────── 工具调用分发 ────────────────────

def handle_tool_call(tool_name: str, arguments: dict) -> str | None:
    """
    处理 Skill 系统的工具调用。
    返回结果文本，如果 tool_name 不属于 Skill 系统则返回 None。
    """
    if tool_name == "skill__load_skill":
        name = arguments.get("name", "")
        body = load_skill_body(name)
        if body is None:
            return f"[错误] 技能 '{name}' 不存在。可用技能: {', '.join(sorted(_catalog.keys()))}"
        # 附加 references 信息
        refs = list_skill_references(name)
        if refs:
            body += f"\n\n---\n📎 此技能附带参考文档: {', '.join(refs)}\n可通过 load_reference 工具加载。"
        return body

    if tool_name == "skill__load_reference":
        skill_name = arguments.get("skill_name", "")
        filename = arguments.get("filename", "")
        content = load_skill_reference(skill_name, filename)
        if content is None:
            refs = list_skill_references(skill_name)
            if refs:
                return f"[错误] 参考文件 '{filename}' 不存在。可用文件: {', '.join(refs)}"
            return f"[错误] 技能 '{skill_name}' 不存在或没有参考文件。"
        return content

    return None


# ──────────────────── 摘要（供 /help 等使用） ────────────────────

def list_skills_summary() -> list[str]:
    """返回所有技能的摘要列表"""
    lines = []
    for name, meta in sorted(_catalog.items()):
        desc = meta.description[:60]
        ref_count = len(meta.references)
        ref_info = f" [{ref_count} refs]" if ref_count else ""
        lines.append(f"  • {name} — {desc}{ref_info}")
    return lines
