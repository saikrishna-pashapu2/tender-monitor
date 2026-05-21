"""email_recipients

Revision ID: ffa97ec39d69
Revises: e868188e7f74
Create Date: 2026-05-20 19:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ffa97ec39d69"
down_revision: str | None = "e868188e7f74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_recipients",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.Column("team", sa.String(length=128), nullable=True),
        sa.Column(
            "groups",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_email_recipients_email"),
    )
    op.create_index(
        "ix_email_recipients_enabled", "email_recipients", ["enabled"]
    )
    op.create_index("ix_email_recipients_team", "email_recipients", ["team"])


def downgrade() -> None:
    op.drop_index("ix_email_recipients_team", table_name="email_recipients")
    op.drop_index("ix_email_recipients_enabled", table_name="email_recipients")
    op.drop_table("email_recipients")
