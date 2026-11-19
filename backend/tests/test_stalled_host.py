"""A host that takes the request and then goes silent.

This is the one AI failure no health check can see, and it cost a whole
evening of categorisations on 2026-11-16.

Ollama's listener lives in the parent `ollama serve` process; the model
runs in a child `llama-server`. That child self-deadlocked inside the
amdgpu KFD driver while mapping the model into VRAM -- kernel state D,
so SIGKILL could not touch it and even shutdown hung. The parent was
perfectly healthy throughout: the TCP connect succeeded and
/api/version answered 200, so every probe the app had said "awake", and
each queued job sat there until the request timeout before failing.

Two things follow, and both are tested here:

  * a timeout has to be told apart from a refusal, because the remedy
    differs -- asleep sorts itself out on the next wake, this needs the
    machine power-cycled;
  * the queue is serial, so after one job discovers a silent host the
    rest must wait rather than each paying the full timeout in turn.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest

from app.ai import jobs
from app.ai.provider import AIHostStalled, AIUnavailable, OllamaProvider
from app.core.config import Settings


def _settings(**over) -> Settings:
    base = dict(ai_provider="custom", ai_host_url="http://deck:11434",
                gpu_probe_url="http://deck:11435", allow_production=False)
    base.update(over)
    return Settings(**base)


def _raising_client(exc: Exception):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            raise exc

    return lambda **kw: _Client()


class TestTheFailureIsClassified:
    def test_a_silent_host_is_its_own_failure(self):
        """Read timeout: the request was accepted, nothing came back."""
        p = OllamaProvider("http://deck:11434", _settings(), timeout=120)
        with patch("httpx.AsyncClient",
                   _raising_client(httpx.ReadTimeout("timed out"))):
            with pytest.raises(AIHostStalled) as got:
                asyncio.run(p.chat("hello"))
        assert "120 seconds" in str(got.value), (
            "the message should say how long it waited")
        assert "restart" in str(got.value).lower(), (
            "retrying does not clear a wedged model process; say so")

    def test_a_stalled_host_is_still_an_unavailable_host(self):
        """Subclass, so every existing `except AIUnavailable` still works."""
        assert issubclass(AIHostStalled, AIUnavailable)

    def test_unreachable_is_not_mistaken_for_stalled(self):
        """A refused connection means asleep, which fixes itself."""
        p = OllamaProvider("http://deck:11434", _settings())
        with patch("httpx.AsyncClient",
                   _raising_client(httpx.ConnectError("refused"))):
            with pytest.raises(AIUnavailable) as got:
                asyncio.run(p.chat("hello"))
        assert not isinstance(got.value, AIHostStalled)

    def test_a_connect_timeout_is_unreachable_not_stalled(self):
        """ConnectTimeout IS a TimeoutException -- order of except matters.

        Caught the wrong way round this reads as "powered on but silent"
        when the machine never answered at all.
        """
        p = OllamaProvider("http://deck:11434", _settings())
        with patch("httpx.AsyncClient",
                   _raising_client(httpx.ConnectTimeout("no route"))):
            with pytest.raises(AIUnavailable) as got:
                asyncio.run(p.chat("hello"))
        assert not isinstance(got.value, AIHostStalled)

    def test_the_timeout_comes_from_settings(self):
        """It was hardcoded at 300s, which is 300s per job of a dead host."""
        p = OllamaProvider("http://deck:11434",
                           _settings(ai_request_timeout_seconds=42))
        assert p.timeout == 42


class TestTheQueueStopsWalkingIntoIt:
    """`jobs` keeps the note in memory; each test starts from clean."""

    def setup_method(self):
        jobs._stalled_until.clear()

    teardown_method = setup_method

    def test_a_stall_puts_the_host_on_hold(self):
        p = OllamaProvider("http://deck:11434", _settings())
        assert jobs._stall_hold_remaining(p) is None
        jobs._note_stall(p, _settings(ai_stall_backoff_seconds=900))
        hold = jobs._stall_hold_remaining(p)
        assert hold is not None and 840 < hold.total_seconds() <= 900

    def test_the_hold_expires_on_its_own(self):
        p = OllamaProvider("http://deck:11434", _settings())
        jobs._stalled_until[p.base_url] = datetime.now(UTC) - timedelta(seconds=1)
        assert jobs._stall_hold_remaining(p) is None
        assert p.base_url not in jobs._stalled_until, "expired note is dropped"

    def test_one_silent_host_does_not_hold_up_another(self):
        """Someone else's own box going quiet is not the deck's problem."""
        mine = OllamaProvider("http://deck:11434", _settings())
        theirs = OllamaProvider("http://their-box:11434", _settings())
        jobs._note_stall(mine, _settings())
        assert jobs._stall_hold_remaining(mine) is not None
        assert jobs._stall_hold_remaining(theirs) is None

    def test_a_success_clears_the_hold(self):
        p = OllamaProvider("http://deck:11434", _settings())
        jobs._note_stall(p, _settings())
        jobs._clear_stall(p)
        assert jobs._stall_hold_remaining(p) is None

    def test_a_provider_with_no_host_is_not_tracked(self):
        """NullProvider has no base_url; it must not crash the bookkeeping."""
        from app.ai.provider import NullProvider

        n = NullProvider()
        jobs._note_stall(n, _settings())
        assert jobs._stall_hold_remaining(n) is None

    def test_the_hold_reason_reads_like_a_sentence(self):
        """It is shown verbatim in the Review tab, so it has to make sense."""
        import inspect

        src = inspect.getsource(jobs.run_job)
        assert "stopped answering" in src
        for jargon in ("stall", "AIHostStalled", "D state", "amdgpu"):
            # The reason string itself, not the comments around it.
            reason = src.split('job.defer_reason = (')[1].split(')')[0]
            assert jargon not in reason, f"jargon {jargon!r} in a shown reason"


class TestRetryingByHandLiftsTheHold:
    """`attempts` is set high by retry to skip the GPU gate.

    That interacts badly with the give-up branch, which keys off the
    same counter: a retried job would sail past the hold and fail
    immediately without so much as a connection attempt. Someone
    pressing retry has usually just power-cycled the machine.
    """

    def test_the_retry_route_clears_the_stall(self):
        import inspect

        from app.api.routes import ai as ai_routes

        src = inspect.getsource(ai_routes)
        assert "_clear_stall(provider_for(user, settings))" in src, (
            "retry must lift the hold, or it fails without trying")
