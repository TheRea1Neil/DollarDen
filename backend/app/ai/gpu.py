"""Is there room on the GPU right now?

The inference host is also a gaming PC. gemma4 needs about 9.5 GB of
its 16 GB card -- measured at 9,815 MB resident -- so a game using more
than roughly 6.3 GB leaves no room for it.

Two things are being protected, in this order:

  1. The GAME. Loading a model onto a card that is mid-frame costs
     stutter, and on Linux there is no graphics-vs-compute preemption
     to soften it. VRAM is allocated, not time-shared: if it does not
     fit, textures spill over PCIe and the hitching is severe.

  2. SLEEP. The machine is in Neil's house. Nothing may wake it between
     01:00 and 08:00, so a transaction that arrives at 3am waits for
     morning rather than spinning up fans next to someone asleep.

Everything else runs IMMEDIATELY. This is a gate, not a schedule: the
normal case is "there is room, go", and deferral is the exception.

FAILING OPEN
------------
If the probe cannot be reached, this says YES.

A dead probe must never stop transactions being categorised. The probe
lives on a machine that gets OS updates; the cost of being wrong in the
permissive direction is one stuttered game, and the cost of being wrong
in the strict direction is a backlog that silently never drains. The
first is recoverable by the user noticing; the second is not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.core.config import Settings

log = logging.getLogger("dollarden.ai.gpu")


@dataclass(frozen=True)
class GpuState:
    reachable: bool
    busy_percent: int | None = None
    vram_free_mb: int | None = None
    vram_total_mb: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "busy_percent": self.busy_percent,
            "vram_free_mb": self.vram_free_mb,
            "vram_total_mb": self.vram_total_mb,
            "error": self.error,
        }


@dataclass(frozen=True)
class Verdict:
    """Whether to run now, and if not, why -- in words a user can read."""

    ok: bool
    reason: str | None = None
    # Machine-readable, so the app can style "quiet hours" differently
    # from "your PC is busy".
    code: str | None = None
    gpu: GpuState | None = None


async def probe(settings: Settings, timeout: float = 5.0) -> GpuState:
    """Ask the host how much of its card is free."""
    url = (settings.gpu_probe_url or "").rstrip("/")
    if not url:
        return GpuState(reachable=False, error="no probe configured")
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(
                f"{url}/gpu",
                headers={"Authorization": f"Bearer {settings.gpu_probe_token}"},
            )
            r.raise_for_status()
            d = r.json()
    except Exception as exc:  # noqa: BLE001
        # Expected whenever the host is asleep. Not an error worth
        # shouting about -- an asleep host has a free GPU by definition.
        return GpuState(reachable=False, error=f"{type(exc).__name__}: {exc}")

    if not d.get("ok"):
        return GpuState(reachable=False, error=str(d.get("error") or "probe not ok"))
    return GpuState(
        reachable=True,
        busy_percent=int(d.get("busy_percent") or 0),
        vram_free_mb=int(d.get("vram_free_mb") or 0),
        vram_total_mb=int(d.get("vram_total_mb") or 0),
    )


def in_quiet_hours(settings: Settings, now: datetime | None = None) -> bool:
    """Is it the middle of the night where the PC physically is?

    The timezone is pinned rather than taken from the clock. The backend
    runs on a Pi set to Europe/London while the PC sits in Denver -- six
    hours apart -- so "01:00 to 08:00" read off the server's own clock
    would protect 19:00 to 02:00 Denver time. That is both the wrong
    window and precisely backwards: it would block the evening and allow
    3am.
    """
    if not settings.ai_quiet_enabled:
        return False
    tz = ZoneInfo(settings.ai_quiet_timezone)
    local = (now or datetime.now(tz)).astimezone(tz).time()
    start = time(settings.ai_quiet_start_hour, 0)
    end = time(settings.ai_quiet_end_hour, 0)
    if start == end:
        return False
    if start < end:
        return start <= local < end
    # Window wraps past midnight, which the default 00:00-08:00 does not
    # but a user-chosen 22:00-06:00 would.
    return local >= start or local < end


async def model_already_loaded(settings: Settings, timeout: float = 5.0) -> bool:
    """Is the model we want already resident on the card?

    If it is, the VRAM it occupies is not an obstacle -- it IS the
    thing we were going to load. Checking free VRAM in that state makes
    the gate block on its own footprint: gemma4 holds ~9 GB of a 16 GB
    card, leaving ~7 GB free, which is under the 10.5 GB the gate
    demands. The first job loads the model, and every job for the next
    five minutes is then deferred by it.

    Observed exactly that: "graphics card is in use (7,221 MB free,
    needs 10,500 MB)" with nothing running but ollama.

    An already-loaded model is the BEST case, not a blocked one.
    """
    base = (settings.ai_host_url or "").rstrip("/")
    if not base:
        return False
    want = (settings.ai_model_text or "").strip()
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{base}/api/ps")
            r.raise_for_status()
            loaded = {m.get("name") for m in (r.json().get("models") or [])}
    except Exception:  # noqa: BLE001
        return False
    return want in loaded


async def can_run_now(
    settings: Settings, *, needs_vram_mb: int | None = None,
    now: datetime | None = None,
) -> Verdict:
    """The gate. Permissive by default; specific when it says no."""
    if in_quiet_hours(settings, now):
        return Verdict(
            ok=False, code="quiet_hours",
            reason=(f"Waiting until {settings.ai_quiet_end_hour:02d}:00 — nothing "
                    f"runs overnight so the computer stays asleep."),
        )

    # Already loaded means already paid for. Checking headroom here
    # would measure our own footprint and defer because of it.
    if await model_already_loaded(settings):
        return Verdict(ok=True)

    state = await probe(settings)
    if not state.reachable:
        # Asleep, or the probe is gone. Either way, go: an asleep host
        # has no game on it, and a missing probe must not become a
        # silent block on every transaction.
        return Verdict(ok=True, gpu=state)

    need = needs_vram_mb or settings.ai_min_vram_mb
    if state.vram_free_mb is not None and state.vram_free_mb < need:
        return Verdict(
            ok=False, code="gpu_busy", gpu=state,
            reason=(f"The computer's graphics card is in use "
                    f"({state.vram_free_mb:,} MB free, needs {need:,} MB). "
                    f"Trying again later."),
        )
    if (state.busy_percent is not None
            and state.busy_percent >= settings.ai_max_gpu_busy_percent):
        return Verdict(
            ok=False, code="gpu_busy", gpu=state,
            reason=(f"The computer's graphics card is busy "
                    f"({state.busy_percent}%). Trying again later."),
        )
    return Verdict(ok=True, gpu=state)
