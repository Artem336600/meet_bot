from __future__ import annotations

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
	raise RuntimeError("DATABASE_URL is not set")

# Приводим URL к формату async для SQLAlchemy+asyncpg
if DATABASE_URL.startswith("postgres://"):
	DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://") and not DATABASE_URL.startswith("postgresql+asyncpg://"):
	DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgresql://"):]

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
	async with session_factory() as session:
		yield session
