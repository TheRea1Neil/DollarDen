"""The GPU gate: run now when there is room, wait when there is not.

The host is also a gaming PC. Two things are being protected -- the
game's frame rate, and Neil's sleep -- and exactly one behaviour is
wanted the rest of the time: go immediately.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.ai import gpu
from app.core.config import Settings


def _settings(**over) -> Settings:
    base = dict(
        gpu_probe_url="http://probe.invalid",
        gpu_probe_token="t",
        ai_min_vram_mb=10_500,
        ai_max_gpu_busy_percent=60,
        ai_quiet_timezone="America/Denver",
        ai_quiet_start_hour=1,
        ai_quiet_end_hour=8,
    )
    base.update(over)
    s = Settings()
    for k, v in base.items():
        object.__setattr__(s, k, v) if hasattr(s, "__dataclass_fields__") else setattr(s, k, v)
    return s


DENVER = ZoneInfo("America/Denver")


class TestQuietHours:
    """Pinned to the PC's zone, not the server's.

    The Pi runs Europe/London and the PC sits in Denver -- six hours
    apart. Read off the server clock, "01:00-08:00" would protect
    19:00-02:00 Denver: the wrong window, and backwards.
    """

    def _at(self, h, m=0, tz=DENVER):
        return datetime(2026, 10, 31, h, m, tzinfo=tz)

    def test_3am_denver_is_quiet(self):
        assert gpu.in_quiet_hours(_settings(), self._at(3)) is True

    def test_9am_denver_is_not(self):
        assert gpu.in_quiet_hours(_settings(), self._at(9)) is False

    def test_8am_exactly_is_awake(self):
        """End is exclusive: 08:00 is the first working minute."""
        assert gpu.in_quiet_hours(_settings(), self._at(8)) is False

    def test_1am_exactly_is_quiet(self):
        assert gpu.in_quiet_hours(_settings(), self._at(1)) is True

    def test_evening_gaming_time_is_not_quiet(self):
        """The window the London-clock bug would have blocked."""
        assert gpu.in_quiet_hours(_settings(), self._at(20)) is False

    def test_a_london_timestamp_is_judged_in_denver(self):
        """3am Denver is 9am London. The zone, not the clock, decides."""
        london = datetime(2026, 10, 31, 9, 0, tzinfo=ZoneInfo("Europe/London"))
        assert gpu.in_quiet_hours(_settings(), london) is True

    def test_a_window_that_wraps_midnight(self):
        s = _settings(ai_quiet_start_hour=22, ai_quiet_end_hour=6)
        assert gpu.in_quiet_hours(s, self._at(23)) is True
        assert gpu.in_quiet_hours(s, self._at(2)) is True
        assert gpu.in_quiet_hours(s, self._at(12)) is False

    def test_equal_hours_means_never_quiet(self):
        s = _settings(ai_quiet_start_hour=0, ai_quiet_end_hour=0)
        assert gpu.in_quiet_hours(s, self._at(3)) is False


class TestGate:
    async def _verdict(self, state, **over):
        import app.ai.gpu as mod

        async def fake_probe(settings, timeout=5.0):
            return state

        real, mod.probe = mod.probe, fake_probe
        try:
            # Midday Denver, so quiet hours never interfere.
            noon = datetime(2026, 10, 31, 12, 0, tzinfo=DENVER)
            return await mod.can_run_now(_settings(**over), now=noon)
        finally:
            mod.probe = real

    async def test_an_idle_card_runs_immediately(self):
        v = await self._verdict(gpu.GpuState(True, busy_percent=2, vram_free_mb=15_900))
        assert v.ok is True

    async def test_a_game_using_the_card_defers(self):
        v = await self._verdict(gpu.GpuState(True, busy_percent=90, vram_free_mb=4_000))
        assert v.ok is False and v.code == "gpu_busy"
        assert "4,000 MB free" in v.reason

    async def test_not_enough_vram_defers_even_when_idle(self):
        """VRAM is allocated, not time-shared. Busy% alone is not enough."""
        v = await self._verdict(gpu.GpuState(True, busy_percent=1, vram_free_mb=6_000))
        assert v.ok is False and v.code == "gpu_busy"

    async def test_a_busy_card_with_free_vram_still_defers(self):
        v = await self._verdict(gpu.GpuState(True, busy_percent=95, vram_free_mb=15_000))
        assert v.ok is False

    async def test_an_unreachable_probe_FAILS_OPEN(self):
        """The most important test here.

        A probe removed by an OS update must cost the gaming
        protection and nothing else. Blocking instead would leave every
        transaction queued forever, silently.
        """
        v = await self._verdict(gpu.GpuState(False, error="connection refused"))
        assert v.ok is True

    async def test_an_asleep_host_is_free(self):
        v = await self._verdict(gpu.GpuState(False, error="timeout"))
        assert v.ok is True

    async def test_quiet_hours_beat_an_idle_card(self):
        import app.ai.gpu as mod

        async def fake_probe(settings, timeout=5.0):
            return gpu.GpuState(True, busy_percent=0, vram_free_mb=16_000)

        real, mod.probe = mod.probe, fake_probe
        try:
            three_am = datetime(2026, 10, 31, 3, 0, tzinfo=DENVER)
            v = await mod.can_run_now(_settings(), now=three_am)
        finally:
            mod.probe = real
        assert v.ok is False and v.code == "quiet_hours"
        assert "08:00" in v.reason

    async def test_the_reason_is_written_for_a_person(self):
        v = await self._verdict(gpu.GpuState(True, busy_percent=99, vram_free_mb=500))
        assert "graphics card" in v.reason.lower()
        assert "trying again" in v.reason.lower()


class TestDoesNotBlockOnItsOwnModel:
    """The gate must not defer because of the memory IT is using.

    gemma4 holds ~9 GB of a 16 GB card, leaving ~7 GB free -- under the
    10.5 GB the gate demands. So the first job loads the model and
    every job for the next five minutes is deferred by it. Observed in
    the wild as:

        graphics card is in use (7,221 MB free, needs 10,500 MB)

    with nothing running but ollama.
    """

    async def _verdict(self, *, loaded, state):
        import app.ai.gpu as mod

        async def fake_probe(settings, timeout=5.0):
            return state

        async def fake_loaded(settings, timeout=5.0):
            return loaded

        real_p, real_l = mod.probe, mod.model_already_loaded
        mod.probe, mod.model_already_loaded = fake_probe, fake_loaded
        try:
            noon = datetime(2026, 10, 31, 12, 0, tzinfo=DENVER)
            return await mod.can_run_now(_settings(), now=noon)
        finally:
            mod.probe, mod.model_already_loaded = real_p, real_l

    async def test_a_loaded_model_runs_despite_low_free_vram(self):
        v = await self._verdict(
            loaded=True,
            state=gpu.GpuState(True, busy_percent=5, vram_free_mb=7_221))
        assert v.ok is True, "its own footprint must not block it"

    async def test_a_game_still_defers_when_nothing_is_loaded(self):
        v = await self._verdict(
            loaded=False,
            state=gpu.GpuState(True, busy_percent=90, vram_free_mb=4_000))
        assert v.ok is False and v.code == "gpu_busy"

    async def test_an_idle_card_with_nothing_loaded_runs(self):
        v = await self._verdict(
            loaded=False,
            state=gpu.GpuState(True, busy_percent=2, vram_free_mb=15_900))
        assert v.ok is True
