"""HTML routes for the read-only browsing UI."""

# ruff: noqa: RUF001
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


def _rows_from_keys(
    mapping: dict[str, Any],
    candidates: tuple[tuple[str, tuple[str, ...]], ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for label, keys in candidates:
        value = _first_present(mapping, *keys)
        if value is None:
            continue
        rows.append({"label": label, "value": value})
    return rows


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


def _extract_goszakup_view(
    tender_raw_json: dict[str, Any],
    *,
    documents: list[dict[str, object]],
    lots: list[dict[str, object]],
) -> dict[str, object]:
    announcement_lots = tender_raw_json.get("announcement_lots")
    if not isinstance(announcement_lots, list):
        announcement_lots = tender_raw_json.get("_announcement_lots")
    announcement_lots_list = [
        item for item in announcement_lots or [] if isinstance(item, dict)
    ]

    selected_lot = lots[0] if lots else {}
    selected_lot_rows = _rows_from_keys(
        {**selected_lot, **tender_raw_json},
        (
            ("Номер лота", ("lot_reference_number",)),
            ("ID лота", ("lot_id",)),
            ("Наименование лота", ("lot_title", "name_ru")),
            ("Количество", ("quantity_text",)),
            ("Сумма", ("amount_text",)),
            ("Способ закупки", ("procurement_method",)),
            ("Статус", ("status_text",)),
            ("Ссылка на лот", ("lot_detail_url", "lot_url")),
        ),
    )

    announcement_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Номер объявления", ("announcement_number",)),
            ("Наименование объявления", ("announcement_title_ru", "announcement_title")),
            ("Статус объявления", ("announcement_status",)),
            ("Дата публикации объявления", ("publish_date_text",)),
            ("Срок начала приема заявок", ("offer_start_text",)),
            ("Срок окончания приема заявок", ("offer_end_text",)),
        ),
    )
    general_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Способ проведения закупки", ("procurement_method",)),
            ("Тип закупки", ("purchase_type",)),
            ("Способ несостоявшейся закупки", ("failed_procurement_method",)),
            ("Вид предмета закупок", ("subject_type",)),
            ("Кол-во лотов в объявлении", ("lot_count_text",)),
            ("Сумма закупки", ("total_amount_text",)),
            ("Признаки", ("signs",)),
            ("Приглашенный поставщик", ("invited_supplier",)),
        ),
    )
    organizer_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("БИН организатора", ("organizer_bin",)),
            ("Организатор", ("organizer_name", "organizer_text")),
            ("Юр. адрес организатора", ("organizer_legal_address",)),
            ("ФИО представителя", ("organizer_representative",)),
            ("Должность", ("organizer_position",)),
            ("E-Mail", ("organizer_email",)),
            ("Создатель объявления", ("announcement_creator",)),
        ),
    )

    return {
        "announcement_title": tender_raw_json.get("announcement_title_ru")
        or tender_raw_json.get("announcement_title"),
        "announcement_number": tender_raw_json.get("announcement_number"),
        "announcement_id": tender_raw_json.get("announcement_id"),
        "announcement_status": tender_raw_json.get("announcement_status")
        or tender_raw_json.get("status_text"),
        "selected_lot": selected_lot,
        "selected_lot_rows": selected_lot_rows,
        "announcement_rows": announcement_rows,
        "general_rows": general_rows,
        "organizer_rows": organizer_rows,
        "announcement_lots": announcement_lots_list,
        "documents": documents,
    }


def _extract_mitwork_view(
    tender_raw_json: dict[str, Any],
    *,
    documents: list[dict[str, object]],
    lots: list[dict[str, object]],
) -> dict[str, object]:
    detail_fields_raw = tender_raw_json.get("detail_fields")
    detail_fields = detail_fields_raw if isinstance(detail_fields_raw, dict) else {}
    merged = {**tender_raw_json, **detail_fields}

    identity_rows = _rows_from_keys(
        merged,
        (
            ("Номер закупки", ("announcement_number",)),
            ("Внутренний ID", ("data_key",)),
            ("Лотов", ("lots_label",)),
            ("Наименование (RU)", ("title_ru_detail", "title_ru")),
            ("Наименование (KZ)", ("title_kk",)),
            ("Страница закупки", ("detail_url",)),
        ),
    )
    timeline_rows = _rows_from_keys(
        merged,
        (
            ("Начало приема заявок", ("offer_start_text_detail", "offer_start_text")),
            ("Окончание приема заявок", ("offer_end_text_detail", "offer_end_text")),
            ("Статус", ("status_text_detail", "status_text")),
        ),
    )
    organizer_rows = _rows_from_keys(
        merged,
        (
            ("Организатор", ("organizer_name", "buyer_name")),
            ("БИН/ИИН", ("buyer_bin",)),
            ("Карточка организатора", ("organizer_url", "subject_url")),
        ),
    )
    process_rows = _rows_from_keys(
        merged,
        (
            ("Способ закупки", ("procurement_method_detail", "procurement_method")),
            ("Тип закупки", ("purchase_type",)),
            ("Правила закупок", ("rules_name",)),
            ("Ссылка на правила", ("rules_url",)),
        ),
    )
    commercial_rows = _rows_from_keys(
        merged,
        (
            ("Стоимость", ("value_text",)),
            ("Валюта", ("currency",)),
        ),
    )

    primary_lot = lots[0] if lots else {}
    primary_lot_rows = _rows_from_keys(
        primary_lot,
        (
            ("Номер", ("number",)),
            ("Код классификации", ("classification_code",)),
            ("Наименование", ("name_ru", "name")),
            ("Описание", ("description_ru", "description")),
            ("Количество", ("quantity_text",)),
            ("Цена за единицу", ("unit_price_text",)),
            ("Сумма", ("total_amount_text",)),
            ("Подано заявок", ("submitted_bids_text",)),
            ("Страница лота", ("lot_url",)),
        ),
    )

    return {
        "announcement_number": tender_raw_json.get("announcement_number")
        or tender_raw_json.get("data_key"),
        "title": detail_fields.get("title_ru_detail")
        or tender_raw_json.get("title_ru")
        or tender_raw_json.get("title"),
        "status": detail_fields.get("status_text_detail")
        or tender_raw_json.get("status_text"),
        "identity_rows": identity_rows,
        "timeline_rows": timeline_rows,
        "organizer_rows": organizer_rows,
        "process_rows": process_rows,
        "commercial_rows": commercial_rows,
        "primary_lot": primary_lot,
        "primary_lot_rows": primary_lot_rows,
        "documents": documents,
        "lots": lots,
    }


def _extract_national_bank_view(
    tender_raw_json: dict[str, Any],
    *,
    documents: list[dict[str, object]],
    lots: list[dict[str, object]],
) -> dict[str, object]:
    lot_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Номер лота", ("data_key",)),
            ("Номер объявления", ("announcement_number",)),
            ("ID объявления", ("announcement_id",)),
            ("Код ЕНСТРУ", ("detail_enstru_code", "enstru_code")),
            ("Тип пункта плана", ("plan_type",)),
            ("Год", ("year",)),
            ("Срок проведения закупки", ("period",)),
            ("Страница лота", ("detail_url",)),
        ),
    )
    lot_detail_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Наименование на русском языке", ("name_ru", "title_ru")),
            ("Наименование на государственном языке", ("name_kk",)),
            (
                "Характеристика на русском языке",
                ("detail_characteristic_ru", "characteristic_ru"),
            ),
            ("Характеристика на государственном языке", ("characteristic_kk",)),
            ("Количество и сумма", ("amount_summary",)),
            ("Стоимость из списка", ("value_text",)),
        ),
    )
    announcement_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Наименование объявления (RU)", ("announcement_name_ru",)),
            ("Наименование объявления (KZ)", ("announcement_name_kk",)),
            ("Дата начала приема заявок", ("announcement_start_text",)),
            ("Дата вскрытия и завершения приема заявок", ("announcement_end_text",)),
            ("Способ закупки", ("procurement_method",)),
            ("Статус объявления", ("announcement_status",)),
            ("Ссылка на объявление", ("announcement_url",)),
        ),
    )
    organizer_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Организатор", ("organizer_name", "buyer_name")),
            ("БИН организатора", ("buyer_bin",)),
            ("Email организатора", ("organizer_email",)),
            ("Карточка организатора", ("organizer_url", "subject_url")),
        ),
    )
    delivery_places_raw = tender_raw_json.get("delivery_places")
    delivery_places = [
        item for item in delivery_places_raw or [] if isinstance(item, dict)
    ]

    return {
        "lot_title": tender_raw_json.get("name_ru")
        or tender_raw_json.get("title_ru")
        or tender_raw_json.get("title"),
        "lot_title_kk": tender_raw_json.get("name_kk"),
        "lot_description": tender_raw_json.get("detail_characteristic_ru")
        or tender_raw_json.get("characteristic_ru"),
        "lot_description_kk": tender_raw_json.get("characteristic_kk"),
        "announcement_id": tender_raw_json.get("announcement_id")
        or tender_raw_json.get("announcement_number"),
        "announcement_status": tender_raw_json.get("announcement_status")
        or tender_raw_json.get("status_text"),
        "amount_summary": tender_raw_json.get("amount_summary")
        or tender_raw_json.get("value_text"),
        "lot_rows": lot_rows,
        "lot_detail_rows": lot_detail_rows,
        "announcement_rows": announcement_rows,
        "organizer_rows": organizer_rows,
        "delivery_places": delivery_places,
        "documents": documents,
        "lots": lots,
    }


def _extract_zakup_unified_view(
    tender_raw_json: dict[str, Any],
    *,
    lots: list[dict[str, object]],
) -> dict[str, object]:
    status_raw = tender_raw_json.get("status")
    status = status_raw if isinstance(status_raw, dict) else {}
    method_raw = tender_raw_json.get("purchase_method")
    purchase_method = method_raw if isinstance(method_raw, dict) else {}
    subject_raw = tender_raw_json.get("purchase_subject")
    purchase_subject = subject_raw if isinstance(subject_raw, dict) else {}
    organizer_raw = tender_raw_json.get("organizer")
    organizer = organizer_raw if isinstance(organizer_raw, dict) else {}

    announcement_rows = [
        {"label": "ID объявления", "value": tender_raw_json.get("id")},
        {"label": "Внешний ID", "value": tender_raw_json.get("external_id")},
        {
            "label": "Номер объявления",
            "value": tender_raw_json.get("announcement_number"),
        },
        {"label": "Наименование", "value": tender_raw_json.get("name")},
        {"label": "Статус", "value": status.get("name")},
        {"label": "Код статуса", "value": status.get("code")},
        {"label": "Количество лотов", "value": tender_raw_json.get("lot_count")},
    ]
    announcement_rows = [
        row for row in announcement_rows if not _is_empty_value(row["value"])
    ]

    procurement_rows = [
        {"label": "Способ закупки", "value": purchase_method.get("name")},
        {
            "label": "Способ закупки (KZ)",
            "value": purchase_method.get("name_kk"),
        },
        {"label": "Код способа", "value": purchase_method.get("code")},
        {"label": "Предмет закупки", "value": purchase_subject.get("name")},
        {
            "label": "Предмет закупки (KZ)",
            "value": purchase_subject.get("name_kk"),
        },
        {"label": "Код предмета", "value": purchase_subject.get("code")},
    ]
    procurement_rows = [
        row for row in procurement_rows if not _is_empty_value(row["value"])
    ]

    organizer_rows = [
        {"label": "Организатор", "value": organizer.get("name")},
        {"label": "БИН/ИИН", "value": organizer.get("iin_bin")},
        {"label": "Тип организации", "value": organizer.get("organization_type")},
        {"label": "Адрес", "value": organizer.get("address")},
        {"label": "ID организатора", "value": organizer.get("id")},
    ]
    organizer_rows = [
        row for row in organizer_rows if not _is_empty_value(row["value"])
    ]

    normalized_lots = []
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        enstrus_raw = lot.get("enstrus")
        enstrus_source = enstrus_raw if isinstance(enstrus_raw, list) else []
        enstrus = [item for item in enstrus_source if isinstance(item, dict)]
        addresses_raw = lot.get("delivery_addresses")
        addresses_source = (
            addresses_raw if isinstance(addresses_raw, list) else []
        )
        addresses = [
            item for item in addresses_source if isinstance(item, dict)
        ]
        lot_status_raw = lot.get("status")
        lot_status = lot_status_raw if isinstance(lot_status_raw, dict) else {}
        system_raw = lot.get("system")
        system = system_raw if isinstance(system_raw, dict) else {}
        normalized_lots.append(
            {
                "id": lot.get("id"),
                "external_id": lot.get("external_id"),
                "announcement_number": lot.get("announcement_number"),
                "lot_number": lot.get("lot_number"),
                "title": lot.get("name_ru") or lot.get("name_kk"),
                "title_kk": lot.get("name_kk"),
                "description": lot.get("description_ru"),
                "description_kk": lot.get("description_kk"),
                "quantity": lot.get("quantity"),
                "total_price": lot.get("total_price"),
                "dumping_price": lot.get("dumping_price"),
                "organization_name": lot.get("organization_name"),
                "purchase_method": lot.get("purchase_method_name"),
                "status": lot_status.get("name"),
                "system": system.get("name"),
                "enstrus": enstrus,
                "delivery_addresses": addresses,
            }
        )

    return {
        "announcement_id": tender_raw_json.get("id"),
        "announcement_number": tender_raw_json.get("announcement_number")
        or tender_raw_json.get("id"),
        "title": tender_raw_json.get("name"),
        "status": status.get("name"),
        "status_code": status.get("code"),
        "purchase_method": purchase_method.get("name"),
        "purchase_subject": purchase_subject.get("name"),
        "announcement_rows": announcement_rows,
        "procurement_rows": procurement_rows,
        "organizer_rows": organizer_rows,
        "lots": normalized_lots,
        "lot_count": tender_raw_json.get("lot_count") or len(normalized_lots),
    }


def _extract_samruk_kazyna_view(
    tender_raw_json: dict[str, Any],
    *,
    lots: list[dict[str, object]],
) -> dict[str, object]:
    customer_raw = tender_raw_json.get("customer")
    customer = customer_raw if isinstance(customer_raw, dict) else {}
    organizer_raw = tender_raw_json.get("organizer")
    organizer = organizer_raw if isinstance(organizer_raw, dict) else {}
    requirement_raw = tender_raw_json.get("advertRequirement")
    requirement = requirement_raw if isinstance(requirement_raw, dict) else {}

    advert_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("ID объявления", ("id",)),
            ("Номер объявления", ("number",)),
            ("Наименование (RU)", ("nameRu",)),
            ("Наименование (KZ)", ("nameKk",)),
            ("Тип тендера", ("tenderType",)),
            ("Статус", ("advertStatus", "simpleStatus")),
            ("Прием заявок с", ("acceptanceBeginDateTime",)),
            ("Прием заявок до", ("acceptanceEndDateTime",)),
            ("Контактный телефон", ("phone",)),
            ("Внутренний номер", ("extensionNumber",)),
            ("Email", ("email",)),
        ),
    )

    customer_address = customer.get("legalAddress")
    customer_address_dict = (
        customer_address if isinstance(customer_address, dict) else {}
    )
    organizer_address = organizer.get("legalAddress")
    organizer_address_dict = (
        organizer_address if isinstance(organizer_address, dict) else {}
    )
    customer_rows: list[dict[str, object]] = [
        {"label": "Заказчик", "value": customer.get("nameRu")},
        {"label": "Заказчик (KZ)", "value": customer.get("nameKk")},
        {"label": "БИН", "value": customer.get("bin") or customer.get("identifier")},
        {"label": "Телефон", "value": customer.get("phone")},
        {"label": "Email", "value": customer.get("email")},
        {
            "label": "Адрес",
            "value": ", ".join(
                str(value)
                for value in (
                    customer_address_dict.get("countryRu"),
                    customer_address_dict.get("katoFullNameRu"),
                    customer_address_dict.get("street"),
                    customer_address_dict.get("building"),
                    customer_address_dict.get("flat"),
                )
                if value
            ),
        },
    ]
    customer_rows = [
        row for row in customer_rows if not _is_empty_value(row["value"])
    ]

    organizer_rows: list[dict[str, object]] = [
        {"label": "Организатор", "value": organizer.get("nameRu")},
        {"label": "Организатор (KZ)", "value": organizer.get("nameKk")},
        {"label": "БИН", "value": organizer.get("bin") or organizer.get("identifier")},
        {
            "label": "Адрес",
            "value": ", ".join(
                str(value)
                for value in (
                    organizer_address_dict.get("countryRu"),
                    organizer_address_dict.get("katoFullNameRu"),
                    organizer_address_dict.get("street"),
                    organizer_address_dict.get("building"),
                    organizer_address_dict.get("flat"),
                )
                if value
            ),
        },
    ]
    organizer_rows = [
        row for row in organizer_rows if not _is_empty_value(row["value"])
    ]

    requirement_rows = [
        {"label": "ID требований", "value": requirement.get("id")},
        {"label": "ID объявления", "value": requirement.get("advertId")},
        {
            "label": "Начало обсуждения",
            "value": requirement.get("discussionBeginDateTime"),
        },
        {
            "label": "Завершение обсуждения",
            "value": requirement.get("discussionEndDateTime"),
        },
    ]
    requirement_rows = [
        row for row in requirement_rows if not _is_empty_value(row["value"])
    ]

    documents_raw = tender_raw_json.get("documents")
    documents_source = documents_raw if isinstance(documents_raw, list) else []
    documents = [item for item in documents_source if isinstance(item, dict)]

    normalized_lots = []
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        tru_raw = lot.get("truHistory")
        tru = tru_raw if isinstance(tru_raw, dict) else {}
        delivery_country_raw = lot.get("deliveryCountry")
        delivery_country = (
            delivery_country_raw if isinstance(delivery_country_raw, dict) else {}
        )
        delivery_kato_raw = lot.get("deliveryKato")
        delivery_kato = (
            delivery_kato_raw if isinstance(delivery_kato_raw, dict) else {}
        )
        customer_for_lot_raw = lot.get("customer")
        customer_for_lot = (
            customer_for_lot_raw
            if isinstance(customer_for_lot_raw, dict)
            else {}
        )
        normalized_lots.append(
            {
                "id": lot.get("id"),
                "number": lot.get("number"),
                "title": lot.get("nameRu") or lot.get("nameKk"),
                "title_kk": lot.get("nameKk"),
                "category": tru.get("category") or lot.get("tenderSubjectType"),
                "tru_code": tru.get("code"),
                "tru_name": tru.get("ru") or tru.get("briefRu"),
                "customer_name": customer_for_lot.get("nameRu"),
                "customer_bin": customer_for_lot.get("bin"),
                "quantity": lot.get("count"),
                "price": lot.get("price"),
                "total_amount": lot.get("sumTruNoNds"),
                "duration": lot.get("durationMonth"),
                "location": lot.get("tenderLocationRu"),
                "delivery_country": delivery_country.get("ru"),
                "delivery_kato": delivery_kato.get("ru")
                or delivery_kato.get("fullRu"),
                "delivery_location": lot.get("deliveryLocationRu"),
            }
        )

    return {
        "advert_id": tender_raw_json.get("id"),
        "number": tender_raw_json.get("number") or tender_raw_json.get("id"),
        "title": tender_raw_json.get("nameRu"),
        "title_kk": tender_raw_json.get("nameKk"),
        "status": tender_raw_json.get("advertStatus")
        or tender_raw_json.get("simpleStatus"),
        "tender_type": tender_raw_json.get("tenderType"),
        "advert_rows": advert_rows,
        "customer_rows": customer_rows,
        "organizer_rows": organizer_rows,
        "requirement_rows": requirement_rows,
        "documents": documents,
        "lots": normalized_lots,
    }


def _extract_ets_tender_view(
    tender_raw_json: dict[str, Any],
    *,
    documents: list[dict[str, object]],
    lots: list[dict[str, object]],
) -> dict[str, object]:
    procedure_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("ID тендера", ("external_id",)),
            ("Тип процедуры", ("procedure_type_text",)),
            ("Краткое название", ("title_short",)),
            ("Название из карточки", ("title_full",)),
            ("Организатор", ("organizer_link_text", "buyer_name")),
            ("Ссылка на организатора", ("organizer_link_url", "buyer_url")),
            ("Опубликовано", ("published_text",)),
            ("Актуально до", ("deadline_text",)),
            ("Последнее изменение", ("last_edited_text",)),
            ("Страница тендера", ("detail_url",)),
        ),
    )
    commercial_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Категория ЕНС ТРУ", ("enstru_text",)),
            ("Код ЕНС ТРУ", ("enstru_code",)),
            ("Наименование ЕНС ТРУ", ("enstru_label",)),
            ("Количество", ("quantity_text",)),
            ("Цена за единицу", ("unit_price_text",)),
            ("Общая стоимость", ("total_price_text",)),
            ("НДС", ("vat_note",)),
        ),
    )
    delivery_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Место поставки", ("delivery_address",)),
            ("Условия оплаты", ("payment_terms",)),
        ),
    )
    description_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Описание из листинга", ("title_description",)),
            ("Полное описание", ("description_full",)),
        ),
    )

    primary_lot = lots[0] if lots else {}
    primary_lot_rows = _rows_from_keys(
        primary_lot,
        (
            ("Наименование", ("name_ru", "name")),
            ("Описание", ("description_ru", "description")),
        ),
    )

    return {
        "tender_number": tender_raw_json.get("external_id"),
        "procedure_type": tender_raw_json.get("procedure_type_text"),
        "title": tender_raw_json.get("title_full")
        or tender_raw_json.get("title_description")
        or tender_raw_json.get("title_short"),
        "description": tender_raw_json.get("description_full")
        or tender_raw_json.get("title_description"),
        "enstru_code": tender_raw_json.get("enstru_code"),
        "enstru_label": tender_raw_json.get("enstru_label"),
        "vat_note": tender_raw_json.get("vat_note"),
        "procedure_rows": procedure_rows,
        "commercial_rows": commercial_rows,
        "delivery_rows": delivery_rows,
        "description_rows": description_rows,
        "primary_lot_rows": primary_lot_rows,
        "documents": documents,
    }


def _extract_xt_xarid_view(
    tender_raw_json: dict[str, Any],
    *,
    documents: list[dict[str, object]],
    lots: list[dict[str, object]],
) -> dict[str, object]:
    meta_raw = tender_raw_json.get("meta")
    meta = meta_raw if isinstance(meta_raw, dict) else {}
    good_maps_raw = meta.get("good_maps")
    good_maps = [
        item for item in good_maps_raw or [] if isinstance(item, dict)
    ]
    source_lots_raw = meta.get("lots")
    source_lots = [
        item for item in source_lots_raw or [] if isinstance(item, dict)
    ]
    area_path_raw = meta.get("area_path")
    area_path = [
        item for item in area_path_raw or [] if isinstance(item, dict)
    ]
    area_names = [
        str(item.get("name"))
        for item in area_path
        if not _is_empty_value(item.get("name"))
    ]
    fin_src_raw = meta.get("fin_src")
    fin_sources = (
        fin_src_raw
        if isinstance(fin_src_raw, list) and is_list_of_scalars(fin_src_raw)
        else []
    )

    procedure_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Tender ID", ("id",)),
            ("Source title", ("name",)),
            ("Status code", ("status",)),
            ("Language", ("lang",)),
            ("Green procurement", ("green",)),
            ("Goods count", ("good_count",)),
            ("Lot count", ("lot_count",)),
            ("Participant count", ("part_count",)),
        ),
    )
    timeline_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Published at", ("publicated_at",)),
            ("Submission deadline", ("close_at",)),
            ("Remaining time", ("remain_time",)),
            ("Objection deadline", ("close_docs_objections_at",)),
            ("Objection remaining time", ("docs_objections_remain_time",)),
        ),
    )
    customer_rows = [
        {"label": "Customer name", "value": meta.get("company_name")},
        {"label": "Customer TIN", "value": meta.get("company_inn")},
        {
            "label": "Region path",
            "value": " / ".join(area_names) if area_names else None,
        },
        {
            "label": "Financing sources",
            "value": ", ".join(str(item) for item in fin_sources)
            if fin_sources
            else None,
        },
    ]
    customer_rows = [
        row for row in customer_rows if not _is_empty_value(row["value"])
    ]

    goods: list[dict[str, object]] = []
    for item in good_maps:
        goods.append(
            {
                "lot_id": item.get("lot_id"),
                "classification_code": item.get("id"),
                "name": item.get("name"),
                "description": _first_present(
                    item,
                    "description",
                    "description_ru",
                    "technical_description",
                    "characteristic",
                ),
                "quantity": item.get("amount"),
                "unit": item.get("unit"),
                "unit_price": item.get("price"),
                "total_amount": item.get("totalcost_item"),
            }
        )

    if not goods:
        for lot in lots:
            goods.append(
                {
                    "lot_id": lot.get("lot_id"),
                    "classification_code": lot.get("classification_code"),
                    "name": lot.get("name_ru") or lot.get("name"),
                    "description": lot.get("description_ru")
                    or lot.get("description"),
                    "quantity": lot.get("quantity"),
                    "unit": lot.get("unit"),
                    "unit_price": lot.get("unit_price"),
                    "total_amount": lot.get("total_amount"),
                }
            )

    status_labels = {
        "docs_objections": "Documentation objections",
    }
    raw_status = tender_raw_json.get("status")
    status = status_labels.get(str(raw_status), raw_status)
    primary_title = (
        (goods[0].get("name") if goods else None)
        or tender_raw_json.get("name")
        or tender_raw_json.get("id")
    )

    return {
        "tender_number": tender_raw_json.get("id"),
        "title": primary_title,
        "status": status,
        "raw_status": raw_status,
        "is_green": tender_raw_json.get("green"),
        "currency": tender_raw_json.get("currency"),
        "procedure_rows": procedure_rows,
        "timeline_rows": timeline_rows,
        "customer_rows": customer_rows,
        "goods": goods,
        "source_lots": source_lots,
        "area_path": area_path,
        "documents": documents,
        "lot_count": tender_raw_json.get("lot_count") or len(source_lots) or len(lots),
        "good_count": tender_raw_json.get("good_count") or len(goods),
        "participant_count": tender_raw_json.get("part_count"),
        "objection_remaining": tender_raw_json.get("docs_objections_remain_time"),
    }


def _extract_tendersinfo_view(
    tender_raw_json: dict[str, Any],
    *,
    lots: list[dict[str, object]],
) -> dict[str, object]:
    notice_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("TendersInfo ID", ("site_tender_id",)),
            ("Region", ("region_name",)),
            ("Country", ("country",)),
            ("Sector", ("sector_name",)),
            ("Source URL", ("url",)),
        ),
    )
    timeline_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Published", ("date_c",)),
            ("Document deadline", ("doc_last",)),
        ),
    )
    commercial_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Estimated cost", ("est_cost_h",)),
        ),
    )
    authority_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Tender authority HTML", ("organisation_h",)),
        ),
    )

    primary_lot = lots[0] if lots else {}
    summary_rows = _rows_from_keys(
        primary_lot,
        (
            ("Title", ("name_en", "name_ru", "name")),
            ("Description", ("description_en", "description_ru", "description")),
        ),
    )

    return {
        "notice_id": tender_raw_json.get("site_tender_id"),
        "title": tender_raw_json.get("short_desc") or tender_raw_json.get("title"),
        "region": tender_raw_json.get("region_name"),
        "sector": tender_raw_json.get("sector_name"),
        "estimated_cost": tender_raw_json.get("est_cost_h"),
        "authority_html": tender_raw_json.get("organisation_h"),
        "notice_rows": notice_rows,
        "timeline_rows": timeline_rows,
        "commercial_rows": commercial_rows,
        "authority_rows": authority_rows,
        "summary_rows": summary_rows,
        "lots": lots,
    }


def _extract_uzbekistan_tenders_view(
    tender_raw_json: dict[str, Any],
    *,
    lots: list[dict[str, object]],
) -> dict[str, object]:
    listing_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("UZT reference", ("external_id",)),
            ("Listing title", ("title",)),
            ("Detail URL", ("detail_url",)),
            ("Listing deadline", ("deadline_text",)),
            ("Listing value", ("value_text",)),
        ),
    )
    detail_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Detail identifier", ("detail_identifier",)),
            ("Published", ("published_text_detail",)),
            ("Deadline", ("deadline_text_detail",)),
            ("Category", ("detail_category",)),
            ("Detail price", ("detail_price",)),
            ("Detail currency", ("detail_price_currency",)),
            ("Meta description", ("detail_meta_description",)),
            ("Detail description", ("detail_description",)),
        ),
    )
    buyer_rows = _rows_from_keys(
        tender_raw_json,
        (
            ("Buyer", ("buyer_name_detail",)),
        ),
    )

    jsonld_raw = tender_raw_json.get("_detail_jsonld")
    jsonld = jsonld_raw if isinstance(jsonld_raw, dict) else {}
    offered_by_raw = jsonld.get("offeredBy")
    offered_by = offered_by_raw if isinstance(offered_by_raw, dict) else {}
    jsonld_flat = {
        **jsonld,
        "offeredBy.name": offered_by.get("name"),
        "offeredBy.type": offered_by.get("@type"),
    }
    jsonld_rows = _rows_from_keys(
        jsonld_flat,
        (
            ("Schema type", ("@type",)),
            ("Identifier", ("identifier",)),
            ("Availability starts", ("availabilityStarts",)),
            ("Availability ends", ("availabilityEnds",)),
            ("Category", ("category",)),
            ("Price", ("price",)),
            ("Currency", ("priceCurrency",)),
            ("Offered by", ("offeredBy.name",)),
            ("Offered by type", ("offeredBy.type",)),
        ),
    )

    primary_lot = lots[0] if lots else {}
    summary_rows = _rows_from_keys(
        primary_lot,
        (
            ("Title", ("name_en", "name_ru", "name", "title")),
            ("Description", ("description_en", "description_ru", "description")),
        ),
    )

    return {
        "reference_no": (
            tender_raw_json.get("detail_identifier")
            or tender_raw_json.get("external_id")
        ),
        "title": tender_raw_json.get("title"),
        "buyer_name": tender_raw_json.get("buyer_name_detail"),
        "category": tender_raw_json.get("detail_category"),
        "value_text": tender_raw_json.get("value_text"),
        "detail_price": tender_raw_json.get("detail_price"),
        "detail_price_currency": tender_raw_json.get("detail_price_currency"),
        "listing_rows": listing_rows,
        "detail_rows": detail_rows,
        "buyer_rows": buyer_rows,
        "summary_rows": summary_rows,
        "jsonld_rows": jsonld_rows,
        "jsonld": jsonld,
        "lots": lots,
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
    goszakup_view: dict[str, object] | None = None
    mitwork_view: dict[str, object] | None = None
    national_bank_view: dict[str, object] | None = None
    zakup_unified_view: dict[str, object] | None = None
    samruk_kazyna_view: dict[str, object] | None = None
    ets_tender_view: dict[str, object] | None = None
    xt_xarid_view: dict[str, object] | None = None
    tendersinfo_view: dict[str, object] | None = None
    uzbekistan_tenders_view: dict[str, object] | None = None
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
        if tender.source_name == "goszakup":
            goszakup_view = _extract_goszakup_view(
                tender.raw_json,
                documents=documents,
                lots=lots,
            )
        if tender.source_name == "mitwork":
            mitwork_view = _extract_mitwork_view(
                tender.raw_json,
                documents=documents,
                lots=lots,
            )
        if tender.source_name == "national_bank":
            national_bank_view = _extract_national_bank_view(
                tender.raw_json,
                documents=documents,
                lots=lots,
            )
        if tender.source_name == "zakup_unified":
            zakup_unified_view = _extract_zakup_unified_view(
                tender.raw_json,
                lots=lots,
            )
        if tender.source_name == "samruk_kazyna":
            samruk_kazyna_view = _extract_samruk_kazyna_view(
                tender.raw_json,
                lots=lots,
            )
        if tender.source_name == "ets_tender":
            ets_tender_view = _extract_ets_tender_view(
                tender.raw_json,
                documents=documents,
                lots=lots,
            )
        if tender.source_name in {"xt_xarid", "xt-xarid"}:
            xt_xarid_view = _extract_xt_xarid_view(
                tender.raw_json,
                documents=documents,
                lots=lots,
            )
        if tender.source_name == "tendersinfo":
            tendersinfo_view = _extract_tendersinfo_view(
                tender.raw_json,
                lots=lots,
            )
        if tender.source_name == "uzbekistan_tenders":
            uzbekistan_tenders_view = _extract_uzbekistan_tenders_view(
                tender.raw_json,
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
            "goszakup_view": goszakup_view,
            "mitwork_view": mitwork_view,
            "national_bank_view": national_bank_view,
            "zakup_unified_view": zakup_unified_view,
            "samruk_kazyna_view": samruk_kazyna_view,
            "ets_tender_view": ets_tender_view,
            "xt_xarid_view": xt_xarid_view,
            "tendersinfo_view": tendersinfo_view,
            "uzbekistan_tenders_view": uzbekistan_tenders_view,
            "related": related,
            "total_tenders": total_tenders,
            "total_sources": total_sources,
            "last_seen": last_seen,
        },
    )


__all__ = ["router"]
