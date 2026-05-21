from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from tender_monitor.core.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models in this project."""


engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    future=True,
)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-friendly async session dependency."""
    async with async_session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Dispose the global engine. Call from process shutdown hooks."""
    await engine.dispose()
