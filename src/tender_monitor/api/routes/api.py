"""JSON API routes mirroring the HTML browsing UI."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.api.deps import get_session
from tender_monitor.api.queries import (
    DEFAULT_PER_PAGE,
    DEFAULT_SORT,
    TenderFilters,
    get_tender,
    list_sources,
    list_tenders,
    normalize_pagination,
    normalize_sort,
    total_pages,
)
from tender_monitor.core.schemas import SourceRead, TenderRead, TenderSummary

router = APIRouter(prefix="/api")


class TenderListResponse(BaseModel):
    tenders: list[TenderSummary]
    total: int
    page: int
    per_page: int
    pages: int


@router.get("/tenders", response_model=TenderListResponse)
async def api_list_tenders(
    session: AsyncSession = Depends(get_session),
    country: list[str] = Query(default_factory=list),
    source: list[str] = Query(default_factory=list),
    # Same default as the HTML route: matched-only unless ``matched=all``.
    matched: str = "any",
    group: list[str] = Query(default_factory=list),
    q: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> TenderListResponse:
    filters = TenderFilters.from_query(
        country=country,
        source=source,
        matched=matched,
        group=group,
        q=q,
        from_=from_,
        to=to,
    )
    sort_key = normalize_sort(sort)
    safe_page, safe_per_page = normalize_pagination(page, per_page)
    result = await list_tenders(session, filters, sort_key, safe_page, safe_per_page)
    return TenderListResponse(
        tenders=[TenderSummary.model_validate(row) for row in result.rows],
        total=result.total,
        page=safe_page,
        per_page=safe_per_page,
        pages=total_pages(result.total, safe_per_page),
    )


@router.get("/tenders/{tender_id}", response_model=TenderRead)
async def api_get_tender(
    tender_id: UUID, session: AsyncSession = Depends(get_session)
) -> TenderRead:
    tender = await get_tender(session, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="Tender not found")
    return TenderRead.model_validate(tender)


@router.get("/sources", response_model=list[SourceRead])
async def api_list_sources(
    session: AsyncSession = Depends(get_session),
) -> list[SourceRead]:
    rows = await list_sources(session)
    return [SourceRead.model_validate(row) for row in rows]


__all__ = ["TenderListResponse", "router"]
