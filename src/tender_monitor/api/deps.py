"""FastAPI dependencies for the API package."""

from __future__ import annotations

from tender_monitor.core.database import get_session

__all__ = ["get_session"]
