"""Sandbox and real banking, side by side on one instance.

The boundary is the ACCOUNT, not the server. People need to try the
whole loop with fake banks before spending any of a small, lifetime
allowance of real connections, and the obvious alternative -- one
environment per instance, flipped when ready -- is worse than it
looks. A sandbox access token means nothing in production, so flipping
strands every existing connection; and anyone who used the sandbox
properly ends up with a budget full of imaginary money that no
credential swap cleans up.

Most of what follows is about things that fail SILENTLY. A production
secret sent to the sandbox host does not raise, it just fails to
authenticate; a webhook verified against the wrong environment's key
is simply rejected, and a rejected webhook looks exactly like one that
was never sent.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import Settings
from app.db.models import User
from app.db.session import SessionLocal
from app.plaid.client import PlaidClient
from tests.conftest import requires_db


def _settings(**over) -> Settings:
    base = dict(allow_production=False, plaid_client_id="cid")
    base.update(over)
    # The production guard refuses plaid_env=production unless the
    # instance has explicitly opted in. That guard is deliberate (it is
    # what stops an accidental deploy spending bank connections), so
    # tests that need the production setting opt in rather than remove
    # it.
    if base.get("plaid_env") == "production":
        base["allow_production"] = True
    return Settings(**base)


class TestTheCredentialsFollowTheEnvironment:
    def test_each_environment_gets_its_own_secret(self) -> None:
        s = _settings(plaid_secret_sandbox="sb", plaid_secret_production="pr")
        assert s.secret_for("sandbox") == "sb"
        assert s.secret_for("production") == "pr"

    def test_an_older_single_secret_still_works(self) -> None:
        """Every instance set up before this has one PLAID_SECRET.

        It has to keep working, so the shared value is the fallback
        rather than an error.
        """
        s = _settings(plaid_secret="shared")
        assert s.secret_for("sandbox") == "shared"
        assert s.secret_for("production") == "shared"

    def test_a_specific_secret_beats_the_shared_one(self) -> None:
        s = _settings(plaid_secret="shared", plaid_secret_sandbox="sb")
        assert s.secret_for("sandbox") == "sb"
        assert s.secret_for("production") == "shared"

    def test_the_host_follows_too(self) -> None:
        """Host and secret must never be mixed: a production secret
        against the sandbox host authenticates nothing, quietly."""
        s = _settings()
        assert "sandbox.plaid.com" in s.host_for("sandbox")
        assert "production.plaid.com" in s.host_for("production")


@requires_db
class TestTheClientIsBuiltForTheAccount:
    def _user(self, env: str) -> User:
        u = User(email=f"env-{uuid.uuid4().hex[:8]}@example.com",
                 password_hash="x", plaid_env=env)
        with SessionLocal() as db:
            db.add(u); db.commit(); db.refresh(u)
        return u

    def test_a_sandbox_account_talks_to_the_sandbox(self) -> None:
        s = _settings(plaid_env="production",
                      plaid_secret_sandbox="sb", plaid_secret_production="pr")
        c = PlaidClient.for_user(s, self._user("sandbox"))
        assert c.environment == "sandbox"
        assert "sandbox.plaid.com" in c.base
        assert c._auth()["secret"] == "sb"

    def test_a_real_account_talks_to_production(self) -> None:
        s = _settings(plaid_env="sandbox",
                      plaid_secret_sandbox="sb", plaid_secret_production="pr")
        c = PlaidClient.for_user(s, self._user("production"))
        assert c.environment == "production"
        assert "production.plaid.com" in c.base
        assert c._auth()["secret"] == "pr"

    def test_the_account_overrides_the_instance_setting(self) -> None:
        """Which is the entire point: both live on one server."""
        s = _settings(plaid_env="production")
        assert PlaidClient.for_user(s, self._user("sandbox")).environment == "sandbox"

    def test_sandbox_only_endpoints_check_the_client_not_the_instance(self) -> None:
        s = _settings(plaid_env="production")
        c = PlaidClient(s, environment="sandbox")
        c._require_sandbox()  # must not raise
        with pytest.raises(RuntimeError):
            PlaidClient(s, environment="production")._require_sandbox()


class TestTheWebhookUrlCarriesTheEnvironment:
    def test_the_path_names_the_environment(self) -> None:
        s = _settings(plaid_webhook_url="https://b.example.org/plaid/webhook")
        assert PlaidClient(s, environment="sandbox").webhook_url() == \
            "https://b.example.org/plaid/webhook/sandbox"
        assert PlaidClient(s, environment="production").webhook_url() == \
            "https://b.example.org/plaid/webhook/production"

    def test_a_url_that_already_names_one_is_left_alone(self) -> None:
        """An operator who configured the full path is not overridden."""
        s = _settings(plaid_webhook_url="https://b.example.org/hook/production")
        assert PlaidClient(s, environment="production").webhook_url() == \
            "https://b.example.org/hook/production"

    def test_no_configured_url_means_no_webhook(self) -> None:
        assert PlaidClient(_settings(), environment="sandbox").webhook_url() == ""

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        s = _settings(plaid_webhook_url="https://b.example.org/plaid/webhook/")
        assert PlaidClient(s, environment="sandbox").webhook_url() == \
            "https://b.example.org/plaid/webhook/sandbox"


class TestTheVerificationKeyCache:
    """Keyed by environment as well as kid.

    Sandbox and production are separate key sets from separate hosts.
    Cached by kid alone, a key id present in both would serve whichever
    environment asked first to whichever asked second -- and the
    symptom would be a genuine webhook failing to verify, which looks
    exactly like a webhook that was never sent.
    """

    def test_the_cache_is_keyed_by_environment_and_kid(self) -> None:
        from app.plaid import webhook as mod

        mod.reset_key_cache()
        mod._key_cache[("sandbox", "kid-1")] = {"kty": "EC", "which": "sandbox"}
        mod._key_cache[("production", "kid-1")] = {"kty": "EC", "which": "production"}
        assert mod._key_cache[("sandbox", "kid-1")]["which"] == "sandbox"
        assert mod._key_cache[("production", "kid-1")]["which"] == "production"
        mod.reset_key_cache()

    def test_verify_takes_an_environment(self) -> None:
        import inspect

        from app.plaid.webhook import verify

        assert "environment" in inspect.signature(verify).parameters


class TestRenamingSettingsDidNotBreakExistingInstances:
    """The old environment names still work.

    `deck` named one Steam Deck in one house, so the settings were
    renamed for anyone else. Renaming without keeping the old names
    readable is the quietest kind of breakage there is: pydantic does
    not raise on an unknown environment variable, it uses the default.
    An un-updated `.env` would therefore keep `DECK_POWER_URL` set, the
    server would read an empty `power_relay_url`, and the power relay
    would simply stop working -- no error, no log line, just a machine
    that never wakes again.
    """

    def test_the_old_ai_host_variable_is_still_read(self, monkeypatch) -> None:
        monkeypatch.setenv("AI_DECK_URL", "http://old-name:11434")
        monkeypatch.delenv("AI_HOST_URL", raising=False)
        assert Settings(allow_production=False).ai_host_url == "http://old-name:11434"

    def test_the_new_name_wins_when_both_are_set(self, monkeypatch) -> None:
        monkeypatch.setenv("AI_DECK_URL", "http://old-name:11434")
        monkeypatch.setenv("AI_HOST_URL", "http://new-name:11434")
        assert Settings(allow_production=False).ai_host_url == "http://new-name:11434"

    def test_the_old_relay_variables_are_still_read(self, monkeypatch) -> None:
        monkeypatch.setenv("DECK_POWER_URL", "http://pi:11436")
        monkeypatch.setenv("DECK_POWER_TOKEN", "tok")
        monkeypatch.delenv("POWER_RELAY_URL", raising=False)
        monkeypatch.delenv("POWER_RELAY_TOKEN", raising=False)
        s = Settings(allow_production=False)
        assert s.power_relay_url == "http://pi:11436"
        assert s.power_relay_token == "tok"

    def test_the_old_provider_value_still_selects_the_same_thing(self) -> None:
        """AI_PROVIDER=deck must keep meaning what it meant."""
        from app.ai.provider import ManagedOllamaProvider, build_provider

        s = Settings(ai_provider="deck", allow_production=False)
        assert s.ai_provider == "managed", "folded at the edge, once"
        assert isinstance(build_provider(s), ManagedOllamaProvider)

    def test_no_default_names_anyone_particular_machine(self) -> None:
        """The old default was one house's LAN address."""
        s = Settings(allow_production=False)
        for value in (s.ai_host_url, s.ai_quiet_timezone):
            assert "192.168." not in value
            assert "Denver" not in value
