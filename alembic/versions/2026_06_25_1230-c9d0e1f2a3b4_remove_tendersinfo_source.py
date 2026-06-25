"""remove tendersinfo source

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-25 12:30:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM tenders
        WHERE source_name = 'tendersinfo'
        """
    )
    op.execute(
        """
        DELETE FROM sources
        WHERE name = 'tendersinfo'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO sources (
            name,
            display_name,
            country,
            base_url,
            enabled,
            schedule_minutes
        )
        VALUES (
            'tendersinfo',
            'TendersInfo (commercial aggregator, KZ + UZ)',
            'KZ',
            'https://www.tendersinfo.com',
            false,
            60
        )
        ON CONFLICT (name) DO NOTHING
        """
    )
