from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class UnifiedEvent(BaseModel):
    id: str = Field(..., description="Уникальный идентификатор события")
    title: Optional[str] = Field(default=None, description="Заголовок события")
    start_at: datetime = Field(..., description="Начало события (UTC)")
    end_at: datetime = Field(..., description="Окончание события (UTC)")
    description: Optional[str] = Field(default=None, description="Описание события")
    location: Optional[str] = Field(default=None, description="Место проведения")


class CalendarProvider(ABC):
    @abstractmethod
    async def get_events(
        self,
        user: object,
        time_min: datetime,
        time_max: datetime,
    ) -> List[UnifiedEvent]:
        """Получить события пользователя в интервале [time_min, time_max]."""
        raise NotImplementedError
