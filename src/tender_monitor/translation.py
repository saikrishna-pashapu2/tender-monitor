"""Title translation boundary for scheduler ingest.

The production client uses Google's browser translate-pa endpoint. That
endpoint is intentionally hidden behind this module so the rest of the
pipeline depends only on the small ``TitleTranslator`` protocol and can
swap providers later without touching connectors or matching.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, SecretStr

from tender_monitor.core.config import settings

GOOGLE_TRANSLATE_PA_URL = "https://translate-pa.googleapis.com/v1/translateHtml"
GOOGLE_TRANSLATE_PA_PROVIDER = "google_translate_pa"


class TranslationError(RuntimeError):
    """Raised when a translation provider cannot return usable results."""


class TitleTranslation(BaseModel):
    text: str
    detected_language: str | None = None


class TitleTranslator(Protocol):
    provider: str

    async def translate_titles(
        self, texts: Sequence[str], *, source_language: str = "auto"
    ) -> list[TitleTranslation]: ...


def _secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


class GoogleTranslatePaTranslator:
    provider = GOOGLE_TRANSLATE_PA_PROVIDER

    def __init__(
        self,
        *,
        api_key: SecretStr | str,
        client: httpx.AsyncClient | None = None,
        endpoint: str = GOOGLE_TRANSLATE_PA_URL,
        timeout: float = 20.0,
    ) -> None:
        self._api_key = _secret_value(api_key)
        self._client = client
        self._endpoint = endpoint
        self._timeout = timeout

    async def translate_titles(
        self, texts: Sequence[str], *, source_language: str = "auto"
    ) -> list[TitleTranslation]:
        clean_texts = [text for text in texts if text.strip()]
        if not clean_texts:
            return []

        request_body = [[clean_texts, source_language, "en"], "te_lib"]
        headers = {
            "accept": "*/*",
            "content-type": "application/json+protobuf",
            "x-goog-api-key": self._api_key,
        }

        try:
            if self._client is not None:
                response = await self._client.post(
                    self._endpoint,
                    headers=headers,
                    content=json.dumps(request_body, ensure_ascii=False).encode(),
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        self._endpoint,
                        headers=headers,
                        content=json.dumps(request_body, ensure_ascii=False).encode(),
                    )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TranslationError(f"translate-pa request failed: {exc}") from exc

        translations = self._parse_response(payload)
        if len(translations) != len(clean_texts):
            raise TranslationError(
                "translate-pa response count mismatch: "
                f"expected {len(clean_texts)}, got {len(translations)}"
            )
        return translations

    def _parse_response(self, payload: Any) -> list[TitleTranslation]:
        if not isinstance(payload, list) or not payload:
            raise TranslationError("translate-pa returned an unexpected response shape")

        translated_texts = _as_string_list(payload[0])
        detected_languages = _as_string_list(payload[1]) if len(payload) > 1 else []
        return [
            TitleTranslation(
                text=text,
                detected_language=detected_languages[index]
                if index < len(detected_languages)
                else None,
            )
            for index, text in enumerate(translated_texts)
        ]


def build_title_translator() -> TitleTranslator | None:
    if not settings.translation_enabled or settings.google_translate_pa_api_key is None:
        return None
    return GoogleTranslatePaTranslator(api_key=settings.google_translate_pa_api_key)


__all__ = [
    "GOOGLE_TRANSLATE_PA_PROVIDER",
    "GOOGLE_TRANSLATE_PA_URL",
    "GoogleTranslatePaTranslator",
    "TitleTranslation",
    "TitleTranslator",
    "TranslationError",
    "build_title_translator",
]
