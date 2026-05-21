from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression as sql_expr

from tender_monitor.core.database import Base
from tender_monitor.core.enums import (
    Country,
    FeedbackVerdict,
    Language,
    NotificationChannel,
    NotificationStatus,
    TenderStatus,
)

country_enum = PG_ENUM(
    Country,
    name="country",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=True,
)
tender_status_enum = PG_ENUM(
    TenderStatus,
    name="tender_status",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=True,
)
language_enum = PG_ENUM(
    Language,
    name="language",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=True,
)
feedback_verdict_enum = PG_ENUM(
    FeedbackVerdict,
    name="feedback_verdict",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=True,
)
notification_channel_enum = PG_ENUM(
    NotificationChannel,
    name="notification_channel",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=True,
)
notification_status_enum = PG_ENUM(
    NotificationStatus,
    name="notification_status",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=True,
)


class Source(Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    country: Mapped[Country] = mapped_column(country_enum, nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_expr.true()
    )
    schedule_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="60"
    )

    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_tenders_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    tenders: Mapped[list[Tender]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Tender(Base):
    __tablename__ = "tenders"
    __table_args__ = (
        UniqueConstraint("source_name", "external_id", name="uq_tenders_source_external"),
        Index("ix_tenders_source_name", "source_name"),
        Index("ix_tenders_canonical_id", "canonical_id"),
        Index("ix_tenders_country", "country"),
        Index("ix_tenders_published_at", "published_at"),
        Index("ix_tenders_deadline_at", "deadline_at"),
        Index("ix_tenders_first_seen_at", "first_seen_at"),
        Index("ix_tenders_matched_groups", "matched_groups", postgresql_using="gin"),
        Index("ix_tenders_raw_json", "raw_json", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    source_name: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sources.name", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    buyer_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    country: Mapped[Country] = mapped_column(country_enum, nullable=False)
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    value_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    value_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[TenderStatus] = mapped_column(
        tender_status_enum, nullable=False, server_default=TenderStatus.unknown.value
    )
    source_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    language: Mapped[Language] = mapped_column(
        language_enum, nullable=False, server_default=Language.other.value
    )

    matched_groups: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    match_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ai_relevance_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    change_log: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_expr.true()
    )

    source: Mapped[Source] = relationship(back_populates="tenders")
    canonical: Mapped[Tender | None] = relationship(
        "Tender", remote_side="Tender.id", foreign_keys=[canonical_id]
    )
    feedback: Mapped[list[Feedback]] = relationship(
        back_populates="tender", cascade="all, delete-orphan"
    )
    notifications: Mapped[list[NotificationLog]] = relationship(
        back_populates="tender", cascade="all, delete-orphan"
    )


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_tender_id", "tender_id"),)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tender_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    verdict: Mapped[FeedbackVerdict] = mapped_column(feedback_verdict_enum, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tender: Mapped[Tender] = relationship(back_populates="feedback")


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        Index("ix_notification_logs_tender_id", "tender_id"),
        Index("ix_notification_logs_sent_at", "sent_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tender_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        notification_channel_enum, nullable=False
    )
    recipient: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        notification_status_enum, nullable=False
    )
    external_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tender: Mapped[Tender] = relationship(back_populates="notifications")


class EmailRecipient(Base):
    """One person / mailbox subscribed to matched-tender notifications.

    ``groups`` is the set of keyword-group names the recipient cares
    about (e.g. ``["esg"]``, ``["credit_rating"]``, or both). The
    dispatcher sends a recipient an email only when a freshly-matched
    tender's ``matched_groups`` intersects with their ``groups``.
    """

    __tablename__ = "email_recipients"
    __table_args__ = (
        Index("ix_email_recipients_enabled", "enabled"),
        Index("ix_email_recipients_team", "team"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    email: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    team: Mapped[str | None] = mapped_column(String(128), nullable=True)
    groups: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default="{}"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sql_expr.true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = [
    "EmailRecipient",
    "Feedback",
    "NotificationLog",
    "Source",
    "Tender",
]
