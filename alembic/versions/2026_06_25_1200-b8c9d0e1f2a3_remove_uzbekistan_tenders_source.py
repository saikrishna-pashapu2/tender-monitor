"""remove uzbekistan_tenders source

Revision ID: b8c9d0e1f2a3
Revises: d4c5b6a79801
Create Date: 2026-06-25 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "d4c5b6a79801"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM tenders
        WHERE source_name = 'uzbekistan_tenders'
        """
    )
    op.execute(
        """
        DELETE FROM sources
        WHERE name = 'uzbekistan_tenders'
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
            'uzbekistan_tenders',
            'UzbekistanTenders.com (commercial aggregator, UZ)',
            'UZ',
            'https://www.uzbekistantenders.com',
            false,
            60
        )
        ON CONFLICT (name) DO NOTHING
        """
    )
