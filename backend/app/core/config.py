"""Application settings, loaded from the environment / .env."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    # ---- app ----
    app_name: str = "no-nonsense-finance"
    debug: bool = False

    # ---- plaid ----
    plaid_client_id: str = ""
    plaid_secret: str = ""
    plaid_env: Literal["sandbox", "production"] = "sandbox"
    allow_production: bool = False
    plaid_products: str = "transactions"
    plaid_country_codes: str = "US"
    plaid_redirect_uri: str = ""
    plaid_webhook_url: str = ""

    # ---- database ----
    postgres_host: str = "nnf-postgres"
    postgres_port: int = 5432
    postgres_db: str = "nnf"
    postgres_user: str = "nnf"
    postgres_password: str = ""

    # ---- crypto ----
    token_encryption_key: str = ""
    jwt_signing_key: str = ""
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    # ---- ai ----
    ai_provider: Literal["deck", "custom", "none"] = "deck"
    ai_deck_url: str = "http://192.168.86.196:11434"
    ai_model_vision: str = "qwen2.5vl:7b"
    ai_model_text: str = "qwen2.5vl:7b"
    ai_confidence_threshold: float = 0.85
    wol_api_url: str = ""
    wol_api_token: str = ""
    ai_wake_timeout_seconds: int = 90
    ai_idle_shutdown_minutes: int = 30

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def plaid_host(self) -> str:
        return {
            "sandbox": "https://sandbox.plaid.com",
            "production": "https://production.plaid.com",
        }[self.plaid_env]

    @field_validator("plaid_env")
    @classmethod
    def _guard_production(cls, v: str, info) -> str:  # type: ignore[no-untyped-def]
        # Production is opt-in and loud. A stray PLAID_ENV in the wrong .env
        # must not quietly start spending connection credits.
        if v == "production" and not info.data.get("allow_production", False):
            raise ValueError(
                "PLAID_ENV=production requires ALLOW_PRODUCTION=true. "
                "See docs/04-plaid-integration.md before enabling."
            )
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
