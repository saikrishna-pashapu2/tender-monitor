"""Outbound notifications (email today; Telegram TBD)."""

from __future__ import annotations

from tender_monitor.notifications.dispatcher import (
    dispatch_for_tender,
    dispatch_many,
)
from tender_monitor.notifications.email import (
    EmailMessageContent,
    EmailSender,
    SMTPEmailSender,
    render_email,
    render_share_email,
)
from tender_monitor.notifications.share import (
    ShareRecipientFailure,
    ShareTenderResult,
    TenderShareSendError,
    list_share_contacts,
    normalize_recipients,
    normalize_sender_name,
    normalize_share_email,
    sender_key_for,
    share_tender_by_email,
)

__all__ = [
    "EmailMessageContent",
    "EmailSender",
    "SMTPEmailSender",
    "ShareRecipientFailure",
    "ShareTenderResult",
    "TenderShareSendError",
    "dispatch_for_tender",
    "dispatch_many",
    "list_share_contacts",
    "normalize_recipients",
    "normalize_sender_name",
    "normalize_share_email",
    "render_email",
    "render_share_email",
    "sender_key_for",
    "share_tender_by_email",
]
