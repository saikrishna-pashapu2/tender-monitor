"""Public API for the keyword matcher."""

from __future__ import annotations

from tender_monitor.matching.keywords import (
    GroupMatch,
    KeywordGroup,
    KeywordsConfig,
    MatchResult,
    match_tender,
    match_text,
)


def load_keywords(path: str) -> KeywordsConfig:
    """Convenience wrapper around ``KeywordsConfig.load``."""
    return KeywordsConfig.load(path)


__all__ = [
    "GroupMatch",
    "KeywordGroup",
    "KeywordsConfig",
    "MatchResult",
    "load_keywords",
    "match_tender",
    "match_text",
]
