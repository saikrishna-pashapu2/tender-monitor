from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.api.queries import (
    TenderFilters,
    get_tender,
    list_related_tenders,
    list_sources,
    list_tenders,
)
from tender_monitor.core.enums import Country
from tender_monitor.core.models import Tender


async def test_list_tenders_no_filters_returns_all_paged(
    seeded_session: AsyncSession,
) -> None:
    result = await list_tenders(seeded_session, TenderFilters(), "newest", 1, 25)
    assert result.total == 12
    assert len(result.rows) == 12


async def test_list_tenders_filter_country(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(country=[Country.UZ]),
        "newest",
        1,
        25,
    )
    assert result.total == 6
    assert all(row.country is Country.UZ for row in result.rows)


async def test_list_tenders_filter_source(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(source=["goszakup"]),
        "newest",
        1,
        25,
    )
    assert result.total == 6
    assert all(row.source_name == "goszakup" for row in result.rows)


async def test_list_tenders_filter_matched_any(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(matched="any"),
        "newest",
        1,
        25,
    )
    assert result.total == 6
    assert all(row.matched_groups for row in result.rows)


async def test_list_tenders_filter_matched_none(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(matched="none"),
        "newest",
        1,
        25,
    )
    assert result.total == 6
    assert all(row.matched_groups == [] for row in result.rows)


async def test_list_tenders_filter_specific_group(
    seeded_session: AsyncSession,
) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(group=["esg"]),
        "newest",
        1,
        25,
    )
    assert result.total == 3
    assert all("esg" in row.matched_groups for row in result.rows)


async def test_list_tenders_search_matches_title(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(q="ESG"),
        "newest",
        1,
        25,
    )
    titles = {row.title for row in result.rows}
    # 'ESG consulting framework', 'ESG and credit rating advisory',
    # 'Sustainability ESG audit (Uzbekistan)'
    assert result.total == 3
    assert any("ESG" in title for title in titles)


async def test_list_tenders_search_matches_buyer(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(q="Halyk"),
        "newest",
        1,
        25,
    )
    assert result.total == 1
    assert result.rows[0].buyer_name == "Halyk Bank"


async def test_list_tenders_search_case_insensitive(
    seeded_session: AsyncSession,
) -> None:
    upper = await list_tenders(
        seeded_session, TenderFilters(q="CREDIT"), "newest", 1, 25
    )
    lower = await list_tenders(
        seeded_session, TenderFilters(q="credit"), "newest", 1, 25
    )
    assert upper.total == lower.total > 0


async def test_list_tenders_date_range_filter(seeded_session: AsyncSession) -> None:
    # Seed dates are anchored at T0=2026-05-18 with negative offsets.
    result = await list_tenders(
        seeded_session,
        TenderFilters(
            from_=datetime(2026, 5, 13, tzinfo=UTC),
            to=datetime(2026, 5, 18, tzinfo=UTC),
        ),
        "newest",
        1,
        25,
    )
    assert 1 <= result.total < 12
    for row in result.rows:
        assert row.published_at is not None
        assert datetime(2026, 5, 13, tzinfo=UTC) <= row.published_at
        assert row.published_at <= datetime(2026, 5, 18, tzinfo=UTC)


async def test_list_tenders_sort_newest(seeded_session: AsyncSession) -> None:
    result = await list_tenders(seeded_session, TenderFilters(), "newest", 1, 25)
    seen = [row.first_seen_at for row in result.rows]
    assert seen == sorted(seen, reverse=True)


async def test_list_tenders_sort_deadline(seeded_session: AsyncSession) -> None:
    result = await list_tenders(seeded_session, TenderFilters(), "deadline", 1, 25)
    deadlines = [row.deadline_at for row in result.rows]
    # nulls last → all None values cluster at the end
    non_null = [d for d in deadlines if d is not None]
    assert non_null == sorted(non_null)
    null_indexes = [i for i, d in enumerate(deadlines) if d is None]
    if null_indexes:
        assert min(null_indexes) == len(non_null)


async def test_list_tenders_sort_value_desc(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session, TenderFilters(), "value_desc", 1, 25
    )
    values = [row.value_amount for row in result.rows if row.value_amount is not None]
    assert values == sorted(values, reverse=True)


async def test_list_tenders_pagination_returns_correct_slice(
    seeded_session: AsyncSession,
) -> None:
    page1 = await list_tenders(seeded_session, TenderFilters(), "newest", 1, 5)
    page2 = await list_tenders(seeded_session, TenderFilters(), "newest", 2, 5)
    page3 = await list_tenders(seeded_session, TenderFilters(), "newest", 3, 5)
    assert len(page1.rows) == 5
    assert len(page2.rows) == 5
    assert len(page3.rows) == 2
    ids = {row.id for row in page1.rows + page2.rows + page3.rows}
    assert len(ids) == 12


async def test_list_tenders_combined_filters(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(country=[Country.KZ], group=["esg"]),
        "newest",
        1,
        25,
    )
    assert result.total == 2
    for row in result.rows:
        assert row.country is Country.KZ
        assert "esg" in row.matched_groups


async def test_get_tender_returns_full_row(seeded_session: AsyncSession) -> None:
    one = (await seeded_session.execute(select(Tender).limit(1))).scalar_one()
    fetched = await get_tender(seeded_session, one.id)
    assert fetched is not None
    assert fetched.id == one.id
    assert fetched.title == one.title


async def test_get_tender_unknown_id_returns_none(
    seeded_session: AsyncSession,
) -> None:
    result = await get_tender(seeded_session, uuid4())
    assert result is None


async def test_list_related_tenders_returns_same_source_only(
    seeded_session: AsyncSession,
) -> None:
    one = (await seeded_session.execute(select(Tender).limit(1))).scalar_one()
    related = await list_related_tenders(seeded_session, one.source_name, one.id)
    assert len(related) >= 1
    for r in related:
        assert r.source_name == one.source_name
        assert r.id != one.id


async def test_list_related_tenders_includes_unmatched(
    seeded_session: AsyncSession,
) -> None:
    # goszakup seed has 4 matched + 2 unmatched. Sidebar must surface
    # both classes — the home list filters matched-only, here we don't.
    one = (
        await seeded_session.execute(
            select(Tender).where(Tender.source_name == "goszakup").limit(1)
        )
    ).scalar_one()
    related = await list_related_tenders(seeded_session, "goszakup", one.id)
    assert len(related) == 5  # 6 goszakup rows, minus the excluded one
    assert any(r.matched_groups == [] for r in related)
    assert any(r.matched_groups != [] for r in related)


async def test_list_sources_ordered_by_display_name(
    seeded_session: AsyncSession,
) -> None:
    rows = await list_sources(seeded_session)
    names = [r.display_name for r in rows]
    assert names == sorted(names)


def test_tender_filters_from_query_parses_country() -> None:
    filters = TenderFilters.from_query(country=["kz", "UZ", "bogus"])
    assert filters.country == [Country.KZ, Country.UZ]


def test_tender_filters_from_query_ignores_invalid_matched() -> None:
    filters = TenderFilters.from_query(matched="wat")
    assert filters.matched is None


def test_tender_filters_from_query_parses_dates() -> None:
    filters = TenderFilters.from_query(from_="2026-01-01", to="not-a-date")
    assert filters.from_ == datetime(2026, 1, 1)
    assert filters.to is None


@pytest.mark.parametrize("matched_value", ["any", "none"])
def test_tender_filters_from_query_accepts_matched(matched_value: str) -> None:
    filters = TenderFilters.from_query(matched=matched_value)
    assert filters.matched == matched_value
