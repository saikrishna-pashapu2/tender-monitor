# ruff: noqa: RUF001
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
    """Wipe committed API tables before AND after every API test.

    The API tests' TestClient opens its own sessions that commit, so
    the rollback in the shared ``db_session`` fixture is not enough to
    isolate API tests from each other — and the API tests run before
    the core/test_models tests alphabetically, so committed leftovers
    would leak across packages without a teardown truncate.
    """
    engine = create_async_engine(test_database_url, future=True)
    truncate_sql = (
        "TRUNCATE notification_logs, share_contacts, feedback, tenders, sources "
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
    """Seed a deterministic dataset: 9 sources, 19 tenders, mixed flags.

    Layout:
      - 6 KZ tenders on ``goszakup`` (4 matched, 2 unmatched)
      - 6 UZ tenders on ``xt_xarid`` (2 matched, 4 unmatched)
      - 1 KZ tender on ``ets_tender`` for source-specific detail rendering
      - 1 KZ tender on ``mitwork`` for source-specific detail rendering
      - 1 KZ tender on ``national_bank`` for source-specific detail rendering
      - 1 KZ tender on ``zakup_unified`` for source-specific detail rendering
      - 1 KZ tender on ``samruk_kazyna`` for source-specific detail rendering
      - 1 UZ tender on ``tendersinfo`` for source-specific detail rendering
      - 1 UZ tender on ``uzbekistan_tenders`` for source-specific detail rendering
      - mixed deadlines (past, near, far) and values
    """
    async with api_session_factory() as session:
        session.add_all(
            [
                make_source(
                    "ets_tender",
                    display_name="ETS-Tender",
                    country=Country.KZ,
                    base_url="https://www.ets-tender.kz",
                ),
                make_source(
                    "goszakup",
                    display_name="Goszakup",
                    country=Country.KZ,
                ),
                make_source(
                    "mitwork",
                    display_name="Eurasian Electronic Portal",
                    country=Country.KZ,
                    base_url="https://eep.mitwork.kz",
                ),
                make_source(
                    "national_bank",
                    display_name="National Bank Procurement Portal",
                    country=Country.KZ,
                    base_url="https://zakup.nationalbank.kz",
                ),
                make_source(
                    "samruk_kazyna",
                    display_name="Samruk-Kazyna Procurement Portal",
                    country=Country.KZ,
                    base_url="https://zakup.sk.kz",
                ),
                make_source(
                    "zakup_unified",
                    display_name="Unified Procurement Portal",
                    country=Country.KZ,
                    base_url="https://zakup.gov.kz",
                ),
                make_source(
                    "tendersinfo",
                    display_name="TendersInfo",
                    country=Country.KZ,
                    base_url="https://www.tendersinfo.com",
                ),
                make_source(
                    "uzbekistan_tenders",
                    display_name="UzbekistanTenders.com",
                    country=Country.UZ,
                    base_url="https://www.uzbekistantenders.com",
                ),
                make_source(
                    "xt_xarid",
                    display_name="XT-Xarid",
                    country=Country.UZ,
                    base_url="https://xt-xarid.uz",
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
                raw_json={
                    "id": "g-1",
                    "announcement_id": "17013627",
                    "announcement_number": "17013627-1",
                    "announcement_title": "Credit rating audit services announcement",
                    "announcement_title_ru": "Credit rating audit services announcement",
                    "announcement_status": "Опубликован (прием ценовых предложений)",
                    "publish_date_text": "2026-05-17 12:00:00",
                    "offer_start_text": "2026-05-17 12:00:00",
                    "offer_end_text": "2026-05-28 12:00:00",
                    "procurement_method": "Запрос ценовых предложений",
                    "purchase_type": "Первая закупка",
                    "subject_type": "Услуга",
                    "organizer_bin": "100140006825",
                    "organizer_name": "National Bank of Kazakhstan",
                    "organizer_legal_address": "Astana, Kazakhstan",
                    "lot_count_text": "1",
                    "total_amount_text": "500 000.00",
                    "lot_reference_number": "84701402-ЗЦП1",
                    "lot_id": "g-1",
                    "lot_title": "Credit rating audit services",
                    "lot_detail_url": "https://goszakup.gov.kz/ru/subpriceoffer/index/17013627/g-1",
                    "quantity_text": "1",
                    "amount_text": "500 000.00",
                    "status_text": "Опубликован (прием ценовых предложений)",
                    "_documents": [
                        {
                            "name": "Техническая спецификация",
                            "signed_text": "Да",
                            "url": "https://goszakup.gov.kz/ru/announce/download_file/17013627/spec.pdf",
                            "source": "announcement_documents_tab",
                        }
                    ],
                    "_announcement_lots": [
                        {
                            "sequence_text": "1",
                            "name_ru": "Credit rating audit services",
                            "description_ru": "Independent rating audit services",
                            "quantity_text": "1",
                            "amount": "500000.00",
                            "amount_text": "500 000.00",
                            "currency": "KZT",
                            "lot_url": "https://goszakup.gov.kz/ru/subpriceoffer/index/17013627/g-1",
                        }
                    ],
                    "_lots": [
                        {
                            "name_ru": "Credit rating audit services",
                            "description_ru": "Credit rating audit services announcement",
                            "quantity_text": "1",
                            "amount_text": "500 000.00",
                        }
                    ],
                },
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
        rows.append(
            make_tender(
                source_name="tendersinfo",
                external_id="532912293",
                title="Conducting Inclusivity Assessment For Sustainable Urban Planning",
                buyer_name="United Nations Development Programme",
                country=Country.UZ,
                matched_groups=["esg"],
                match_details={
                    "esg": {
                        "matched_phrases": ["sustainable"],
                        "matched_tokens": [],
                    }
                },
                value_amount=None,
                value_currency=None,
                deadline_offset_days=14,
                published_offset_days=-2,
                first_seen_offset_minutes=67,
                raw_json={
                    "site_tender_id": "532912293",
                    "region_name": "Uzbekistan",
                    "country": "UZ",
                    "sector_name": "Environment And Pollution",
                    "short_desc": (
                        "Conducting Inclusivity Assessment For Sustainable Urban Planning\n"
                        "open In A New Window"
                    ),
                    "date_c": "16-May-2026",
                    "doc_last": "01-Jun-2026",
                    "est_cost_h": "",
                    "organisation_h": (
                        "<br><b>Tender Authority: </b>"
                        "United Nations Development Programme"
                    ),
                    "url": (
                        "https://www.tendersinfo.com/tenders_details/"
                        "532912293-conducting-inclusivity-assessment.php"
                    ),
                    "_lots": [
                        {
                            "name_ru": None,
                            "name_en": (
                                "Conducting Inclusivity Assessment For "
                                "Sustainable Urban Planning"
                            ),
                            "description_ru": None,
                            "description_en": None,
                        }
                    ],
                },
            )
        )
        rows.append(
            make_tender(
                source_name="uzbekistan_tenders",
                external_id="86ddab7",
                title="Provision of services for assigning an international credit rating",
                buyer_name="Central Bank of the Republic of Uzbekistan",
                country=Country.UZ,
                matched_groups=["credit_rating"],
                match_details={
                    "credit_rating": {
                        "matched_phrases": ["credit rating"],
                        "matched_tokens": [],
                    }
                },
                value_amount=Decimal("1500.00"),
                value_currency="USD",
                deadline_offset_days=12,
                published_offset_days=-1,
                first_seen_offset_minutes=68,
                raw_json={
                    "external_id": "86ddab7",
                    "title": (
                        "Provision of services for assigning an international "
                        "credit rating"
                    ),
                    "detail_url": (
                        "https://www.uzbekistantenders.com/tender/"
                        "provision-services-assigning-international-credit-"
                        "rating-86ddab7.php"
                    ),
                    "deadline_text": "30 May 2026",
                    "value_text": "Refer Document",
                    "detail_meta_description": (
                        "Provision of services for assigning an international "
                        "credit rating, Ref Id: 86ddab7"
                    ),
                    "detail_description": (
                        "Provision of services for assigning an international "
                        "credit rating for sovereign bond issuance."
                    ),
                    "detail_identifier": "86ddab7",
                    "published_text_detail": "2026-05-17",
                    "deadline_text_detail": "2026-05-30",
                    "detail_category": "Finance and Related Services",
                    "detail_price": "1500",
                    "detail_price_currency": "USD",
                    "buyer_name_detail": (
                        "Central Bank of the Republic of Uzbekistan"
                    ),
                    "_detail_jsonld": {
                        "@context": "https://schema.org",
                        "@type": "Offer",
                        "description": (
                            "Provision of services for assigning an "
                            "international credit rating for sovereign bond "
                            "issuance."
                        ),
                        "identifier": "86ddab7",
                        "availabilityStarts": "2026-05-17",
                        "availabilityEnds": "2026-05-30",
                        "category": "Finance and Related Services",
                        "price": "1500",
                        "priceCurrency": "USD",
                        "offeredBy": {
                            "@type": "Organization",
                            "name": (
                                "Central Bank of the Republic of Uzbekistan"
                            ),
                        },
                    },
                    "_lots": [
                        {
                            "name_en": (
                                "Provision of services for assigning an "
                                "international credit rating"
                            ),
                            "description_en": (
                                "International credit rating services for "
                                "sovereign bond issuance and investor "
                                "communications."
                            ),
                        }
                    ],
                },
            )
        )
        rows.append(
            make_tender(
                source_name="mitwork",
                external_id="194361",
                title="Consulting services for assessment/analysis of activities",
                buyer_name='Joint-Stock Company "SOCIAL-ENTREPRENEURSHIP CORPORATION "ALMATY"',
                value_amount=Decimal("11000000.00"),
                value_currency="KZT",
                deadline_offset_days=8,
                published_offset_days=-14,
                first_seen_offset_minutes=65,
                raw_json={
                    "data_key": "194361",
                    "announcement_number": "193447-2",
                    "lots_label": "Лотов: 1",
                    "title_ru": "Consulting services for assessment/analysis of activities",
                    "detail_url": "https://eep.mitwork.kz/ru/publics/buy/194361",
                    "value_text": "11,000,000.00 KZT",
                    "procurement_method": "Tender",
                    "offer_start_text": "2026-06-04 12:00",
                    "offer_end_text": "2026-06-11 12:00",
                    "buyer_name": 'Joint-Stock Company "SOCIAL-ENTREPRENEURSHIP CORPORATION "ALMATY"',
                    "buyer_bin": "100840016104",
                    "subject_url": "https://eep.mitwork.kz/ru/publics/subject/14784",
                    "status_text": "Published",
                    "detail_fields": {
                        "title_kk": "Қызметті бағалау/талдау бойынша consultingлық қызметтер",
                        "title_ru_detail": "Consulting services for assessment/analysis of activities",
                        "offer_start_text_detail": "2026-06-04 12:00",
                        "offer_end_text_detail": "2026-06-11 12:00:00 in 8 days",
                        "organizer_name": 'Joint-Stock Company "SOCIAL-ENTREPRENEURSHIP CORPORATION "ALMATY"',
                        "organizer_url": "https://eep.mitwork.kz/ru/publics/subject/14784",
                        "procurement_method_detail": "Tender",
                        "rules_name": "Order of the Minister of Finance of the Republic of Kazakhstan dated November 30, 2021 No. 1253",
                        "rules_url": "https://adilet.zan.kz/rus/docs/V2100025488",
                        "purchase_type": "Procurement of services",
                        "status_text_detail": "Published",
                    },
                    "_documents": [
                        {
                            "category": "Draft agreements",
                            "name": "contract_project_s_2026_193447_v1.pdf",
                            "url": "https://eep.mitwork.kz/ru/files/download/contract_project_s_2026_193447_v1.pdf",
                            "preview_url": "https://eep.mitwork.kz/ru/files/show/contract_project_s_2026_193447_v1.pdf",
                            "size_text": "512 KB",
                            "uploaded_at_text": "2026-06-04 12:03",
                            "hash": "0123456789abcdef",
                            "ext": "PDF",
                            "source": "detail_page",
                        }
                    ],
                    "_lots": [
                        {
                            "number": "657251-ОI2",
                            "classification_code": "749019.000.000003",
                            "name_ru": "Consulting services for assessment/analysis of activities",
                            "description_ru": "Consulting services for conducting an independent assessment of corporate governance",
                            "quantity_text": "1.000",
                            "unit_price_text": "11,000,000.00 KZT",
                            "unit_price_amount": "11000000.00",
                            "total_amount_text": "11,000,000.00 KZT",
                            "total_amount": "11000000.00",
                            "currency": "KZT",
                            "submitted_bids_text": "0",
                            "lot_url": "https://eep.mitwork.kz/ru/publics/lot/649071",
                        }
                    ],
                },
            )
        )
        rows.append(
            make_tender(
                source_name="national_bank",
                external_id="228344",
                title="Текущий ремонт административного здания",
                buyer_name='РГУ "НАЦИОНАЛЬНЫЙ БАНК РЕСПУБЛИКИ КАЗАХСТАН"',
                value_amount=Decimal("7964231.03"),
                value_currency="KZT",
                deadline_offset_days=4,
                published_offset_days=-3,
                first_seen_offset_minutes=68,
                raw_json={
                    "data_key": "228344",
                    "announcement_number": "102968",
                    "title_ru": "Текущий ремонт административного здания",
                    "name_ru": "Текущий ремонт административного здания",
                    "name_kk": "Әкімшілік ғимаратты ағымдағы жөндеу",
                    "enstru_code": "410040.300.000009",
                    "detail_enstru_code": "410040.300.000009",
                    "detail_url": "https://zakup.nationalbank.kz/ru/publics/lot/228344",
                    "characteristic_ru": "Текущий ремонт административного здания по адресу ул. Сатпаева",
                    "detail_characteristic_ru": "Текущий ремонт административного здания по адресу ул. Сатпаева",
                    "characteristic_kk": "Сәтпаев көшесіндегі әкімшілік ғимаратты ағымдағы жөндеу",
                    "value_text": "7 964 231,03 KZT",
                    "amount_summary": "1 услуга, 7 964 231,03 KZT",
                    "plan_type": "Закупки, не превышающие финансовый год",
                    "year": "2026",
                    "period": "Май",
                    "buyer_name": 'РГУ "НАЦИОНАЛЬНЫЙ БАНК РЕСПУБЛИКИ КАЗАХСТАН"',
                    "buyer_bin": "941240001151",
                    "subject_url": "https://zakup.nationalbank.kz/ru/publics/subject/280",
                    "status_text": "Опубликован",
                    "announcement_id": "102968",
                    "announcement_url": "https://zakup.nationalbank.kz/ru/publics/buy/102968",
                    "announcement_name_ru": "Текущий ремонт административного здания",
                    "announcement_name_kk": "Әкімшілік ғимаратты ағымдағы жөндеу",
                    "announcement_start_text": "2026-05-15 09:00:00",
                    "announcement_end_text": "2026-05-22 09:00:00",
                    "organizer_name": 'РГУ "НАЦИОНАЛЬНЫЙ БАНК РЕСПУБЛИКИ КАЗАХСТАН"',
                    "organizer_url": "https://zakup.nationalbank.kz/ru/publics/subject/280",
                    "organizer_email": "dinara.beisbayeva@nationalbank.kz",
                    "procurement_method": "Запрос ценовых предложений",
                    "announcement_status": "Опубликовано",
                    "delivery_places": [
                        {
                            "country": "Казахстан",
                            "place": "г. Алматы, ул. Сатпаева",
                            "quantity": "1",
                        }
                    ],
                    "_documents": [
                        {
                            "category": "Проект договора",
                            "name": "ПД_ ТР Сатпаева.docx",
                            "size_text": "55.6 КБ",
                            "uploaded_at_text": "2026-05-15 09:10:00",
                            "hash": "752e59e8d9a789bad39d1832be8280b5",
                            "url": "https://zakup.nationalbank.kz/ru/files/download/752e59e8d9a789bad39d1832be8280b5/?buyid=102968",
                            "ext": "DOCX",
                            "source": "detail_page",
                        }
                    ],
                    "_lots": [
                        {
                            "name_ru": "Текущий ремонт административного здания",
                            "description_ru": "Текущий ремонт административного здания по адресу ул. Сатпаева",
                        }
                    ],
                },
            )
        )
        rows.append(
            make_tender(
                source_name="zakup_unified",
                external_id="39385974",
                title="Закуп строительных материалов для благоустройства территории",
                buyer_name='ГУ "Аппарат акима города Алматы"',
                value_amount=Decimal("22700000.00"),
                value_currency="KZT",
                deadline_offset_days=4,
                published_offset_days=-10,
                first_seen_offset_minutes=69,
                raw_json={
                    "id": 39385974,
                    "external_id": 9876543,
                    "announcement_number": "39385974",
                    "name": "Закуп строительных материалов для благоустройства территории",
                    "publish_date": 1778370738,
                    "offer_start_date": 1778370738,
                    "offer_end_date": 1779580800,
                    "total_price": 22700000.0,
                    "status": {
                        "id": 6,
                        "name": "Опубликован",
                        "is_active": True,
                        "code": "published",
                    },
                    "purchase_method": {
                        "id": 2,
                        "name": "Открытый конкурс",
                        "name_kk": "Ашық конкурс",
                        "code": "open_tender",
                    },
                    "purchase_subject": {
                        "id": 1,
                        "code": "goods",
                        "name": "Товары",
                        "name_kk": "Тауарлар",
                    },
                    "organizer": {
                        "id": 100123,
                        "iin_bin": "950140000123",
                        "name": 'ГУ "Аппарат акима города Алматы"',
                        "address": "г. Алматы, пр. Достык 85",
                        "organization_type": "Государственное учреждение",
                    },
                    "lot_count": 2,
                    "_lots": [
                        {
                            "id": 91234561,
                            "announcement_id": 39385974,
                            "announcement_number": "39385974-1",
                            "lot_number": "1",
                            "name_ru": None,
                            "name_kk": None,
                            "description_ru": None,
                            "description_kk": None,
                            "total_price": 18500000.0,
                            "dumping_price": None,
                            "quantity": 1.0,
                            "organization_name": 'ГУ "Аппарат акима города"',
                            "delivery_addresses": [
                                {
                                    "id": 5001,
                                    "name_ru": "г. Алматы, ул. Тестовая 1",
                                }
                            ],
                            "purchase_method_name": "Открытый конкурс",
                            "purchase_method_id": 2,
                            "offer_start_date": "2026-05-08T02:12:18Z",
                            "offer_end_date": "2026-05-22T18:00:00Z",
                            "announcement_publish_date": "2026-05-08T02:12:18Z",
                            "status": {
                                "id": 6,
                                "name": "Опубликован",
                                "is_active": True,
                                "code": "published",
                            },
                            "system": {"id": 1, "name": "GoszakupRK"},
                            "enstrus": [
                                {
                                    "id": 7001,
                                    "code": "23.61.20",
                                    "name": "Изделия из бетона",
                                }
                            ],
                            "external_id": 1001,
                        },
                        {
                            "id": 91234562,
                            "announcement_id": 39385974,
                            "announcement_number": "39385974-2",
                            "lot_number": "2",
                            "name_ru": "Поставка цемента марки М500",
                            "name_kk": "M500 маркалы цемент жеткізу",
                            "description_ru": "Поставка цемента в мешках по 50 кг",
                            "description_kk": None,
                            "total_price": 4200000.0,
                            "dumping_price": None,
                            "quantity": 50.0,
                            "organization_name": 'ГУ "Аппарат акима города"',
                            "delivery_addresses": [
                                {"id": 5002, "name_ru": "г. Алматы, склад №3"}
                            ],
                            "purchase_method_name": "Открытый конкурс",
                            "purchase_method_id": 2,
                            "offer_start_date": "2026-05-08T02:12:18Z",
                            "offer_end_date": "2026-05-22T18:00:00Z",
                            "announcement_publish_date": "2026-05-08T02:12:18Z",
                            "status": {
                                "id": 6,
                                "name": "Опубликован",
                                "is_active": True,
                                "code": "published",
                            },
                            "system": {"id": 1, "name": "GoszakupRK"},
                            "enstrus": [
                                {"id": 7002, "code": "23.51.12", "name": "Цемент"}
                            ],
                            "external_id": 1002,
                        },
                    ],
                },
            )
        )
        rows.append(
            make_tender(
                source_name="samruk_kazyna",
                external_id="1220290",
                title="Работы по капитальному ремонту нежилых зданий/сооружений/помещений (13 лотов)",
                buyer_name='Акционерное общество "Алатау Жарық Компаниясы"',
                value_amount=Decimal("34191869.47"),
                value_currency="KZT",
                deadline_offset_days=1,
                published_offset_days=-5,
                first_seen_offset_minutes=71,
                raw_json={
                    "id": 1220290,
                    "number": "1220290",
                    "nameRu": "Работы по капитальному ремонту нежилых зданий/сооружений/помещений (13 лотов)",
                    "nameKk": "Тұрғын емес ғимараттарды/құрылыстарды/үй-жайларды күрделі жөндеу жұмыстары (13 лот)",
                    "acceptanceBeginDateTime": "2026-05-13T10:00:00+05:00",
                    "acceptanceEndDateTime": "2026-05-19T10:00:00+05:00",
                    "customer": {
                        "id": 10476878,
                        "identifier": "960840000483",
                        "nameRu": 'Акционерное общество "Алатау Жарық Компаниясы"',
                        "nameKk": 'АҚ "Алатау Жарық Компаниясы"',
                        "bin": "960840000483",
                        "phone": "+7 (727) 376-1973",
                        "email": "info@azhk.kz",
                        "legalAddress": {
                            "countryRu": "КАЗАХСТАН",
                            "katoFullNameRu": "г.Алматы, Бостандыкский район",
                            "street": "Манаса",
                            "building": "24 Б",
                            "flat": "401",
                        },
                    },
                    "organizer": {
                        "id": 10476878,
                        "identifier": "960840000483",
                        "nameRu": 'Акционерное общество "Алатау Жарық Компаниясы"',
                        "nameKk": 'АҚ "Алатау Жарық Компаниясы"',
                        "bin": "960840000483",
                        "legalAddress": {
                            "countryRu": "КАЗАХСТАН",
                            "katoFullNameRu": "г.Алматы, Бостандыкский район",
                            "street": "Манаса",
                            "building": "24 Б",
                            "flat": "401",
                        },
                    },
                    "phone": "+7 (727) 376-1939",
                    "extensionNumber": "3939",
                    "email": "luteuliyeva@azhk.kz",
                    "tenderType": "OTOU",
                    "sumTruNoNds": 34191869.47,
                    "simpleStatus": "ACTIVE",
                    "advertStatus": "PUBLISHED",
                    "documents": [
                        {
                            "documentCategory": "TENDER_DOCUMENTATION_ATTACHMENT",
                            "fileUid": "cef87f7e-1838-48e3-b331-324875fd773b-2026-minio",
                            "fileName": "Тендерная_документация_1198864_2026-04-03.pdf",
                            "contentType": "application/pdf",
                        },
                        {
                            "documentCategory": "ADVERT_ANNOUNCEMENT",
                            "fileUid": "b9cfe65e-58f0-40ff-a221-76ee0b0461bd-2026-minio",
                            "fileName": "объявление_о_закупке_1220290.pdf",
                            "contentType": "application/pdf",
                        },
                    ],
                    "advertRequirement": {
                        "id": 5555413584,
                        "advertId": 1220290,
                        "discussionBeginDateTime": None,
                        "discussionEndDateTime": None,
                    },
                    "_lots": [
                        {
                            "id": 4418837,
                            "number": "4418837",
                            "nameRu": "Работы по капитальному ремонту нежилых зданий/сооружений/помещений",
                            "nameKk": "Тұрғын емес ғимараттарды күрделі жөндеу бойынша жұмыстар",
                            "truHistory": {
                                "id": 5275835444,
                                "code": "410040.300.000010",
                                "ru": "Работы по капитальному ремонту нежилых зданий/сооружений/помещений",
                                "kk": "Тұрғын емес ғимараттарды күрделі жөндеу бойынша жұмыстар",
                                "briefRu": "Работы по капитальному ремонту нежилых зданий/сооружений/помещений",
                                "category": "WORKS",
                            },
                            "customer": {
                                "id": 10476878,
                                "identifier": "960840000483",
                                "nameRu": 'Акционерное общество "Алатау Жарық Компаниясы"',
                                "bin": "960840000483",
                            },
                            "tenderType": "OTOU",
                            "tenderSubjectType": "WORKS",
                            "tenderLocationRu": "ул.Манаса, 24Б",
                            "deliveryCountry": {"id": 8932450, "code": "KZ", "ru": "КАЗАХСТАН"},
                            "deliveryKato": {"id": 17112, "code": "750000000", "ru": "г.Алматы"},
                            "deliveryLocationRu": "КАЗАХСТАН, г.Алматы",
                            "durationMonth": "03.2026",
                            "count": 1.0,
                            "price": 1150635.01,
                            "sumTruNoNds": 1150635.01,
                        },
                        {
                            "id": 4418838,
                            "number": "4418838",
                            "nameRu": "Работы по капитальному ремонту сетей электроснабжения",
                            "nameKk": "Электрмен жабдықтау желілерін күрделі жөндеу жұмыстары",
                            "truHistory": {
                                "id": 5275835445,
                                "code": "410040.300.000012",
                                "ru": "Работы по капитальному ремонту сетей электроснабжения",
                                "kk": "Электрмен жабдықтау желілерін күрделі жөндеу жұмыстары",
                                "category": "WORKS",
                            },
                            "customer": {
                                "id": 10476878,
                                "identifier": "960840000483",
                                "nameRu": 'Акционерное общество "Алатау Жарық Компаниясы"',
                                "bin": "960840000483",
                            },
                            "tenderType": "OTOU",
                            "tenderSubjectType": "WORKS",
                            "tenderLocationRu": "ул.Манаса, 24Б",
                            "sumTruNoNds": 33041234.46,
                        },
                    ],
                },
            )
        )
        rows.append(
            make_tender(
                source_name="ets_tender",
                external_id="2085996",
                title="Лист стальной г/к",
                buyer_name="АО «GALANZ bottlers»",
                value_amount=Decimal("1550000.00"),
                value_currency="KZT",
                deadline_offset_days=2,
                published_offset_days=-1,
                first_seen_offset_minutes=72,
                raw_json={
                    "external_id": "2085996",
                    "procedure_type_text": "Запрос предложений",
                    "title_short": "Запрос предложений № 2085996",
                    "title_description": "Лист стальной горячекатаный",
                    "title_full": "Лист стальной г/к",
                    "description_full": (
                        "Лист стальной горячекатаный для производственных нужд. "
                        "Поставка осуществляется в течение 10 календарных дней."
                    ),
                    "detail_url": "/market/metall/tender-2085996/",
                    "buyer_name": "АО «GALANZ bottlers»",
                    "buyer_url": "/firms/ao-galanz-bottlers/135/",
                    "published_text": "18.05.2026 11:12",
                    "deadline_text": "20.05.2026 15:00",
                    "last_edited_text": "18.05.2026 11:12",
                    "enstru_text": "241031.900.000011 — Лист стальной горячекатаный, толщиной от 4 до 10 мм",
                    "enstru_code": "241031.900.000011",
                    "enstru_label": "Лист стальной горячекатаный, толщиной от 4 до 10 мм",
                    "quantity_text": "10",
                    "unit_price_text": "155 000,00 тенге",
                    "total_price_text": "1 550 000,00 тенге (цена с НДС, НДС: 16%)",
                    "vat_note": "(цена с НДС, НДС: 16%)",
                    "delivery_address": "г. Алматы, ул. Промышленная, 5",
                    "payment_terms": "Безналичный расчёт, в течение 30 календарных дней после поставки",
                    "organizer_link_text": "АО «GALANZ bottlers»",
                    "organizer_link_url": "/firms/ao-galanz-bottlers/135/",
                    "_documents": [
                        {
                            "category": None,
                            "name": "Техническая спецификация",
                            "url": "https://www.ets-tender.kz/uploads/specification.pdf",
                            "ext": "PDF",
                            "source": "detail_page",
                        }
                    ],
                    "_lots": [
                        {
                            "name_ru": "Лист стальной г/к",
                            "description_ru": (
                                "Лист стальной горячекатаный для производственных нужд. "
                                "Поставка осуществляется в течение 10 календарных дней."
                            ),
                        }
                    ],
                },
            )
        )
        # UZ / xt_xarid
        rows.append(
            make_tender(
                source_name="xt_xarid",
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
                raw_json={
                    "id": "x-1",
                    "totalcost": 3000000.0,
                    "status": "docs_objections",
                    "remain_time": 86400,
                    "publicated_at": "2026-05-17T12:00:00",
                    "part_count": 2,
                    "name": "Тендер",
                    "lot_count": 1,
                    "lang": "uz-UZ@cyrillic",
                    "green": True,
                    "good_count": 1,
                    "docs_objections_remain_time": 3600,
                    "currency": "UZS",
                    "close_docs_objections_at": "2026-05-19T18:00:00",
                    "close_at": "2026-06-02T12:00:00",
                    "meta": {
                        "lots": [
                            {
                                "total_sum_lot": 3000000.0,
                                "lot_id": 1,
                                "item_count": 1,
                            }
                        ],
                        "good_maps": [
                            {
                                "unit": "усл.ед",
                                "totalcost_item": 3000000.0,
                                "price": 3000000.0,
                                "name": "Sustainability ESG audit (Uzbekistan)",
                                "description": (
                                    "Independent ESG audit, climate-risk review, "
                                    "and sustainability recommendations."
                                ),
                                "lot_id": 1,
                                "id": "74.90.13.000-00001",
                                "amount": 1,
                            }
                        ],
                        "fin_src": ["401310860262777073202054003"],
                        "company_name": "Uzbekistan Railways",
                        "company_inn": "200837307",
                        "area_path": [
                            {
                                "path": "area.33",
                                "name": "Республика Узбекистан",
                            },
                            {
                                "path": "area.33.2137",
                                "name": "город Ташкент",
                            },
                        ],
                    },
                    "_documents": [
                        {
                            "category": "Technical document",
                            "name": "climate-strategy.pdf",
                            "url": "https://xarid.uzex.uz/x-cloud?file_path=tender%2Fuser-files%2F2026%2F5%2F1%2Fclimate-strategy.pdf",
                            "ext": "PDF",
                            "size_bytes": 125193,
                            "source": "detail_slot",
                        }
                    ],
                    "_lots": [
                        {
                            "name_ru": "Sustainability ESG audit (Uzbekistan)",
                            "description_ru": (
                                "Independent ESG audit, climate-risk review, "
                                "and sustainability recommendations."
                            ),
                            "lot_id": 1,
                            "classification_code": "74.90.13.000-00001",
                            "quantity": 1,
                            "unit": "усл.ед",
                            "unit_price": 3000000.0,
                            "total_amount": 3000000.0,
                        }
                    ],
                },
            )
        )
        rows.append(
            make_tender(
                source_name="xt_xarid",
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
                source_name="xt_xarid",
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
                source_name="xt_xarid",
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
                source_name="xt_xarid",
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
                source_name="xt_xarid",
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
