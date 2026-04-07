"""配置编辑路由"""

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import get_current_user

router = APIRouter()

ENV_FILE = Path(".env")
ADMIN_DIR = Path("data/admin")

# Admin 可编辑的上下文文件白名单
_ADMIN_FILES = {"SOUL.md", "AGENTS.md", "USER.md", "MEMORY.md", "HEARTBEAT.md"}

# .env 中需要脱敏的 key 模式
_SENSITIVE_RE = re.compile(r"(api_key|secret|password)", re.IGNORECASE)


class EnvUpdateRequest(BaseModel):
    key: str
    value: str


class FileUpdateRequest(BaseModel):
    content: str


@router.get("/env")
async def get_env(_user: str = Depends(get_current_user)):
    """读取 .env 配置（敏感值脱敏）"""
    if not ENV_FILE.exists():
        return {"entries": []}

    entries = []
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            entries.append({"raw": line, "key": "", "value": "", "is_comment": True})
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            # 脱敏处理
            display_value = value
            if _SENSITIVE_RE.search(key) and value:
                display_value = value[:8] + "****" + value[-4:] if len(value) > 12 else "****"
            entries.append({
                "raw": line,
                "key": key,
                "value": display_value,
                "is_comment": False,
                "is_sensitive": bool(_SENSITIVE_RE.search(key)),
            })
        else:
            entries.append({"raw": line, "key": "", "value": "", "is_comment": True})
    return {"entries": entries}


@router.put("/env")
async def update_env(req: EnvUpdateRequest, _user: str = Depends(get_current_user)):
    """更新 .env 中的指定配置项"""
    if not ENV_FILE.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, ".env 文件不存在")

    content = ENV_FILE.read_text(encoding="utf-8")
    lines = content.splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, _ = stripped.partition("=")
            if key.strip() == req.key:
                lines[i] = f"{req.key}={req.value}"
                found = True
                break
    if not found:
        lines.append(f"{req.key}={req.value}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "note": "配置已更新，部分设置需要重启 Bot 生效"}


@router.get("/admin")
async def list_admin_files(_user: str = Depends(get_current_user)):
    """列举 Admin 上下文文件"""
    files = []
    for fname in sorted(_ADMIN_FILES):
        path = ADMIN_DIR / fname
        files.append({
            "filename": fname,
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
        })
    return files


@router.get("/admin/{filename}")
async def get_admin_file(filename: str, _user: str = Depends(get_current_user)):
    """读取指定 Admin 上下文文件"""
    if filename not in _ADMIN_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不允许访问文件 '{filename}'")
    path = ADMIN_DIR / filename
    if not path.exists():
        return {"filename": filename, "content": ""}
    return {"filename": filename, "content": path.read_text(encoding="utf-8")}


@router.put("/admin/{filename}")
async def update_admin_file(
    filename: str,
    req: FileUpdateRequest,
    _user: str = Depends(get_current_user),
):
    """更新指定 Admin 上下文文件"""
    if filename not in _ADMIN_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不允许编辑文件 '{filename}'")
    path = ADMIN_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(req.content, encoding="utf-8")
    return {"ok": True}
