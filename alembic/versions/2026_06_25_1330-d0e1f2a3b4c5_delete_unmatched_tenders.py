"""Delete unmatched tenders.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-06-25 13:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM tenders
        WHERE COALESCE(array_length(matched_groups, 1), 0) = 0
        """
    )


def downgrade() -> None:
    # Deleted unmatched tenders cannot be reconstructed from the schema.
    pass
