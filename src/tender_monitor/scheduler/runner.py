"""APScheduler wiring: one IntervalTrigger job per enabled source.

Each job is a thin wrapper around ``ingest_source`` that catches
propagated errors so one bad source can't kill the scheduler. The
runner also runs each enabled source immediately on startup so the
operator doesn't have to wait a full interval for first data.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from tender_monitor.core.database import async_session_factory
from tender_monitor.core.logging import get_logger
from tender_monitor.core.models import Source
from tender_monitor.scheduler.ingest import ingest_source

logger = get_logger(__name__)


async def _job(source_name: str) -> None:
    """Run one ingest, swallowing exceptions so the scheduler stays up.

    ``ingest_source`` already records failures on the Source row and
    logs at ERROR; we catch here purely to prevent APScheduler from
    treating the job as crashed.
    """
    try:
        await ingest_source(source_name)
    except Exception as exc:
        logger.error(
            "scheduler.job.failed",
            source=source_name,
            error_type=type(exc).__name__,
            error=str(exc),
        )


class Runner:
    """Owns the AsyncIOScheduler and graceful shutdown wiring."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone=UTC)
        self.shutdown_event: asyncio.Event | None = None

    async def _load_enabled_sources(self) -> list[Source]:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Source).where(Source.enabled.is_(True))
            )
            return list(result.scalars().all())

    async def start(self) -> None:
        sources = await self._load_enabled_sources()
        if not sources:
            logger.warning("scheduler.start.no_enabled_sources")

        now = datetime.now(UTC)
        job_specs: list[tuple[str, int]] = []
        for source in sources:
            self.scheduler.add_job(
                _job,
                args=[source.name],
                trigger=IntervalTrigger(minutes=source.schedule_minutes),
                id=f"ingest:{source.name}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
                next_run_time=now,
            )
            job_specs.append((source.name, source.schedule_minutes))

        self.scheduler.start()
        logger.info(
            "scheduler.start",
            jobs=[{"source": name, "every_minutes": every} for name, every in job_specs],
        )

    async def stop(self) -> None:
        # wait=True lets in-flight ingests finish; we accept a few extra
        # seconds at shutdown to avoid leaving the DB in a half-state.
        self.scheduler.shutdown(wait=True)
        logger.info("scheduler.stop")

    def _install_signal_handlers(self) -> None:
        assert self.shutdown_event is not None

        def _request_shutdown(*_: object) -> None:
            assert self.shutdown_event is not None
            self.shutdown_event.set()

        if sys.platform == "win32":
            # add_signal_handler isn't supported on Windows; signal.signal
            # still triggers between async iterations for SIGINT.
            signal.signal(signal.SIGINT, _request_shutdown)
            return

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_shutdown)

    async def run_forever(self) -> None:
        self.shutdown_event = asyncio.Event()
        self._install_signal_handlers()
        await self.start()
        try:
            await self.shutdown_event.wait()
        finally:
            await self.stop()


__all__ = ["Runner"]
