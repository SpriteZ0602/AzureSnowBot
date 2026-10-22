"""图库管理和图片读取均使用 Dashboard 的 JWT 依赖。"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from ..auth import get_current_user
from ... import stickers as library

router = APIRouter(dependencies=[Depends(get_current_user)])


class TagsUpdate(BaseModel):
    tags: list[str] = Field(min_length=1, max_length=5)


class SettingsUpdate(BaseModel):
    enabled: bool


def _validate(gid: str):
    try:
        library.group_dir(gid)
    except library.StickerError as exc:
        raise HTTPException(400, str(exc)) from exc


def _mutate(fn, *args):
    try:
        return fn(*args)
    except library.StickerError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "图库文件暂时不可写，请稍后重试") from exc


def _public(item: dict) -> dict:
    return {key: item.get(key) for key in ("id", "tags", "description", "uploader", "size", "mime", "created_at", "last_used_at")}


@router.get("/groups")
async def list_groups():
    session_root = Path("data/sessions/groups")
    gids = set(library.groups()) | {p.name for p in session_root.glob("*") if p.is_dir() and p.name.isascii() and p.name.isdigit()}
    rows = []
    for gid in sorted(gids):
        entries = library.items(gid)
        rows.append({"group_id": gid, "count": len(entries), "bytes": sum(i["size"] for i in entries),
                     "enabled": library.enabled(gid), "tags": library.tag_counts(gid)})
    return {"groups": rows, "total_bytes": library.total_bytes(), "max_total_bytes": library.MAX_TOTAL_BYTES,
            "max_count": library.MAX_COUNT}


@router.get("/{gid}")
async def list_stickers(gid: str, tag: str = "", page: int = Query(1, ge=1), page_size: int = Query(24, ge=1, le=50)):
    _validate(gid)
    entries = library.items(gid)
    matches = [i for i in entries if not tag or tag in i["tags"]]
    matches.sort(key=lambda i: i["created_at"], reverse=True)
    return {"items": [_public(i) for i in matches[(page - 1) * page_size:page * page_size]],
            "total": len(matches), "tags": library.tag_counts(gid), "count": len(entries),
            "bytes": sum(i["size"] for i in entries), "enabled": library.enabled(gid)}


@router.get("/{gid}/{sticker_id}/image")
async def get_image(gid: str, sticker_id: str):
    _validate(gid)
    try:
        item, content = library.read_image(gid, sticker_id)
    except (library.StickerError, OSError) as exc:
        raise HTTPException(404, "这张表情已经删除或文件不存在") from exc
    return Response(content=content, media_type=item["mime"], headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


@router.patch("/{gid}/{sticker_id}")
async def update_tags(gid: str, sticker_id: str, body: TagsUpdate):
    _validate(gid)
    return _public(_mutate(library.update_tags, gid, sticker_id, body.tags))


@router.delete("/{gid}/{sticker_id}")
async def delete_sticker(gid: str, sticker_id: str):
    _validate(gid)
    return {"deleted": _mutate(library.delete, gid, sticker_id)}


@router.put("/{gid}/settings")
async def update_settings(gid: str, body: SettingsUpdate):
    _validate(gid)
    _mutate(library.set_enabled, gid, body.enabled)
    return {"enabled": library.enabled(gid)}
