"""Shared internal teammate identity helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.core.models import TeamMember

MAX_TEAM_MEMBER_NAME_LENGTH = 256


def normalize_team_member_name(value: str) -> str | None:
    display_name = " ".join(value.strip().split())
    if not display_name:
        return None
    if len(display_name) > MAX_TEAM_MEMBER_NAME_LENGTH:
        raise ValueError("Name must be 256 characters or fewer.")
    return display_name


def member_key_for(display_name: str) -> str:
    return " ".join(display_name.strip().split()).casefold()


async def upsert_team_member(
    session: AsyncSession,
    display_name: str,
    *,
    touch: bool = True,
) -> TeamMember:
    normalized_name = normalize_team_member_name(display_name)
    if normalized_name is None:
        raise ValueError("Enter your name.")

    member_key = member_key_for(normalized_name)
    member = (
        await session.execute(
            select(TeamMember).where(TeamMember.member_key == member_key)
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if member is None:
        member = TeamMember(
            display_name=normalized_name,
            member_key=member_key,
            last_used_at=now,
            use_count=1,
        )
        session.add(member)
        await session.flush()
        return member

    member.display_name = normalized_name
    if touch:
        member.last_used_at = now
        member.use_count += 1
    await session.flush()
    return member


async def list_team_members(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> list[TeamMember]:
    stmt = (
        select(TeamMember)
        .order_by(TeamMember.last_used_at.desc(), TeamMember.display_name.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = [
    "MAX_TEAM_MEMBER_NAME_LENGTH",
    "list_team_members",
    "member_key_for",
    "normalize_team_member_name",
    "upsert_team_member",
]
