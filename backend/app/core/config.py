"""Application settings, loaded from the environment / .env."""
from __future__ import annotations

import logging

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    # ---- app ----
    app_name: str = "dollarden"
    debug: bool = False

    # ---- plaid ----
    plaid_client_id: str = ""
    # Kept as the fallback for both environments, because that is what
    # every instance set up before accounts had their own environment
    # already has in its .env. An environment-specific secret below
    # wins when set; otherwise this one is used, and nothing breaks on
    # upgrade.
    plaid_secret: str = ""
    # Plaid issues ONE client id and a SEPARATE secret per environment,
    # which is why there is one of the first and two of the second.
    plaid_secret_sandbox: str = ""
    plaid_secret_production: str = ""
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

    # Pull from Plaid on a schedule, not only when a webhook arrives.
    #
    # Webhooks need a public HTTPS address, which a plain-HTTP install
    # on a home network does not have -- so without this it would only
    # ever update when someone pulled down on the screen.
    #
    # It earns its place even WITH webhooks working: webhooks get
    # missed. The server is down for an update, a request is dropped, a
    # reverse-proxy path changes and starts 404ing. The Item stays
    # healthy and transactions simply stop arriving, which is the
    # hardest kind of failure to notice.
    plaid_sync_enabled: bool = True
    plaid_sync_interval_minutes: int = 60

    # ---- database ----
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "dollarden"
    postgres_user: str = "dollarden"
    postgres_password: str = ""

    # Reverse proxies whose X-Forwarded-For we trust. Caddy's IP on the wg
    # bridge. Anything else could spoof the header and dodge rate limits.
    # Which proxy addresses may set X-Forwarded-For, for rate limiting.
    #
    # Loopback only by default. The old default was one particular
    # Docker bridge address, which on anyone else's machine is either
    # meaningless or, worse, a subnet they did not intend to trust.
    # Set it to your reverse proxy's address on the container network.
    trusted_proxies: str = "127.0.0.1"

    # ---- crypto ----
    token_encryption_key: str = ""
    jwt_signing_key: str = ""
    # Anyone who can reach the server being able to create an account.
    #
    # Off. A household instance has no reason to accept strangers, and
    # on a public URL an open endpoint means someone else creating an
    # account and spending the owner's finite Plaid connections.
    #
    # Two ways through remain: the very first account on an empty
    # instance (which becomes the owner), and an owner creating one.
    # The test suite turns this on, because it makes users constantly.
    allow_open_registration: bool = False
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    # ---- ai ----
    # "managed" is an Ollama host this server may wake and, if a relay
    # is wired up, power on. "custom" is one that is simply expected to
    # be there. "deck" is the old name for "managed" -- one particular
    # machine in one particular house -- and is still accepted so an
    # un-updated .env keeps working.
    ai_provider: Literal["managed", "custom", "openai", "none", "deck"] = "none"
    # A hosted model, reached over an OpenAI-shaped API.
    #
    # One implementation covers most of them, because most speak that
    # shape: OpenAI itself, Gemini through its compatibility endpoint,
    # anything on OpenRouter (Claude included), and local servers like
    # vLLM or LM Studio. Anthropic's own API is NOT OpenAI-shaped;
    # Claude direct would be a second provider.
    #
    # The trade is worth stating where people will read it: merchant
    # names, amounts and the goals someone writes are sent to whoever
    # runs that endpoint. Self-hosting is the default for that reason,
    # not because this is second class.
    ai_api_base_url: str = ""
    ai_api_key: str = ""
    # Where the model runs. "deck" was one particular Steam Deck in one
    # particular house; this is just the inference host.
    #
    # AliasChoices keeps the old environment name working. Renaming a
    # setting without it is the quietest kind of breakage: pydantic
    # does not error on an unknown variable, it uses the default, so
    # AI_DECK_URL in an un-updated .env would simply be ignored and the
    # server would start talking to localhost with nothing in the log.
    ai_host_url: str = Field(
        default="http://127.0.0.1:11434",
        validation_alias=AliasChoices("AI_HOST_URL", "AI_DECK_URL"),
    )
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
    # Whether to hold AI work overnight at all. Off means run whenever.
    ai_quiet_enabled: bool = True
    # UTC rather than a real place, because the default has to be wrong
    # for everyone equally rather than right for one house. Set it to
    # where the AI machine physically is.
    ai_quiet_timezone: str = "UTC"
    ai_quiet_start_hour: int = 0
    ai_quiet_end_hour: int = 8
    # How long to wait before re-trying a deferred job.
    ai_defer_minutes: int = 60
    # After this many deferrals the job runs regardless. A stuttered
    # frame costs less than a transaction that is never categorised.
    ai_max_defer_attempts: int = 24

    wol_api_url: str = ""
    wol_api_token: str = ""
    ai_wake_timeout_seconds: int = 90
    # How long one inference request may take before it is called a
    # failure. This is the only bound on a host that has stopped
    # answering, and the job queue is serial, so it is also the per-job
    # cost of a wedged machine: at the previous hardcoded 300s a stalled
    # deck turned every queued transaction into a six-minute stall.
    # Still generous -- a cold model load on the deck is ~40s and a long
    # Ask answer well under a minute.
    ai_request_timeout_seconds: int = 120
    # After a request stalls (host on, nothing came back), how long to
    # stop sending work. An unkillable model process does not recover on
    # its own, so marching the rest of the queue into it just multiplies
    # the wait.
    ai_stall_backoff_seconds: int = 900
    # How many times a job may be held for a silent host before it gives
    # up and falls back. Four holds is about an hour; past that the
    # machine is not coming back on its own, and a transaction stuck on
    # "waiting for the AI" forever is worse than one in the review queue,
    # which is where it belongs and what the queue is for.
    ai_stall_max_holds: int = 4
    # Relay across the deck's power switch, driven by a GPIO on the Pi.
    # The fallback for when Wake-on-LAN cannot help: a machine at soft
    # off with WoL disabled, after a power cut, or frozen hard.
    power_relay_url: str = Field(
        default="",
        validation_alias=AliasChoices("POWER_RELAY_URL", "DECK_POWER_URL"),
    )
    power_relay_token: str = Field(
        default="",
        validation_alias=AliasChoices("POWER_RELAY_TOKEN", "DECK_POWER_TOKEN"),
    )
    # How long to wait for a cold boot after the button is pressed.
    # Much longer than a wake: this is POST plus a full SteamOS start,
    # not a resume.
    power_relay_boot_seconds: int = Field(
        default=150,
        validation_alias=AliasChoices(
            "POWER_RELAY_BOOT_SECONDS", "DECK_POWER_BOOT_SECONDS"),
    )
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
    def _normalise_provider(self) -> "Settings":
        """Fold the old provider name into the new one, once, here.

        Doing it at the edge means nothing downstream has to know the
        old name existed -- no `in ("deck", "managed")` checks spreading
        through the code, which is how a rename ends up half-done
        forever.
        """
        if self.ai_provider == "deck":
            object.__setattr__(self, "ai_provider", "managed")
        return self

    def secret_for(self, environment: str) -> str:
        """The secret for one environment, falling back to the shared one.

        An instance configured before accounts had environments has a
        single `PLAID_SECRET` and nothing else; that must keep working,
        so it is the fallback rather than an error.
        """
        specific = (
            self.plaid_secret_sandbox if environment == "sandbox"
            else self.plaid_secret_production
        )
        return specific or self.plaid_secret

    def host_for(self, environment: str) -> str:
        """Plaid's base URL for one environment."""
        return (
            "https://sandbox.plaid.com" if environment == "sandbox"
            else "https://production.plaid.com"
        )

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


log = logging.getLogger("dollarden.config")



@lru_cache
def get_settings() -> Settings:
    """The environment, with any owner-set overrides laid on top.

    Order matters and is deliberate: `.env` is the baseline, the
    database wins. That is what lets an existing install keep behaving
    exactly as it did until somebody actually changes something in the
    portal, and what lets a new install be configured through a browser
    without ever opening a shell.

    Every failure path here falls back to the environment rather than
    raising. This function is called during startup, before migrations
    have necessarily run, and by the admin portal itself -- so a
    database that is not ready, a table that does not exist yet, or one
    bad stored value must never be able to stop the server. The way out
    of that would be the portal it just prevented from loading.
    """
    base = Settings()

    # Nothing in here may call get_settings() again. Decrypting the
    # stored secrets used to, via decrypt_token, and the consequences
    # were worse than a loop: the nested call opened its own database
    # session each time until the pool was exhausted and the process
    # hung. Guarding the recursion only moved the damage -- lru_cache
    # resolves a reentrant miss by keeping the INNER result and
    # discarding the outer one, so the cache ended up holding settings
    # with every stored value missing, silently, while the caller that
    # triggered the load got the right answer. The encryption key is
    # therefore passed down rather than looked up.
    try:
        from app.core.instance_settings import load_overrides
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            # The key goes IN. Letting the loader fetch it would
            # re-enter this function; see _fernet in core/security.py.
            overrides = load_overrides(db, base.token_encryption_key)
    except Exception:  # noqa: BLE001
        # Normal on a fresh container: the table arrives with the
        # migration. Debug rather than warning so first boot is quiet.
        log.debug("no stored settings; using the environment", exc_info=True)
        return base

    if not overrides:
        return base

    try:
        # Rebuilt through Settings rather than patched onto it, so
        # stored values are validated exactly like environment ones.
        # A bad value raises here, where it can be ignored, instead of
        # surfacing later as something inexplicable.
        return Settings(**{**base.model_dump(), **overrides})
    except Exception:  # noqa: BLE001
        log.warning(
            "stored settings are invalid and were ignored; running from the "
            "environment. Offending keys: %s", sorted(overrides), exc_info=True)
        return base


def refresh_settings() -> None:
    """Forget the cached settings, so the next read sees new values.

    Called after the portal writes. Without it a saved change appears
    to have worked and does nothing until the container restarts.
    """
    get_settings.cache_clear()
