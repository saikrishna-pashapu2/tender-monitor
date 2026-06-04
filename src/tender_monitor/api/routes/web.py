"""HTML routes for the read-only browsing UI."""

from __future__ import annotations

from typing import Any
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


def _display_source_key(key: str) -> str:
    return key.lstrip("_")


def _extract_source_facts(
    raw_json: dict[str, Any],
) -> list[tuple[str, object]]:
    omitted = {
        "_documents",
        "_lots",
        "title",
        "buyer_name",
        "buyer_external_id",
        "source_url",
    }
    facts: list[tuple[str, object]] = []
    for key, value in sorted(raw_json.items()):
        if key in omitted or key.startswith("__"):
            continue
        if isinstance(value, dict):
            continue
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            continue
        facts.append((_display_source_key(key), value))
    return facts


def _extract_source_sections(
    raw_json: dict[str, Any],
) -> list[tuple[str, object]]:
    sections: list[tuple[str, object]] = []
    for key, value in sorted(raw_json.items()):
        if key in {"_documents", "_lots"}:
            continue
        if isinstance(value, dict) and value:
            sections.append((_display_source_key(key), value))
            continue
        if isinstance(value, list) and value and any(
            isinstance(item, dict | list) for item in value
        ):
            sections.append((_display_source_key(key), value))
    return sections


def _extract_documents(raw_json: object) -> list[dict[str, object]]:
    if not isinstance(raw_json, dict):
        return []

    raw_documents = raw_json.get("_documents")
    if not isinstance(raw_documents, list):
        return []

    documents: list[dict[str, object]] = []
    for item in raw_documents:
        if isinstance(item, dict):
            documents.append(item)
    return documents


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
    documents: list[dict[str, object]] = []
    source_facts: list[tuple[str, object]] = []
    source_sections: list[tuple[str, object]] = []
    if isinstance(tender.raw_json, dict):
        documents = _extract_documents(tender.raw_json)
        raw_lots = tender.raw_json.get("_lots")
        if isinstance(raw_lots, list):
            for entry in raw_lots:
                if isinstance(entry, dict):
                    lots.append(entry)
        source_facts = _extract_source_facts(tender.raw_json)
        source_sections = _extract_source_sections(tender.raw_json)

    related = await list_related_tenders(session, tender.source_name, tender.id)

    return templates.TemplateResponse(
        request,
        "tenders/detail.html",
        {
            "tender": tender,
            "lots": lots,
            "documents": documents,
            "source_facts": source_facts,
            "source_sections": source_sections,
            "related": related,
            "total_tenders": total_tenders,
            "total_sources": total_sources,
            "last_seen": last_seen,
        },
    )


__all__ = ["router"]
