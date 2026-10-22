"""人格管理路由"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import get_current_user
from ..path_security import confined_path, validate_group_id, validate_name

router = APIRouter()


def _persona_paths(name: str, group_id: str = ""):
    from ...persona.manager import GLOBAL_PERSONA_DIR, GROUP_SESSION_DIR

    validate_name(name)
    global_path = confined_path(GLOBAL_PERSONA_DIR, f"{name}.txt")
    confined_path(GLOBAL_PERSONA_DIR, "_base.txt")
    group_path = None
    if group_id:
        validate_group_id(group_id)
        group_path = confined_path(GROUP_SESSION_DIR, group_id, "personas", f"{name}.txt")
        confined_path(GROUP_SESSION_DIR, group_id, "config.json")
    return global_path, group_path


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
        _persona_paths(name)
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
            validate_group_id(gid)
            confined_path(GROUP_SESSION_DIR, gid, "config.json")
            confined_path(GROUP_SESSION_DIR, gid, "personas")
            active = get_active_persona(gid)
            for pname in list_group_personas(gid):
                _persona_paths(pname, gid)
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
    _persona_paths(name, group_id)
    from ...persona.manager import load_persona_prompt
    prompt = load_persona_prompt(name, group_id or None)
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"人格 '{name}' 不存在")
    return {"name": name, "group_id": group_id, "prompt": prompt}


@router.post("")
async def create_persona(req: PersonaCreateRequest, _user: str = Depends(get_current_user)):
    """创建新人格"""
    from ...persona.manager import persona_exists

    global_path, group_path = _persona_paths(req.name, req.group_id)

    if req.group_id:
        if persona_exists(req.name, req.group_id):
            raise HTTPException(status.HTTP_409_CONFLICT, f"人格 '{req.name}' 已存在")
        group_path.parent.mkdir(parents=True, exist_ok=True)
        group_path.write_text(req.prompt.strip(), encoding="utf-8")
    else:
        path = global_path
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
    global_path, group_path = _persona_paths(name, group_id)
    path = group_path if group_id else global_path

    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"人格 '{name}' 不存在")
    path.write_text(req.prompt.strip(), encoding="utf-8")
    return {"ok": True}


@router.delete("/{name}")
async def delete_persona(name: str, group_id: str = "", _user: str = Depends(get_current_user)):
    """删除人格"""
    global_path, group_path = _persona_paths(name, group_id)
    if name in ("default", "_base"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除默认人格")

    path = group_path if group_id else global_path
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"人格 '{name}' 不存在")
    path.unlink()

    return {"ok": True}


@router.put("/groups/{gid}/active")
async def set_group_active_persona(
    gid: str,
    name: str,
    _user: str = Depends(get_current_user),
):
    """切换群活跃人格"""
    _persona_paths(name, validate_group_id(gid))
    from ...persona.manager import persona_exists, set_active_persona

    if not persona_exists(name, gid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"人格 '{name}' 不存在")
    set_active_persona(gid, name)
    return {"ok": True, "active_persona": name}
