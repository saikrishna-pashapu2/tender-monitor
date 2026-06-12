"""Keyword matching: config models, loader, and pure match functions.

Matches are produced as JSONB-shaped dicts so the scheduler (Prompt 6)
can write them straight onto `tenders.matched_groups` and
`tenders.match_details` without further translation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from tender_monitor.core.schemas import TenderUpsert


class KeywordGroup(BaseModel):
    """One named filter (e.g. 'esg', 'credit_rating')."""

    model_config = ConfigDict(extra="forbid")

    phrases: list[str] = Field(default_factory=list)
    tokens: list[str] = Field(default_factory=list)
    exclude_if_contains: list[str] = Field(default_factory=list)

    _token_patterns: list[tuple[str, re.Pattern[str]]] = PrivateAttr(
        default_factory=list
    )

    @field_validator("phrases", "exclude_if_contains", mode="after")
    @classmethod
    def _strip_non_empty(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for entry in value:
            stripped = entry.strip()
            if not stripped:
                raise ValueError(
                    "entries must be non-empty strings (got empty/whitespace-only)"
                )
            cleaned.append(stripped)
        return cleaned

    @field_validator("tokens", mode="after")
    @classmethod
    def _strip_token(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for entry in value:
            stripped = entry.strip()
            if not stripped:
                raise ValueError("tokens must be non-empty strings")
            if any(ch.isspace() for ch in stripped):
                raise ValueError(
                    f"token {stripped!r} contains whitespace; multi-word "
                    "entries belong in 'phrases' so substring matching "
                    "is used instead of word-boundary matching"
                )
            cleaned.append(stripped)
        return cleaned

    @model_validator(mode="after")
    def _compile_tokens(self) -> Self:
        # Compile each token to its own \b<token>\b pattern so we can
        # report which specific tokens fired without re-matching.
        self._token_patterns = [
            (token, re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE))
            for token in self.tokens
        ]
        return self

    @property
    def token_patterns(self) -> list[tuple[str, re.Pattern[str]]]:
        return self._token_patterns


class KeywordsConfig(BaseModel):
    """Container for the full keywords.yaml shape."""

    model_config = ConfigDict(extra="forbid")

    groups: dict[str, KeywordGroup] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> KeywordsConfig:
        """Load and validate a YAML keywords file.

        Raises pydantic.ValidationError if the file is malformed or
        violates any of the per-field rules.
        """
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        return cls.model_validate(data)


class GroupMatch(BaseModel):
    """A single group's matches, useful for downstream consumers that
    want a typed list rather than the JSONB-shaped dict."""

    group: str
    matched_phrases: list[str]
    matched_tokens: list[str]


class MatchResult(BaseModel):
    """Result of running the matcher on a piece of text or a tender.

    `match_details` is shaped to drop straight into the JSONB
    `tenders.match_details` column; the keys mirror the YAML's
    sub-fields (matched_phrases, matched_tokens) so format-tweaking
    later is local to this file plus the YAML.
    """

    matched_groups: list[str] = Field(default_factory=list)
    match_details: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    @property
    def is_match(self) -> bool:
        return bool(self.matched_groups)


def _excluded(text_lower: str, exclude_if_contains: list[str]) -> bool:
    return any(excl.lower() in text_lower for excl in exclude_if_contains)


def match_text(text: str, config: KeywordsConfig) -> MatchResult:
    """Run every group in the config against `text` and return a MatchResult.

    Pure function: no I/O, no caching, no mutable global state. Each
    group is evaluated independently; a tender can match zero, one, or
    several. Exclusions short-circuit: if any exclude substring is
    present, that group is skipped entirely.
    """
    if not text:
        return MatchResult()

    text_lower = text.lower()
    matched_groups: list[str] = []
    match_details: dict[str, dict[str, list[str]]] = {}

    for group_name, group in config.groups.items():
        if _excluded(text_lower, group.exclude_if_contains):
            continue

        matched_phrases = [
            phrase for phrase in group.phrases if phrase.lower() in text_lower
        ]
        matched_tokens = [
            token for token, pattern in group.token_patterns if pattern.search(text)
        ]

        if not matched_phrases and not matched_tokens:
            continue

        matched_groups.append(group_name)
        match_details[group_name] = {
            "matched_phrases": matched_phrases,
            "matched_tokens": matched_tokens,
        }

    return MatchResult(matched_groups=matched_groups, match_details=match_details)


def _searchable_text(tender: TenderUpsert) -> str:
    """Build the haystack we run the matcher against.

    Strategy: title + buyer_name + recursive walk over every string
    value inside raw_json. That makes nested detail payloads from
    connectors like UZEX searchable without maintaining a source-
    specific allowlist here.
    """
    parts: list[str] = [tender.title]
    if tender.title_en:
        parts.append(tender.title_en)
    if tender.buyer_name:
        parts.append(tender.buyer_name)

    raw_json = tender.raw_json
    if not isinstance(raw_json, dict):
        return " ".join(parts)

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
            return
        if isinstance(value, dict):
            for nested in value.values():
                _walk(nested)
            return
        if isinstance(value, list):
            for nested in value:
                _walk(nested)

    _walk(raw_json)

    return " ".join(parts)


def match_tender(tender: TenderUpsert, config: KeywordsConfig) -> MatchResult:
    """Build the searchable text from a TenderUpsert and run match_text."""
    return match_text(_searchable_text(tender), config)


__all__ = [
    "GroupMatch",
    "KeywordGroup",
    "KeywordsConfig",
    "MatchResult",
    "match_tender",
    "match_text",
]
