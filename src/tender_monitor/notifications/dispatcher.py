"""Decide *who* gets emailed about a freshly-matched tender, and send.

The dispatcher is the only place that knows the rules:
  1. Recipient must be ``enabled = True``.
  2. Recipient's subscribed ``groups`` must intersect with the tender's
     ``matched_groups``.
  3. We must not have already sent this tender to this recipient over
     this channel (``notification_logs`` is consulted for dedup).

A failed send for one recipient never blocks the others — every send
is wrapped in try/except, logged, and recorded in ``notification_logs``
with the appropriate status.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tender_monitor.core.config import settings
from tender_monitor.core.enums import NotificationChannel, NotificationStatus
from tender_monitor.core.logging import get_logger
from tender_monitor.core.models import (
    EmailRecipient,
    NotificationLog,
    Tender,
)
from tender_monitor.notifications.email import (
    EmailSender,
    SMTPEmailSender,
    render_email,
)

logger = get_logger(__name__)


async def _already_sent(
    session: AsyncSession, tender_id: UUID, recipient: str
) -> bool:
    """True if there's a successful email send for (tender, recipient)."""
    stmt = (
        select(NotificationLog.id)
        .where(NotificationLog.tender_id == tender_id)
        .where(NotificationLog.channel == NotificationChannel.email)
        .where(NotificationLog.recipient == recipient)
        .where(NotificationLog.status == NotificationStatus.sent)
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def _load_recipients_for(
    session: AsyncSession, matched_groups: list[str]
) -> list[EmailRecipient]:
    """Recipients whose subscription overlaps with the tender's groups."""
    if not matched_groups:
        return []
    rows = (
        await session.execute(
            select(EmailRecipient).where(EmailRecipient.enabled.is_(True))
        )
    ).scalars().all()
    matched = set(matched_groups)
    return [r for r in rows if matched.intersection(r.groups or [])]


async def dispatch_for_tender(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tender_id: UUID,
    sender: EmailSender,
    app_base_url: str | None = None,
) -> int:
    """Send the match-notification email for one tender. Returns the
    number of recipients successfully emailed.

    Each (recipient, status) attempt is recorded in ``notification_logs``
    so the next run can dedup off it. A failure on one recipient is
    logged and continues to the next.
    """
    base_url = app_base_url or settings.app_base_url
    async with session_factory() as session:
        tender = (
            await session.execute(select(Tender).where(Tender.id == tender_id))
        ).scalar_one_or_none()
        if tender is None or not tender.matched_groups:
            return 0

        recipients = await _load_recipients_for(session, list(tender.matched_groups))
        if not recipients:
            return 0

        message = render_email(tender=tender, app_base_url=base_url)

        sent_count = 0
        for recipient in recipients:
            if await _already_sent(session, tender.id, recipient.email):
                logger.debug(
                    "notifications.email.skipped_duplicate",
                    tender_id=str(tender.id),
                    recipient=recipient.email,
                )
                continue
            try:
                await sender.send(to=recipient.email, message=message)
                session.add(
                    NotificationLog(
                        tender_id=tender.id,
                        channel=NotificationChannel.email,
                        recipient=recipient.email,
                        status=NotificationStatus.sent,
                    )
                )
                sent_count += 1
            except Exception as exc:
                logger.error(
                    "notifications.email.failed",
                    tender_id=str(tender.id),
                    recipient=recipient.email,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                session.add(
                    NotificationLog(
                        tender_id=tender.id,
                        channel=NotificationChannel.email,
                        recipient=recipient.email,
                        status=NotificationStatus.failed,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        await session.commit()
        return sent_count


async def dispatch_many(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tender_ids: Iterable[UUID],
    sender: EmailSender | None = None,
    app_base_url: str | None = None,
) -> int:
    """Convenience wrapper used by the scheduler after an ingest run.

    Constructs the production SMTP sender if none is provided. Returns
    the total recipient-emails sent across the batch.
    """
    actual_sender: EmailSender = sender if sender is not None else SMTPEmailSender()
    total = 0
    for tid in tender_ids:
        try:
            total += await dispatch_for_tender(
                session_factory=session_factory,
                tender_id=tid,
                sender=actual_sender,
                app_base_url=app_base_url,
            )
        except Exception as exc:
            logger.error(
                "notifications.dispatch.failed",
                tender_id=str(tid),
                error_type=type(exc).__name__,
                error=str(exc),
            )
    return total


__all__ = [
    "dispatch_for_tender",
    "dispatch_many",
]
