from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tender_monitor.core import models  # noqa: F401  -- registers tables on Base.metadata
from tender_monitor.core.database import Base

FIXTURES_DIR = Path(__file__).parent / "fixtures"

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@localhost:5432/tender_monitor_test"
)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def setup_database(test_database_url: str) -> AsyncIterator[None]:
    """Drop and recreate the full schema once per test session.

    Owns its own engine so it can run in the session loop while per-test
    fixtures use the function loop without cross-loop connection sharing.
    The pgcrypto extension is required for gen_random_uuid() server defaults.
    """
    engine = create_async_engine(test_database_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        yield
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def db_session(test_database_url: str) -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession wrapped in a transaction that rolls back at end.

    Each test gets a fresh engine and connection, so every test runs in
    its own event loop without sharing pooled connections across loops.
    State never leaks between tests because the outer transaction is
    rolled back unconditionally.
    """
    engine = create_async_engine(test_database_url, future=True)
    connection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, class_=AsyncSession
    )
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()
