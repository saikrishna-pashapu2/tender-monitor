"""share_contacts

Revision ID: a451d79b6b2a
Revises: 6af0d38a8f3b
Create Date: 2026-06-18 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a451d79b6b2a"
down_revision: str | None = "6af0d38a8f3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "share_contacts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("sender_name", sa.String(length=256), nullable=False),
        sa.Column("sender_key", sa.String(length=256), nullable=False),
        sa.Column("email", sa.String(length=256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "use_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sender_key",
            "email",
            name="uq_share_contacts_sender_email",
        ),
    )
    op.create_index(
        "ix_share_contacts_sender_key",
        "share_contacts",
        ["sender_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_share_contacts_sender_key", table_name="share_contacts")
    op.drop_table("share_contacts")
