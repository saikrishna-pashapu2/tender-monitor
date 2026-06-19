"""Tender like helpers."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tender_monitor.core.models import TeamMember, Tender, TenderLike
from tender_monitor.team import member_key_for, upsert_team_member


async def like_tender(
    session: AsyncSession,
    *,
    tender_id: UUID,
    member_name: str,
) -> TenderLike | None:
    tender_exists = (
        await session.execute(select(Tender.id).where(Tender.id == tender_id))
    ).scalar_one_or_none()
    if tender_exists is None:
        return None

    member = await upsert_team_member(session, member_name)
    existing = (
        await session.execute(
            select(TenderLike)
            .options(selectinload(TenderLike.team_member))
            .where(TenderLike.tender_id == tender_id)
            .where(TenderLike.team_member_id == member.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    like = TenderLike(tender_id=tender_id, team_member_id=member.id)
    session.add(like)
    await session.flush()
    return like


async def unlike_tender(
    session: AsyncSession,
    *,
    tender_id: UUID,
    member_key: str,
) -> bool | None:
    tender_exists = (
        await session.execute(select(Tender.id).where(Tender.id == tender_id))
    ).scalar_one_or_none()
    if tender_exists is None:
        return None

    normalized_key = member_key_for(member_key)
    like = (
        await session.execute(
            select(TenderLike)
            .join(TeamMember, TeamMember.id == TenderLike.team_member_id)
            .where(TenderLike.tender_id == tender_id)
            .where(TeamMember.member_key == normalized_key)
        )
    ).scalar_one_or_none()
    if like is None:
        return False

    await session.delete(like)
    await session.flush()
    return True


async def list_tender_likes(
    session: AsyncSession,
    tender_id: UUID,
) -> list[TenderLike]:
    stmt = (
        select(TenderLike)
        .options(selectinload(TenderLike.team_member))
        .where(TenderLike.tender_id == tender_id)
        .order_by(TenderLike.created_at.desc(), TenderLike.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


__all__ = ["like_tender", "list_tender_likes", "unlike_tender"]
