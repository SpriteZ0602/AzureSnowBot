"""对话历史路由"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from ..auth import get_current_user

router = APIRouter()

ADMIN_HISTORY = Path("data/admin/history.jsonl")
GROUPS_DIR = Path("data/sessions/groups")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    results = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return results


@router.get("/admin")
async def get_admin_history(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    _user: str = Depends(get_current_user),
):
    """分页读取 Admin 私聊历史"""
    all_msgs = _load_jsonl(ADMIN_HISTORY)
    total = len(all_msgs)
    # 倒序分页（最新的在前）
    all_msgs.reverse()
    start = (page - 1) * size
    end = start + size
    return {
        "total": total,
        "page": page,
        "size": size,
        "messages": all_msgs[start:end],
    }


@router.get("/groups")
async def list_groups(_user: str = Depends(get_current_user)):
    """列举所有群聊及其最后活跃时间"""
    groups = []
    if GROUPS_DIR.is_dir():
        for gdir in sorted(GROUPS_DIR.iterdir()):
            if not gdir.is_dir():
                continue
            cfg_path = gdir / "config.json"
            config = {}
            if cfg_path.exists():
                try:
                    config = json.loads(cfg_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            # 找到该群下所有人格历史文件
            personas = sorted(
                p.stem for p in gdir.glob("*.jsonl")
                if p.stem != "_chatlog"
            )
            groups.append({
                "group_id": gdir.name,
                "active_persona": config.get("active_persona", "default"),
                "last_message_at": config.get("last_message_at", ""),
                "personas": personas,
            })
    return groups


@router.get("/groups/{gid}")
async def get_group_history(
    gid: str,
    persona: str = Query("default"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    _user: str = Depends(get_current_user),
):
    """分页读取群聊对话历史"""
    path = GROUPS_DIR / gid / f"{persona}.jsonl"
    all_msgs = _load_jsonl(path)
    total = len(all_msgs)
    all_msgs.reverse()
    start = (page - 1) * size
    end = start + size
    return {
        "group_id": gid,
        "persona": persona,
        "total": total,
        "page": page,
        "size": size,
        "messages": all_msgs[start:end],
    }


@router.get("/groups/{gid}/chatlog")
async def get_group_chatlog(
    gid: str,
    hours: float = Query(24, ge=1, le=168),
    user_name: str = Query(""),
    keyword: str = Query(""),
    limit: int = Query(200, ge=1, le=1000),
    _user: str = Depends(get_current_user),
):
    """检索群聊全量记录"""
    from ...group.chatlog import load_chatlog

    records = load_chatlog(
        gid,
        hours=hours,
        user_name=user_name or None,
        keyword=keyword or None,
        limit=limit,
    )
    return {
        "group_id": gid,
        "count": len(records),
        "records": records,
    }
