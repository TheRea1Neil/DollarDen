"""The power relay is a last resort, and must behave like one.

It sits across the deck's physical power switch, so a short press is a
TOGGLE: on a running machine it is an ACPI shutdown request. Anything
that reflexively retries it would turn the deck off rather than on,
and repeatedly pressing a machine that is mid-boot is how a filesystem
gets corrupted.

So: only after Wake-on-LAN has failed, only when the host is
unreachable, only once, and never force-off from a web request.
"""
from __future__ import annotations

import pytest

from app.ai import hardpower
from app.core.config import Settings


def _settings(**over) -> Settings:
    base = dict(ai_provider="managed", ai_host_url="http://deck:11434",
                power_relay_url="http://pi:11436", power_relay_token="t")
    base.update(over)
    return Settings(**base)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """Stands in for httpx.AsyncClient."""

    def __init__(self, response=None, raises: Exception | None = None) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        self.calls.append(url)
        if self.raises:
            raise self.raises
        return self.response

    async def get(self, url, **kw):
        self.calls.append(url)
        if self.raises:
            raise self.raises
        return self.response


def _patch(monkeypatch, client):
    monkeypatch.setattr(hardpower.httpx, "AsyncClient", lambda **kw: client)


@pytest.mark.asyncio
async def test_a_press_is_reported_as_pressed(monkeypatch):
    client = FakeClient(FakeResponse(200, {"pressed": True}))
    _patch(monkeypatch, client)
    out = await hardpower.press_once(_settings())
    assert out.pressed is True
    assert client.calls == ["http://pi:11436/press"]


@pytest.mark.asyncio
async def test_a_running_deck_is_never_pressed(monkeypatch):
    """409 from the shim. Pressing here would SHUT IT DOWN."""
    client = FakeClient(FakeResponse(409, {"error": "deck is already reachable"}))
    _patch(monkeypatch, client)
    out = await hardpower.press_once(_settings())
    assert out.pressed is False
    assert "already on" in out.detail


@pytest.mark.asyncio
async def test_a_recent_press_is_not_repeated(monkeypatch):
    client = FakeClient(FakeResponse(429, {"retry_after_seconds": 120}))
    _patch(monkeypatch, client)
    out = await hardpower.press_once(_settings())
    assert out.pressed is False
    assert "120" in out.detail


@pytest.mark.asyncio
async def test_an_unreachable_relay_is_not_an_exception(monkeypatch):
    """The caller is already handling a failure; do not add another."""
    client = FakeClient(raises=RuntimeError("no route to host"))
    _patch(monkeypatch, client)
    out = await hardpower.press_once(_settings())
    assert out.pressed is False
    assert "did not respond" in out.detail


@pytest.mark.asyncio
async def test_nothing_happens_when_no_relay_is_configured(monkeypatch):
    client = FakeClient(FakeResponse(200, {"pressed": True}))
    _patch(monkeypatch, client)
    out = await hardpower.press_once(_settings(power_relay_url="",
                                               power_relay_token=""))
    assert out.pressed is False
    assert client.calls == [], "must not call anything"


def test_configured_requires_both_url_and_token():
    assert hardpower.configured(_settings()) is True
    assert hardpower.configured(_settings(power_relay_token="")) is False
    assert hardpower.configured(_settings(power_relay_url="")) is False


def test_there_is_no_force_off_in_the_web_path():
    """Holding the button cuts power with no filesystem flush. That
    decision belongs to a person at a terminal, not a web request."""
    assert not hasattr(hardpower, "force_off")
    assert "force" not in hardpower.press_once.__doc__.lower()
