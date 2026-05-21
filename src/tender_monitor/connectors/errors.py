from __future__ import annotations


class ConnectorError(Exception):
    """Base for all connector errors."""


class FetchError(ConnectorError):
    """Network, HTTP status, or top-level response parsing failure.

    Total failure: caller should retry later.
    """


class ParseError(ConnectorError):
    """A single item could not be normalized.

    Per-item failure: the base class catches these and records them in
    FetchResult.partial_errors.
    """


class AuthError(FetchError):
    """Authentication or session failure. Likely needs human intervention."""


class RateLimitError(FetchError):
    """Source rate-limited us. Caller should back off."""


__all__ = [
    "AuthError",
    "ConnectorError",
    "FetchError",
    "ParseError",
    "RateLimitError",
]
