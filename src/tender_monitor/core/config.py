from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AnyUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(
        ...,
        description="SQLAlchemy async DSN, e.g. postgresql+asyncpg://user:pass@host:5432/db",
    )
    environment: Environment = "development"
    log_level: LogLevel = "INFO"

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    email_to: str | None = Field(
        default=None,
        description="Comma-separated list of recipient email addresses.",
    )

    anthropic_api_key: SecretStr | None = None
    google_translate_pa_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Browser Google translate-pa API key. If unset, title translation is disabled."
        ),
    )
    translation_enabled: bool = True
    translation_batch_size: int = Field(default=50, ge=1, le=100)

    # Public URL of the web UI; used to build links inside notification
    # emails. Override per environment (`https://tender.internal/…`).
    app_base_url: str = "http://localhost:8000"
    usd_fx_rates: dict[str, float] = Field(
        default_factory=lambda: {
            "USD": 1.0,
            "KZT": 470.17,
            "UZS": 11970.68,
        },
        description=(
            "Mapping of local-currency code to local-units-per-1-USD. "
            'Accepts JSON in USD_FX_RATES, e.g. {"KZT":470.17,"UZS":11970.68,"USD":1}.'
        ),
    )

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        AnyUrl(value)
        if not value.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError(
                "DATABASE_URL must be a postgresql+asyncpg:// or postgresql:// DSN"
            )
        if value.startswith("postgresql://"):
            value = "postgresql+asyncpg://" + value.removeprefix("postgresql://")
        parts = urlsplit(value)
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            query.append(("ssl" if key == "sslmode" else key, item))
        value = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )
        return value

    @field_validator("usd_fx_rates", mode="before")
    @classmethod
    def _parse_usd_fx_rates(cls, value: object) -> object:
        if value is None or isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("USD_FX_RATES must decode to a JSON object")
            return parsed
        raise ValueError("USD_FX_RATES must be a mapping or JSON object string")

    @property
    def email_recipients(self) -> list[str]:
        if not self.email_to:
            return []
        return [addr.strip() for addr in self.email_to.split(",") if addr.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
