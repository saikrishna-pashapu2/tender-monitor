"""initial schema

Revision ID: e868188e7f74
Revises:
Create Date: 2026-05-08 08:53:03.730259+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e868188e7f74"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ENUM_DEFINITIONS: dict[str, tuple[str, ...]] = {
    "country": ("KZ", "UZ"),
    "tender_status": ("announced", "open", "closed", "awarded", "cancelled", "unknown"),
    "language": ("ru", "kk", "uz", "en", "other"),
    "feedback_verdict": ("good_match", "bad_match", "missed"),
    "notification_channel": ("telegram", "email"),
    "notification_status": ("sent", "failed", "skipped"),
}


def _enum(name: str, *, create_type: bool) -> postgresql.ENUM:
    return postgresql.ENUM(
        *_ENUM_DEFINITIONS[name], name=name, create_type=create_type
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    bind = op.get_bind()
    for enum_name in _ENUM_DEFINITIONS:
        _enum(enum_name, create_type=True).create(bind, checkfirst=True)

    op.create_table(
        "sources",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("country", _enum("country", create_type=False), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "schedule_minutes", sa.Integer(), server_default="60", nullable=False
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "total_tenders_seen", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )

    op.create_table(
        "tenders",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("canonical_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("buyer_name", sa.Text(), nullable=True),
        sa.Column("buyer_external_id", sa.String(length=64), nullable=True),
        sa.Column("country", _enum("country", create_type=False), nullable=False),
        sa.Column("sector", sa.String(length=128), nullable=True),
        sa.Column("value_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("value_currency", sa.String(length=3), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            _enum("tender_status", create_type=False),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("source_url", sa.String(length=1024), nullable=False),
        sa.Column(
            "language",
            _enum("language", create_type=False),
            server_default="other",
            nullable=False,
        ),
        sa.Column(
            "matched_groups",
            sa.ARRAY(sa.String()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("match_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ai_relevance_score", sa.Integer(), nullable=True),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("ai_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "change_log",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["canonical_id"], ["tenders.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_name"],
            ["sources.name"],
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_name", "external_id", name="uq_tenders_source_external"
        ),
    )
    op.create_index("ix_tenders_source_name", "tenders", ["source_name"])
    op.create_index("ix_tenders_canonical_id", "tenders", ["canonical_id"])
    op.create_index("ix_tenders_country", "tenders", ["country"])
    op.create_index(
        "ix_tenders_published_at",
        "tenders",
        [sa.text("published_at DESC")],
    )
    op.create_index("ix_tenders_deadline_at", "tenders", ["deadline_at"])
    op.create_index("ix_tenders_first_seen_at", "tenders", ["first_seen_at"])
    op.create_index(
        "ix_tenders_matched_groups",
        "tenders",
        ["matched_groups"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_tenders_raw_json", "tenders", ["raw_json"], postgresql_using="gin"
    )

    op.create_table(
        "feedback",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "verdict", _enum("feedback_verdict", create_type=False), nullable=False
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedback_tender_id", "feedback", ["tender_id"])

    op.create_table(
        "notification_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel",
            _enum("notification_channel", create_type=False),
            nullable=False,
        ),
        sa.Column("recipient", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            _enum("notification_status", create_type=False),
            nullable=False,
        ),
        sa.Column("external_message_id", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_logs_tender_id", "notification_logs", ["tender_id"]
    )
    op.create_index(
        "ix_notification_logs_sent_at", "notification_logs", ["sent_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_notification_logs_sent_at", table_name="notification_logs")
    op.drop_index("ix_notification_logs_tender_id", table_name="notification_logs")
    op.drop_table("notification_logs")

    op.drop_index("ix_feedback_tender_id", table_name="feedback")
    op.drop_table("feedback")

    op.drop_index(
        "ix_tenders_raw_json", table_name="tenders", postgresql_using="gin"
    )
    op.drop_index(
        "ix_tenders_matched_groups", table_name="tenders", postgresql_using="gin"
    )
    op.drop_index("ix_tenders_first_seen_at", table_name="tenders")
    op.drop_index("ix_tenders_deadline_at", table_name="tenders")
    op.drop_index("ix_tenders_published_at", table_name="tenders")
    op.drop_index("ix_tenders_country", table_name="tenders")
    op.drop_index("ix_tenders_canonical_id", table_name="tenders")
    op.drop_index("ix_tenders_source_name", table_name="tenders")
    op.drop_table("tenders")

    op.drop_table("sources")

    bind = op.get_bind()
    for enum_name in reversed(list(_ENUM_DEFINITIONS)):
        _enum(enum_name, create_type=False).drop(bind, checkfirst=True)
