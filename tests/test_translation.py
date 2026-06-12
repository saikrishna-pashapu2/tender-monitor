from __future__ import annotations

import json

import httpx
import pytest

from tender_monitor.translation import (
    GOOGLE_TRANSLATE_PA_URL,
    GoogleTranslatePaTranslator,
    TranslationError,
)


async def test_google_translate_pa_translates_batch_and_detects_languages() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                ["ESG audit services", "Credit rating services"],
                ["ru", "uz"],
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = GoogleTranslatePaTranslator(api_key="test-key", client=client)
        result = await translator.translate_titles(
            ["source title one", "source title two"], source_language="auto"
        )

    assert [item.text for item in result] == [
        "ESG audit services",
        "Credit rating services",
    ]
    assert [item.detected_language for item in result] == ["ru", "uz"]
    assert len(requests) == 1
    assert str(requests[0].url) == GOOGLE_TRANSLATE_PA_URL
    assert requests[0].headers["x-goog-api-key"] == "test-key"
    assert requests[0].headers["content-type"] == "application/json+protobuf"
    assert json.loads(requests[0].content.decode()) == [
        [["source title one", "source title two"], "auto", "en"],
        "te_lib",
    ]


async def test_google_translate_pa_raises_on_count_mismatch() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[["only one translation"]])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = GoogleTranslatePaTranslator(api_key="test-key", client=client)
        with pytest.raises(TranslationError, match="count mismatch"):
            await translator.translate_titles(["one", "two"], source_language="auto")
