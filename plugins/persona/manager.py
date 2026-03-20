"""
Persona 管理模块
────────────────
双层人格体系：
  - 通用人格: data/personas/<name>.txt             → 所有群共享
  - 群私有人格: data/sessions/groups/<gid>/personas/<name>.txt  → 仅该群可见

查找优先级：群私有 > 通用（同名时群的覆盖通用）

目录结构:
  data/personas/<name>.txt                        → 通用人格
  data/sessions/groups/<gid>/
      _active.json                                → {"persona": "default"}
      personas/<name>.txt                         → 群私有人格
      <persona>.jsonl                             → 该人格下的对话历史
"""

import json
from pathlib import Path
from nonebot.log import logger

# ──────────────────── 路径常量 ────────────────────
GLOBAL_PERSONA_DIR = Path("data/personas")
GROUP_SESSION_DIR = Path("data/sessions/groups")

GLOBAL_PERSONA_DIR.mkdir(parents=True, exist_ok=True)
GROUP_SESSION_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_PERSONA = "default"


# ──────────────────── 路径工具 ────────────────────

def _group_persona_dir(group_id: str) -> Path:
    d = GROUP_SESSION_DIR / group_id / "personas"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ──────────────────── Persona 定义 ────────────────────

def list_global_personas() -> list[str]:
    """返回所有通用人格名称"""
    return sorted(p.stem for p in GLOBAL_PERSONA_DIR.glob("*.txt"))


def list_group_personas(group_id: str) -> list[str]:
    """返回指定群的私有人格名称"""
    d = _group_persona_dir(group_id)
    return sorted(p.stem for p in d.glob("*.txt"))


def list_personas(group_id: str) -> list[str]:
    """返回该群可用的所有人格（通用 + 群私有，去重排序）"""
    all_names = set(list_global_personas()) | set(list_group_personas(group_id))
    return sorted(all_names)


def persona_exists(name: str, group_id: str | None = None) -> bool:
    """检查人格是否存在（群私有优先）"""
    if group_id:
        if (_group_persona_dir(group_id) / f"{name}.txt").exists():
            return True
    return (GLOBAL_PERSONA_DIR / f"{name}.txt").exists()


def is_group_persona(name: str, group_id: str) -> bool:
    """检查是否为群私有人格"""
    return (_group_persona_dir(group_id) / f"{name}.txt").exists()


def is_global_persona(name: str) -> bool:
    """检查是否为通用人格"""
    return (GLOBAL_PERSONA_DIR / f"{name}.txt").exists()


def load_persona_prompt(name: str, group_id: str | None = None) -> str | None:
    """加载人格 prompt，自动拼装 _base.txt 公共指令 + 人格内容"""
    # 加载公共基底提示词
    base_path = GLOBAL_PERSONA_DIR / "_base.txt"
    base_prompt = ""
    if base_path.exists():
        base_prompt = base_path.read_text(encoding="utf-8").strip()

    # 加载人格提示词（群私有优先）
    persona_prompt = None
    if group_id:
        group_path = _group_persona_dir(group_id) / f"{name}.txt"
        if group_path.exists():
            persona_prompt = group_path.read_text(encoding="utf-8").strip()
    if persona_prompt is None:
        global_path = GLOBAL_PERSONA_DIR / f"{name}.txt"
        if global_path.exists():
            persona_prompt = global_path.read_text(encoding="utf-8").strip()
    if persona_prompt is None:
        return None

    # 拼装：人格内容 + 公共指令
    if base_prompt:
        return f"{persona_prompt}\n\n{base_prompt}"
    return persona_prompt


# ──────────────────── 群私有人格增删 ────────────────────

def create_group_persona(group_id: str, name: str, prompt: str) -> None:
    """创建群私有人格"""
    path = _group_persona_dir(group_id) / f"{name}.txt"
    path.write_text(prompt.strip(), encoding="utf-8")


def delete_group_persona(group_id: str, name: str) -> bool:
    """删除群私有人格，返回是否成功"""
    path = _group_persona_dir(group_id) / f"{name}.txt"
    if path.exists():
        path.unlink()
        return True
    return False


# ──────────────────── 群激活状态 ────────────────────

def _group_dir(group_id: str) -> Path:
    d = GROUP_SESSION_DIR / group_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _active_path(group_id: str) -> Path:
    return _group_dir(group_id) / "_active.json"


def _session_path(group_id: str, persona_name: str) -> Path:
    return _group_dir(group_id) / f"{persona_name}.jsonl"


def get_active_persona(group_id: str) -> str:
    """获取该群当前激活的人格名称"""
    path = _active_path(group_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("persona", DEFAULT_PERSONA)
        except (json.JSONDecodeError, KeyError):
            pass
    return DEFAULT_PERSONA


def set_active_persona(group_id: str, persona_name: str) -> None:
    """设置该群激活的人格"""
    path = _active_path(group_id)
    path.write_text(
        json.dumps({"persona": persona_name}, ensure_ascii=False),
        encoding="utf-8",
    )


# ──────────────────── 会话持久化（按 persona 隔离） ────────────────────

def load_history(group_id: str, persona_name: str | None = None) -> list[dict]:
    """加载指定群 + 人格的对话历史"""
    if persona_name is None:
        persona_name = get_active_persona(group_id)
    path = _session_path(group_id, persona_name)
    if not path.exists():
        return []
    messages = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


def append_message(group_id: str, message: dict, persona_name: str | None = None) -> None:
    """追加消息到指定群 + 人格的历史"""
    if persona_name is None:
        persona_name = get_active_persona(group_id)
    path = _session_path(group_id, persona_name)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


def clear_history(group_id: str, persona_name: str | None = None) -> None:
    """清除指定群 + 人格的对话历史"""
    if persona_name is None:
        persona_name = get_active_persona(group_id)
    path = _session_path(group_id, persona_name)
    if path.exists():
        path.unlink()
