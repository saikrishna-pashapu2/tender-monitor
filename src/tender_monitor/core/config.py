from __future__ import annotations

from functools import lru_cache
from typing import Literal

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

    # Public URL of the web UI; used to build links inside notification
    # emails. Override per environment (`https://tender.internal/…`).
    app_base_url: str = "http://localhost:8000"

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        AnyUrl(value)
        if not value.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError(
                "DATABASE_URL must be a postgresql+asyncpg:// or postgresql:// DSN"
            )
        return value

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
