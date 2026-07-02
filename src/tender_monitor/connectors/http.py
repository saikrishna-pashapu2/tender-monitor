from __future__ import annotations

import functools
import ssl
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en;q=0.8"

# HTTP statuses that warrant a retry. Intentionally narrow: 429 +
# transient gateway/server errors.
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 502, 503, 504})


def make_client(
    timeout: float = 30.0,
    connect_timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    verify: str | ssl.SSLContext | None = None,
) -> httpx.AsyncClient:
    """Return a configured httpx.AsyncClient.

    Connectors should call this rather than instantiating httpx directly
    so timeouts, headers, and transport overrides stay consistent. The
    `transport` parameter exists so tests can inject MockTransport.
    """
    merged_headers: dict[str, str] = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
    }
    if headers:
        merged_headers.update(headers)

    kwargs: dict[str, object] = {}
    if verify is not None:
        kwargs["verify"] = verify

    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
        follow_redirects=True,
        headers=merged_headers,
        transport=transport,
        **kwargs,
    )


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.NetworkError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


P = ParamSpec("P")
R = TypeVar("R")


def with_retry(
    max_attempts: int = 3,
    initial_wait: float = 1.0,
    max_wait: float = 8.0,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorate an async function so it retries transient HTTP failures.

    Retries on httpx.TimeoutException, httpx.NetworkError, and
    httpx.HTTPStatusError where status is in {429, 502, 503, 504}.
    Backoff is exponential with jitter (~1s, 2s, 4s by default). Other
    exceptions propagate immediately so logical errors are not masked.
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            retrying = AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential_jitter(initial=initial_wait, max=max_wait),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            )
            async for attempt in retrying:
                with attempt:
                    return await func(*args, **kwargs)
            raise RuntimeError("unreachable: tenacity exited without raising")

        return wrapper

    return decorator


__all__ = [
    "DEFAULT_ACCEPT_LANGUAGE",
    "DEFAULT_USER_AGENT",
    "RETRYABLE_STATUS_CODES",
    "make_client",
    "with_retry",
]
