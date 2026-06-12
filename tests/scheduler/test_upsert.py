from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.core.enums import Country, TenderStatus
from tender_monitor.core.models import Source, Tender
from tender_monitor.core.schemas import TenderUpsert
from tender_monitor.matching import MatchResult
from tender_monitor.scheduler.upsert import (
    UpsertOutcome,
    upsert_tender,
)

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


async def _make_source(session: AsyncSession) -> None:
    session.add(
        Source(
            name="goszakup",
            display_name="Goszakup",
            country=Country.KZ,
            base_url="https://zakup.gov.kz",
        )
    )
    await session.flush()


def _base_upsert(**overrides: object) -> TenderUpsert:
    defaults: dict[str, object] = {
        "source_name": "goszakup",
        "external_id": "T-1",
        "title": "Initial title",
        "buyer_name": "Acme Org",
        "country": Country.KZ,
        "value_amount": Decimal("1000.00"),
        "value_currency": "KZT",
        "deadline_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        "status": TenderStatus.open,
        "source_url": "https://example.test/T-1",
        "raw_json": {"id": "T-1"},
    }
    defaults.update(overrides)
    return TenderUpsert.model_validate(defaults)


async def test_upsert_creates_new_tender(db_session: AsyncSession) -> None:
    await _make_source(db_session)

    result = await upsert_tender(
        db_session,
        _base_upsert(),
        MatchResult(matched_groups=["esg"], match_details={"esg": {"matched_phrases": ["ESG audit"], "matched_tokens": []}}),
        T0,
    )

    assert result.outcome is UpsertOutcome.created

    row = (await db_session.execute(select(Tender))).scalar_one()
    assert row.matched_groups == ["esg"]
    assert row.match_details == {
        "esg": {"matched_phrases": ["ESG audit"], "matched_tokens": []}
    }
    assert row.first_seen_at == T0
    assert row.title_en is None
    assert row.last_seen_at == T0
    assert row.last_changed_at == T0
    assert row.change_log == []
    assert row.is_active is True


async def test_upsert_persists_translation_fields(db_session: AsyncSession) -> None:
    await _make_source(db_session)
    translated_at = T0 + timedelta(seconds=5)

    await upsert_tender(
        db_session,
        _base_upsert(
            title_en="Initial title in English",
            title_language="ru",
            translation_provider="google_translate_pa",
            title_translated_at=translated_at,
        ),
        MatchResult(),
        T0,
    )

    row = (await db_session.execute(select(Tender))).scalar_one()
    assert row.title_en == "Initial title in English"
    assert row.title_language == "ru"
    assert row.translation_provider == "google_translate_pa"
    assert row.title_translated_at == translated_at


async def test_upsert_preserves_translation_when_title_unchanged_and_none_supplied(
    db_session: AsyncSession,
) -> None:
    await _make_source(db_session)
    translated_at = T0 + timedelta(seconds=5)
    await upsert_tender(
        db_session,
        _base_upsert(
            title_en="Initial title in English",
            title_language="ru",
            translation_provider="google_translate_pa",
            title_translated_at=translated_at,
        ),
        MatchResult(),
        T0,
    )

    await upsert_tender(db_session, _base_upsert(), MatchResult(), T0 + timedelta(hours=1))

    row = (await db_session.execute(select(Tender))).scalar_one()
    assert row.title_en == "Initial title in English"
    assert row.title_language == "ru"
    assert row.translation_provider == "google_translate_pa"
    assert row.title_translated_at == translated_at


async def test_upsert_clears_translation_when_title_changes_and_none_supplied(
    db_session: AsyncSession,
) -> None:
    await _make_source(db_session)
    await upsert_tender(
        db_session,
        _base_upsert(
            title_en="Initial title in English",
            title_language="ru",
            translation_provider="google_translate_pa",
            title_translated_at=T0 + timedelta(seconds=5),
        ),
        MatchResult(),
        T0,
    )

    await upsert_tender(
        db_session,
        _base_upsert(title="Changed title"),
        MatchResult(),
        T0 + timedelta(hours=1),
    )

    row = (await db_session.execute(select(Tender))).scalar_one()
    assert row.title == "Changed title"
    assert row.title_en is None
    assert row.title_language is None
    assert row.translation_provider is None
    assert row.title_translated_at is None


async def test_upsert_unchanged_updates_only_last_seen(
    db_session: AsyncSession,
) -> None:
    await _make_source(db_session)
    upsert = _base_upsert()
    await upsert_tender(db_session, upsert, MatchResult(), T0)

    t1 = T0 + timedelta(seconds=60)
    result = await upsert_tender(db_session, upsert, MatchResult(), t1)

    assert result.outcome is UpsertOutcome.unchanged
    row = (await db_session.execute(select(Tender))).scalar_one()
    assert row.last_seen_at == t1
    assert row.last_changed_at == T0
    assert row.change_log == []


async def test_upsert_tracks_deadline_change(db_session: AsyncSession) -> None:
    await _make_source(db_session)
    await upsert_tender(db_session, _base_upsert(), MatchResult(), T0)

    new_deadline = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    t1 = T0 + timedelta(hours=1)
    result = await upsert_tender(
        db_session,
        _base_upsert(deadline_at=new_deadline),
        MatchResult(),
        t1,
    )

    assert result.outcome is UpsertOutcome.updated
    assert result.changes == ["deadline_at"]

    row = (await db_session.execute(select(Tender))).scalar_one()
    assert row.deadline_at == new_deadline
    assert row.last_changed_at == t1
    assert len(row.change_log) == 1

    entry = row.change_log[0]
    assert entry["at"] == t1.isoformat()
    assert set(entry["fields"].keys()) == {"deadline_at"}
    assert entry["fields"]["deadline_at"]["old"] == "2026-06-01T12:00:00+00:00"
    assert entry["fields"]["deadline_at"]["new"] == "2026-06-02T12:00:00+00:00"


async def test_upsert_tracks_value_amount_change(
    db_session: AsyncSession,
) -> None:
    await _make_source(db_session)
    await upsert_tender(db_session, _base_upsert(), MatchResult(), T0)

    t1 = T0 + timedelta(hours=1)
    result = await upsert_tender(
        db_session,
        _base_upsert(value_amount=Decimal("9999.99")),
        MatchResult(),
        t1,
    )

    assert result.outcome is UpsertOutcome.updated
    assert result.changes == ["value_amount"]
    row = (await db_session.execute(select(Tender))).scalar_one()
    entry = row.change_log[0]
    # Decimal serialised as a string, not a float (preserves precision).
    assert entry["fields"]["value_amount"]["old"] == "1000.00"
    assert entry["fields"]["value_amount"]["new"] == "9999.99"


async def test_upsert_tracks_status_change(db_session: AsyncSession) -> None:
    await _make_source(db_session)
    await upsert_tender(db_session, _base_upsert(), MatchResult(), T0)

    t1 = T0 + timedelta(hours=1)
    result = await upsert_tender(
        db_session,
        _base_upsert(status=TenderStatus.closed),
        MatchResult(),
        t1,
    )

    assert result.outcome is UpsertOutcome.updated
    assert result.changes == ["status"]
    row = (await db_session.execute(select(Tender))).scalar_one()
    entry = row.change_log[0]
    # Enum is serialised as its .value, not "TenderStatus.open".
    assert entry["fields"]["status"]["old"] == "open"
    assert entry["fields"]["status"]["new"] == "closed"


async def test_upsert_overwrites_match_results_silently(
    db_session: AsyncSession,
) -> None:
    await _make_source(db_session)
    await upsert_tender(
        db_session,
        _base_upsert(),
        MatchResult(
            matched_groups=["esg"],
            match_details={"esg": {"matched_phrases": ["ESG"], "matched_tokens": []}},
        ),
        T0,
    )

    t1 = T0 + timedelta(hours=1)
    result = await upsert_tender(
        db_session, _base_upsert(), MatchResult(), t1
    )

    # Match results aren't tracked; outcome stays "unchanged" even
    # though matched_groups went from ["esg"] to [].
    assert result.outcome is UpsertOutcome.unchanged
    row = (await db_session.execute(select(Tender))).scalar_one()
    assert row.matched_groups == []
    assert row.match_details is None


async def test_upsert_simultaneous_multi_field_change(
    db_session: AsyncSession,
) -> None:
    await _make_source(db_session)
    await upsert_tender(db_session, _base_upsert(), MatchResult(), T0)

    t1 = T0 + timedelta(hours=1)
    new_deadline = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    result = await upsert_tender(
        db_session,
        _base_upsert(deadline_at=new_deadline, value_amount=Decimal("2000.00")),
        MatchResult(),
        t1,
    )

    assert result.outcome is UpsertOutcome.updated
    assert set(result.changes) == {"deadline_at", "value_amount"}
    row = (await db_session.execute(select(Tender))).scalar_one()
    assert len(row.change_log) == 1
    entry = row.change_log[0]
    assert set(entry["fields"].keys()) == {"deadline_at", "value_amount"}


@pytest.mark.parametrize(
    ("old_amount", "new_amount", "should_change"),
    [
        (Decimal("100.00"), Decimal("100"), False),  # Decimal equality
        (Decimal("100.00"), Decimal("100.01"), True),
    ],
)
async def test_decimal_equality_is_aware(
    db_session: AsyncSession,
    old_amount: Decimal,
    new_amount: Decimal,
    should_change: bool,
) -> None:
    await _make_source(db_session)
    await upsert_tender(
        db_session, _base_upsert(value_amount=old_amount), MatchResult(), T0
    )

    t1 = T0 + timedelta(hours=1)
    result = await upsert_tender(
        db_session,
        _base_upsert(value_amount=new_amount),
        MatchResult(),
        t1,
    )

    if should_change:
        assert result.outcome is UpsertOutcome.updated
    else:
        assert result.outcome is UpsertOutcome.unchanged
