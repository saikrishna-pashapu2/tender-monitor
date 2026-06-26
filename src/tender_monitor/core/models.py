from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
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

country_enum = SAEnum(
    Country,
    name="country",
    values_callable=lambda enum: [member.value for member in enum],
    native_enum=False,
    create_constraint=False,
    length=2,
)
tender_status_enum = SAEnum(
    TenderStatus,
    name="tender_status",
    values_callable=lambda enum: [member.value for member in enum],
    native_enum=False,
    create_constraint=False,
    length=32,
)
language_enum = SAEnum(
    Language,
    name="language",
    values_callable=lambda enum: [member.value for member in enum],
    native_enum=False,
    create_constraint=False,
    length=16,
)
feedback_verdict_enum = SAEnum(
    FeedbackVerdict,
    name="feedback_verdict",
    values_callable=lambda enum: [member.value for member in enum],
    native_enum=False,
    create_constraint=False,
    length=32,
)
notification_channel_enum = SAEnum(
    NotificationChannel,
    name="notification_channel",
    values_callable=lambda enum: [member.value for member in enum],
    native_enum=False,
    create_constraint=False,
    length=32,
)
notification_status_enum = SAEnum(
    NotificationStatus,
    name="notification_status",
    values_callable=lambda enum: [member.value for member in enum],
    native_enum=False,
    create_constraint=False,
    length=32,
)


class Source(Base):
    __tablename__ = "monitored_tender_sources"

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
    __tablename__ = "monitored_tenders"
    __table_args__ = (
        UniqueConstraint("source_name", "external_id", name="uq_monitored_tenders_source_external"),
        Index("idx_monitored_tenders_source_name", "source_name"),
        Index("idx_monitored_tenders_canonical_id", "canonical_id"),
        Index("idx_monitored_tenders_country", "country"),
        Index("idx_monitored_tenders_published_at", "published_at"),
        Index("idx_monitored_tenders_deadline_at", "deadline_at"),
        Index("idx_monitored_tenders_first_seen_at", "first_seen_at"),
        Index("idx_monitored_tenders_matched_groups", "matched_groups", postgresql_using="gin"),
        Index("idx_monitored_tenders_raw_json", "raw_json", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    source_name: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("monitored_tender_sources.name", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitored_tenders.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    translation_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title_translated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    likes: Mapped[list[TenderLike]] = relationship(
        back_populates="tender",
        cascade="all, delete-orphan",
        order_by="TenderLike.created_at.desc()",
    )

    @property
    def like_count(self) -> int:
        return len(self.likes)


class Feedback(Base):
    __tablename__ = "monitored_tender_feedback"
    __table_args__ = (Index("idx_monitored_tender_feedback_tender_id", "tender_id"),)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tender_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitored_tenders.id", ondelete="CASCADE"),
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
    __tablename__ = "monitored_notification_logs"
    __table_args__ = (
        Index("idx_monitored_notification_logs_tender_id", "tender_id"),
        Index("idx_monitored_notification_logs_sent_at", "sent_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tender_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitored_tenders.id", ondelete="CASCADE"),
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

    __tablename__ = "monitored_email_recipients"
    __table_args__ = (
        Index("idx_monitored_email_recipients_enabled", "enabled"),
        Index("idx_monitored_email_recipients_team", "team"),
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


class ShareContact(Base):
    """Saved one-off tender share recipient for a typed sender name."""

    __tablename__ = "monitored_share_contacts"
    __table_args__ = (
        UniqueConstraint(
            "sender_key",
            "email",
            name="uq_monitored_share_contacts_sender_email",
        ),
        Index("idx_monitored_share_contacts_sender_key", "sender_key"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    sender_name: Mapped[str] = mapped_column(String(256), nullable=False)
    sender_key: Mapped[str] = mapped_column(String(256), nullable=False)
    email: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    use_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )


class TeamMember(Base):
    """Internal teammate identity used by likes and share sender history."""

    __tablename__ = "monitored_team_members"
    __table_args__ = (
        UniqueConstraint("member_key", name="uq_monitored_team_members_member_key"),
        Index("idx_monitored_team_members_member_key", "member_key"),
        Index("idx_monitored_team_members_last_used_at", "last_used_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    member_key: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    use_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )

    likes: Mapped[list[TenderLike]] = relationship(
        back_populates="team_member",
        cascade="all, delete-orphan",
        order_by="TenderLike.created_at.desc()",
    )


class TenderLike(Base):
    """A single teammate's like on a tender."""

    __tablename__ = "monitored_tender_likes"
    __table_args__ = (
        UniqueConstraint(
            "tender_id",
            "team_member_id",
            name="uq_monitored_tender_likes_tender_member",
        ),
        Index("idx_monitored_tender_likes_tender_id", "tender_id"),
        Index("idx_monitored_tender_likes_team_member_id", "team_member_id"),
        Index("idx_monitored_tender_likes_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tender_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitored_tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    team_member_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitored_team_members.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tender: Mapped[Tender] = relationship(back_populates="likes")
    team_member: Mapped[TeamMember] = relationship(back_populates="likes")


__all__ = [
    "EmailRecipient",
    "Feedback",
    "NotificationLog",
    "ShareContact",
    "Source",
    "TeamMember",
    "Tender",
    "TenderLike",
]
