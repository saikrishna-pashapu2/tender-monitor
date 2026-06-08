"""Async SMTP sender + HTML email rendering.

Boundary layer between the dispatcher (knows recipients + tenders) and
the outside world (Gmail SMTP). Keep this module dumb: build a message,
authenticate, send. The dispatcher decides *what* and *to whom*.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tender_monitor.api.templating import (
    country_flag,
    deadline_state,
    group_color,
    humanize_key,
    pretty_amount,
    pretty_amount_with_usd,
    pretty_scalar,
    source_color,
    timeago,
)
from tender_monitor.core.config import settings
from tender_monitor.core.logging import get_logger

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


@dataclass(slots=True)
class EmailMessageContent:
    subject: str
    html: str
    text: str


class EmailSender(Protocol):
    """The dispatcher only depends on this Protocol so tests can sub in
    a fake that records calls instead of touching SMTP."""

    async def send(self, *, to: str, message: EmailMessageContent) -> None: ...


_email_env: Environment | None = None


def _env() -> Environment:
    global _email_env
    if _email_env is None:
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters["timeago"] = timeago
        env.filters["deadline_state"] = deadline_state
        env.filters["source_color"] = source_color
        env.filters["group_color"] = group_color
        env.filters["country_flag"] = country_flag
        env.filters["pretty_amount"] = pretty_amount
        env.filters["pretty_amount_with_usd"] = pretty_amount_with_usd
        env.filters["pretty_scalar"] = pretty_scalar
        env.filters["humanize_key"] = humanize_key
        _email_env = env
    return _email_env


def render_email(*, tender: Any, app_base_url: str) -> EmailMessageContent:
    """Render the HTML + plain-text bodies for one matched tender."""
    env = _env()
    detail_url = f"{app_base_url.rstrip('/')}/tenders/{tender.id}"
    context = {
        "tender": tender,
        "detail_url": detail_url,
        "source_url": tender.source_url,
        "matched_groups": list(tender.matched_groups or []),
        "match_details": tender.match_details or {},
    }
    html = env.get_template("tender_match.html").render(**context)
    text = env.get_template("tender_match.txt").render(**context)
    title_snippet = (tender.title or "").strip()
    if len(title_snippet) > 80:
        title_snippet = title_snippet[:77] + "…"
    groups = "+".join(tender.matched_groups or []) or "match"
    subject = f"[{groups}] {title_snippet}"
    return EmailMessageContent(subject=subject, html=html, text=text)


class SMTPEmailSender:
    """Production SMTP sender. Pulls config from the global ``settings``."""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        sender_from: str | None = None,
    ) -> None:
        self.host = host or settings.smtp_host or ""
        self.port = port or settings.smtp_port
        self.user = user or settings.smtp_user or ""
        pw = settings.smtp_password.get_secret_value() if settings.smtp_password else ""
        self.password = password if password is not None else pw
        self.sender_from = sender_from or settings.smtp_from or self.user

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.sender_from)

    async def send(self, *, to: str, message: EmailMessageContent) -> None:
        if not self.configured:
            raise RuntimeError(
                "SMTP not configured; set SMTP_HOST / SMTP_USER / "
                "SMTP_PASSWORD / SMTP_FROM in .env"
            )
        msg = EmailMessage()
        msg["From"] = self.sender_from
        msg["To"] = to
        msg["Subject"] = message.subject
        msg.set_content(message.text)
        msg.add_alternative(message.html, subtype="html")

        await aiosmtplib.send(
            msg,
            hostname=self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            start_tls=True,
        )
        logger.info(
            "notifications.email.sent",
            to=to,
            subject=message.subject,
        )


__all__ = [
    "TEMPLATES_DIR",
    "EmailMessageContent",
    "EmailSender",
    "SMTPEmailSender",
    "render_email",
]
