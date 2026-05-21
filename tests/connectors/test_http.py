from __future__ import annotations

import httpx
import pytest

from tender_monitor.connectors.http import (
    DEFAULT_ACCEPT_LANGUAGE,
    DEFAULT_USER_AGENT,
    make_client,
    with_retry,
)


async def test_make_client_sets_default_headers() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with make_client(transport=transport) as client:
        response = await client.get("https://example.test/")

    assert response.status_code == 200
    assert captured, "expected the mock transport to receive a request"
    headers = captured[0].headers
    assert headers["user-agent"] == DEFAULT_USER_AGENT
    assert headers["accept-language"] == DEFAULT_ACCEPT_LANGUAGE


async def test_make_client_merges_custom_headers() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    async with make_client(
        headers={"X-Test": "yes", "User-Agent": "custom/1.0"},
        transport=transport,
    ) as client:
        await client.get("https://example.test/")

    headers = captured[0].headers
    assert headers["x-test"] == "yes"
    assert headers["user-agent"] == "custom/1.0"  # caller-supplied wins
    assert headers["accept-language"] == DEFAULT_ACCEPT_LANGUAGE


async def test_with_retry_retries_on_503() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    @with_retry(max_attempts=3, initial_wait=0.0, max_wait=0.0)
    async def fetch() -> httpx.Response:
        async with make_client(transport=transport) as client:
            response = await client.get("https://example.test/")
            response.raise_for_status()
            return response

    response = await fetch()
    assert response.status_code == 200
    assert len(calls) == 3


async def test_with_retry_does_not_retry_on_400() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": "bad request"})

    transport = httpx.MockTransport(handler)

    @with_retry(max_attempts=3, initial_wait=0.0, max_wait=0.0)
    async def fetch() -> httpx.Response:
        async with make_client(transport=transport) as client:
            response = await client.get("https://example.test/")
            response.raise_for_status()
            return response

    with pytest.raises(httpx.HTTPStatusError):
        await fetch()
    assert len(calls) == 1


async def test_with_retry_gives_up_after_max_attempts() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)

    @with_retry(max_attempts=3, initial_wait=0.0, max_wait=0.0)
    async def fetch() -> httpx.Response:
        async with make_client(transport=transport) as client:
            response = await client.get("https://example.test/")
            response.raise_for_status()
            return response

    with pytest.raises(httpx.HTTPStatusError):
        await fetch()
    assert len(calls) == 3
