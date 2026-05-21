from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tender_monitor.core.enums import Country, TenderStatus
from tender_monitor.core.models import Source, Tender

T0 = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _truncate_tables(test_database_url: str) -> AsyncIterator[None]:
    """Wipe the four domain tables before AND after every API test.

    The API tests' TestClient opens its own sessions that commit, so
    the rollback in the shared ``db_session`` fixture is not enough to
    isolate API tests from each other — and the API tests run before
    the core/test_models tests alphabetically, so committed leftovers
    would leak across packages without a teardown truncate.
    """
    engine = create_async_engine(test_database_url, future=True)
    truncate_sql = (
        "TRUNCATE notification_logs, feedback, tenders, sources "
        "RESTART IDENTITY CASCADE"
    )
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(truncate_sql)
        yield
        async with engine.begin() as conn:
            await conn.exec_driver_sql(truncate_sql)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def api_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def api_session_factory(
    api_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=api_engine, expire_on_commit=False, class_=AsyncSession
    )


def make_source(
    name: str,
    *,
    display_name: str | None = None,
    country: Country = Country.KZ,
    base_url: str = "https://example.test",
) -> Source:
    return Source(
        name=name,
        display_name=display_name or name.title(),
        country=country,
        base_url=base_url,
    )


def make_tender(
    *,
    source_name: str,
    external_id: str,
    title: str,
    buyer_name: str | None = None,
    country: Country = Country.KZ,
    matched_groups: list[str] | None = None,
    match_details: dict[str, dict[str, list[str]]] | None = None,
    value_amount: Decimal | None = None,
    value_currency: str | None = "KZT",
    deadline_offset_days: int | None = None,
    published_offset_days: int = 0,
    first_seen_offset_minutes: int = 0,
    raw_json: dict[str, object] | None = None,
) -> Tender:
    published = T0 + timedelta(days=published_offset_days)
    deadline = (
        T0 + timedelta(days=deadline_offset_days)
        if deadline_offset_days is not None
        else None
    )
    first_seen = T0 + timedelta(minutes=first_seen_offset_minutes)
    return Tender(
        source_name=source_name,
        external_id=external_id,
        title=title,
        buyer_name=buyer_name,
        country=country,
        value_amount=value_amount,
        value_currency=value_currency,
        published_at=published,
        deadline_at=deadline,
        status=TenderStatus.open,
        source_url=f"https://example.test/{source_name}/{external_id}",
        matched_groups=matched_groups or [],
        match_details=match_details,
        raw_json=raw_json or {"id": external_id},
        first_seen_at=first_seen,
        last_seen_at=first_seen,
        last_changed_at=first_seen,
        change_log=[],
        is_active=True,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def seeded_session(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Seed a deterministic dataset: 2 sources, 12 tenders, mixed flags.

    Layout:
      - 6 KZ tenders on ``goszakup`` (4 matched, 2 unmatched)
      - 6 UZ tenders on ``xt-xarid`` (2 matched, 4 unmatched)
      - mixed deadlines (past, near, far) and values
    """
    async with api_session_factory() as session:
        session.add_all(
            [
                make_source(
                    "goszakup",
                    display_name="Goszakup",
                    country=Country.KZ,
                ),
                make_source(
                    "xt-xarid",
                    display_name="XT-Xarid",
                    country=Country.UZ,
                ),
            ]
        )
        await session.flush()

        rows: list[Tender] = []
        # KZ / goszakup
        rows.append(
            make_tender(
                source_name="goszakup",
                external_id="g-1",
                title="Credit rating audit services",
                buyer_name="National Bank of Kazakhstan",
                matched_groups=["credit_rating"],
                match_details={
                    "credit_rating": {
                        "matched_phrases": ["credit rating"],
                        "matched_tokens": [],
                    }
                },
                value_amount=Decimal("500000.00"),
                deadline_offset_days=10,
                published_offset_days=-1,
                first_seen_offset_minutes=10,
            )
        )
        rows.append(
            make_tender(
                source_name="goszakup",
                external_id="g-2",
                title="ESG consulting framework",
                buyer_name="KazMunayGas",
                matched_groups=["esg"],
                match_details={
                    "esg": {"matched_phrases": ["ESG"], "matched_tokens": []}
                },
                value_amount=Decimal("1200000.00"),
                deadline_offset_days=5,
                published_offset_days=-2,
                first_seen_offset_minutes=20,
            )
        )
        rows.append(
            make_tender(
                source_name="goszakup",
                external_id="g-3",
                title="ESG and credit rating advisory",
                buyer_name="Samruk-Kazyna",
                matched_groups=["esg", "credit_rating"],
                match_details={
                    "esg": {"matched_phrases": ["ESG"], "matched_tokens": []},
                    "credit_rating": {
                        "matched_phrases": ["credit rating"],
                        "matched_tokens": [],
                    },
                },
                value_amount=Decimal("4500000.00"),
                deadline_offset_days=2,
                published_offset_days=-3,
                first_seen_offset_minutes=30,
            )
        )
        rows.append(
            make_tender(
                source_name="goszakup",
                external_id="g-4",
                title="Credit risk modelling",
                buyer_name="Halyk Bank",
                matched_groups=["credit_rating"],
                match_details={
                    "credit_rating": {
                        "matched_phrases": ["credit"],
                        "matched_tokens": [],
                    }
                },
                value_amount=Decimal("200000.00"),
                deadline_offset_days=20,
                published_offset_days=-4,
                first_seen_offset_minutes=40,
            )
        )
        rows.append(
            make_tender(
                source_name="goszakup",
                external_id="g-5",
                title="Office supplies for the ministry",
                buyer_name="Ministry of Finance",
                matched_groups=[],
                value_amount=Decimal("50000.00"),
                deadline_offset_days=30,
                published_offset_days=-5,
                first_seen_offset_minutes=50,
            )
        )
        rows.append(
            make_tender(
                source_name="goszakup",
                external_id="g-6",
                title="Cleaning services contract",
                buyer_name="Ministry of Education",
                matched_groups=[],
                value_amount=None,
                value_currency=None,
                deadline_offset_days=-1,
                published_offset_days=-7,
                first_seen_offset_minutes=60,
            )
        )
        # UZ / xt-xarid
        rows.append(
            make_tender(
                source_name="xt-xarid",
                external_id="x-1",
                title="Sustainability ESG audit (Uzbekistan)",
                buyer_name="Uzbekistan Railways",
                country=Country.UZ,
                matched_groups=["esg"],
                match_details={
                    "esg": {"matched_phrases": ["ESG"], "matched_tokens": []}
                },
                value_amount=Decimal("3000000.00"),
                value_currency="UZS",
                deadline_offset_days=15,
                published_offset_days=-1,
                first_seen_offset_minutes=70,
            )
        )
        rows.append(
            make_tender(
                source_name="xt-xarid",
                external_id="x-2",
                title="Credit rating methodology review",
                buyer_name="Uzbekistan Central Bank",
                country=Country.UZ,
                matched_groups=["credit_rating"],
                match_details={
                    "credit_rating": {
                        "matched_phrases": ["credit rating"],
                        "matched_tokens": [],
                    }
                },
                value_amount=Decimal("250000.00"),
                value_currency="UZS",
                deadline_offset_days=7,
                published_offset_days=-2,
                first_seen_offset_minutes=80,
            )
        )
        rows.append(
            make_tender(
                source_name="xt-xarid",
                external_id="x-3",
                title="IT infrastructure modernisation",
                buyer_name="Ministry of Digital Development",
                country=Country.UZ,
                value_amount=Decimal("500000.00"),
                value_currency="UZS",
                deadline_offset_days=25,
                published_offset_days=-3,
                first_seen_offset_minutes=90,
            )
        )
        rows.append(
            make_tender(
                source_name="xt-xarid",
                external_id="x-4",
                title="Construction of administrative building",
                buyer_name="Tashkent Municipality",
                country=Country.UZ,
                value_amount=Decimal("8000000.00"),
                value_currency="UZS",
                deadline_offset_days=40,
                published_offset_days=-5,
                first_seen_offset_minutes=100,
            )
        )
        rows.append(
            make_tender(
                source_name="xt-xarid",
                external_id="x-5",
                title="Catering for state events",
                buyer_name="Cabinet of Ministers",
                country=Country.UZ,
                value_amount=Decimal("150000.00"),
                value_currency="UZS",
                deadline_offset_days=60,
                published_offset_days=-10,
                first_seen_offset_minutes=110,
            )
        )
        rows.append(
            make_tender(
                source_name="xt-xarid",
                external_id="x-6",
                title="Translation services",
                buyer_name="Ministry of Foreign Affairs",
                country=Country.UZ,
                value_amount=None,
                value_currency=None,
                deadline_offset_days=None,
                published_offset_days=-15,
                first_seen_offset_minutes=120,
            )
        )

        session.add_all(rows)
        await session.commit()

        yield session
