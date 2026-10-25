"""记忆管理路由"""

from pathlib import Path
from botcore.storage import atomic_write_text

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..auth import get_current_user
from ..path_security import confined_path, validate_group_id

router = APIRouter()

ADMIN_MEMORY = Path("data/admin/MEMORY.md")

# 枚举"已知群"用的目录：群只要有过会话就会存在，即使还没产生记忆文件。
# 注意它和群记忆的存储目录不是同一个 —— 记忆在 data/groups/<群号>/。
SESSION_DIR = Path("data/sessions/groups")


def _memory_path(scope: str) -> Path:
    """根据 scope 返回 MEMORY.md 路径。scope=admin 或 群号。

    群记忆路径统一走 persona.manager.group_memory_path，与 Bot 侧共用同一份
    定义（data/groups/<群号>/MEMORY.md）。之前这里写成了
    data/sessions/groups/<群号>/MEMORY.md，导致 Dashboard 编辑的群记忆
    存不到 Bot 会读的位置。
    """
    if scope == "admin":
        return confined_path(ADMIN_MEMORY.parent, ADMIN_MEMORY.name)
    from ...persona.manager import GROUP_MEMORY_DIR
    return confined_path(GROUP_MEMORY_DIR, validate_group_id(scope), "MEMORY.md")


class MemoryUpdateRequest(BaseModel):
    content: str


class MemorySearchRequest(BaseModel):
    query: str
    max_results: int = 10


@router.get("/scopes")
async def list_memory_scopes(_user: str = Depends(get_current_user)):
    """列举所有可管理的记忆范围

    群列表从会话目录枚举（有会话的群都列出来，即使还没产生记忆文件），
    但 exists 标记的是真正的记忆文件位置。
    """
    scopes = [{"id": "admin", "label": "Admin 私聊", "exists": ADMIN_MEMORY.exists()}]
    if SESSION_DIR.is_dir():
        for gdir in sorted(SESSION_DIR.iterdir()):
            if gdir.is_dir():
                scopes.append({
                    "id": gdir.name,
                    "label": f"群 {gdir.name}",
                    "exists": _memory_path(gdir.name).exists(),
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
    atomic_write_text(path, req.content)

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
