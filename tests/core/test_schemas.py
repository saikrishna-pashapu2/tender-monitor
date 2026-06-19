from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tender_monitor.core.enums import (
    Country,
    FeedbackVerdict,
    Language,
    TenderStatus,
)
from tender_monitor.core.models import Tender
from tender_monitor.core.schemas import FeedbackCreate, TenderSummary, TenderUpsert


def test_tender_summary_from_orm() -> None:
    tender = Tender(
        id=uuid4(),
        source_name="goszakup",
        external_id="T-1",
        title="ESG audit services",
        buyer_name="State Procurement Agency",
        country=Country.KZ,
        value_amount=Decimal("12345.67"),
        value_currency="KZT",
        deadline_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        published_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        source_url="https://goszakup.gov.kz/T-1",
        status=TenderStatus.open,
        language=Language.ru,
        raw_json={"original": "payload"},
        matched_groups=["esg"],
        ai_relevance_score=8,
    )

    summary = TenderSummary.model_validate(tender)

    assert summary.id == tender.id
    assert summary.source_name == "goszakup"
    assert summary.external_id == "T-1"
    assert summary.title == "ESG audit services"
    assert summary.buyer_name == "State Procurement Agency"
    assert summary.country is Country.KZ
    assert summary.value_amount == Decimal("12345.67")
    assert summary.value_currency == "KZT"
    assert summary.deadline_at == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    assert summary.published_at == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    assert summary.matched_groups == ["esg"]
    assert summary.ai_relevance_score == 8
    assert summary.source_url == "https://goszakup.gov.kz/T-1"
    assert summary.like_count == 0
    assert summary.likes == []


def test_feedback_create_validation() -> None:
    tender_id = uuid4()

    valid = FeedbackCreate(
        tender_id=tender_id,
        verdict=FeedbackVerdict.good_match,
        note="looks relevant",
        created_by="ops@finvizier.com",
    )
    assert valid.verdict is FeedbackVerdict.good_match

    valid_str = FeedbackCreate.model_validate(
        {"tender_id": str(tender_id), "verdict": "bad_match"}
    )
    assert valid_str.verdict is FeedbackVerdict.bad_match
    assert valid_str.note is None

    with pytest.raises(ValidationError):
        FeedbackCreate.model_validate(
            {"tender_id": str(tender_id), "verdict": "not_a_verdict"}
        )


def test_tender_upsert_coerces_raw_json_to_json_safe_values() -> None:
    upsert = TenderUpsert(
        source_name="mitwork",
        external_id="T-1",
        title="Test",
        country=Country.KZ,
        status=TenderStatus.open,
        source_url="https://example.test/T-1",
        language=Language.ru,
        raw_json={
            "amount": Decimal("123.45"),
            "nested": {
                "seen_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                "items": [Decimal("7.50"), uuid4()],
            },
        },
    )

    assert upsert.raw_json["amount"] == "123.45"
    assert upsert.raw_json["nested"]["seen_at"] == "2026-06-01T12:00:00+00:00"
    assert upsert.raw_json["nested"]["items"][0] == "7.50"
    assert isinstance(upsert.raw_json["nested"]["items"][1], str)
