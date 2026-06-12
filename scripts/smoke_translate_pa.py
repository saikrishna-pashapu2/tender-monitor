# ruff: noqa: RUF001
"""Smoke-test Google's translate-pa browser endpoint.

This is intentionally not part of the pytest suite: it calls an external,
unofficial Google endpoint and needs a browser API key supplied at runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

URL = "https://translate-pa.googleapis.com/v1/translateHtml"


def _extract_translations(payload: Any) -> list[str]:
    translations: list[str] = []
    if not isinstance(payload, list):
        return translations

    candidates = payload[0] if payload and isinstance(payload[0], list) else payload
    for item in candidates:
        if isinstance(item, list) and item and isinstance(item[0], str):
            translations.append(item[0])
        elif isinstance(item, str):
            translations.append(item)
    return translations


async def _translate(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    texts: list[str],
    source_language: str,
) -> list[str]:
    request_body = [[texts, source_language, "en"], "te_lib"]
    headers = {
        "accept": "*/*",
        "content-type": "application/json+protobuf",
        "x-goog-api-key": api_key,
    }

    response = await client.post(
        URL,
        headers=headers,
        content=json.dumps(request_body, ensure_ascii=False).encode(),
    )
    print(f"source={source_language} status={response.status_code}")
    print(f"content_type={response.headers.get('content-type')}")
    print(f"raw={response.text[:1000]}")
    response.raise_for_status()
    return _extract_translations(response.json())


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key = os.environ["GOOGLE_TRANSLATE_PA_API_KEY"]
    cases = [
        (
            "ru",
            [
                "Услуги по проведению ESG аудита и оценке климатических рисков",
                "Услуги по присвоению кредитного рейтинга",
                "Поставка канцелярских товаров",
            ],
        ),
        (
            "auto",
            [
                "Услуги по проведению ESG аудита и оценке климатических рисков",
                "Кредиттік рейтинг беру бойынша қызметтер",
                "Kredit reytingini berish bo'yicha xizmatlar",
            ],
        ),
    ]

    async with httpx.AsyncClient(timeout=20.0) as client:
        for source_language, texts in cases:
            translations = await _translate(
                client,
                api_key=api_key,
                texts=texts,
                source_language=source_language,
            )
            for source, translated in zip(texts, translations, strict=False):
                print(f"{source} -> {translated}")


if __name__ == "__main__":
    asyncio.run(main())
