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

import inspect

import pytest
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.ai import gpu, provider
from app.core.config import Settings

JARGON = ["ollama", "gpu_busy", "traceback", "exception", "none)",
          "wake-on-lan api", "http", "errno", "stacktrace"]


def _settings(**over) -> Settings:
    base = dict(ai_provider="deck", ai_deck_url="http://x:11434",
                gpu_probe_url="http://x:11435", allow_production=False)
    base.update(over)
    return Settings(**base)


def test_no_unavailable_message_leaks_jargon():
    src = inspect.getsource(provider)
    # Every literal passed to AIUnavailable, including implicit joins.
    for block in re.findall(r"AIUnavailable\(\s*(.*?)\)", src, re.S):
        text = " ".join(re.findall(r'"([^"]*)"', block)).lower()
        if not text:
            continue
        for word in JARGON:
            assert word not in text, f"jargon {word!r} in user message: {text!r}"


def test_an_unavailable_message_says_what_to_do():
    """A dead end is worse than a wait: say what would change it."""
    src = inspect.getsource(provider)
    for block in re.findall(r"AIUnavailable\(\s*(.*?)\)", src, re.S):
        text = " ".join(re.findall(r'"([^"]*)"', block)).lower()
        if not text or "switched off" in text:
            continue
        assert "try again" in text, f"no next step offered: {text!r}"


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
