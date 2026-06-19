from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tender_monitor.connectors.registry import (
    all_connectors,
    clear_registry,
    register,
)


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _truncate_tables(test_database_url: str) -> AsyncIterator[None]:
    """Wipe the four domain tables before every scheduler test.

    The scheduler opens its own committing sessions, so the SAVEPOINT
    rollback in ``db_session`` is not enough to isolate scheduler
    tests from each other (or from the test_upsert tests that share
    the same database). TRUNCATE CASCADE is fast on these tables.
    """
    engine = create_async_engine(test_database_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "TRUNCATE notification_logs, tender_likes, team_members, feedback, "
                "tenders, sources RESTART IDENTITY CASCADE"
            )
        yield
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def clean_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Per-test async engine pointed at the test database.

    Tables are emptied by the autouse ``_truncate_tables`` fixture, so
    this fixture just gives ingest tests an engine to bind a
    session_factory against.
    """
    engine = create_async_engine(test_database_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def scheduler_session_factory(
    clean_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=clean_engine, expire_on_commit=False, class_=AsyncSession
    )


@pytest.fixture
def captured_logs() -> Iterator[list[dict[str, object]]]:
    """Capture structlog events emitted inside the test.

    Reset structlog's defaults first so our get_logger's cached
    BoundLoggers pick up the test capture processor.
    """
    structlog.reset_defaults()
    with structlog.testing.capture_logs() as logs:
        yield logs


@pytest.fixture
def fresh_registry() -> Iterator[None]:
    """Snapshot/clear/restore the connector registry around a test."""
    snapshot = all_connectors()
    clear_registry()
    try:
        yield
    finally:
        clear_registry()
        for cls in snapshot.values():
            register(cls)
