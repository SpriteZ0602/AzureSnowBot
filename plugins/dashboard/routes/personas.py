"""人格管理路由"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import get_current_user

router = APIRouter()


class PersonaCreateRequest(BaseModel):
    name: str
    prompt: str
    group_id: str = ""  # 空 = 通用人格


class PersonaUpdateRequest(BaseModel):
    prompt: str


@router.get("")
async def list_all_personas(_user: str = Depends(get_current_user)):
    """列举所有人格（通用 + 各群私有）"""
    from ...persona.manager import (
        list_global_personas,
        list_group_personas,
        load_persona_prompt,
        get_active_persona,
        GLOBAL_PERSONA_DIR,
        GROUP_SESSION_DIR,
    )

    # 通用人格
    global_list = []
    for name in list_global_personas():
        if name == "_base":
            continue
        prompt = load_persona_prompt(name)
        global_list.append({
            "name": name,
            "scope": "global",
            "prompt_preview": (prompt or "")[:200],
        })

    # 群私有人格
    group_list = []
    if GROUP_SESSION_DIR.is_dir():
        for gdir in GROUP_SESSION_DIR.iterdir():
            if not gdir.is_dir():
                continue
            gid = gdir.name
            active = get_active_persona(gid)
            for pname in list_group_personas(gid):
                prompt = load_persona_prompt(pname, gid)
                group_list.append({
                    "name": pname,
                    "scope": "group",
                    "group_id": gid,
                    "is_active": pname == active,
                    "prompt_preview": (prompt or "")[:200],
                })

    return {
        "global": global_list,
        "group": group_list,
    }


@router.get("/{name}")
async def get_persona(name: str, group_id: str = "", _user: str = Depends(get_current_user)):
    """读取指定人格的完整 prompt"""
    from ...persona.manager import load_persona_prompt
    prompt = load_persona_prompt(name, group_id or None)
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"人格 '{name}' 不存在")
    return {"name": name, "group_id": group_id, "prompt": prompt}


@router.post("")
async def create_persona(req: PersonaCreateRequest, _user: str = Depends(get_current_user)):
    """创建新人格"""
    from ...persona.manager import persona_exists, create_group_persona, GLOBAL_PERSONA_DIR

    if req.group_id:
        if persona_exists(req.name, req.group_id):
            raise HTTPException(status.HTTP_409_CONFLICT, f"人格 '{req.name}' 已存在")
        create_group_persona(req.group_id, req.name, req.prompt)
    else:
        path = GLOBAL_PERSONA_DIR / f"{req.name}.txt"
        if path.exists():
            raise HTTPException(status.HTTP_409_CONFLICT, f"通用人格 '{req.name}' 已存在")
        path.write_text(req.prompt.strip(), encoding="utf-8")

    return {"ok": True, "name": req.name}


@router.put("/{name}")
async def update_persona(
    name: str,
    req: PersonaUpdateRequest,
    group_id: str = "",
    _user: str = Depends(get_current_user),
):
    """更新人格 prompt"""
    from ...persona.manager import GLOBAL_PERSONA_DIR, _group_persona_dir

    if group_id:
        path = _group_persona_dir(group_id) / f"{name}.txt"
    else:
        path = GLOBAL_PERSONA_DIR / f"{name}.txt"

    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"人格 '{name}' 不存在")
    path.write_text(req.prompt.strip(), encoding="utf-8")
    return {"ok": True}


@router.delete("/{name}")
async def delete_persona(name: str, group_id: str = "", _user: str = Depends(get_current_user)):
    """删除人格"""
    if name in ("default", "_base"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除默认人格")

    from ...persona.manager import delete_group_persona, GLOBAL_PERSONA_DIR

    if group_id:
        if not delete_group_persona(group_id, name):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"群私有人格 '{name}' 不存在")
    else:
        path = GLOBAL_PERSONA_DIR / f"{name}.txt"
        if not path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"通用人格 '{name}' 不存在")
        path.unlink()

    return {"ok": True}


@router.put("/groups/{gid}/active")
async def set_group_active_persona(
    gid: str,
    name: str,
    _user: str = Depends(get_current_user),
):
    """切换群活跃人格"""
    from ...persona.manager import persona_exists, set_active_persona

    if not persona_exists(name, gid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"人格 '{name}' 不存在")
    set_active_persona(gid, name)
    return {"ok": True, "active_persona": name}
