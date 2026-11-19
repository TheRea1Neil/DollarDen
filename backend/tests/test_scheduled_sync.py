"""The background Plaid schedule.

It exists for two situations. A plain-HTTP install receives no webhooks
at all, because Plaid will not post to an unencrypted URL. And even
where webhooks work they get missed -- a dropped request, a restart, a
reverse-proxy path that starts 404ing while Plaid goes on reporting the
connection as perfectly healthy. Both failures look identical from the
app: transactions quietly stop arriving.

The floor on the interval is the part worth testing hardest. A
mistyped setting must not become a request every second against
someone's bank, unattended, forever.
"""
from __future__ import annotations

from app.core.config import Settings
from app.plaid.scheduled import MIN_INTERVAL_MINUTES, interval_minutes


def _s(**over) -> Settings:
    base = dict(allow_production=False)
    base.update(over)
    return Settings(**base)


class TestTheInterval:
    def test_the_default_is_hourly(self):
        assert interval_minutes(_s()) == 60

    def test_a_chosen_interval_is_respected(self):
        assert interval_minutes(_s(plaid_sync_interval_minutes=15)) == 15

    def test_two_minutes_is_the_floor(self):
        assert MIN_INTERVAL_MINUTES == 2
        assert interval_minutes(_s(plaid_sync_interval_minutes=2)) == 2

    def test_anything_below_the_floor_is_raised_to_it(self):
        """A mistyped 0 must not mean "as fast as the loop can run"."""
        for attempt in (1, 0, -5, -9999):
            assert interval_minutes(
                _s(plaid_sync_interval_minutes=attempt)) == MIN_INTERVAL_MINUTES

    def test_a_long_interval_is_left_alone(self):
        assert interval_minutes(_s(plaid_sync_interval_minutes=1440)) == 1440


class TestItCanBeTurnedOff:
    def test_enabled_by_default(self):
        assert _s().plaid_sync_enabled is True

    def test_it_can_be_disabled(self):
        assert _s(plaid_sync_enabled=False).plaid_sync_enabled is False


class TestQuietHours:
    """Configurable, and able to be switched off entirely."""

    def test_the_default_window_is_midnight_to_eight(self):
        s = _s()
        assert s.ai_quiet_enabled is True
        assert s.ai_quiet_start_hour == 0
        assert s.ai_quiet_end_hour == 8

    def test_the_default_timezone_names_no_particular_house(self):
        """It has to be wrong for everyone equally, not right for one."""
        assert _s().ai_quiet_timezone == "UTC"

    def test_switching_quiet_hours_off_means_never_quiet(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.ai.gpu import in_quiet_hours

        three_am = datetime(2026, 11, 17, 3, 0, tzinfo=ZoneInfo("UTC"))
        assert in_quiet_hours(_s(), three_am) is True
        assert in_quiet_hours(_s(ai_quiet_enabled=False), three_am) is False

    def test_a_custom_window_is_honoured(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.ai.gpu import in_quiet_hours

        s = _s(ai_quiet_start_hour=22, ai_quiet_end_hour=6)
        tz = ZoneInfo("UTC")
        # Wraps past midnight.
        assert in_quiet_hours(s, datetime(2026, 11, 17, 23, 0, tzinfo=tz)) is True
        assert in_quiet_hours(s, datetime(2026, 11, 17, 3, 0, tzinfo=tz)) is True
        assert in_quiet_hours(s, datetime(2026, 11, 17, 12, 0, tzinfo=tz)) is False


class TestTheScheduleDoesNotWaitForQuietHours:
    """Quiet hours protect the AI machine, not the network.

    A scheduled pull makes an HTTP request and wakes nothing. Pausing it
    overnight would only mean the morning starts with stale balances,
    and the AI work it queues is already deferred by the job gate.
    """

    def test_the_loop_does_not_consult_quiet_hours(self):
        import inspect

        from app.plaid import scheduled

        src = inspect.getsource(scheduled)
        assert "in_quiet_hours" not in src
        assert "quiet hours" in src.lower(), "but it should say why, in a comment"


class TestDeadConnectionsAreLeftAlone:
    """Found by running the schedule against the real database.

    The first pass tried six connections. One belonged to the real
    budget and synced. The rest were sandbox leftovers on old test
    accounts -- "First Platypus Bank", "Test Bank" -- being called
    against production, where their tokens mean nothing. Without this
    the schedule would retry all five every hour, forever, logging an
    identical failure each time.
    """

    def test_an_invalid_token_is_recognised_as_permanent(self):
        from app.plaid.client import PlaidError

        dead = PlaidError(400, {"error_code": "INVALID_ACCESS_TOKEN"})
        assert dead.is_dead is True
        assert dead.needs_reauth is False, "nothing the user can sign in to fix"

    def test_a_deleted_item_is_permanent_too(self):
        from app.plaid.client import PlaidError

        assert PlaidError(400, {"error_code": "ITEM_NOT_FOUND"}).is_dead is True

    def test_needing_a_login_is_not_dead(self):
        """The user CAN fix this one, so it must stay in the rotation."""
        from app.plaid.client import PlaidError

        reauth = PlaidError(400, {"error_code": "ITEM_LOGIN_REQUIRED"})
        assert reauth.needs_reauth is True
        assert reauth.is_dead is False

    def test_an_ordinary_failure_is_neither(self):
        from app.plaid.client import PlaidError

        rate_limited = PlaidError(429, {"error_code": "RATE_LIMIT_EXCEEDED"})
        assert rate_limited.is_dead is False
        assert rate_limited.needs_reauth is False

    def test_only_the_schedule_skips_them(self):
        """Pressing refresh is an explicit ask; honour it."""
        import inspect

        from app.plaid import scheduled, sync

        assert "skip_dead: bool = False" in inspect.getsource(sync.refresh_items), \
            "manual refresh must still try"
        assert "skip_dead=True" in inspect.getsource(scheduled.run_once)
