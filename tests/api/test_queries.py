from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.api.queries import (
    TenderFilters,
    get_tender,
    list_liked_tenders,
    list_related_tenders,
    list_sources,
    list_tenders,
)
from tender_monitor.core.enums import Country
from tender_monitor.core.models import TeamMember, Tender, TenderLike


async def test_list_tenders_no_filters_returns_all_paged(
    seeded_session: AsyncSession,
) -> None:
    result = await list_tenders(seeded_session, TenderFilters(), "newest", 1, 25)
    expected_total = len(
        (await seeded_session.execute(select(Tender))).scalars().all()
    )
    assert result.total == expected_total
    assert len(result.rows) == expected_total


async def test_list_tenders_filter_country(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(country=[Country.UZ]),
        "newest",
        1,
        25,
    )
    seeded_tenders = (
        await seeded_session.execute(select(Tender))
    ).scalars().all()
    expected_total = sum(1 for row in seeded_tenders if row.country is Country.UZ)
    assert result.total == expected_total
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
    seeded_tenders = (
        await seeded_session.execute(select(Tender))
    ).scalars().all()
    expected_total = sum(1 for row in seeded_tenders if row.matched_groups)
    assert result.total == expected_total
    assert all(row.matched_groups for row in result.rows)


async def test_list_tenders_filter_matched_none(seeded_session: AsyncSession) -> None:
    result = await list_tenders(
        seeded_session,
        TenderFilters(matched="none"),
        "newest",
        1,
        25,
    )
    seeded_tenders = (
        await seeded_session.execute(select(Tender))
    ).scalars().all()
    expected_total = sum(1 for row in seeded_tenders if not row.matched_groups)
    assert result.total == expected_total
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
    seeded_tenders = (
        await seeded_session.execute(select(Tender))
    ).scalars().all()
    expected_total = sum(1 for row in seeded_tenders if "esg" in row.matched_groups)
    assert result.total == expected_total
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


async def test_list_tenders_search_matches_translated_title(
    seeded_session: AsyncSession,
) -> None:
    row = (
        await seeded_session.execute(
            select(Tender).where(Tender.title == "Office supplies for the ministry")
        )
    ).scalar_one()
    row.title_en = "Translated title only marker"
    await seeded_session.flush()

    result = await list_tenders(
        seeded_session,
        TenderFilters(q="only marker"),
        "newest",
        1,
        25,
    )

    assert result.total == 1
    assert result.rows[0].id == row.id


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
    total_seeded = len(
        (await seeded_session.execute(select(Tender))).scalars().all()
    )
    assert 1 <= result.total < total_seeded
    for row in result.rows:
        assert row.published_at is not None
        assert datetime(2026, 5, 13, tzinfo=UTC) <= row.published_at
        assert row.published_at <= datetime(2026, 5, 18, tzinfo=UTC)


async def test_list_tenders_sort_newest(seeded_session: AsyncSession) -> None:
    result = await list_tenders(seeded_session, TenderFilters(), "newest", 1, 25)
    published = [row.published_at for row in result.rows]
    non_null = [p for p in published if p is not None]
    assert non_null == sorted(non_null, reverse=True)
    null_indexes = [i for i, p in enumerate(published) if p is None]
    if null_indexes:
        assert min(null_indexes) == len(non_null)


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
    page4 = await list_tenders(seeded_session, TenderFilters(), "newest", 4, 5)
    assert len(page1.rows) == 5
    assert len(page2.rows) == 5
    assert len(page3.rows) == 5
    assert len(page4.rows) == page1.total - 15
    ids = {row.id for row in page1.rows + page2.rows + page3.rows + page4.rows}
    assert len(ids) == page1.total


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


async def test_list_liked_tenders_orders_by_recent_like(
    seeded_session: AsyncSession,
) -> None:
    rows = (
        await seeded_session.execute(
            select(Tender)
            .where(Tender.source_name == "goszakup")
            .where(Tender.external_id.in_(["g-1", "g-5"]))
            .order_by(Tender.external_id.asc())
        )
    ).scalars().all()
    member = TeamMember(display_name="Sai Kumar", member_key="sai kumar")
    seeded_session.add(member)
    await seeded_session.flush()
    seeded_session.add_all(
        [
            TenderLike(
                tender_id=rows[0].id,
                team_member_id=member.id,
                created_at=datetime(2026, 5, 20, 10, 0, tzinfo=UTC),
            ),
            TenderLike(
                tender_id=rows[1].id,
                team_member_id=member.id,
                created_at=datetime(2026, 5, 20, 11, 0, tzinfo=UTC),
            ),
        ]
    )
    await seeded_session.flush()

    result = await list_liked_tenders(seeded_session)

    assert result.total == 2
    assert [row.external_id for row in result.rows] == ["g-5", "g-1"]
    assert result.rows[0].likes[0].team_member.display_name == "Sai Kumar"


async def test_list_liked_tenders_searches_title_and_buyer(
    seeded_session: AsyncSession,
) -> None:
    rows = (
        await seeded_session.execute(
            select(Tender)
            .where(Tender.external_id.in_(["g-1", "x-1"]))
            .order_by(Tender.external_id.asc())
        )
    ).scalars().all()
    member = TeamMember(display_name="Aisha", member_key="aisha")
    seeded_session.add(member)
    await seeded_session.flush()
    for index, row in enumerate(rows):
        seeded_session.add(
            TenderLike(
                tender_id=row.id,
                team_member_id=member.id,
                created_at=datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
                + timedelta(minutes=index),
            )
        )
    await seeded_session.flush()

    title_result = await list_liked_tenders(seeded_session, q="Sustainability")
    buyer_result = await list_liked_tenders(seeded_session, q="National Bank")

    assert title_result.total == 1
    assert title_result.rows[0].external_id == "x-1"
    assert buyer_result.total == 1
    assert buyer_result.rows[0].external_id == "g-1"


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
