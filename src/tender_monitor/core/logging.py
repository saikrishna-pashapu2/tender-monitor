from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog
from structlog.stdlib import BoundLogger
from structlog.types import Processor

from tender_monitor.core.config import settings


def configure_logging() -> None:
    """Configure structlog + stdlib logging.

    JSON output in production, pretty console output otherwise. Level is read
    from settings.log_level. Safe to call more than once.
    """
    level = getattr(logging, settings.log_level, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.is_production:
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> BoundLogger:
    """Return a configured structlog logger. Call configure_logging() first."""
    logger: Any = structlog.get_logger(name) if name else structlog.get_logger()
    return cast(BoundLogger, logger)
