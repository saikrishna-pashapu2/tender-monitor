from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tender_monitor.core.enums import (
    Country,
    NotificationStatus,
    TenderStatus,
)
from tender_monitor.core.models import (
    EmailRecipient,
    NotificationLog,
    Source,
    Tender,
)
from tender_monitor.notifications.dispatcher import dispatch_for_tender
from tender_monitor.notifications.email import EmailMessageContent, EmailSender


@dataclass(slots=True)
class RecordingSender(EmailSender):  # type: ignore[misc]
    """Test double: stores every send() call, raises on demand."""
    sent: list[tuple[str, str]] = field(default_factory=list)
    fail_for: set[str] = field(default_factory=set)

    async def send(self, *, to: str, message: EmailMessageContent) -> None:
        if to in self.fail_for:
            raise RuntimeError(f"forced failure for {to}")
        self.sent.append((to, message.subject))


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _truncate_tables(test_database_url: str) -> AsyncIterator[None]:
    engine = create_async_engine(test_database_url, future=True)
    truncate_sql = (
        "TRUNCATE notification_logs, feedback, tenders, sources, "
        "email_recipients RESTART IDENTITY CASCADE"
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
async def factory(
    test_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = create_async_engine(test_database_url, future=True)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


T0 = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)


async def _seed_world(
    factory: async_sessionmaker[AsyncSession],
    *,
    matched_groups: list[str],
    recipients: list[tuple[str, list[str], bool]],
) -> Tender:
    """Insert one source, one tender (with given matched_groups), and N
    recipients (email, groups, enabled). Returns the tender."""
    async with factory() as session:
        session.add(
            Source(
                name="goszakup",
                display_name="Goszakup",
                country=Country.KZ,
                base_url="https://example.test",
            )
        )
        for email, groups, enabled in recipients:
            session.add(
                EmailRecipient(
                    email=email,
                    groups=groups,
                    enabled=enabled,
                )
            )
        await session.flush()
        tender = Tender(
            source_name="goszakup",
            external_id="t-1",
            title="Credit rating audit services",
            buyer_name="National Bank",
            country=Country.KZ,
            value_amount=Decimal("500000.00"),
            value_currency="KZT",
            published_at=T0 - timedelta(hours=1),
            deadline_at=T0 + timedelta(days=7),
            status=TenderStatus.open,
            source_url="https://example.test/t-1",
            matched_groups=matched_groups,
            match_details={
                g: {"matched_phrases": ["credit rating"], "matched_tokens": []}
                for g in matched_groups
            },
            raw_json={"id": "t-1"},
            first_seen_at=T0,
            last_seen_at=T0,
            last_changed_at=T0,
            change_log=[],
            is_active=True,
        )
        session.add(tender)
        await session.commit()
        return tender


async def test_dispatch_sends_to_subscribed_recipients_only(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tender = await _seed_world(
        factory,
        matched_groups=["esg"],
        recipients=[
            ("esg-only@test", ["esg"], True),
            ("credit-only@test", ["credit_rating"], True),
            ("both@test", ["esg", "credit_rating"], True),
        ],
    )
    sender = RecordingSender()
    sent = await dispatch_for_tender(
        session_factory=factory, tender_id=tender.id, sender=sender
    )
    assert sent == 2
    recipients = {addr for addr, _ in sender.sent}
    assert recipients == {"esg-only@test", "both@test"}


async def test_dispatch_skips_disabled_recipients(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tender = await _seed_world(
        factory,
        matched_groups=["esg"],
        recipients=[
            ("active@test", ["esg"], True),
            ("paused@test", ["esg"], False),
        ],
    )
    sender = RecordingSender()
    sent = await dispatch_for_tender(
        session_factory=factory, tender_id=tender.id, sender=sender
    )
    assert sent == 1
    assert sender.sent[0][0] == "active@test"


async def test_dispatch_dedups_via_notification_logs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tender = await _seed_world(
        factory,
        matched_groups=["esg"],
        recipients=[("once@test", ["esg"], True)],
    )
    sender = RecordingSender()

    first = await dispatch_for_tender(
        session_factory=factory, tender_id=tender.id, sender=sender
    )
    second = await dispatch_for_tender(
        session_factory=factory, tender_id=tender.id, sender=sender
    )
    assert first == 1
    assert second == 0  # already-sent → skipped
    assert len(sender.sent) == 1


async def test_dispatch_logs_failures_without_crashing_other_recipients(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tender = await _seed_world(
        factory,
        matched_groups=["esg"],
        recipients=[
            ("broken@test", ["esg"], True),
            ("works@test", ["esg"], True),
        ],
    )
    sender = RecordingSender(fail_for={"broken@test"})
    sent = await dispatch_for_tender(
        session_factory=factory, tender_id=tender.id, sender=sender
    )
    assert sent == 1
    assert sender.sent == [("works@test", sender.sent[0][1])]

    async with factory() as session:
        logs = (
            await session.execute(
                select(NotificationLog).order_by(NotificationLog.recipient.asc())
            )
        ).scalars().all()
    by_recipient = {log.recipient: log for log in logs}
    assert by_recipient["broken@test"].status is NotificationStatus.failed
    assert by_recipient["broken@test"].error is not None
    assert by_recipient["works@test"].status is NotificationStatus.sent


async def test_dispatch_does_nothing_for_unmatched_tender(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tender = await _seed_world(
        factory,
        matched_groups=[],
        recipients=[("subscriber@test", ["esg"], True)],
    )
    sender = RecordingSender()
    sent = await dispatch_for_tender(
        session_factory=factory, tender_id=tender.id, sender=sender
    )
    assert sent == 0
    assert sender.sent == []


def test_render_email_includes_phrases_and_links() -> None:
    """Minimum-fidelity render check — subject + bodies must reference
    the tender title, matched group, and the detail link."""
    from types import SimpleNamespace
    from uuid import uuid4

    from tender_monitor.notifications.email import render_email

    tender = SimpleNamespace(
        id=uuid4(),
        title="ESG audit services",
        buyer_name="Acme Co",
        country=Country.KZ,
        source_name="goszakup",
        source_url="https://src/example",
        published_at=T0,
        deadline_at=T0 + timedelta(days=5),
        value_amount=Decimal("100000"),
        value_currency="KZT",
        matched_groups=["esg"],
        match_details={
            "esg": {"matched_phrases": ["ESG audit"], "matched_tokens": ["ESG"]}
        },
    )
    message = render_email(tender=tender, app_base_url="http://app/")
    assert "ESG audit services" in message.html
    assert "ESG audit" in message.html  # phrase chip
    assert "esg" in message.subject
    assert "http://app/tenders/" in message.html
    assert "View on source" in message.html
    assert "ESG audit services" in message.text


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["esg"], ["esg"]),
        (["credit_rating", "esg"], ["esg", "credit_rating"]),
        (["nonsense"], []),
        ([], []),
    ],
)
def test_settings_normalise_groups(raw: list[str], expected: list[str]) -> None:
    from tender_monitor.api.routes.settings import _normalise_groups

    assert _normalise_groups(raw) == expected
