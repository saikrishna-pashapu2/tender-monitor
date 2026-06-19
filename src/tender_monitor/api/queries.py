"""Read-only DB query helpers powering the web UI and JSON API.

All filter logic lives here. Routes are thin: parse query params into
``TenderFilters``, call one of the helpers below, and render. The same
helpers back both the HTML pages and the JSON API so behavior cannot
drift between the two surfaces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypeVar
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tender_monitor.core.enums import Country
from tender_monitor.core.models import Source, Tender, TenderLike

SortKey = Literal["newest", "deadline", "value_desc", "value_asc"]
SORT_KEYS: tuple[str, ...] = ("newest", "deadline", "value_desc", "value_asc")
DEFAULT_SORT: SortKey = "newest"

MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 25

MatchedFilter = Literal["any", "none"]


@dataclass(slots=True)
class TenderFilters:
    country: list[Country] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    matched: MatchedFilter | None = None
    group: list[str] = field(default_factory=list)
    q: str | None = None
    from_: datetime | None = None
    to: datetime | None = None

    @classmethod
    def from_query(
        cls,
        *,
        country: list[str] | None = None,
        source: list[str] | None = None,
        matched: str | None = None,
        group: list[str] | None = None,
        q: str | None = None,
        from_: str | None = None,
        to: str | None = None,
    ) -> TenderFilters:
        country_values: list[Country] = []
        for raw in country or []:
            value = raw.strip().upper()
            if not value:
                continue
            try:
                country_values.append(Country(value))
            except ValueError:
                continue

        source_values = [s.strip() for s in (source or []) if s and s.strip()]
        group_values = [g.strip() for g in (group or []) if g and g.strip()]

        # Three legal values from the URL:
        #   "any"  → only matched tenders
        #   "none" → only unmatched tenders
        #   "all"  → no match filter applied (None on the dataclass)
        # Anything else (including the empty string) is treated as
        # "all" so users can clear the filter by submitting an empty
        # value in the UI.
        matched_value: MatchedFilter | None = (
            matched  # type: ignore[assignment]
            if matched in ("any", "none")
            else None
        )

        query_value = q.strip() if q and q.strip() else None

        return cls(
            country=country_values,
            source=source_values,
            matched=matched_value,
            group=group_values,
            q=query_value,
            from_=_parse_iso_date(from_),
            to=_parse_iso_date(to),
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.country
            or self.source
            or self.matched
            or self.group
            or self.q
            or self.from_
            or self.to
        )


def _parse_iso_date(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 parser. Returns None on any failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(slots=True)
class ListResult:
    rows: list[Tender]
    total: int


_SelectT = TypeVar("_SelectT", bound=Select)  # type: ignore[type-arg]


def _apply_filters(stmt: _SelectT, filters: TenderFilters) -> _SelectT:
    if filters.country:
        stmt = stmt.where(Tender.country.in_(filters.country))
    if filters.source:
        stmt = stmt.where(Tender.source_name.in_(filters.source))

    if filters.matched == "any":
        stmt = stmt.where(func.coalesce(func.array_length(Tender.matched_groups, 1), 0) > 0)
    elif filters.matched == "none":
        stmt = stmt.where(func.coalesce(func.array_length(Tender.matched_groups, 1), 0) == 0)

    for group_name in filters.group:
        # ``elem = ANY(matched_groups)`` via array_position — the base
        # ARRAY type doesn't expose the @> operator and migrating to
        # the dialect-specific ARRAY just for this is overkill.
        stmt = stmt.where(
            func.array_position(Tender.matched_groups, group_name).is_not(None)
        )

    if filters.q:
        like = f"%{filters.q}%"
        stmt = stmt.where(
            or_(
                Tender.title.ilike(like),
                Tender.title_en.ilike(like),
                Tender.buyer_name.ilike(like),
            )
        )

    if filters.from_:
        stmt = stmt.where(Tender.published_at >= filters.from_)
    if filters.to:
        stmt = stmt.where(Tender.published_at <= filters.to)

    return stmt


def _apply_search(stmt: _SelectT, q: str | None) -> _SelectT:
    if not q:
        return stmt
    like = f"%{q}%"
    return stmt.where(
        or_(
            Tender.title.ilike(like),
            Tender.title_en.ilike(like),
            Tender.buyer_name.ilike(like),
        )
    )


def _apply_sort(stmt: _SelectT, sort: str) -> _SelectT:
    if sort == "deadline":
        return stmt.order_by(Tender.deadline_at.asc().nulls_last(), Tender.id.asc())
    if sort == "value_desc":
        return stmt.order_by(Tender.value_amount.desc().nulls_last(), Tender.id.asc())
    if sort == "value_asc":
        return stmt.order_by(Tender.value_amount.asc().nulls_last(), Tender.id.asc())
    # "Newest first" in the UI refers to the tender's upstream publish
    # time, not when our scheduler first ingested the row.
    return stmt.order_by(
        Tender.published_at.desc().nulls_last(),
        Tender.first_seen_at.desc().nulls_last(),
        Tender.id.asc(),
    )


def normalize_sort(sort: str | None) -> SortKey:
    if sort in SORT_KEYS:
        return sort  # type: ignore[return-value]
    return DEFAULT_SORT


def normalize_pagination(page: int, per_page: int) -> tuple[int, int]:
    safe_page = max(1, page)
    safe_per_page = max(1, min(per_page, MAX_PER_PAGE))
    return safe_page, safe_per_page


async def list_tenders(
    session: AsyncSession,
    filters: TenderFilters,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> ListResult:
    """Paginated tender list with filtering + sorting.

    Runs a count query in parallel with the data query so the UI can
    show "X of Y" without overfetching. ``ListResult.rows`` is empty
    when the page is past the end; the caller decides how to surface
    that.
    """
    sort_key = normalize_sort(sort)
    safe_page, safe_per_page = normalize_pagination(page, per_page)

    base = _apply_filters(
        select(Tender).options(
            selectinload(Tender.likes).selectinload(TenderLike.team_member)
        ),
        filters,
    )
    total = (
        await session.execute(
            _apply_filters(select(func.count()).select_from(Tender), filters)
        )
    ).scalar_one()

    data_stmt = (
        _apply_sort(base, sort_key)
        .offset((safe_page - 1) * safe_per_page)
        .limit(safe_per_page)
    )
    rows = (await session.execute(data_stmt)).scalars().all()
    return ListResult(rows=list(rows), total=int(total))


async def get_tender(session: AsyncSession, tender_id: UUID) -> Tender | None:
    stmt = (
        select(Tender)
        .options(selectinload(Tender.likes).selectinload(TenderLike.team_member))
        .where(Tender.id == tender_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# Today the busiest source has ~600 rows; 1000 is the upper bound the
# UI can render without becoming a memory hog. If a future source
# scales past this, we'd move the sidebar to paginated / lazy loading.
RELATED_LIMIT_MAX = 1000


async def list_related_tenders(
    session: AsyncSession,
    source_name: str,
    exclude_id: UUID,
    limit: int | None = None,
) -> list[Tender]:
    """All recent tenders from the same source, for the detail-page sidebar.

    Includes matched + unmatched on purpose — the home list filters to
    matched-only, but an analyst on a detail page benefits from seeing
    everything else the source has published recently in one glance.
    Ordered newest-first by ``first_seen_at``, excluding the current row.

    Default ``limit=None`` returns up to ``RELATED_LIMIT_MAX`` rows.
    """
    effective_limit = limit if limit is not None else RELATED_LIMIT_MAX
    stmt = (
        select(Tender)
        .options(selectinload(Tender.likes).selectinload(TenderLike.team_member))
        .where(Tender.source_name == source_name)
        .where(Tender.id != exclude_id)
        .order_by(Tender.first_seen_at.desc().nulls_last(), Tender.id.asc())
        .limit(effective_limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_liked_tenders(
    session: AsyncSession,
    *,
    q: str | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> ListResult:
    """Paginated tenders with at least one like, newest like first."""
    safe_page, safe_per_page = normalize_pagination(page, per_page)
    query_value = q.strip() if q and q.strip() else None
    latest_like = (
        select(
            TenderLike.tender_id.label("tender_id"),
            func.max(TenderLike.created_at).label("liked_at"),
        )
        .group_by(TenderLike.tender_id)
        .subquery()
    )

    count_stmt = _apply_search(
        select(func.count())
        .select_from(Tender)
        .join(latest_like, latest_like.c.tender_id == Tender.id),
        query_value,
    )
    total = (await session.execute(count_stmt)).scalar_one()

    data_stmt = (
        _apply_search(
            select(Tender)
            .options(selectinload(Tender.likes).selectinload(TenderLike.team_member))
            .join(latest_like, latest_like.c.tender_id == Tender.id),
            query_value,
        )
        .order_by(latest_like.c.liked_at.desc().nulls_last(), Tender.id.asc())
        .offset((safe_page - 1) * safe_per_page)
        .limit(safe_per_page)
    )
    rows = (await session.execute(data_stmt)).scalars().all()
    return ListResult(rows=list(rows), total=int(total))


async def list_sources(session: AsyncSession) -> list[Source]:
    stmt = select(Source).order_by(Source.display_name.asc())
    return list((await session.execute(stmt)).scalars().all())


async def overall_counters(session: AsyncSession) -> tuple[int, int, datetime | None]:
    """(total_tenders, total_sources, last_seen_at across all tenders).

    Used by the base template's footer / nav. Cheap enough to run on
    every render against the indexed columns.
    """
    total_tenders = (await session.execute(select(func.count()).select_from(Tender))).scalar_one()
    total_sources = (await session.execute(select(func.count()).select_from(Source))).scalar_one()
    last_seen = (await session.execute(select(func.max(Tender.last_seen_at)))).scalar_one()
    return int(total_tenders), int(total_sources), last_seen


def total_pages(total: int, per_page: int) -> int:
    if total <= 0 or per_page <= 0:
        return 1
    return max(1, math.ceil(total / per_page))


__all__ = [
    "DEFAULT_PER_PAGE",
    "DEFAULT_SORT",
    "MAX_PER_PAGE",
    "RELATED_LIMIT_MAX",
    "SORT_KEYS",
    "ListResult",
    "SortKey",
    "TenderFilters",
    "get_tender",
    "list_liked_tenders",
    "list_related_tenders",
    "list_sources",
    "list_tenders",
    "normalize_pagination",
    "normalize_sort",
    "overall_counters",
    "total_pages",
]
