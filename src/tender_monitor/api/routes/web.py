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
from tender_monitor.api.templating import (
    humanize_key,
    is_list_of_scalars,
    is_scalar,
    templates,
)

router = APIRouter()


SOURCE_DETAIL_OMITTED_KEYS = {
    "_documents",
    "_lots",
    "title",
    "buyer_name",
    "buyer_external_id",
    "source_url",
}

SOURCE_GROUP_ORDER = (
    "Identity",
    "Timeline",
    "Commercial",
    "Parties",
    "Process",
    "References",
    "Additional facts",
)

SOURCE_SECTION_TITLES = {
    "detail_fields": "Announcement profile",
    "_detail": "Tender record",
    "_parsed_detail": "Parsed embedded detail",
    "announcement_lots": "Announcement lots",
    "_announcement_lots": "Announcement lots",
    "fields": "Criteria forms",
    "qualification_fields": "Qualification forms",
    "js_fields": "Dynamic criteria",
    "js_qualification_fields": "Qualification prompts",
}

UZEX_DOCUMENT_TAB_ORDER = (
    "Technical specifications and expert opinion",
    "Technical documentation",
    "Protocols",
    "Contracts",
    "Other files",
)


def _display_source_key(key: str) -> str:
    return key.lstrip("_")


def _is_empty_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | dict | tuple | set):
        return len(value) == 0
    return False


def _bucket_source_field(key: str) -> str:
    normalized = key.casefold()
    if any(
        token in normalized
        for token in (
            "number",
            "id",
            "display",
            "code",
            "label",
            "title",
            "category",
            "classification",
            "name",
        )
    ):
        return "Identity"
    if any(
        token in normalized
        for token in (
            "date",
            "time",
            "start",
            "end",
            "deadline",
            "created",
            "updated",
            "opening",
            "placement",
            "submit",
        )
    ):
        return "Timeline"
    if any(
        token in normalized
        for token in (
            "amount",
            "sum",
            "cost",
            "price",
            "currency",
            "deposit",
            "advance",
            "value",
            "point",
        )
    ):
        return "Commercial"
    if any(
        token in normalized
        for token in (
            "buyer",
            "customer",
            "seller",
            "organizer",
            "address",
            "contact",
            "region",
            "district",
            "bin",
            "tin",
            "inn",
            "fullname",
            "job",
        )
    ):
        return "Parties"
    if any(
        token in normalized
        for token in (
            "status",
            "method",
            "procedure",
            "language",
            "rule",
            "type",
            "source",
            "qualification",
            "financing",
            "evaluation",
        )
    ):
        return "Process"
    if "url" in normalized or "link" in normalized or "path" in normalized:
        return "References"
    return "Additional facts"


def _sequence_item_title(item: dict[str, Any], index: int) -> str:
    for key in (
        "label",
        "name",
        "name_ru",
        "title",
        "title_ru_detail",
        "number",
        "display_no",
        "id",
        "Id",
    ):
        value = item.get(key)
        if not _is_empty_value(value):
            return str(value)
    return f"Item {index}"


def _present_value(value: object) -> dict[str, object]:
    if is_scalar(value):
        return {"kind": "scalar", "value": value}
    if is_list_of_scalars(value):
        assert isinstance(value, list)
        return {"kind": "scalar_list", "items": value}
    if isinstance(value, dict):
        rows: list[dict[str, object]] = []
        for key, nested_value in value.items():
            if _is_empty_value(nested_value):
                continue
            rows.append(
                {
                    "label": humanize_key(_display_source_key(key)),
                    "value": _present_value(nested_value),
                }
            )
        return {"kind": "mapping", "rows": rows}
    if isinstance(value, list):
        items: list[dict[str, object]] = []
        for index, item in enumerate(value, start=1):
            if _is_empty_value(item):
                continue
            if isinstance(item, dict):
                rows = []
                for key, nested_value in item.items():
                    if _is_empty_value(nested_value):
                        continue
                    rows.append(
                        {
                            "label": humanize_key(_display_source_key(key)),
                            "value": _present_value(nested_value),
                        }
                    )
                items.append(
                    {
                        "kind": "mapping",
                        "title": _sequence_item_title(item, index),
                        "rows": rows,
                    }
                )
                continue
            items.append(
                {
                    "kind": "scalar",
                    "title": f"Item {index}",
                    "value": item,
                }
            )
        return {"kind": "sequence", "items": items}
    return {"kind": "scalar", "value": value}


def _extract_source_groups(raw_json: dict[str, Any]) -> list[dict[str, object]]:
    grouped_rows: dict[str, list[dict[str, object]]] = {
        title: [] for title in SOURCE_GROUP_ORDER
    }
    for key, value in raw_json.items():
        if key in SOURCE_DETAIL_OMITTED_KEYS or key.startswith("__"):
            continue
        if _is_empty_value(value):
            continue
        if isinstance(value, dict):
            continue
        if isinstance(value, list) and any(
            isinstance(item, dict | list) for item in value
        ):
            continue
        bucket = _bucket_source_field(key)
        grouped_rows[bucket].append(
            {
                "label": humanize_key(_display_source_key(key)),
                "value": _present_value(value),
            }
        )

    groups: list[dict[str, object]] = []
    for title in SOURCE_GROUP_ORDER:
        rows = grouped_rows.get(title) or []
        if rows:
            groups.append({"title": title, "rows": rows})
    return groups


def _section_summary(value: object) -> str | None:
    if isinstance(value, dict):
        return f"{len(value)} fields"
    if isinstance(value, list):
        return f"{len(value)} item{'s' if len(value) != 1 else ''}"
    return None


def _extract_source_sections(raw_json: dict[str, Any]) -> list[dict[str, object]]:
    sections: list[dict[str, object]] = []
    for key, value in raw_json.items():
        if key in {"_documents", "_lots"} or _is_empty_value(value):
            continue
        if not (
            isinstance(value, dict)
            or (
                isinstance(value, list)
                and any(isinstance(item, dict | list) for item in value)
            )
        ):
            continue
        sections.append(
            {
                "title": SOURCE_SECTION_TITLES.get(
                    key, humanize_key(_display_source_key(key))
                ),
                "summary": _section_summary(value),
                "content": _present_value(value),
            }
        )
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


def _first_present(mapping: dict[str, Any], *keys: str) -> object | None:
    for key in keys:
        value = mapping.get(key)
        if not _is_empty_value(value):
            return value
    return None


def _uzex_document_tab_label(document: dict[str, object]) -> str:
    category = str(document.get("category") or "").casefold()
    if any(token in category for token in ("protocol", "conclusion")):
        return "Protocols"
    if any(token in category for token in ("contract", "agreement", "deal")):
        return "Contracts"
    if any(
        token in category
        for token in ("technical document", "criteria form", "qualification")
    ):
        return "Technical documentation"
    if any(token in category for token in ("technical attachment", "expertise")):
        return "Technical specifications and expert opinion"
    return "Other files"


def _build_uzex_info_rows(
    tender_raw_json: dict[str, Any],
    parsed_detail: dict[str, Any],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    consumed_keys: set[str] = set()
    candidates = (
        ("Customer's Taxpayer Identification Number", ("customer_tin", "customer_inn")),
        ("Customer name", ("customer_name",)),
        ("Registration form", ("registration_form", "registration_form_name", "purchase_type")),
        (
            "Proposal evaluation method",
            ("evaluation_method", "proposal_evaluation_method", "winner_method_detail"),
        ),
        (
            "Procedure for considering proposals",
            ("procedure_for_considering_proposals", "procedure_considering", "submit_type"),
        ),
        ("Deposit", ("deposit", "deposit_required")),
        ("Letter of guarantee", ("letter_of_guarantee", "guarantee_letter")),
        ("Deposit size", ("deposit_size",)),
        ("Advance payment amount", ("advance_payment_amount", "prepayment_amount")),
        ("Unlock method", ("unlock_method",)),
        ("Payment order", ("payment_order",)),
        ("Placement term", ("placement_term",)),
        ("Opening date", ("opening_date",)),
        ("Payment term", ("payment_term", "payment_term_full")),
        ("Customer address", ("customer_address", "address")),
        ("Delivery address", ("delivery_address",)),
        ("Submit languages", ("submit_languages",)),
        ("Status", ("status_name",)),
        ("Financing source", ("financing_source",)),
        ("Additional information", ("additional_information", "addon_description")),
        ("Technical description", ("technical_description",)),
        ("Min. point", ("min_point", "minimum_point")),
        ("Number of views", ("views", "number_of_views")),
        ("Contact number", ("contact_number", "phone")),
        ("Special conditions", ("special_conditions",)),
    )

    for label, keys in candidates:
        value = _first_present(tender_raw_json, *keys)
        if value is None and label == "Submit languages":
            languages = parsed_detail.get("languages")
            if isinstance(languages, list) and languages:
                value = ", ".join(
                    str(item.get("Name") or item.get("name"))
                    for item in languages
                    if isinstance(item, dict)
                    and not _is_empty_value(item.get("Name") or item.get("name"))
                )
        if value is None:
            continue
        consumed_keys.update(keys)
        rows.append({"label": label, "value": value})

    omitted_keys = {
        "_detail",
        "_documents",
        "_listing_deal_id",
        "_listing_status_id",
        "_listing_status_name",
        "_lots",
        "_parsed_detail",
        "addon_description",
        "budget_products",
        "contacts",
        "fields",
        "js_fields",
        "js_qualification_fields",
        "languages",
        "qualification_fields",
        "technical_description",
    }

    for mapping in (tender_raw_json, parsed_detail):
        for key, value in mapping.items():
            if key in consumed_keys or key in omitted_keys:
                continue
            if _is_empty_value(value):
                continue
            if not is_scalar(value) and not is_list_of_scalars(value):
                continue
            rows.append(
                {
                    "label": humanize_key(_display_source_key(key)),
                    "value": value,
                }
            )
    return rows


def _extract_uzex_view(
    tender_raw_json: dict[str, Any],
    *,
    documents: list[dict[str, object]],
    lots: list[dict[str, object]],
) -> dict[str, object]:
    parsed_detail = tender_raw_json.get("_parsed_detail")
    parsed_detail_dict = parsed_detail if isinstance(parsed_detail, dict) else {}
    budget_products = parsed_detail_dict.get("budget_products")
    budget_products_list = (
        budget_products if isinstance(budget_products, list) else []
    )
    contacts = parsed_detail_dict.get("contacts")
    contacts_list = contacts if isinstance(contacts, list) else []
    languages = parsed_detail_dict.get("languages")
    languages_list = languages if isinstance(languages, list) else []
    technical_items = [
        item
        for item in tender_raw_json.get("js_fields", [])
        if isinstance(item, dict)
    ]
    qualification_items = [
        item
        for item in tender_raw_json.get("js_qualification_fields", [])
        if isinstance(item, dict)
    ]
    technical_forms = parsed_detail_dict.get("fields")
    qualification_forms = parsed_detail_dict.get("qualification_fields")
    technical_forms_list = (
        technical_forms if isinstance(technical_forms, list) else []
    )
    qualification_forms_list = (
        qualification_forms if isinstance(qualification_forms, list) else []
    )

    tab_docs: dict[str, list[dict[str, object]]] = {
        label: [] for label in UZEX_DOCUMENT_TAB_ORDER
    }
    for document in documents:
        tab_docs[_uzex_document_tab_label(document)].append(document)

    document_tabs = [
        {"label": label, "documents": tab_docs[label]}
        for label in UZEX_DOCUMENT_TAB_ORDER
        if tab_docs[label]
    ]

    primary_lot = lots[0] if lots else {}
    primary_budget = (
        budget_products_list[0]
        if budget_products_list and isinstance(budget_products_list[0], dict)
        else {}
    )
    primary_lot_rows = [
        {
            "label": humanize_key(_display_source_key(key)),
            "value": value,
        }
        for key, value in primary_lot.items()
        if not _is_empty_value(value)
    ]
    primary_budget_rows = [
        {
            "label": humanize_key(_display_source_key(key)),
            "value": value,
        }
        for key, value in primary_budget.items()
        if not _is_empty_value(value)
    ]
    info_rows = _build_uzex_info_rows(tender_raw_json, parsed_detail_dict)

    return {
        "reference_no": tender_raw_json.get("display_no") or tender_raw_json.get("id"),
        "status_name": tender_raw_json.get("status_name"),
        "timeline_rows": [
            {"label": "Start date", "value": tender_raw_json.get("start_date")},
            {"label": "End date", "value": tender_raw_json.get("end_date")},
            {
                "label": "Total starting price",
                "value": tender_raw_json.get("start_cost"),
                "currency": tender_raw_json.get("currency_codeabc"),
            },
        ],
        "title": tender_raw_json.get("name") or primary_lot.get("name_ru"),
        "lot_title": tender_raw_json.get("name") or primary_lot.get("name_ru"),
        "lot_description": (
            primary_lot.get("description_ru")
            or tender_raw_json.get("technical_description")
            or tender_raw_json.get("addon_description")
        ),
        "primary_lot_rows": primary_lot_rows,
        "budget_products": budget_products_list,
        "primary_budget": primary_budget,
        "primary_budget_rows": primary_budget_rows,
        "contacts": contacts_list,
        "languages": languages_list,
        "technical_items": technical_items,
        "technical_forms": technical_forms_list,
        "qualification_items": qualification_items,
        "qualification_forms": qualification_forms_list,
        "document_tabs": document_tabs,
        "info_rows": info_rows,
        "raw_status_id": tender_raw_json.get("status_id"),
    }


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
    source_groups: list[dict[str, object]] = []
    source_sections: list[dict[str, object]] = []
    uzex_view: dict[str, object] | None = None
    if isinstance(tender.raw_json, dict):
        documents = _extract_documents(tender.raw_json)
        raw_lots = tender.raw_json.get("_lots")
        if isinstance(raw_lots, list):
            for entry in raw_lots:
                if isinstance(entry, dict):
                    lots.append(entry)
        if tender.source_name == "uzex_etender":
            uzex_view = _extract_uzex_view(
                tender.raw_json,
                documents=documents,
                lots=lots,
            )
        source_groups = _extract_source_groups(tender.raw_json)
        source_sections = _extract_source_sections(tender.raw_json)

    related = await list_related_tenders(session, tender.source_name, tender.id, limit=12)

    return templates.TemplateResponse(
        request,
        "tenders/detail.html",
        {
            "tender": tender,
            "lots": lots,
            "documents": documents,
            "source_groups": source_groups,
            "source_sections": source_sections,
            "uzex_view": uzex_view,
            "related": related,
            "total_tenders": total_tenders,
            "total_sources": total_sources,
            "last_seen": last_seen,
        },
    )


__all__ = ["router"]
