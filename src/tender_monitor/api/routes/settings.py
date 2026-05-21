"""Settings UI — manage who gets the matched-tender notification emails."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tender_monitor.api.deps import get_session
from tender_monitor.api.queries import overall_counters
from tender_monitor.api.templating import templates
from tender_monitor.core.models import EmailRecipient

router = APIRouter(prefix="/settings")

# Hard-coded for v1 — these are the two groups defined in
# config/keywords.yaml. If a third group lands later, surface it from
# the loaded KeywordsConfig instead of duplicating the list here.
AVAILABLE_GROUPS = ("esg", "credit_rating")

# Team is the front-end's single source of truth — picking a team
# determines both the displayed label and which keyword groups the
# recipient gets emailed about. The dropdown values are stored in
# ``email_recipients.team`` verbatim and used by ``_groups_from_team``
# to derive ``email_recipients.groups`` at save time.
TEAM_OPTIONS: tuple[tuple[str, str], ...] = (
    ("esg", "ESG"),
    ("credit_rating", "Credit Rating"),
    ("both", "Both (ESG + Credit Rating)"),
)
_VALID_TEAMS = {value for value, _ in TEAM_OPTIONS}


def _groups_from_team(team: str) -> list[str]:
    """Map a team choice to the canonical keyword-group list."""
    if team == "both":
        return ["esg", "credit_rating"]
    if team in AVAILABLE_GROUPS:
        return [team]
    return []


def _team_from_groups(groups: list[str] | None) -> str:
    """Inverse: derive the team dropdown value from stored groups.

    Used when rendering the edit row so the dropdown reflects the
    current subscription state. An empty/unknown set falls back to
    ``"esg"`` so the dropdown always shows something selected.
    """
    valid = {g for g in (groups or []) if g in AVAILABLE_GROUPS}
    if {"esg", "credit_rating"}.issubset(valid):
        return "both"
    if "credit_rating" in valid:
        return "credit_rating"
    if "esg" in valid:
        return "esg"
    return "esg"


# Backwards-compat alias used by tests written against the older API.
def _normalise_groups(values: list[str]) -> list[str]:
    valid = {g for g in values if g in AVAILABLE_GROUPS}
    return [g for g in AVAILABLE_GROUPS if g in valid]


async def _all_recipients(session: AsyncSession) -> list[EmailRecipient]:
    stmt = select(EmailRecipient).order_by(
        EmailRecipient.team.asc().nulls_last(),
        EmailRecipient.email.asc(),
    )
    return list((await session.execute(stmt)).scalars().all())


@router.get("/recipients", response_class=HTMLResponse)
async def list_recipients(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    recipients = await _all_recipients(session)
    total_tenders, total_sources, last_seen = await overall_counters(session)
    return templates.TemplateResponse(
        request,
        "settings/recipients.html",
        {
            "recipients": [
                (r, _team_from_groups(list(r.groups or []))) for r in recipients
            ],
            "team_options": TEAM_OPTIONS,
            "available_groups": AVAILABLE_GROUPS,
            "total_tenders": total_tenders,
            "total_sources": total_sources,
            "last_seen": last_seen,
            "form_error": None,
            "form_values": {},
        },
    )


@router.post("/recipients")
async def create_recipient(
    request: Request,
    session: AsyncSession = Depends(get_session),
    email: str = Form(...),
    name: str = Form(""),
    team: str = Form(""),
) -> Response:
    email_clean = email.strip().lower()
    if not email_clean or "@" not in email_clean:
        return await _render_with_error(
            request, session, "Enter a valid email address.",
            {"email": email, "name": name, "team": team},
        )
    team_value = team.strip().lower()
    if team_value not in _VALID_TEAMS:
        return await _render_with_error(
            request, session, "Pick a team (ESG, Credit Rating, or Both).",
            {"email": email, "name": name, "team": team},
        )

    recipient = EmailRecipient(
        email=email_clean,
        name=name.strip() or None,
        team=team_value,
        groups=_groups_from_team(team_value),
        enabled=True,
    )
    session.add(recipient)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _render_with_error(
            request, session, f"{email_clean!r} is already subscribed.",
            {"email": email, "name": name, "team": team},
        )
    return RedirectResponse(url="/settings/recipients", status_code=303)


@router.post("/recipients/{recipient_id}/update", response_class=HTMLResponse)
async def update_recipient(
    recipient_id: UUID,
    session: AsyncSession = Depends(get_session),
    name: str = Form(""),
    team: str = Form(""),
    enabled: str = Form(""),
) -> RedirectResponse:
    recipient = (
        await session.execute(
            select(EmailRecipient).where(EmailRecipient.id == recipient_id)
        )
    ).scalar_one_or_none()
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found")

    recipient.name = name.strip() or None
    team_value = team.strip().lower()
    if team_value in _VALID_TEAMS:
        recipient.team = team_value
        recipient.groups = _groups_from_team(team_value)
    recipient.enabled = enabled.lower() in ("on", "true", "1", "yes")
    await session.commit()
    return RedirectResponse(url="/settings/recipients", status_code=303)


@router.post("/recipients/{recipient_id}/delete", response_class=HTMLResponse)
async def delete_recipient(
    recipient_id: UUID, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    recipient = (
        await session.execute(
            select(EmailRecipient).where(EmailRecipient.id == recipient_id)
        )
    ).scalar_one_or_none()
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found")
    await session.delete(recipient)
    await session.commit()
    return RedirectResponse(url="/settings/recipients", status_code=303)


async def _render_with_error(
    request: Request,
    session: AsyncSession,
    message: str,
    form_values: dict[str, object],
) -> HTMLResponse:
    recipients = await _all_recipients(session)
    total_tenders, total_sources, last_seen = await overall_counters(session)
    return templates.TemplateResponse(
        request,
        "settings/recipients.html",
        {
            "recipients": [
                (r, _team_from_groups(list(r.groups or []))) for r in recipients
            ],
            "team_options": TEAM_OPTIONS,
            "available_groups": AVAILABLE_GROUPS,
            "total_tenders": total_tenders,
            "total_sources": total_sources,
            "last_seen": last_seen,
            "form_error": message,
            "form_values": form_values,
        },
        status_code=400,
    )


__all__ = ["router"]
