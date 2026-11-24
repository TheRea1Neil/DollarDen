"""Last-resort power control for the AI host, through a relay on the Pi.

Wake-on-LAN only reaches a machine whose NIC is still powered. It
cannot help after a power cut, when the BIOS has WoL disabled, or when
SteamOS has frozen hard -- and those are exactly the cases where the
budget silently stops categorising anything.

The relay sits across the motherboard's power switch header, so this
is the software equivalent of walking over and pressing the button.
That framing is the safety model:

  * It is tried ONCE, and only after Wake-on-LAN has already failed.
  * It is never tried against a machine that answers on the network.
    A short press is a TOGGLE -- on a running host it is an ACPI
    shutdown request, so a reflexive retry loop would turn the deck
    off rather than on.
  * Failing to wake it is reported to the user, not retried. A relay
    that keeps stabbing the power button of a machine mid-boot is how
    a filesystem gets corrupted.

Force-off is deliberately NOT exposed here. Holding the button for
five seconds cuts power with no filesystem flush; that belongs to a
person at a terminal who has decided it is warranted, not to a web
request. See docs/16-deck-power.md.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings

log = logging.getLogger("dollarden.ai.hardpower")


class PowerResult:
    """What the relay attempt did, in terms a user can be told."""

    def __init__(self, pressed: bool, detail: str) -> None:
        self.pressed = pressed
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"PowerResult(pressed={self.pressed}, detail={self.detail!r})"


def configured(settings: Settings) -> bool:
    return bool(settings.power_relay_url and settings.power_relay_token)


async def status(settings: Settings, timeout: float = 5.0) -> dict[str, Any] | None:
    """What the relay shim reports, or None if it cannot be reached."""
    if not configured(settings):
        return None
    url = settings.power_relay_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(
                f"{url}/status",
                headers={"Authorization": f"Bearer {settings.power_relay_token}"},
            )
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # noqa: BLE001
        log.info("power relay unreachable: %s", exc)
        return None


async def press_once(settings: Settings, timeout: float = 20.0) -> PowerResult:
    """Ask the Pi to tap the deck's power button, once.

    The shim refuses if the deck is reachable or if it pressed
    recently; both come back as a plain explanation rather than an
    error, because neither is a fault -- they are the safety rules
    doing their job.
    """
    if not configured(settings):
        return PowerResult(False, "no power relay is configured")

    url = settings.power_relay_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                f"{url}/press",
                headers={"Authorization": f"Bearer {settings.power_relay_token}"},
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("power relay call failed: %s", exc)
        return PowerResult(False, "the power relay on the Pi did not respond")

    if r.status_code == 200:
        log.info("power relay pressed the deck's power button")
        return PowerResult(True, "pressed the power button")
    if r.status_code == 409:
        return PowerResult(False, "the deck is already on")
    if r.status_code == 429:
        wait = (r.json() or {}).get("retry_after_seconds")
        return PowerResult(
            False,
            f"the power button was pressed moments ago; waiting {wait}s"
            if wait else "the power button was pressed moments ago",
        )
    return PowerResult(False, f"the power relay refused ({r.status_code})")
