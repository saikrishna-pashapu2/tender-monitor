from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tender_monitor.api.app import app
from tender_monitor.api.deps import get_session
from tender_monitor.core.models import EmailRecipient


@pytest_asyncio.fixture(loop_scope="function", autouse=True)
async def _truncate_recipients(test_database_url: str) -> AsyncIterator[None]:
    engine = create_async_engine(test_database_url, future=True)
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "TRUNCATE email_recipients RESTART IDENTITY CASCADE"
            )
        yield
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "TRUNCATE email_recipients RESTART IDENTITY CASCADE"
            )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def _override(test_database_url: str) -> AsyncIterator[None]:
    engine = create_async_engine(test_database_url, future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.fixture
def client(_override: None) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_list_recipients_renders_empty_state(client: TestClient) -> None:
    resp = client.get("/settings/recipients")
    assert resp.status_code == 200
    assert "No recipients yet" in resp.text


def test_create_recipient_persists_row(client: TestClient) -> None:
    resp = client.post(
        "/settings/recipients",
        data={
            "email": "Foo@Example.Com",
            "name": "Foo Bar",
            "team": "esg",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/recipients"

    list_resp = client.get("/settings/recipients")
    assert "foo@example.com" in list_resp.text.lower()
    assert "Foo Bar" in list_resp.text


def test_create_recipient_rejects_invalid_email(client: TestClient) -> None:
    resp = client.post(
        "/settings/recipients",
        data={"email": "not-an-email", "team": "esg"},
    )
    assert resp.status_code == 400
    assert "valid email" in resp.text.lower()


def test_create_recipient_requires_a_team(client: TestClient) -> None:
    resp = client.post(
        "/settings/recipients",
        data={"email": "x@y.com"},
    )
    assert resp.status_code == 400
    assert "team" in resp.text.lower()


def test_create_recipient_rejects_invalid_team(client: TestClient) -> None:
    resp = client.post(
        "/settings/recipients",
        data={"email": "x@y.com", "team": "marketing"},
    )
    assert resp.status_code == 400
    assert "team" in resp.text.lower()


def test_create_recipient_rejects_duplicate(client: TestClient) -> None:
    client.post(
        "/settings/recipients",
        data={"email": "dup@test.com", "team": "esg"},
        follow_redirects=False,
    )
    resp = client.post(
        "/settings/recipients",
        data={"email": "dup@test.com", "team": "credit_rating"},
    )
    assert resp.status_code == 400
    assert "already subscribed" in resp.text.lower()


def test_create_recipient_team_both_subscribes_to_both_groups(
    client: TestClient, test_database_url: str
) -> None:
    """The 'both' team value must expand to both groups in the DB."""
    import asyncio

    client.post(
        "/settings/recipients",
        data={"email": "both@test.com", "team": "both"},
        follow_redirects=False,
    )
    recipient = asyncio.new_event_loop().run_until_complete(
        _get_recipient(test_database_url, "both@test.com")
    )
    assert set(recipient.groups) == {"esg", "credit_rating"}
    assert recipient.team == "both"


async def _get_recipient(test_database_url: str, email: str) -> EmailRecipient:
    engine = create_async_engine(test_database_url, future=True)
    try:
        async with engine.connect() as conn:
            session = AsyncSession(bind=conn, expire_on_commit=False)
            row = (
                await session.execute(
                    select(EmailRecipient).where(EmailRecipient.email == email)
                )
            ).scalar_one()
            return row
    finally:
        await engine.dispose()


def test_update_and_delete_recipient(
    client: TestClient, test_database_url: str
) -> None:
    import asyncio

    client.post(
        "/settings/recipients",
        data={"email": "edit@test.com", "team": "esg"},
        follow_redirects=False,
    )
    recipient = asyncio.new_event_loop().run_until_complete(
        _get_recipient(test_database_url, "edit@test.com")
    )

    # update: switch team to credit_rating, rename, pause
    resp = client.post(
        f"/settings/recipients/{recipient.id}/update",
        data={
            "name": "Edited Name",
            "team": "credit_rating",
            # no `enabled` field -> paused
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Verify the DB reflects the change.
    updated = asyncio.new_event_loop().run_until_complete(
        _get_recipient(test_database_url, "edit@test.com")
    )
    assert updated.team == "credit_rating"
    assert set(updated.groups) == {"credit_rating"}
    assert updated.enabled is False

    after = client.get("/settings/recipients")
    assert "Edited Name" in after.text
    assert "paused" in after.text

    # delete
    resp = client.post(
        f"/settings/recipients/{recipient.id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    final = client.get("/settings/recipients")
    assert "edit@test.com" not in final.text


def test_delete_unknown_recipient_returns_404(client: TestClient) -> None:
    from uuid import uuid4

    resp = client.post(
        f"/settings/recipients/{uuid4()}/delete", follow_redirects=False
    )
    assert resp.status_code == 404


def test_settings_link_in_navbar(client: TestClient) -> None:
    resp = client.get("/settings/recipients")
    assert resp.status_code == 200
    assert 'href="/settings/recipients"' in resp.text
