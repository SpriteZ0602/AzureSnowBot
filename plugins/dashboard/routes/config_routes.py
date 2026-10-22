"""配置编辑路由"""

import re
from io import StringIO
from pathlib import Path

from dotenv import set_key
from dotenv.parser import parse_stream
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from ..auth import get_current_user
from ..path_security import confined_path

router = APIRouter()

ENV_FILE = Path(".env")
ADMIN_DIR = Path("data/admin")

# Admin 可编辑的上下文文件白名单
_ADMIN_FILES = {"SOUL.md", "AGENTS.md", "USER.md", "MEMORY.md", "HEARTBEAT.md"}

# .env 中需要脱敏的 key 模式
_SENSITIVE_RE = re.compile(r"(api.?key|secret|password|token|credential|authorization|private.?key)", re.IGNORECASE)


class EnvUpdateRequest(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128)
    value: str = Field(max_length=20000)

    @field_validator("value")
    @classmethod
    def single_line(cls, value: str) -> str:
        if any(ch in value for ch in "\r\n\x00"):
            raise ValueError("配置值不能包含换行或空字符")
        return value


class FileUpdateRequest(BaseModel):
    content: str


@router.get("/env")
async def get_env(_user: str = Depends(get_current_user)):
    """读取 .env 配置（敏感值脱敏）"""
    if not ENV_FILE.exists():
        return {"entries": []}

    entries = []
    for binding in parse_stream(StringIO(ENV_FILE.read_text(encoding="utf-8"))):
        if binding.key and not binding.error:
            key, value = binding.key, binding.value or ""
            sensitive = bool(_SENSITIVE_RE.search(key))
            display_value = "****" if sensitive and value else value
            entries.append({
                "raw": f"{key}={display_value}",
                "key": key,
                "value": display_value,
                "is_comment": False,
                "is_sensitive": sensitive,
            })
        else:
            # 不返回解析失败的原文，防止多行凭据从错误行泄露。
            raw = "[无法解析的配置行]" if binding.error else binding.original.string.rstrip("\r\n")
            entries.append({"raw": raw, "key": "", "value": "", "is_comment": True})
    return {"entries": entries}


@router.put("/env")
async def update_env(req: EnvUpdateRequest, _user: str = Depends(get_current_user)):
    """更新 .env 中的指定配置项"""
    if not ENV_FILE.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, ".env 文件不存在")

    set_key(str(ENV_FILE), req.key, req.value, quote_mode="always")
    return {"ok": True, "note": "配置已更新，部分设置需要重启 Bot 生效"}


@router.get("/admin")
async def list_admin_files(_user: str = Depends(get_current_user)):
    """列举 Admin 上下文文件"""
    files = []
    for fname in sorted(_ADMIN_FILES):
        path = confined_path(ADMIN_DIR, fname)
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
    path = confined_path(ADMIN_DIR, filename)
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
    path = confined_path(ADMIN_DIR, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(req.content, encoding="utf-8")
    return {"ok": True}
