from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.google import GoogleCalendarProvider
from app.db.models import Meeting, Notification, OAuthToken, User
from app.db.session import session_factory
from app.bot import build_bot
from aiogram import types


async def _upsert_meeting(
    session: AsyncSession,
    user: User,
    event_id: str,
    title: str | None,
    start_at: datetime,
    end_at: datetime | None,
    location: str | None,
    description: str | None,
) -> Meeting:
    result = await session.execute(
        select(Meeting).where(Meeting.user_id == user.id, Meeting.external_id == event_id)
    )
    meeting: Meeting | None = result.scalar_one_or_none()
    if meeting is None:
        meeting = Meeting(
            user_id=user.id,
            title=title,
            start_at=start_at,
            end_at=end_at,
            location=location,
            description=description,
            external_id=event_id,
        )
        session.add(meeting)
        await session.flush()
    else:
        meeting.title = title
        meeting.start_at = start_at
        meeting.end_at = end_at
        meeting.location = location
        meeting.description = description
    return meeting


async def _ensure_notification(
    session: AsyncSession, meeting_id: int, user_id: int, scheduled_at: datetime
) -> None:
    # не создавать прошедшие напоминания
    if scheduled_at <= datetime.now(timezone.utc):
        return
    existing = await session.execute(
        select(Notification).where(
            Notification.meeting_id == meeting_id,
            Notification.scheduled_at == scheduled_at,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return
    session.add(
        Notification(
            user_id=user_id,
            meeting_id=meeting_id,
            scheduled_at=scheduled_at,
            status=None,
            channel="telegram",
        )
    )


async def sync_google_events() -> None:
    now = datetime.now(timezone.utc)
    window_days = int(os.getenv("SYNC_WINDOW_DAYS", "14"))
    time_min = now - timedelta(days=1)
    time_max = now + timedelta(days=window_days)

    async with session_factory() as session:
        # пользователи у которых есть google токен
        res = await session.execute(
            select(User).join(OAuthToken).where(OAuthToken.provider == "google")
        )
        users = res.scalars().all()

        for user in users:
            provider = GoogleCalendarProvider(session)
            events = await provider.get_events(user, time_min, time_max)
            for e in events:
                meeting = await _upsert_meeting(
                    session,
                    user,
                    e.id,
                    e.title,
                    e.start_at,
                    getattr(e, "end_at", None),
                    getattr(e, "location", None),
                    getattr(e, "description", None),
                )
                # напоминания: -1 день, -1 час от начала
                await _ensure_notification(session, meeting.id, user.id, e.start_at - timedelta(days=1))
                await _ensure_notification(session, meeting.id, user.id, e.start_at - timedelta(hours=1))

        await session.commit()


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    interval_minutes = int(os.getenv("SCHEDULER_INTERVAL_MINUTES", "10"))
    scheduler.add_job(sync_google_events, "interval", minutes=interval_minutes, id="sync-google")
    # джоб на отправку уведомлений каждую минуту
    scheduler.add_job(process_notifications, "interval", minutes=1, id="notify")
    return scheduler


async def process_notifications() -> None:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        # Выбрать запланированные и не отправленные
        res = await session.execute(
            select(Notification, User, Meeting)
            .join(User, User.id == Notification.user_id)
            .join(Meeting, Meeting.id == Notification.meeting_id)
            .where(Notification.scheduled_at <= now, Notification.sent_at.is_(None))
            .order_by(Notification.scheduled_at.asc())
            .limit(20)
        )
        rows = res.all()

        if not rows:
            return

        bot = build_bot()

        for notif, user, meeting in rows:
            # Требуется tg_id для отправки
            if user.tg_id is None:
                continue
            start_str = meeting.start_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if meeting.start_at else "?"
            title = meeting.title or "(без названия)"

            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [
                    types.InlineKeyboardButton(text="Напомнить через 15 мин", callback_data=f"snooze:{notif.id}:15"),
                    types.InlineKeyboardButton(text="Ок", callback_data=f"ack:{notif.id}"),
                ]
            ])

            text = f"Напоминание: {title}\nНачало: {start_str}"
            try:
                await bot.send_message(chat_id=user.tg_id, text=text, reply_markup=kb)
            except Exception:
                # не фейлим батч, просто пропускаем
                continue

            notif.sent_at = now
        await session.commit()


