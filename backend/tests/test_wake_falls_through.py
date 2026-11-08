"""A failed wake request must not stop the power relay being tried.

Hit for real. The deck was hard powered off, the Wake-on-LAN service
returned an error, and ensure_awake raised immediately -- so the relay
that exists precisely for a fully powered-off machine was never
reached. The user saw "Could not wake the AI computer" and the PC
stayed dark.

A failing wake request is the STRONGEST evidence the machine is off,
not a reason to give up.
"""
from __future__ import annotations

import pytest

from app.ai import hardpower, provider
from app.ai.provider import AIUnavailable, DeckProvider
from app.core.config import Settings


def _settings(**over) -> Settings:
    base = dict(ai_provider="deck", ai_deck_url="http://deck:11434",
                wol_api_url="http://wol", wol_api_token="w",
                deck_power_url="http://pi:11436", deck_power_token="t",
                ai_wake_timeout_seconds=0, deck_power_boot_seconds=0)
    base.update(over)
    return Settings(**base)


class _Boom:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        raise RuntimeError("wake service returned 500")


def _never_open(self, host, port):
    return False


@pytest.mark.asyncio
async def test_the_relay_is_tried_when_the_wake_request_errors(monkeypatch):
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda **kw: _Boom())
    monkeypatch.setattr(DeckProvider, "_port_open", _never_open)

    pressed = []

    async def fake_press(settings, timeout=20.0):
        pressed.append(True)
        return hardpower.PowerResult(True, "pressed the power button")

    monkeypatch.setattr(hardpower, "press_once", fake_press)

    p = DeckProvider(_settings())
    with pytest.raises(AIUnavailable):
        # Boot timeout is 0, so it gives up after the press -- the point
        # is that the press HAPPENED.
        await p.ensure_awake()
    assert pressed, "a failed wake request must fall through to the relay"


@pytest.mark.asyncio
async def test_without_a_relay_the_old_message_still_stands(monkeypatch):
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda **kw: _Boom())
    monkeypatch.setattr(DeckProvider, "_port_open", _never_open)

    p = DeckProvider(_settings(deck_power_url="", deck_power_token=""))
    with pytest.raises(AIUnavailable, match="unplugged or off the network"):
        await p.ensure_awake()


@pytest.mark.asyncio
async def test_a_host_that_is_already_up_is_left_alone(monkeypatch):
    monkeypatch.setattr(DeckProvider, "_port_open", lambda self, h, p: True)

    async def must_not_run(settings, timeout=20.0):
        raise AssertionError("the relay must never fire at a running host")

    monkeypatch.setattr(hardpower, "press_once", must_not_run)
    await DeckProvider(_settings()).ensure_awake()


@pytest.mark.asyncio
async def test_a_relay_only_setup_works_without_wake_on_lan(monkeypatch):
    """The relay alone is a complete answer; WoL is the optimisation."""
    monkeypatch.setattr(DeckProvider, "_port_open", _never_open)
    pressed = []

    async def fake_press(settings, timeout=20.0):
        pressed.append(True)
        return hardpower.PowerResult(True, "pressed")

    monkeypatch.setattr(hardpower, "press_once", fake_press)

    p = DeckProvider(_settings(wol_api_url="", wol_api_token=""))
    with pytest.raises(AIUnavailable):
        await p.ensure_awake()
    assert pressed
