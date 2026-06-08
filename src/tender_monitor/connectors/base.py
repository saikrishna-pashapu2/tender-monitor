from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from tender_monitor.connectors.http import make_client
from tender_monitor.core.logging import get_logger
from tender_monitor.core.schemas import TenderUpsert

logger = get_logger(__name__)

HttpClientFactory = Callable[[], httpx.AsyncClient]


class FetchResult(BaseModel):
    """Outcome of a single connector run.

    `tenders` are the successfully normalized items. `partial_errors`
    captures per-item normalization failures (the run as a whole still
    succeeded). A failure of `_fetch_raw` raises FetchError instead and
    no FetchResult is produced.
    """

    source_name: str
    tenders: list[TenderUpsert] = Field(default_factory=list)
    partial_errors: list[str] = Field(default_factory=list)
    fetched_at: datetime
    duration_ms: float
    raw_item_count: int


class Connector(ABC):
    """One concrete subclass per tender source.

    Subclasses MUST:
      - declare ``source_name`` as a ClassVar[str]
      - implement ``_fetch_raw`` and ``_normalize``
      - register themselves via ``@register`` from
        ``tender_monitor.connectors.registry``

    Subclasses MUST NOT override ``fetch_latest`` — the orchestration
    (timing, partial-error collection) lives there and stays uniform
    across every source.

    Missing-`source_name` failure mode: instantiation of a subclass
    without a class-level ``source_name`` raises ``TypeError``. We chose
    instantiation-time over class-definition-time so abstract
    intermediate subclasses are still allowed.
    """

    source_name: ClassVar[str]

    def __init__(self, http_client_factory: HttpClientFactory | None = None) -> None:
        if not getattr(type(self), "source_name", None):
            raise TypeError(
                f"{type(self).__name__} must declare a class-level "
                "`source_name` before it can be instantiated."
            )
        self._http_client_factory = http_client_factory
        # Per-call hint stashed by ``fetch_latest`` before invoking
        # ``_fetch_raw`` and cleared in a finally afterwards.
        # ``None`` means "no hint provided"; an empty set means "hint
        # provided but no IDs known". Most connectors ignore this and
        # never read it. Connectors must not use the hint in a way that
        # prevents existing tenders from being refreshed and rematched.
        # See AGENTS.md under "Scheduler and ingestion" for the contract.
        self._known_external_ids: set[str] | None = None

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client()

    @abstractmethod
    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        """Return raw items from the source.

        May raise FetchError (network, HTTP, top-level parse failure).
        """

    @abstractmethod
    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        """Convert a single raw item to a TenderUpsert.

        May raise ParseError. Per-item failures are caught by
        ``fetch_latest`` and recorded in ``partial_errors``.
        """

    async def fetch_latest(
        self,
        since: datetime | None = None,
        known_external_ids: set[str] | None = None,
    ) -> FetchResult:
        """Run the connector once and return a FetchResult.

        Concrete connectors do not override this method. They override
        ``_fetch_raw`` (whole-fetch, may raise FetchError) and
        ``_normalize`` (per-item, may raise ParseError).

        ``known_external_ids`` is an optional hint from the scheduler:
        the set of ``external_id`` values this source has produced
        recently (~14 days). Connectors are free to ignore it. The
        ones that DO use it read ``self._known_external_ids`` inside
        ``_fetch_raw`` -- the value is stashed here before the call
        and cleared in a ``finally`` so it never leaks across runs.
        """
        started = time.perf_counter()
        fetched_at = datetime.now(UTC)

        self._known_external_ids = known_external_ids
        try:
            raw_items = await self._fetch_raw(since)

            tenders: list[TenderUpsert] = []
            partial_errors: list[str] = []
            for index, raw in enumerate(raw_items):
                try:
                    tenders.append(self._normalize(raw))
                except Exception as exc:
                    logger.warning(
                        "connector.normalize_failed",
                        source=type(self).source_name,
                        item_index=index,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    partial_errors.append(
                        f"item {index}: {type(exc).__name__}: {exc}"
                    )
        finally:
            self._known_external_ids = None

        duration_ms = (time.perf_counter() - started) * 1000.0
        return FetchResult(
            source_name=type(self).source_name,
            tenders=tenders,
            partial_errors=partial_errors,
            fetched_at=fetched_at,
            duration_ms=duration_ms,
            raw_item_count=len(raw_items),
        )


__all__ = [
    "Connector",
    "FetchResult",
    "HttpClientFactory",
]
