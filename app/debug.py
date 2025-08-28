from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.session import get_async_session
from app.calendar.google import GoogleCalendarProvider


router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/google/events")
async def list_google_events(
    time_from: str = Query(..., alias="from"),
    time_to: str = Query(..., alias="to"),
    user_id: Optional[int] = Query(None),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        from_dt = datetime.fromisoformat(time_from.replace("Z", "+00:00"))
        to_dt = datetime.fromisoformat(time_to.replace("Z", "+00:00"))
        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=timezone.utc)
        if to_dt.tzinfo is None:
            to_dt = to_dt.replace(tzinfo=timezone.utc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid datetime: {exc}")

    # Pick user
    if user_id is not None:
        res = await session.execute(select(User).where(User.id == user_id))
        user = res.scalar_one_or_none()
    else:
        res = await session.execute(select(User).order_by(User.id.asc()).limit(1))
        user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    provider = GoogleCalendarProvider(session)
    events = await provider.get_events(user, from_dt, to_dt)
    return [e.model_dump() for e in events]


@router.post("/sync")
async def run_sync(session: AsyncSession = Depends(get_async_session)):
    from app.tasks.scheduler import sync_google_events

    try:
        await sync_google_events()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        # Вернуть текст ошибки для диагностики
        return {"status": "error", "detail": str(exc)}


