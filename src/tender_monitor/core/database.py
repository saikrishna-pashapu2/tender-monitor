from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from tender_monitor.core.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models in this project."""


def _get_engine_url_and_connect_args(database_url: str) -> tuple[URL, dict[str, Any]]:
    parsed = urlsplit(database_url)
    auth, _, _host_port = parsed.netloc.rpartition("@")
    username: str | None = None
    password: str | None = None
    if auth:
        raw_username, separator, raw_password = auth.partition(":")
        username = unquote(raw_username) if raw_username else None
        password = unquote(raw_password) if separator else None

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    sslmode = str(query.pop("sslmode", "")).lower()
    ssl_value = str(query.pop("ssl", "")).lower()
    connect_args: dict[str, Any] = {}

    # PostgreSQL sslmode=require means "encrypt, do not verify hostname/CA".
    # asyncpg needs that expressed as an explicit SSLContext, not as a URL query.
    if sslmode in {"allow", "prefer", "require"} or ssl_value in {
        "1",
        "true",
        "require",
    }:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ssl_context
    elif sslmode in {"verify-ca", "verify-full"} or ssl_value in {
        "verify-ca",
        "verify-full",
    }:
        connect_args["ssl"] = ssl.create_default_context()
    elif sslmode == "disable" or ssl_value in {"0", "false", "disable"}:
        connect_args["ssl"] = False

    return (
        URL.create(
            drivername=parsed.scheme,
            username=username,
            password=password,
            host=parsed.hostname,
            port=parsed.port,
            database=parsed.path.lstrip("/") or None,
            query=query,
        ),
        connect_args,
    )


engine_url, engine_connect_args = _get_engine_url_and_connect_args(
    settings.database_url,
)

engine: AsyncEngine = create_async_engine(
    engine_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=engine_connect_args,
    future=True,
)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-friendly async session dependency."""
    async with async_session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Dispose the global engine. Call from process shutdown hooks."""
    await engine.dispose()
