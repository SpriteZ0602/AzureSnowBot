"""提醒管理路由"""

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import get_current_user

router = APIRouter()


@router.get("")
async def list_reminders(_user: str = Depends(get_current_user)):
    """列举所有提醒"""
    from ...reminder.scheduler import get_all_reminders
    jobs = get_all_reminders()
    return [
        {
            "id": j.id,
            "chat_type": j.chat_type,
            "target_id": j.target_id,
            "user_id": j.user_id,
            "creator_name": j.creator_name,
            "message": j.message,
            "fire_at": j.fire_at,
            "created_at": j.created_at,
            "recurring": j.recurring,
            "daily_time": j.daily_time,
        }
        for j in jobs
    ]


@router.delete("/{job_id}")
async def cancel_reminder(job_id: str, _user: str = Depends(get_current_user)):
    """取消提醒"""
    from ...reminder.scheduler import cancel_reminder
    ok = cancel_reminder(job_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"提醒 '{job_id}' 不存在")
    return {"ok": True}
