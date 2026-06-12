"""Per-tender upsert with change tracking.

Pure(ish) DB-write layer: the only thing this module knows about is
SQLAlchemy and the canonical schema. Connector orchestration, source
health, and the matcher contract all live one layer up in
``scheduler.ingest``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.core.models import Tender
from tender_monitor.core.schemas import TenderUpsert
from tender_monitor.matching import MatchResult

# Fields whose changes drive "this tender changed" alerts. Other field
# updates apply silently. Keep this whitelist small — every entry here
# adds noise to the change_log and Prompt 7's notifier.
TRACKED_FIELDS: tuple[str, ...] = ("title", "status", "deadline_at", "value_amount")


class UpsertOutcome(str, Enum):
    created = "created"
    updated = "updated"
    unchanged = "unchanged"


class UpsertResult(BaseModel):
    outcome: UpsertOutcome
    changes: list[str] = Field(default_factory=list)
    tender_id: UUID


def _serialize(value: Any) -> Any:
    """Best-effort JSON-friendly serialization for change_log entries."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _values_differ(old: Any, new: Any) -> bool:
    """Equality-aware comparison.

    Decimal == Decimal handles "100" == "100.00".
    datetime equality requires both sides timezone-aware (the schema
    guarantees that); naive vs aware here would raise, which is what
    we want — that's a programming bug.
    """
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    return bool(old != new)


async def upsert_tender(
    session: AsyncSession,
    upsert: TenderUpsert,
    match: MatchResult,
    now: datetime,
) -> UpsertResult:
    """Insert or update one tender row.

    Behavior:
      - INSERT when no row with (source_name, external_id) exists.
        first_seen_at = last_seen_at = last_changed_at = now;
        change_log = []; is_active = True.
      - UPDATE always sets last_seen_at, raw_json, matched_groups,
        match_details, is_active=True.
      - For each TRACKED_FIELDS member that differs, also update the
        normalized column, set last_changed_at = now, and append a
        change_log entry shaped as
        ``{"at": iso, "fields": {field: {"old": ..., "new": ...}}}``.
      - Matcher results are overwritten on every run (not tracked) so
        a keyword tweak takes effect immediately on the next ingest.
    """
    existing = (
        await session.execute(
            select(Tender).where(
                Tender.source_name == upsert.source_name,
                Tender.external_id == upsert.external_id,
            )
        )
    ).scalar_one_or_none()

    match_details_value: dict[str, dict[str, list[str]]] | None = (
        match.match_details if match.match_details else None
    )

    if existing is None:
        tender = Tender(
            source_name=upsert.source_name,
            external_id=upsert.external_id,
            title=upsert.title,
            title_en=upsert.title_en,
            title_language=upsert.title_language,
            translation_provider=upsert.translation_provider,
            title_translated_at=upsert.title_translated_at,
            buyer_name=upsert.buyer_name,
            buyer_external_id=upsert.buyer_external_id,
            country=upsert.country,
            sector=upsert.sector,
            value_amount=upsert.value_amount,
            value_currency=upsert.value_currency,
            published_at=upsert.published_at,
            deadline_at=upsert.deadline_at,
            status=upsert.status,
            source_url=upsert.source_url,
            language=upsert.language,
            matched_groups=list(match.matched_groups),
            match_details=match_details_value,
            raw_json=upsert.raw_json,
            first_seen_at=now,
            last_seen_at=now,
            last_changed_at=now,
            change_log=[],
            is_active=True,
        )
        session.add(tender)
        await session.flush()
        return UpsertResult(outcome=UpsertOutcome.created, tender_id=tender.id)

    title_changed = _values_differ(existing.title, upsert.title)
    translation_supplied = any(
        value is not None
        for value in (
            upsert.title_en,
            upsert.title_language,
            upsert.translation_provider,
            upsert.title_translated_at,
        )
    )

    # Always-write fields (not change-tracked).
    existing.last_seen_at = now
    existing.is_active = True
    existing.raw_json = upsert.raw_json
    existing.matched_groups = list(match.matched_groups)
    existing.match_details = match_details_value
    # Buyer / org metadata changes silently — they get noisy on real
    # data and aren't useful as alerts.
    existing.buyer_name = upsert.buyer_name
    existing.buyer_external_id = upsert.buyer_external_id
    existing.sector = upsert.sector
    existing.value_currency = upsert.value_currency
    existing.published_at = upsert.published_at
    existing.source_url = upsert.source_url
    existing.language = upsert.language
    if translation_supplied or title_changed:
        existing.title_en = upsert.title_en
        existing.title_language = upsert.title_language
        existing.translation_provider = upsert.translation_provider
        existing.title_translated_at = upsert.title_translated_at

    # Tracked fields: diff + log.
    changed_fields: dict[str, dict[str, Any]] = {}
    for field in TRACKED_FIELDS:
        old_value = getattr(existing, field)
        new_value = getattr(upsert, field)
        if _values_differ(old_value, new_value):
            changed_fields[field] = {
                "old": _serialize(old_value),
                "new": _serialize(new_value),
            }
            setattr(existing, field, new_value)

    if changed_fields:
        existing.last_changed_at = now
        entry: dict[str, Any] = {
            "at": now.isoformat(),
            "fields": changed_fields,
        }
        existing.change_log = [*existing.change_log, entry]
        await session.flush()
        return UpsertResult(
            outcome=UpsertOutcome.updated,
            changes=list(changed_fields.keys()),
            tender_id=existing.id,
        )

    await session.flush()
    return UpsertResult(outcome=UpsertOutcome.unchanged, tender_id=existing.id)


__all__ = [
    "TRACKED_FIELDS",
    "UpsertOutcome",
    "UpsertResult",
    "upsert_tender",
]
