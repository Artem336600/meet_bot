from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .base import CalendarProvider, UnifiedEvent


class FakeCalendarProvider(CalendarProvider):
    async def get_events(self, user: Any, time_min: datetime, time_max: datetime) -> list[UnifiedEvent]:
        base_start = datetime.now(timezone.utc).replace(microsecond=0)
        events = [
            UnifiedEvent(
                id="evt-1",
                title="Standup Meeting",
                start_at=base_start + timedelta(minutes=5),
                end_at=base_start + timedelta(minutes=20),
                location="Online",
                description=f"User={getattr(user, 'id', user)}",
            ),
            UnifiedEvent(
                id="evt-2",
                title="Planning",
                start_at=base_start + timedelta(hours=1),
                end_at=base_start + timedelta(hours=2),
                location="Room 101",
                description="Quarter planning",
            ),
        ]

        result: list[UnifiedEvent] = []
        for e in events:
            overlaps = not (e.end_at <= time_min or e.start_at >= time_max)
            if overlaps:
                result.append(e)
        return result


