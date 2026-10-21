"""Rate limiting. The login endpoint is internet-facing."""
from __future__ import annotations

from app.core.ratelimit import Limit, SlidingWindowLimiter


def test_allows_up_to_the_limit_then_blocks() -> None:
    lim = SlidingWindowLimiter()
    limit = Limit(max_hits=3, window_seconds=60)
    assert [lim.check("ip", limit)[0] for _ in range(3)] == [True, True, True]
    allowed, retry = lim.check("ip", limit)
    assert allowed is False
    assert retry > 0


def test_buckets_are_independent_per_key() -> None:
    lim = SlidingWindowLimiter()
    limit = Limit(max_hits=1, window_seconds=60)
    assert lim.check("a", limit)[0] is True
    assert lim.check("b", limit)[0] is True   # b is not punished for a
    assert lim.check("a", limit)[0] is False


def test_window_slides(monkeypatch) -> None:
    import app.core.ratelimit as rl

    now = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: now[0])
    lim = rl.SlidingWindowLimiter()
    limit = rl.Limit(max_hits=2, window_seconds=60)

    assert lim.check("ip", limit)[0] is True
    assert lim.check("ip", limit)[0] is True
    assert lim.check("ip", limit)[0] is False

    now[0] += 61          # the window moves past both hits
    assert lim.check("ip", limit)[0] is True


def test_spoofed_forwarded_header_is_ignored_from_untrusted_peers() -> None:
    """An attacker must not dodge the limit by setting X-Forwarded-For."""
    from app.core.ratelimit import client_key

    class Req:
        def __init__(self, peer, xff):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"x-forwarded-for": xff} if xff else {}

    trusted = {"10.42.42.43"}
    # From our proxy: the forwarded address is believed.
    assert client_key(Req("10.42.42.43", "1.2.3.4"), trusted) == "1.2.3.4"
    # Direct from a stranger claiming to be someone else: ignored.
    assert client_key(Req("9.9.9.9", "1.2.3.4"), trusted) == "9.9.9.9"


def test_register_endpoint_actually_returns_429(client) -> None:
    """End-to-end: the limit is wired into the route, not just the class.

    This test deliberately does NOT reset the limiter mid-run.
    """
    import uuid

    from app.core.ratelimit import limiter

    limiter.reset()
    codes = []
    for _ in range(5):
        r = client.post(
            "/api/auth/register",
            json={"email": f"rl-{uuid.uuid4().hex[:8]}@example.com",
                  "password": "a-sufficiently-long-password"},
        )
        codes.append(r.status_code)
    limiter.reset()

    assert 429 in codes, f"rate limit never fired: {codes}"
    assert codes.count(201) <= 3, f"more registrations allowed than the limit: {codes}"
    # A 429 must tell the caller when to come back.
    assert codes[-1] == 429
