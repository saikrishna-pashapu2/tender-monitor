"""HTML routes for the read-only browsing UI."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.api.deps import get_session
from tender_monitor.api.queries import (
    DEFAULT_PER_PAGE,
    DEFAULT_SORT,
    TenderFilters,
    get_tender,
    list_related_tenders,
    list_sources,
    list_tenders,
    normalize_pagination,
    normalize_sort,
    overall_counters,
    total_pages,
)
from tender_monitor.api.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def tender_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
    country: list[str] = Query(default_factory=list),
    source: list[str] = Query(default_factory=list),
    # Default to matched-only — the product brief says the UI surfaces
    # ESG / credit-rating-relevant tenders, not the entire procurement
    # firehose. Pass ``?matched=all`` to opt out of the filter.
    matched: str = "any",
    group: list[str] = Query(default_factory=list),
    q: str | None = None,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> HTMLResponse:
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
    sources_rows = await list_sources(session)
    total_tenders, total_sources, last_seen = await overall_counters(session)

    template = (
        "tenders/_results.html"
        if request.headers.get("HX-Request")
        else "tenders/list.html"
    )
    context = {
        "tenders": result.rows,
        "total": result.total,
        "page": safe_page,
        "per_page": safe_per_page,
        "pages": total_pages(result.total, safe_per_page),
        "filters": filters,
        "sources": sources_rows,
        "sort": sort_key,
        "total_tenders": total_tenders,
        "total_sources": total_sources,
        "last_seen": last_seen,
    }
    return templates.TemplateResponse(request, template, context)


@router.get("/tenders/{tender_id}", response_class=HTMLResponse)
async def tender_detail(
    request: Request,
    tender_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    tender = await get_tender(session, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="Tender not found")
    total_tenders, total_sources, last_seen = await overall_counters(session)

    lots: list[dict[str, object]] = []
    extra_fields: list[tuple[str, object]] = []
    if isinstance(tender.raw_json, dict):
        raw_lots = tender.raw_json.get("_lots")
        if isinstance(raw_lots, list):
            for entry in raw_lots:
                if isinstance(entry, dict):
                    lots.append(entry)
        # Every other top-level key becomes a row in "Additional fields
        # from source". Keys prefixed with ``_`` are internal containers
        # we render separately (``_lots`` here, ``_detail`` later when
        # the per-source detail-fetch lands).
        for key, value in sorted(tender.raw_json.items()):
            if key.startswith("_"):
                continue
            extra_fields.append((key, value))

    related = await list_related_tenders(session, tender.source_name, tender.id)

    return templates.TemplateResponse(
        request,
        "tenders/detail.html",
        {
            "tender": tender,
            "lots": lots,
            "extra_fields": extra_fields,
            "related": related,
            "total_tenders": total_tenders,
            "total_sources": total_sources,
            "last_seen": last_seen,
        },
    )


__all__ = ["router"]
