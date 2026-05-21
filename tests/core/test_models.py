from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.core.enums import Country, TenderStatus
from tender_monitor.core.models import Source, Tender


async def _make_source(session: AsyncSession, name: str = "goszakup") -> Source:
    source = Source(
        name=name,
        display_name="Государственные закупки",
        country=Country.KZ,
        base_url="https://goszakup.gov.kz",
    )
    session.add(source)
    await session.flush()
    return source


async def test_create_source(db_session: AsyncSession) -> None:
    await _make_source(db_session)

    fetched = (
        await db_session.execute(select(Source).where(Source.name == "goszakup"))
    ).scalar_one()

    assert fetched.name == "goszakup"
    assert fetched.display_name == "Государственные закупки"
    assert fetched.country is Country.KZ
    assert fetched.base_url == "https://goszakup.gov.kz"
    assert fetched.enabled is True
    assert fetched.schedule_minutes == 60
    assert fetched.consecutive_failures == 0
    assert fetched.total_tenders_seen == 0
    assert fetched.last_run_at is None
    assert fetched.last_success_at is None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


async def test_create_tender(db_session: AsyncSession) -> None:
    await _make_source(db_session)

    raw = {"id": "T-1", "title": "Server procurement", "nested": {"a": [1, 2, 3]}}
    tender = Tender(
        source_name="goszakup",
        external_id="T-1",
        title="Server procurement",
        country=Country.KZ,
        source_url="https://goszakup.gov.kz/announce/T-1",
        raw_json=raw,
    )
    db_session.add(tender)
    await db_session.flush()
    await db_session.refresh(tender)

    assert tender.id is not None
    assert tender.matched_groups == []
    assert tender.is_active is True
    assert tender.status is TenderStatus.unknown
    assert tender.raw_json == raw
    assert tender.change_log == []
    assert tender.first_seen_at is not None
    assert tender.last_seen_at is not None
    assert tender.last_changed_at is not None


async def test_tender_unique_constraint(db_session: AsyncSession) -> None:
    await _make_source(db_session)

    db_session.add(
        Tender(
            source_name="goszakup",
            external_id="T-DUP",
            title="A",
            country=Country.KZ,
            source_url="https://example.com/a",
            raw_json={},
        )
    )
    await db_session.flush()

    db_session.add(
        Tender(
            source_name="goszakup",
            external_id="T-DUP",
            title="B",
            country=Country.KZ,
            source_url="https://example.com/b",
            raw_json={},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_canonical_id_self_ref(db_session: AsyncSession) -> None:
    await _make_source(db_session)

    primary = Tender(
        source_name="goszakup",
        external_id="P-1",
        title="Primary listing",
        country=Country.KZ,
        source_url="https://example.com/p1",
        raw_json={},
    )
    db_session.add(primary)
    await db_session.flush()
    await db_session.refresh(primary)

    duplicate = Tender(
        source_name="goszakup",
        external_id="D-1",
        title="Duplicate listing on same source",
        country=Country.KZ,
        source_url="https://example.com/d1",
        raw_json={},
        canonical_id=primary.id,
    )
    db_session.add(duplicate)
    await db_session.flush()
    await db_session.refresh(duplicate)

    assert duplicate.canonical_id == primary.id
    assert duplicate.canonical is not None
    assert duplicate.canonical.id == primary.id
