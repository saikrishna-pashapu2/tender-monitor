from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tender_monitor.core.enums import Country
from tender_monitor.core.schemas import TenderUpsert
from tender_monitor.matching.keywords import (
    KeywordGroup,
    KeywordsConfig,
    MatchResult,
    match_tender,
    match_text,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KEYWORDS_PATH = PROJECT_ROOT / "config" / "keywords.yaml"


@pytest.fixture(scope="module")
def real_config() -> KeywordsConfig:
    return KeywordsConfig.load(KEYWORDS_PATH)


def _make_tender(
    *,
    title: str,
    buyer_name: str | None = None,
    raw_json: dict[str, Any] | None = None,
) -> TenderUpsert:
    return TenderUpsert(
        source_name="goszakup",
        external_id="t-1",
        title=title,
        buyer_name=buyer_name,
        country=Country.KZ,
        source_url="https://example.test/t-1",
        raw_json=raw_json or {},
    )


# ---------------------------------------------------------------------------
# Config loading and validation
# ---------------------------------------------------------------------------


def test_load_valid_yaml(real_config: KeywordsConfig) -> None:
    assert set(real_config.groups.keys()) == {"credit_rating", "esg"}

    credit = real_config.groups["credit_rating"]
    esg = real_config.groups["esg"]

    assert len(credit.phrases) >= 15
    assert len(credit.tokens) >= 3
    assert len(credit.exclude_if_contains) >= 4

    assert len(esg.phrases) >= 20
    assert "ESG" in esg.tokens
    # Excludes are allowed on esg now too — broader phrases like
    # "корпоративное управление" pick up generic "корпоративная карта"
    # / "корпоративная сеть" tenders without them.


def test_load_rejects_empty_phrases() -> None:
    with pytest.raises(ValidationError):
        KeywordGroup(phrases=["valid phrase", "", "another"])

    with pytest.raises(ValidationError):
        KeywordGroup(phrases=["valid phrase", "   "])


def test_load_rejects_token_with_space() -> None:
    with pytest.raises(ValidationError) as exc_info:
        KeywordGroup(tokens=["Эксперт РА"])

    message = str(exc_info.value)
    assert "tokens" in message
    assert "phrases" in message  # error mentions the right place to put it


# ---------------------------------------------------------------------------
# match_text — phrase semantics
# ---------------------------------------------------------------------------


def test_match_text_credit_rating_positive(real_config: KeywordsConfig) -> None:
    result = match_text(
        "Оказание услуг по присвоению кредитного рейтинга", real_config
    )
    assert result.is_match
    assert "credit_rating" in result.matched_groups
    matched_phrases = result.match_details["credit_rating"]["matched_phrases"]
    assert any("кредитного рейтинга" in p.lower() for p in matched_phrases)


def test_match_text_credit_rating_excluded(real_config: KeywordsConfig) -> None:
    # Both a positive phrase and an exclusion form are present; exclusion wins.
    text = "Кредитный рейтинг банка по продукту кредитная карта"
    result = match_text(text, real_config)
    assert "credit_rating" not in result.matched_groups


# ---------------------------------------------------------------------------
# match_text — token semantics
# ---------------------------------------------------------------------------


def test_match_text_token_word_boundary_positive(
    real_config: KeywordsConfig,
) -> None:
    result = match_text("ESG audit services for the bank", real_config)
    assert "esg" in result.matched_groups
    esg_details = result.match_details["esg"]
    assert "ESG" in esg_details["matched_tokens"]


def test_match_text_token_word_boundary_negative(
    real_config: KeywordsConfig,
) -> None:
    # ESG token must be a whole word; substrings inside larger words
    # (e.g. ASSEMBLAGE has SE+...) must not fire it.
    result = match_text("ASSEMBLAGE OF MESSENGERS", real_config)
    assert "esg" not in result.matched_groups


def test_match_text_cyrillic_token_boundary(
    real_config: KeywordsConfig,
) -> None:
    # Whole-word АКРА triggers the token.
    positive = match_text("АКРА присвоила рейтинг банку", real_config)
    assert "credit_rating" in positive.matched_groups
    assert "АКРА" in positive.match_details["credit_rating"]["matched_tokens"]

    # АКРА as a substring inside a larger Cyrillic word must NOT trigger.
    # МАКРАМЕ contains the contiguous substring АКРА but is one word.
    negative = match_text("закупка изделий из МАКРАМЕ", real_config)
    assert "credit_rating" not in negative.matched_groups


# ---------------------------------------------------------------------------
# match_text — overall behavior
# ---------------------------------------------------------------------------


def test_match_text_no_match(real_config: KeywordsConfig) -> None:
    result = match_text(
        "Поставка офисной бумаги формата A4", real_config
    )
    assert result.matched_groups == []
    assert result.match_details == {}
    assert result.is_match is False


def test_match_text_multiple_groups(real_config: KeywordsConfig) -> None:
    result = match_text(
        "ESG strategy review and присвоение кредитного рейтинга", real_config
    )
    assert set(result.matched_groups) == {"esg", "credit_rating"}


# ---------------------------------------------------------------------------
# match_tender — searchable text construction
# ---------------------------------------------------------------------------


def test_match_tender_pulls_from_lots(real_config: KeywordsConfig) -> None:
    tender = _make_tender(
        title="Закупка канцтоваров",
        raw_json={
            "_lots": [
                {
                    "id": 1,
                    "name_ru": None,
                    "description_ru": "присвоение кредитного рейтинга компании",
                    "description_kk": None,
                }
            ]
        },
    )
    result = match_tender(tender, real_config)
    assert "credit_rating" in result.matched_groups


def test_match_tender_uses_buyer_name(real_config: KeywordsConfig) -> None:
    tender = _make_tender(
        title="Услуги по обслуживанию",  # benign title
        buyer_name="АКРА (рейтинговое агентство)",
    )
    result = match_tender(tender, real_config)
    assert "credit_rating" in result.matched_groups


def test_match_tender_walks_nested_detail_payload(
    real_config: KeywordsConfig,
) -> None:
    tender = _make_tender(
        title="Закупка консалтинговых услуг",
        raw_json={
            "_detail": {
                "technical_description": (
                    "Iqlim strategiyasini ishlab chiqish hamda "
                    "dekarbonizatsiya rejasini tayyorlash"
                ),
                "js_fields": [
                    {
                        "label": "Mutaxassislar haqida ma'lumot",
                        "description": (
                            "Loyiha menejeri CFA ESG Investing "
                            "sertifikatiga ega bo‘lishi lozim."
                        ),
                    }
                ],
            }
        },
    )
    result = match_tender(tender, real_config)
    assert "esg" in result.matched_groups


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------


def test_match_result_serializes_to_json() -> None:
    result = MatchResult(
        matched_groups=["esg"],
        match_details={
            "esg": {
                "matched_phrases": ["ESG audit"],
                "matched_tokens": ["ESG"],
            }
        },
    )
    dumped = result.model_dump()
    assert dumped == {
        "matched_groups": ["esg"],
        "match_details": {
            "esg": {
                "matched_phrases": ["ESG audit"],
                "matched_tokens": ["ESG"],
            }
        },
    }


# ---------------------------------------------------------------------------
# Regression guard against false matches on real goszakup data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "приобретение хозяйственных товаров",
        (
            "Работы по ремонту/реконструкции электрического, "
            "электрораспределительного/регулирующего оборудования и "
            "аналогичной аппаратуры"
        ),
        "Приобретение хозяйственные перчатки",
    ],
)
def test_real_goszakup_titles_do_not_falsematch(
    title: str, real_config: KeywordsConfig
) -> None:
    result = match_text(title, real_config)
    assert result.is_match is False, (
        f"unexpected match on title {title!r}: {result.matched_groups}"
    )
