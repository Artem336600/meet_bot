from __future__ import annotations

import asyncio
from datetime import datetime, date, timedelta, timezone
import os
from typing import Any, List

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.calendar.base import CalendarProvider, UnifiedEvent
from app.db.models import OAuthToken, User


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_google_datetime(value: dict[str, Any]) -> tuple[datetime, bool]:
    # Returns (dt, is_all_day)
    if "dateTime" in value and value["dateTime"]:
        dt = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        return dt, False
    if "date" in value and value["date"]:
        d = date.fromisoformat(value["date"])
        dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        return dt, True
    raise ValueError("Invalid Google event datetime format")


class GoogleCalendarProvider(CalendarProvider):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_events(
        self,
        user: User,
        time_min: datetime,
        time_max: datetime,
    ) -> List[UnifiedEvent]:
        # Load token
        result = await self._session.execute(
            select(OAuthToken).where(
                OAuthToken.user_id == user.id, OAuthToken.provider == "google"
            )
        )
        token: OAuthToken | None = result.scalar_one_or_none()
        if token is None:
            return []

        creds = Credentials(
            token=token.access_token,
            refresh_token=token.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.getenv("GOOGLE_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        )

        # Refresh if needed
        if creds.expired and creds.refresh_token:
            await asyncio.to_thread(creds.refresh, GoogleRequest())
            token.access_token = creds.token
            if creds.expiry:
                token.expires_at = creds.expiry
            await self._session.commit()

        # Build service and fetch events in a worker thread (blocking client)
        def _fetch() -> list[dict[str, Any]]:
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            events = (
                service.events()  # type: ignore[no-untyped-call]
                .list(
                    calendarId="primary",
                    timeMin=_rfc3339(time_min),
                    timeMax=_rfc3339(time_max),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            return events.get("items", [])

        items = await asyncio.to_thread(_fetch)

        unified: list[UnifiedEvent] = []
        for it in items:
            start_dt, start_all_day = _parse_google_datetime(it.get("start", {}))
            end_dt, end_all_day = _parse_google_datetime(it.get("end", {}))
            # Google all-day end is exclusive; normalize to inclusive end by subtracting 1 second
            if start_all_day or end_all_day:
                # keep start at 00:00, end at next day 00:00; client may treat as all-day
                pass
            unified.append(
                UnifiedEvent(
                    id=it.get("id", ""),
                    title=it.get("summary"),
                    start_at=start_dt,
                    end_at=end_dt,
                    description=it.get("description"),
                    location=it.get("location"),
                )
            )

        return unified


