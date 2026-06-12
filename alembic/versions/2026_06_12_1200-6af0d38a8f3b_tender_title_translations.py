"""tender title translations

Revision ID: 6af0d38a8f3b
Revises: ffa97ec39d69
Create Date: 2026-06-12 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6af0d38a8f3b"
down_revision: str | None = "ffa97ec39d69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenders", sa.Column("title_en", sa.Text(), nullable=True))
    op.add_column("tenders", sa.Column("title_language", sa.String(length=16), nullable=True))
    op.add_column(
        "tenders", sa.Column("translation_provider", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "tenders", sa.Column("title_translated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tenders", "title_translated_at")
    op.drop_column("tenders", "translation_provider")
    op.drop_column("tenders", "title_language")
    op.drop_column("tenders", "title_en")
