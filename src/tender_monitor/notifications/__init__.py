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
)

__all__ = [
    "EmailMessageContent",
    "EmailSender",
    "SMTPEmailSender",
    "dispatch_for_tender",
    "dispatch_many",
    "render_email",
]
