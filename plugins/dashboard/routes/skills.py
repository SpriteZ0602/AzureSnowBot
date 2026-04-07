"""技能管理路由"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import get_current_user

router = APIRouter()

SKILLS_DIR = Path("data/skills")
ADMIN_SKILLS_DIR = Path("data/admin_skills")


class SkillCreateRequest(BaseModel):
    name: str
    description: str
    body: str
    admin_only: bool = False


class SkillUpdateRequest(BaseModel):
    description: str = ""
    body: str = ""


@router.get("")
async def list_skills(_user: str = Depends(get_current_user)):
    """列举所有技能"""
    from ...skill.manager import get_catalog

    catalog = get_catalog()
    return [
        {
            "name": meta.name,
            "description": meta.description,
            "admin_only": meta.admin_only,
            "references": meta.references,
        }
        for meta in sorted(catalog.values(), key=lambda m: m.name)
    ]


@router.get("/{name}")
async def get_skill(name: str, _user: str = Depends(get_current_user)):
    """读取技能 SKILL.md 完整内容"""
    from ...skill.manager import load_skill_body, get_skill_meta

    meta = get_skill_meta(name)
    if not meta:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"技能 '{name}' 不存在")
    body = load_skill_body(name)
    return {
        "name": meta.name,
        "description": meta.description,
        "admin_only": meta.admin_only,
        "references": meta.references,
        "body": body or "",
    }


@router.post("")
async def create_skill(req: SkillCreateRequest, _user: str = Depends(get_current_user)):
    """创建新技能"""
    base_dir = ADMIN_SKILLS_DIR if req.admin_only else SKILLS_DIR
    skill_dir = base_dir / req.name
    if skill_dir.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, f"技能 '{req.name}' 已存在")

    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {req.name}\ndescription: {req.description}\n---\n\n{req.body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    # 重新扫描技能目录
    from ...skill.manager import scan_skills
    scan_skills()

    return {"ok": True, "name": req.name}


@router.put("/{name}")
async def update_skill(name: str, req: SkillUpdateRequest, _user: str = Depends(get_current_user)):
    """更新技能 SKILL.md"""
    from ...skill.manager import get_skill_meta

    meta = get_skill_meta(name)
    if not meta:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"技能 '{name}' 不存在")

    desc = req.description or meta.description
    content = f"---\nname: {name}\ndescription: {desc}\n---\n\n{req.body}"
    (meta.path / "SKILL.md").write_text(content, encoding="utf-8")

    from ...skill.manager import scan_skills
    scan_skills()

    return {"ok": True}


@router.delete("/{name}")
async def delete_skill(name: str, _user: str = Depends(get_current_user)):
    """删除技能"""
    from ...skill.manager import get_skill_meta

    meta = get_skill_meta(name)
    if not meta:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"技能 '{name}' 不存在")

    import shutil
    shutil.rmtree(meta.path)

    from ...skill.manager import scan_skills
    scan_skills()

    return {"ok": True}
