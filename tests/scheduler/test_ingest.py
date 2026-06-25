from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tender_monitor.connectors.base import Connector
from tender_monitor.connectors.errors import FetchError
from tender_monitor.connectors.registry import register
from tender_monitor.core.enums import Country
from tender_monitor.core.models import Source, Tender
from tender_monitor.core.schemas import TenderUpsert
from tender_monitor.scheduler.ingest import (
    DEFAULT_BACKFILL,
    KNOWN_IDS_LOOKBACK_DAYS,
    ingest_source,
    reset_keywords_cache,
)
from tender_monitor.translation import TitleTranslation, TranslationError

T0 = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
SOURCE_NAME = "_fake_ingest"


def _fake_upsert(
    external_id: str = "F-1", title: str = "ESG audit services"
) -> TenderUpsert:
    return TenderUpsert(
        source_name=SOURCE_NAME,
        external_id=external_id,
        title=title,
        country=Country.KZ,
        source_url=f"https://example.test/{external_id}",
        raw_json={"id": external_id, "title": title},
    )


class _FakeConnector(Connector):
    """In-memory connector controlled by the test via class attributes."""

    source_name: ClassVar[str] = SOURCE_NAME

    tenders: ClassVar[list[TenderUpsert]] = []
    raw_count: ClassVar[int] = 0
    raise_exc: ClassVar[BaseException | None] = None
    seen_since: ClassVar[list[datetime | None]] = []
    seen_known_ids: ClassVar[list[set[str] | None]] = []

    @classmethod
    def reset(
        cls,
        *,
        tenders: list[TenderUpsert] | None = None,
        raw_count: int | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        cls.tenders = list(tenders) if tenders is not None else []
        cls.raw_count = (
            raw_count if raw_count is not None else len(cls.tenders)
        )
        cls.raise_exc = raise_exc
        cls.seen_since = []
        cls.seen_known_ids = []

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        type(self).seen_since.append(since)
        # Snapshot the hint the base class stashed for this call.
        hint = self._known_external_ids
        type(self).seen_known_ids.append(set(hint) if hint is not None else None)
        exc = type(self).raise_exc
        if exc is not None:
            raise exc
        return [{"_index": i} for i in range(type(self).raw_count)]

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        index = raw["_index"]
        return type(self).tenders[index]


class _FakeTitleTranslator:
    provider = "fake_translate"

    def __init__(self, translations: dict[str, str]) -> None:
        self.translations = translations
        self.calls: list[list[str]] = []

    async def translate_titles(
        self, texts: Sequence[str], *, source_language: str = "auto"
    ) -> list[TitleTranslation]:
        self.calls.append(list(texts))
        return [
            TitleTranslation(
                text=self.translations[text],
                detected_language="ru",
            )
            for text in texts
        ]


class _FailingTitleTranslator:
    provider = "fake_translate"

    async def translate_titles(
        self, texts: Sequence[str], *, source_language: str = "auto"
    ) -> list[TitleTranslation]:
        raise TranslationError(f"failed for {len(texts)} title(s)")


@pytest.fixture(autouse=True)
def _register_fake_connector(fresh_registry: None) -> Iterator[None]:
    """Register the FakeConnector and ensure the keywords cache is cold."""
    register(_FakeConnector)
    reset_keywords_cache()
    with patch("tender_monitor.scheduler.ingest.build_title_translator", return_value=None):
        yield
    reset_keywords_cache()


async def _make_source(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    last_run_at: datetime | None = None,
    enabled: bool = True,
) -> None:
    async with session_factory() as session:
        session.add(
            Source(
                name=SOURCE_NAME,
                display_name="Fake",
                country=Country.KZ,
                base_url="https://example.test",
                enabled=enabled,
                schedule_minutes=30,
                last_run_at=last_run_at,
            )
        )
        await session.commit()


def _frozen_now(value: datetime) -> Callable[[], datetime]:
    return lambda: value


async def test_ingest_creates_source_health(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    _FakeConnector.reset(tenders=[_fake_upsert("F-1"), _fake_upsert("F-2")])

    result = await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )

    assert result.created == 2
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.fetched == 2
    assert result.normalized == 2

    async with scheduler_session_factory() as session:
        source = (await session.execute(select(Source))).scalar_one()
        assert source.last_run_at == T0
        assert source.last_success_at == T0
        assert source.consecutive_failures == 0
        assert source.last_error is None
        assert source.total_tenders_seen == 2


async def test_ingest_increments_failures_and_reraises(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    _FakeConnector.reset(raise_exc=FetchError("upstream is down"))

    with pytest.raises(FetchError):
        await ingest_source(
            SOURCE_NAME,
            session_factory=scheduler_session_factory,
            now=_frozen_now(T0),
        )

    async with scheduler_session_factory() as session:
        source = (await session.execute(select(Source))).scalar_one()
        assert source.consecutive_failures == 1
        assert source.last_error is not None
        assert "FetchError" in source.last_error
        assert source.last_success_at is None
        # last_run_at is committed before the connector runs, so even a
        # failure leaves a marker that this source was attempted.
        assert source.last_run_at == T0


async def test_ingest_skips_disabled_source(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory, enabled=False)
    _FakeConnector.reset(tenders=[_fake_upsert("F-1")])

    result = await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )

    assert result.skipped is True
    assert result.created == 0
    # The connector should not have been called at all.
    assert _FakeConnector.seen_since == []


async def test_ingest_initial_since_falls_back_to_backfill(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory, last_run_at=None)
    _FakeConnector.reset(tenders=[_fake_upsert("F-1")])

    await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )

    assert _FakeConnector.seen_since == [T0 - DEFAULT_BACKFILL]


async def test_ingest_subsequent_since_uses_last_run_at(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    previous_run = T0 - timedelta(hours=1)
    await _make_source(scheduler_session_factory, last_run_at=previous_run)
    _FakeConnector.reset(tenders=[_fake_upsert("F-1")])

    await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )

    assert _FakeConnector.seen_since == [previous_run]


async def test_ingest_matcher_exception_skips_persistence(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    _FakeConnector.reset(tenders=[_fake_upsert("F-1"), _fake_upsert("F-2")])

    def _broken_matcher(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("buggy keyword regex")

    with patch(
        "tender_monitor.scheduler.ingest.match_tender", side_effect=_broken_matcher
    ):
        result = await ingest_source(
            SOURCE_NAME,
            session_factory=scheduler_session_factory,
            now=_frozen_now(T0),
        )

    assert result.created == 0
    assert result.matched == 0

    async with scheduler_session_factory() as session:
        rows = (await session.execute(select(Tender))).scalars().all()
        assert rows == []


async def test_ingest_deletes_existing_tender_that_no_longer_matches(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    _FakeConnector.reset(tenders=[_fake_upsert("F-1")])
    first = await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )
    assert first.created == 1
    assert first.matched == 1

    _FakeConnector.reset(tenders=[_fake_upsert("F-1", title="Plain procurement")])
    second = await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0 + timedelta(hours=1)),
    )

    assert second.created == 0
    assert second.deleted == 1
    assert second.matched == 0

    async with scheduler_session_factory() as session:
        rows = (await session.execute(select(Tender))).scalars().all()
        assert rows == []


async def test_ingest_translates_title_before_matching_and_saving(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    _FakeConnector.reset(tenders=[_fake_upsert("F-1", title="Plain procurement")])
    translator = _FakeTitleTranslator(
        {"Plain procurement": "ESG audit and climate risk services"}
    )

    result = await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
        title_translator=translator,
    )

    assert result.created == 1
    assert result.matched == 1
    assert translator.calls == [["Plain procurement"]]

    async with scheduler_session_factory() as session:
        row = (await session.execute(select(Tender))).scalar_one()
        assert row.title == "Plain procurement"
        assert row.title_en == "ESG audit and climate risk services"
        assert row.title_language == "ru"
        assert row.translation_provider == "fake_translate"
        assert row.title_translated_at == T0
        assert row.matched_groups == ["esg"]


async def test_ingest_reuses_stored_translation_for_unchanged_title(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    title = "Plain procurement"
    _FakeConnector.reset(tenders=[_fake_upsert("F-1", title=title)])
    first_translator = _FakeTitleTranslator(
        {title: "ESG audit and climate risk services"}
    )
    await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
        title_translator=first_translator,
    )

    second_translator = _FakeTitleTranslator(
        {title: "Different translation that should not be used"}
    )
    _FakeConnector.reset(tenders=[_fake_upsert("F-1", title=title)])
    result = await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0 + timedelta(hours=1)),
        title_translator=second_translator,
    )

    assert result.unchanged == 1
    assert result.matched == 1
    assert second_translator.calls == []

    async with scheduler_session_factory() as session:
        row = (await session.execute(select(Tender))).scalar_one()
        assert row.title_en == "ESG audit and climate risk services"
        assert row.title_translated_at == T0


async def test_ingest_translation_failure_skips_unmatched_tender(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    _FakeConnector.reset(tenders=[_fake_upsert("F-1", title="Plain procurement")])

    result = await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
        title_translator=_FailingTitleTranslator(),
    )

    assert result.created == 0
    assert result.matched == 0

    async with scheduler_session_factory() as session:
        rows = (await session.execute(select(Tender))).scalars().all()
        assert rows == []


async def test_ingest_change_logged_at_info_level(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
    captured_logs: list[dict[str, object]],
) -> None:
    await _make_source(scheduler_session_factory)

    initial = _fake_upsert("F-1", title="ESG initial title")
    initial = initial.model_copy(
        update={"deadline_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC)}
    )
    _FakeConnector.reset(tenders=[initial])
    await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )

    changed = initial.model_copy(
        update={"deadline_at": datetime(2026, 6, 8, 12, 0, tzinfo=UTC)}
    )
    _FakeConnector.reset(tenders=[changed])
    await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0 + timedelta(hours=1)),
    )

    change_events = [
        log for log in captured_logs if log.get("event") == "scheduler.tender.changed"
    ]
    assert len(change_events) == 1
    event = change_events[0]
    assert event.get("source") == SOURCE_NAME
    assert event.get("fields") == ["deadline_at"]
    assert event.get("log_level") == "info"


# ---------------------------------------------------------------------------
# known_external_ids hint — scheduler-side query + plumbing
# ---------------------------------------------------------------------------


async def _seed_tender(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    external_id: str,
    last_seen_at: datetime,
    source_name: str = SOURCE_NAME,
) -> None:
    """Insert a Tender row owned by ``source_name``, with the given
    ``last_seen_at``. The scheduler's known-IDs query filters on this
    column so tests exercise the lookback boundary directly.
    """
    async with session_factory() as session:
        session.add(
            Tender(
                source_name=source_name,
                external_id=external_id,
                title=f"seed-{external_id}",
                country=Country.KZ,
                source_url=f"https://example.test/{external_id}",
                matched_groups=["esg"],
                match_details={
                    "esg": {"matched_phrases": ["ESG"], "matched_tokens": []}
                },
                raw_json={"id": external_id},
                first_seen_at=last_seen_at,
                last_seen_at=last_seen_at,
                last_changed_at=last_seen_at,
            )
        )
        await session.commit()


async def test_ingest_loads_known_ids_and_passes_to_connector(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    # Three tenders with last_seen_at inside the lookback window.
    for ext in ("S-1", "S-2", "S-3"):
        await _seed_tender(
            scheduler_session_factory,
            external_id=ext,
            last_seen_at=T0 - timedelta(days=1),
        )

    _FakeConnector.reset(tenders=[])
    await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )

    assert _FakeConnector.seen_known_ids == [{"S-1", "S-2", "S-3"}]


async def test_ingest_excludes_stale_ids_outside_lookback(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    await _seed_tender(
        scheduler_session_factory,
        external_id="recent",
        last_seen_at=T0 - timedelta(days=1),
    )
    await _seed_tender(
        scheduler_session_factory,
        external_id="stale",
        last_seen_at=T0 - timedelta(days=KNOWN_IDS_LOOKBACK_DAYS + 1),
    )

    _FakeConnector.reset(tenders=[])
    await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )

    # The stale (15-day-old) ID is outside the lookback and must NOT
    # appear in the hint set; the recent one must.
    assert _FakeConnector.seen_known_ids == [{"recent"}]


async def test_ingest_empty_db_passes_empty_set(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    _FakeConnector.reset(tenders=[])

    await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )

    # No rows for this source → empty set, NOT None (the scheduler
    # always produces a set; None is reserved for callers that don't
    # provide the kwarg at all, e.g. CLI run-connector).
    assert _FakeConnector.seen_known_ids == [set()]


async def test_ingest_only_returns_ids_for_target_source(
    scheduler_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_source(scheduler_session_factory)
    # Tenders carry a FK to sources(name); both sources need to exist
    # for the seed to succeed. The "other_source" row stays minimal --
    # we only want it to satisfy the FK so a tender can hang off it.
    async with scheduler_session_factory() as session:
        session.add(
            Source(
                name="other_source",
                display_name="Other",
                country=Country.KZ,
                base_url="https://other.test",
                enabled=True,
                schedule_minutes=30,
            )
        )
        await session.commit()

    await _seed_tender(
        scheduler_session_factory,
        external_id="mine",
        last_seen_at=T0 - timedelta(days=1),
        source_name=SOURCE_NAME,
    )
    await _seed_tender(
        scheduler_session_factory,
        external_id="theirs",
        last_seen_at=T0 - timedelta(days=1),
        source_name="other_source",
    )

    _FakeConnector.reset(tenders=[])
    await ingest_source(
        SOURCE_NAME,
        session_factory=scheduler_session_factory,
        now=_frozen_now(T0),
    )

    assert _FakeConnector.seen_known_ids == [{"mine"}]
