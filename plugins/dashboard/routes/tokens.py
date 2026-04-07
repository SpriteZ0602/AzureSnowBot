"""Token 统计路由"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query

from ..auth import get_current_user

router = APIRouter()

# Gemini 3 Flash 定价 (USD / 1M tokens)
_PRICE_INPUT = 0.5
_PRICE_OUTPUT = 3.0


@router.get("/daily")
async def get_daily_stats(
    days: int = Query(30, ge=1, le=365),
    _user: str = Depends(get_current_user),
):
    """按天聚合 Token 用量"""
    from ...token_stats import _stats, _lock

    today = datetime.now().date()
    result = []
    with _lock:
        for i in range(days):
            date_str = (today - timedelta(days=i)).isoformat()
            day_data = _stats.get(date_str, {})
            prompt = sum(s.get("prompt", 0) for s in day_data.values())
            completion = sum(s.get("completion", 0) for s in day_data.values())
            total = sum(s.get("total", 0) for s in day_data.values())
            calls = sum(s.get("calls", 0) for s in day_data.values())
            if total > 0 or calls > 0:
                result.append({
                    "date": date_str,
                    "prompt": prompt,
                    "completion": completion,
                    "total": total,
                    "calls": calls,
                })
    result.reverse()
    return result


@router.get("/by-source")
async def get_by_source(
    date: str = Query("", description="YYYY-MM-DD，空则取今天"),
    _user: str = Depends(get_current_user),
):
    """按来源分类 Token 用量"""
    from ...token_stats import _stats, _lock

    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        day_data = _stats.get(date, {})
    return {
        "date": date,
        "sources": dict(day_data),
    }


@router.get("/cost")
async def get_cost(
    days: int = Query(30, ge=1, le=365),
    _user: str = Depends(get_current_user),
):
    """费用趋势"""
    from ...token_stats import _stats, _lock

    today = datetime.now().date()
    result = []
    with _lock:
        for i in range(days):
            date_str = (today - timedelta(days=i)).isoformat()
            day_data = _stats.get(date_str, {})
            prompt = sum(s.get("prompt", 0) for s in day_data.values())
            completion = sum(s.get("completion", 0) for s in day_data.values())
            cost = (prompt / 1_000_000 * _PRICE_INPUT) + (completion / 1_000_000 * _PRICE_OUTPUT)
            if cost > 0:
                result.append({
                    "date": date_str,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "cost_usd": round(cost, 4),
                })
    result.reverse()
    return result
