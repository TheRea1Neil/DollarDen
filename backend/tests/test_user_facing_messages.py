"""Anything the user can be shown has to make sense to the user.

AIUnavailable messages land in ai_jobs.error and are rendered verbatim
in the app. "Ollama call failed: ConnectError" tells a developer
something and the owner of the budget nothing.

The deferral reasons matter more: a deferred job has NOT failed, and
the wording has to say what is happening and that it will sort itself
out, or people assume the feature is broken -- which is exactly what
happened when a game was running.
"""
from __future__ import annotations

import ast
import inspect

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.ai import gpu, provider
from app.core.config import Settings

JARGON = ["ollama", "gpu_busy", "traceback", "exception", "none)",
          "wake-on-lan api", "http", "errno", "stacktrace"]

# Both the base class and its subclasses -- a message does not stop
# being user-facing because it was raised by a more specific type.
RAISED = ("AIUnavailable", "AIHostStalled")

# A next step, in any of the forms the app actually has.
#
# "try again" is the usual one, but it is the WRONG advice for a host
# that is powered on and silent -- retrying buys another full timeout,
# and what helps is a restart. A hosted service adds two more: a key
# that was rejected and an address that was never set are both fixed in
# the settings, and telling someone to "try again" there would be
# actively unhelpful.
#
# The rule this encodes is "say what would change it", not "say one of
# these phrases".
NEXT_STEPS = ["try again", "turn it on", "restart", "check", "add the address"]


def _messages() -> list[str]:
    """Every string handed to one of those exceptions, as the user sees it.

    Parsed, not matched. The regex this replaced stopped at the first
    `)` in the call, which falls INSIDE "(nothing came back in N
    seconds)" -- so it silently truncated the message and then asserted
    confidently about the half it had kept. An f-string also splits
    across Constant and FormattedValue nodes, which no regex over the
    source text reassembles correctly.
    """
    out = []
    for node in ast.walk(ast.parse(inspect.getsource(provider))):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in RAISED:
            continue
        parts: list[str] = []
        for arg in node.args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    parts.append(sub.value)
        if parts:
            out.append(" ".join(parts).lower())
    return out


def test_every_unavailable_message_was_found():
    """A guard on the guard: an empty sweep would pass everything below."""
    found = _messages()
    assert len(found) >= 6, f"only found {len(found)} messages to check"


def _settings(**over) -> Settings:
    base = dict(ai_provider="managed", ai_host_url="http://x:11434",
                gpu_probe_url="http://x:11435", allow_production=False)
    base.update(over)
    return Settings(**base)


def test_no_unavailable_message_leaks_jargon():
    for text in _messages():
        for word in JARGON:
            assert word not in text, f"jargon {word!r} in user message: {text!r}"


def test_an_unavailable_message_says_what_to_do():
    """A dead end is worse than a wait: say what would change it."""
    for text in _messages():
        if "switched off" in text:
            continue
        assert any(step in text for step in NEXT_STEPS), (
            f"no next step offered: {text!r}")


@pytest.mark.asyncio
async def test_a_busy_gpu_explains_itself_with_a_number():
    """The reason is shown verbatim, so it has to read like a sentence."""
    real_probe, real_loaded = gpu.probe, gpu.model_already_loaded

    async def fake_probe(settings, timeout=5.0):
        return gpu.GpuState(True, busy_percent=79, vram_free_mb=12_000)

    async def not_loaded(settings, timeout=5.0):
        return False

    gpu.probe, gpu.model_already_loaded = fake_probe, not_loaded
    try:
        noon = datetime(2026, 11, 7, 12, 0, tzinfo=ZoneInfo("America/Denver"))
        v = await gpu.can_run_now(_settings(), now=noon)
    finally:
        gpu.probe, gpu.model_already_loaded = real_probe, real_loaded

    assert v.ok is False
    assert "79%" in v.reason, "the user should see the actual figure"
    assert "later" in v.reason.lower(), "say it will retry on its own"
    assert "gpu_busy" not in v.reason, "that is the code, not the reason"
