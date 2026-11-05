"""Application settings, loaded from the environment / .env."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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
    # Android OAuth uses the app's applicationId instead of a redirect URI,
    # and the two are mutually exclusive. See PlaidClient.create_link_token.
    plaid_android_package_name: str = ""
    plaid_webhook_url: str = ""
    # Shortest gap between pull-to-refresh actually reaching the bank.
    # /accounts/balance/get hits the institution, and a user can pull as
    # fast as they like.
    plaid_refresh_min_seconds: int = 20
    # How often a pull may ask PLAID to go re-read the bank, as opposed
    # to asking Plaid what it already holds. That call reaches the
    # institution and is rate limited, so it gets a much longer leash
    # than the ordinary sync.
    plaid_force_refresh_min_seconds: int = 180
    # How long a pull waits for Plaid's fetch to land before syncing.
    # Long enough for a quick bank, short enough not to feel stuck.
    plaid_force_refresh_wait_seconds: float = 2.5

    # ---- database ----
    postgres_host: str = "nnf-postgres"
    postgres_port: int = 5432
    postgres_db: str = "nnf"
    postgres_user: str = "nnf"
    postgres_password: str = ""

    # Reverse proxies whose X-Forwarded-For we trust. Caddy's IP on the wg
    # bridge. Anything else could spoof the header and dodge rate limits.
    trusted_proxies: str = "10.42.42.43,127.0.0.1"

    # ---- crypto ----
    token_encryption_key: str = ""
    jwt_signing_key: str = ""
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    # ---- ai ----
    ai_provider: Literal["deck", "custom", "none"] = "deck"
    ai_deck_url: str = "http://192.168.86.196:11434"
    ai_model_vision: str = "gemma4:12b"
    ai_model_text: str = "gemma4:12b"
    ai_confidence_threshold: float = 0.85
    # --- GPU headroom gate (deploy/deck/gpu-probe.py) -------------- #
    # The inference host is also a gaming PC. Jobs wait rather than
    # fight a game for VRAM. See app/ai/gpu.py.
    gpu_probe_url: str = ""
    gpu_probe_token: str = ""
    # gemma4:12b measured at 9,815 MB resident on a 16,304 MB card.
    # 10,500 leaves a little headroom without being so strict that an
    # idle desktop looks busy.
    ai_min_vram_mb: int = 10_500
    ai_max_gpu_busy_percent: int = 60
    # Nothing runs overnight: the machine is in a bedroom-adjacent room
    # and waking it means fans. PINNED to the PC's own zone -- the Pi
    # runs Europe/London and the PC sits in Denver.
    ai_quiet_timezone: str = "America/Denver"
    ai_quiet_start_hour: int = 1
    ai_quiet_end_hour: int = 8
    # How long to wait before re-trying a deferred job.
    ai_defer_minutes: int = 60
    # After this many deferrals the job runs regardless. A stuttered
    # frame costs less than a transaction that is never categorised.
    ai_max_defer_attempts: int = 24

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

    @model_validator(mode="after")
    def _guard_production(self) -> "Settings":
        # Production is opt-in and loud. A stray PLAID_ENV in the wrong .env
        # must not quietly start spending connection credits.
        #
        # This was a field_validator on plaid_env reading
        # info.data["allow_production"]. Pydantic v2 validates in
        # declaration order and info.data holds only fields validated SO
        # FAR -- allow_production is declared after plaid_env, so the flag
        # was always absent and production was unreachable even when
        # correctly configured. A model_validator sees every field and does
        # not care about declaration order.
        if self.plaid_env == "production" and not self.allow_production:
            raise ValueError(
                "PLAID_ENV=production requires ALLOW_PRODUCTION=true. "
                "See docs/04-plaid-integration.md before enabling."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
