"""team members and tender likes

Revision ID: d4c5b6a79801
Revises: a451d79b6b2a
Create Date: 2026-06-19 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d4c5b6a79801"
down_revision: str | None = "a451d79b6b2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "team_members",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("member_key", sa.String(length=256), nullable=False),
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
        sa.UniqueConstraint("member_key", name="uq_team_members_member_key"),
    )
    op.create_index(
        "ix_team_members_member_key",
        "team_members",
        ["member_key"],
    )
    op.create_index(
        "ix_team_members_last_used_at",
        "team_members",
        ["last_used_at"],
    )

    op.execute(
        """
        WITH candidate_names AS (
            SELECT
                sender_name AS display_name,
                sender_key AS member_key,
                max(last_used_at) AS last_used_at,
                count(*)::integer AS use_count
            FROM share_contacts
            WHERE btrim(sender_name) <> ''
              AND btrim(sender_key) <> ''
            GROUP BY sender_name, sender_key

            UNION ALL

            SELECT
                regexp_replace(btrim(name), '\\s+', ' ', 'g') AS display_name,
                lower(regexp_replace(btrim(name), '\\s+', ' ', 'g')) AS member_key,
                max(updated_at) AS last_used_at,
                1 AS use_count
            FROM email_recipients
            WHERE name IS NOT NULL
              AND btrim(name) <> ''
            GROUP BY regexp_replace(btrim(name), '\\s+', ' ', 'g')
        ),
        ranked_names AS (
            SELECT
                display_name,
                member_key,
                last_used_at,
                use_count,
                row_number() OVER (
                    PARTITION BY member_key
                    ORDER BY last_used_at DESC, display_name ASC
                ) AS rn
            FROM candidate_names
        )
        INSERT INTO team_members (
            display_name,
            member_key,
            created_at,
            updated_at,
            last_used_at,
            use_count
        )
        SELECT
            display_name,
            member_key,
            now(),
            now(),
            coalesce(last_used_at, now()),
            greatest(use_count, 1)
        FROM ranked_names
        WHERE rn = 1
        ON CONFLICT (member_key) DO NOTHING
        """
    )

    op.create_table(
        "tender_likes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tender_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tender_id"], ["tenders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["team_member_id"], ["team_members.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tender_id",
            "team_member_id",
            name="uq_tender_likes_tender_member",
        ),
    )
    op.create_index("ix_tender_likes_tender_id", "tender_likes", ["tender_id"])
    op.create_index(
        "ix_tender_likes_team_member_id",
        "tender_likes",
        ["team_member_id"],
    )
    op.create_index(
        "ix_tender_likes_created_at",
        "tender_likes",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tender_likes_created_at", table_name="tender_likes")
    op.drop_index("ix_tender_likes_team_member_id", table_name="tender_likes")
    op.drop_index("ix_tender_likes_tender_id", table_name="tender_likes")
    op.drop_table("tender_likes")

    op.drop_index("ix_team_members_last_used_at", table_name="team_members")
    op.drop_index("ix_team_members_member_key", table_name="team_members")
    op.drop_table("team_members")
