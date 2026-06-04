from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tender_monitor.core.enums import (
    Country,
    FeedbackVerdict,
    Language,
    NotificationChannel,
    NotificationStatus,
    TenderStatus,
)


class _ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(nested) for nested in value]
    return value


class SourceRead(_ORMModel):
    name: str
    display_name: str
    country: Country
    base_url: str
    enabled: bool
    schedule_minutes: int
    last_run_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    total_tenders_seen: int
    updated_at: datetime


class TenderSummary(_ORMModel):
    id: UUID
    source_name: str
    external_id: str
    title: str
    buyer_name: str | None
    country: Country
    value_amount: Decimal | None
    value_currency: str | None
    deadline_at: datetime | None
    matched_groups: list[str]
    # Per-group `matched_phrases` / `matched_tokens`. Cheap to include in
    # the summary payload (already on the row) and what API consumers
    # like the portal need to render "why this matched" without a
    # second round-trip to the detail endpoint.
    match_details: dict[str, Any] | None
    ai_relevance_score: int | None
    source_url: str
    published_at: datetime | None


class TenderRead(_ORMModel):
    id: UUID
    source_name: str
    external_id: str
    canonical_id: UUID | None
    title: str
    buyer_name: str | None
    buyer_external_id: str | None
    country: Country
    sector: str | None
    value_amount: Decimal | None
    value_currency: str | None
    published_at: datetime | None
    deadline_at: datetime | None
    status: TenderStatus
    source_url: str
    language: Language
    matched_groups: list[str]
    match_details: dict[str, Any] | None
    ai_relevance_score: int | None
    ai_summary: str | None
    ai_processed_at: datetime | None
    raw_json: dict[str, Any]
    first_seen_at: datetime
    last_seen_at: datetime
    last_changed_at: datetime
    change_log: list[dict[str, Any]]
    is_active: bool


class FeedbackRead(_ORMModel):
    id: UUID
    tender_id: UUID
    verdict: FeedbackVerdict
    note: str | None
    created_by: str | None
    created_at: datetime


class FeedbackCreate(BaseModel):
    tender_id: UUID
    verdict: FeedbackVerdict
    note: str | None = None
    created_by: str | None = None


class NotificationLogRead(_ORMModel):
    id: UUID
    tender_id: UUID
    channel: NotificationChannel
    recipient: str
    status: NotificationStatus
    external_message_id: str | None
    error: str | None
    sent_at: datetime


class TenderUpsert(BaseModel):
    """Connector output: the canonical shape a connector produces.

    Excludes id, first_seen_at, matched_groups, ai_*, change_log — those are
    derived or managed by the system, not the connector.
    """

    model_config = ConfigDict(extra="forbid")

    source_name: str
    external_id: str
    title: str
    buyer_name: str | None = None
    buyer_external_id: str | None = None
    country: Country
    sector: str | None = None
    value_amount: Decimal | None = None
    value_currency: str | None = Field(default=None, max_length=3, min_length=3)
    published_at: datetime | None = None
    deadline_at: datetime | None = None
    status: TenderStatus = TenderStatus.unknown
    source_url: str
    language: Language = Language.other
    raw_json: dict[str, Any]

    @field_validator("raw_json", mode="before")
    @classmethod
    def _coerce_raw_json_json_safe(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any]:
        return _json_safe(value or {})


__all__ = [
    "FeedbackCreate",
    "FeedbackRead",
    "NotificationLogRead",
    "SourceRead",
    "TenderRead",
    "TenderSummary",
    "TenderUpsert",
]
