"""Which OAuth return path goes on the link token.

Worth testing precisely because the failure is silent: Plaid accepts a
token carrying the wrong parameter, Link opens, the user picks their
accounts, and then lands back at the bank's login with no Item created
and no error anywhere. Nothing downstream can detect it, so the
assertion has to live here.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.plaid.client import PlaidClient


def _settings(**over) -> Settings:
    base = dict(
        plaid_client_id="id",
        plaid_secret="sec",
        plaid_env="sandbox",
        plaid_products="transactions",
        plaid_country_codes="US",
    )
    base.update(over)
    return Settings(**base)


def _body(settings: Settings, platform: str | None) -> dict:
    body: dict = {}
    PlaidClient(settings)._apply_oauth_return_path(body, platform)
    return body


def test_android_gets_the_package_name_and_never_a_redirect_uri():
    # Both configured: the platform decides, not whichever happens to be set.
    s = _settings(
        plaid_redirect_uri="https://finance.example.org/plaid/oauth",
        plaid_android_package_name="org.dollarden.app",
    )
    body = _body(s, "android")
    assert body == {"android_package_name": "org.dollarden.app"}
    assert "redirect_uri" not in body


def test_ios_gets_the_redirect_uri_and_never_a_package_name():
    s = _settings(
        plaid_redirect_uri="https://finance.example.org/plaid/oauth",
        plaid_android_package_name="org.dollarden.app",
    )
    body = _body(s, "ios")
    assert body == {"redirect_uri": "https://finance.example.org/plaid/oauth"}
    assert "android_package_name" not in body


def test_sandbox_tolerates_missing_config():
    # The non-OAuth sandbox institutions link fine without either, and
    # development should not require production credentials.
    assert _body(_settings(), "android") == {}
    assert _body(_settings(), "ios") == {}


@pytest.mark.parametrize(
    "platform,configured,missing",
    [
        ("android", {"plaid_redirect_uri": "https://x/y"}, "PLAID_ANDROID_PACKAGE_NAME"),
        ("ios", {"plaid_android_package_name": "org.x.y"}, "PLAID_REDIRECT_URI"),
    ],
)
def test_production_refuses_when_the_platform_is_unconfigured(platform, configured, missing):
    # Having the OTHER platform configured must not count. This is the
    # exact shape of the bug: set the redirect URI for iOS, ship Android,
    # watch every Bank of America link dead-end.
    s = _settings(plaid_env="production", allow_production=True, **configured)
    with pytest.raises(RuntimeError) as err:
        _body(s, platform)
    assert missing in str(err.value)


def test_production_unspecified_platform_still_demands_a_redirect_uri():
    s = _settings(plaid_env="production", allow_production=True)
    with pytest.raises(RuntimeError):
        _body(s, None)


def test_production_is_reachable_when_the_flag_is_set():
    """The guard must permit the configuration it documents.

    It did not. `_guard_production` was a field_validator on plaid_env
    reading info.data["allow_production"], but pydantic v2 validates in
    declaration order and allow_production is declared afterwards -- so
    the flag was never visible and PLAID_ENV=production raised no matter
    what. A guard that cannot be satisfied is not a guard, and this would
    have surfaced on the first boot of the production cutover.
    """
    s = _settings(plaid_env="production", allow_production=True)
    assert s.plaid_env == "production"
    assert s.plaid_host == "https://production.plaid.com"


def test_production_still_refused_without_the_flag():
    with pytest.raises(Exception, match="ALLOW_PRODUCTION"):
        _settings(plaid_env="production")
