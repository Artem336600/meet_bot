from datetime import datetime, timedelta, timezone

import pytest

from app.calendar.fake import FakeCalendarProvider
from app.calendar.base import UnifiedEvent


@pytest.mark.asyncio
async def test_fake_provider_returns_unified_event():
    provider = FakeCalendarProvider()
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    events = await provider.get_events(user=None, time_min=now, time_max=now + timedelta(hours=10))
    assert len(events) >= 1
    assert isinstance(events[0], UnifiedEvent)

