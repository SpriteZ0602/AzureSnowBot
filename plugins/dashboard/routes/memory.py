"""记忆管理路由"""

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..auth import get_current_user

router = APIRouter()

ADMIN_MEMORY = Path("data/admin/MEMORY.md")
GROUPS_DIR = Path("data/sessions/groups")


def _memory_path(scope: str) -> Path:
    """根据 scope 返回 MEMORY.md 路径。scope=admin 或 群号"""
    if scope == "admin":
        return ADMIN_MEMORY
    return GROUPS_DIR / scope / "MEMORY.md"


class MemoryUpdateRequest(BaseModel):
    content: str


class MemorySearchRequest(BaseModel):
    query: str
    max_results: int = 10


@router.get("/scopes")
async def list_memory_scopes(_user: str = Depends(get_current_user)):
    """列举所有可管理的记忆范围"""
    scopes = [{"id": "admin", "label": "Admin 私聊", "exists": ADMIN_MEMORY.exists()}]
    if GROUPS_DIR.is_dir():
        for gdir in sorted(GROUPS_DIR.iterdir()):
            if gdir.is_dir():
                mem = gdir / "MEMORY.md"
                scopes.append({
                    "id": gdir.name,
                    "label": f"群 {gdir.name}",
                    "exists": mem.exists(),
                })
    return scopes


@router.get("/content")
async def get_memory_content(
    scope: str = Query("admin"),
    _user: str = Depends(get_current_user),
):
    """读取 MEMORY.md 原文"""
    path = _memory_path(scope)
    if not path.exists():
        return {"scope": scope, "content": ""}
    return {"scope": scope, "content": path.read_text(encoding="utf-8")}


@router.put("/content")
async def update_memory_content(
    req: MemoryUpdateRequest,
    scope: str = Query("admin"),
    _user: str = Depends(get_current_user),
):
    """更新 MEMORY.md 并刷新索引"""
    path = _memory_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(req.content, encoding="utf-8")

    # 仅 admin 记忆触发索引刷新
    if scope == "admin":
        try:
            from ...memory.indexer import sync_index
            await sync_index()
        except Exception:
            pass

    return {"ok": True}


@router.post("/search")
async def search_memory(
    req: MemorySearchRequest,
    _user: str = Depends(get_current_user),
):
    """语义搜索记忆（仅 admin）"""
    try:
        from ...memory.indexer import search
        results = await search(req.query, max_results=req.max_results)
        return {
            "query": req.query,
            "results": [
                {
                    "text": r.get("text", ""),
                    "source": r.get("source", ""),
                    "score": round(r.get("score", 0), 4),
                }
                for r in results
            ],
        }
    except Exception as e:
        return {"query": req.query, "results": [], "error": str(e)}


@router.get("/index-status")
async def get_index_status(_user: str = Depends(get_current_user)):
    """获取记忆索引状态"""
    import json

    index_file = Path("data/admin/.memory_index.json")
    if not index_file.exists():
        return {"exists": False, "chunks": 0}
    try:
        data = json.loads(index_file.read_text(encoding="utf-8"))
        chunks = data.get("chunks", [])
        return {
            "exists": True,
            "chunks": len(chunks),
            "sources": list({c.get("source", "") for c in chunks}),
        }
    except (json.JSONDecodeError, OSError):
        return {"exists": False, "chunks": 0}


@router.get("/structured")
async def get_structured_memories(
    type_filter: str = Query("", description="按类型过滤: identity/preference/fact/task/emotion"),
    keyword: str = Query("", description="按关键词过滤"),
    limit: int = Query(100, ge=1, le=500),
    _user: str = Depends(get_current_user),
):
    """查看结构化记忆条目"""
    from ...memory.structured import search_memories

    memories_path = Path("data/admin/memories.jsonl")
    entries = search_memories(
        memories_path,
        type_filter=type_filter,
        keyword=keyword,
        limit=limit,
    )

    # 统计各类型数量
    from ...memory.structured import load_memories
    all_entries = load_memories(memories_path)
    type_counts: dict[str, int] = {}
    for e in all_entries:
        t = e.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "total": len(all_entries),
        "type_counts": type_counts,
        "entries": entries,
    }
