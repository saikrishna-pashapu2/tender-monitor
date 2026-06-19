from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.core.models import TeamMember
from tender_monitor.team import (
    list_team_members,
    member_key_for,
    normalize_team_member_name,
    upsert_team_member,
)


def test_normalize_team_member_name() -> None:
    assert normalize_team_member_name(" Sai   Kumar ") == "Sai Kumar"
    assert normalize_team_member_name("   ") is None
    assert member_key_for(" Sai   Kumar ") == "sai kumar"


def test_normalize_team_member_name_rejects_long_value() -> None:
    with pytest.raises(ValueError):
        normalize_team_member_name("x" * 257)


async def test_upsert_team_member_reuses_normalized_key(
    db_session: AsyncSession,
) -> None:
    first = await upsert_team_member(db_session, "Sai Kumar")
    second = await upsert_team_member(db_session, " sai   kumar ")

    assert second.id == first.id
    assert second.display_name == "sai kumar"
    assert second.member_key == "sai kumar"
    assert second.use_count == 2

    rows = (await db_session.execute(select(TeamMember))).scalars().all()
    assert rows == [second]


async def test_list_team_members_orders_recent_first(
    db_session: AsyncSession,
) -> None:
    first = await upsert_team_member(db_session, "First Person")
    second = await upsert_team_member(db_session, "Second Person")
    first.last_used_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    second.last_used_at = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    await db_session.flush()

    rows = await list_team_members(db_session)

    assert rows[0].id == second.id
    assert rows[1].id == first.id
