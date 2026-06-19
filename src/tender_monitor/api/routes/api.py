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
    list_liked_tenders,
    list_sources,
    list_tenders,
    normalize_pagination,
    normalize_sort,
    total_pages,
)
from tender_monitor.core.schemas import (
    SourceRead,
    TeamMemberRead,
    TenderLikeCreate,
    TenderLikeRead,
    TenderLikeState,
    TenderRead,
    TenderSummary,
)
from tender_monitor.likes import like_tender, list_tender_likes, unlike_tender
from tender_monitor.team import list_team_members

router = APIRouter(prefix="/api")


class TenderListResponse(BaseModel):
    tenders: list[TenderSummary]
    total: int
    page: int
    per_page: int
    pages: int


async def _tender_like_state(
    session: AsyncSession,
    tender_id: UUID,
) -> TenderLikeState:
    likes = await list_tender_likes(session, tender_id)
    return TenderLikeState(
        tender_id=tender_id,
        like_count=len(likes),
        likes=[TenderLikeRead.model_validate(like) for like in likes],
    )


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


@router.get("/liked-tenders", response_model=TenderListResponse)
async def api_list_liked_tenders(
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> TenderListResponse:
    safe_page, safe_per_page = normalize_pagination(page, per_page)
    result = await list_liked_tenders(
        session,
        q=q,
        page=safe_page,
        per_page=safe_per_page,
    )
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


@router.post("/tenders/{tender_id}/likes", response_model=TenderLikeState)
async def api_like_tender(
    tender_id: UUID,
    payload: TenderLikeCreate,
    session: AsyncSession = Depends(get_session),
) -> TenderLikeState:
    try:
        like = await like_tender(
            session,
            tender_id=tender_id,
            member_name=payload.member_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if like is None:
        raise HTTPException(status_code=404, detail="Tender not found")
    await session.commit()
    return await _tender_like_state(session, tender_id)


@router.delete(
    "/tenders/{tender_id}/likes/{member_key}",
    response_model=TenderLikeState,
)
async def api_unlike_tender(
    tender_id: UUID,
    member_key: str,
    session: AsyncSession = Depends(get_session),
) -> TenderLikeState:
    result = await unlike_tender(
        session,
        tender_id=tender_id,
        member_key=member_key,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Tender not found")
    await session.commit()
    return await _tender_like_state(session, tender_id)


@router.get("/sources", response_model=list[SourceRead])
async def api_list_sources(
    session: AsyncSession = Depends(get_session),
) -> list[SourceRead]:
    rows = await list_sources(session)
    return [SourceRead.model_validate(row) for row in rows]


@router.get("/team-members", response_model=list[TeamMemberRead])
async def api_list_team_members(
    session: AsyncSession = Depends(get_session),
) -> list[TeamMemberRead]:
    rows = await list_team_members(session)
    return [TeamMemberRead.model_validate(row) for row in rows]


__all__ = ["TenderListResponse", "router"]
