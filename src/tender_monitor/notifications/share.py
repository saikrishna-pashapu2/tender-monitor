"""One-off tender sharing by email."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.core.config import settings
from tender_monitor.core.enums import NotificationChannel, NotificationStatus
from tender_monitor.core.logging import get_logger
from tender_monitor.core.models import NotificationLog, ShareContact, Tender
from tender_monitor.notifications.email import (
    EmailSender,
    SMTPEmailSender,
    render_share_email,
)

logger = get_logger(__name__)

_SHARE_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_RECIPIENT_SPLIT_RE = re.compile(r"[\s,;]+")


@dataclass(slots=True)
class ShareRecipientFailure:
    recipient: str
    error: str


@dataclass(slots=True)
class ShareTenderResult:
    sent: list[str] = field(default_factory=list)
    failed: list[ShareRecipientFailure] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    @property
    def attempted_count(self) -> int:
        return len(self.sent) + len(self.failed)


class TenderShareSendError(RuntimeError):
    """Backward-compatible exception type for older callers."""


def normalize_sender_name(value: str) -> str | None:
    sender_name = " ".join(value.strip().split())
    return sender_name or None


def sender_key_for(sender_name: str) -> str:
    return " ".join(sender_name.strip().split()).casefold()


def normalize_share_email(value: str) -> str | None:
    email = value.strip().lower()
    if not email or not _SHARE_EMAIL_RE.fullmatch(email):
        return None
    return email


def normalize_recipients(values: Iterable[str]) -> tuple[list[str], list[str]]:
    recipients: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()

    for value in values:
        for token in _RECIPIENT_SPLIT_RE.split(value.strip()):
            if not token:
                continue
            email = normalize_share_email(token)
            if email is None:
                invalid.append(token)
                continue
            if email in seen:
                continue
            seen.add(email)
            recipients.append(email)

    return recipients, invalid


async def list_share_contacts(
    session: AsyncSession,
    sender_name: str,
    *,
    limit: int = 50,
) -> list[str]:
    normalized_sender_name = normalize_sender_name(sender_name)
    if normalized_sender_name is None:
        return []

    stmt = (
        select(ShareContact.email)
        .where(ShareContact.sender_key == sender_key_for(normalized_sender_name))
        .order_by(ShareContact.last_used_at.desc(), ShareContact.email.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _upsert_share_contacts(
    session: AsyncSession,
    *,
    sender_name: str,
    recipients: list[str],
) -> None:
    if not recipients:
        return

    sender_key = sender_key_for(sender_name)
    now = datetime.now(UTC)
    existing_rows = (
        await session.execute(
            select(ShareContact)
            .where(ShareContact.sender_key == sender_key)
            .where(ShareContact.email.in_(recipients))
        )
    ).scalars().all()
    existing = {contact.email: contact for contact in existing_rows}

    for recipient in recipients:
        contact = existing.get(recipient)
        if contact is None:
            session.add(
                ShareContact(
                    sender_name=sender_name,
                    sender_key=sender_key,
                    email=recipient,
                    last_used_at=now,
                    use_count=1,
                )
            )
            continue

        contact.sender_name = sender_name
        contact.last_used_at = now
        contact.use_count += 1


async def share_tender_by_email(
    *,
    session: AsyncSession,
    tender_id: UUID,
    sender_name: str,
    recipients: list[str],
    message: str | None = None,
    sender: EmailSender | None = None,
    app_base_url: str | None = None,
) -> ShareTenderResult | None:
    """Send one tender to arbitrary email addresses.

    Returns ``None`` when the tender does not exist. Every attempted
    recipient is recorded in notification_logs, and successful/attempted
    addresses are saved in share_contacts for the typed sender name. This
    intentionally does not create EmailRecipient subscription rows.
    """
    tender = (
        await session.execute(select(Tender).where(Tender.id == tender_id))
    ).scalar_one_or_none()
    if tender is None:
        return None

    normalized_sender_name = normalize_sender_name(sender_name)
    normalized_recipients, invalid = normalize_recipients(recipients)
    if normalized_sender_name is None:
        return ShareTenderResult(invalid=invalid)
    if invalid or not normalized_recipients:
        return ShareTenderResult(invalid=invalid)

    await _upsert_share_contacts(
        session,
        sender_name=normalized_sender_name,
        recipients=normalized_recipients,
    )

    actual_sender: EmailSender = sender if sender is not None else SMTPEmailSender()
    clean_message = message.strip() if message and message.strip() else None
    email_message = render_share_email(
        tender=tender,
        app_base_url=app_base_url or settings.app_base_url,
        sender_name=normalized_sender_name,
        message=clean_message,
    )

    result = ShareTenderResult()
    for recipient in normalized_recipients:
        try:
            await actual_sender.send(to=recipient, message=email_message)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "notifications.share_email.failed",
                tender_id=str(tender.id),
                recipient=recipient,
                sender_name=normalized_sender_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            session.add(
                NotificationLog(
                    tender_id=tender.id,
                    channel=NotificationChannel.email,
                    recipient=recipient,
                    status=NotificationStatus.failed,
                    error=error,
                )
            )
            result.failed.append(ShareRecipientFailure(recipient=recipient, error=error))
            continue

        session.add(
            NotificationLog(
                tender_id=tender.id,
                channel=NotificationChannel.email,
                recipient=recipient,
                status=NotificationStatus.sent,
            )
        )
        result.sent.append(recipient)
        logger.info(
            "notifications.share_email.sent",
            tender_id=str(tender.id),
            recipient=recipient,
            sender_name=normalized_sender_name,
            subject=email_message.subject,
        )

    await session.commit()
    return result


__all__ = [
    "ShareRecipientFailure",
    "ShareTenderResult",
    "TenderShareSendError",
    "list_share_contacts",
    "normalize_recipients",
    "normalize_sender_name",
    "normalize_share_email",
    "sender_key_for",
    "share_tender_by_email",
]
