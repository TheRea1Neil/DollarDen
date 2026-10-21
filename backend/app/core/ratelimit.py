"""Rate limiting for internet-facing endpoints.

Caddy has no rate-limit module in the build we run, and the login endpoint is
publicly reachable -- scanners found the hostname within five seconds of its
certificate being issued (docs/07). So the limiter lives here.

In-process sliding window. That is sufficient because this deployment is a
single API container; if it ever scales horizontally the backing store must
move to Redis, or each replica will permit the full budget independently.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class Limit:
    max_hits: int
    window_seconds: int


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check(self, key: str, limit: Limit) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            cutoff = now - limit.window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit.max_hits:
                retry = int(bucket[0] + limit.window_seconds - now) + 1
                return False, max(retry, 1)

            bucket.append(now)
            self._maybe_sweep(now)
            return True, 0

    def _maybe_sweep(self, now: float) -> None:
        """Drop empty buckets occasionally so memory cannot grow without bound.

        Called with the lock held.
        """
        if now - self._last_sweep < 300:
            return
        self._last_sweep = now
        for k in [k for k, v in self._hits.items() if not v]:
            del self._hits[k]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def client_key(request: Request, trusted_proxies: set[str]) -> str:
    """Identify the caller.

    Behind Caddy every request appears to come from the proxy, so
    X-Forwarded-For is required to tell callers apart -- but it is only
    honoured when the immediate peer is actually our proxy. Otherwise a
    client could spoof the header and dodge the limit entirely.
    """
    peer = request.client.host if request.client else "unknown"
    if peer in trusted_proxies:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return peer


def enforce(request: Request, limit: Limit, bucket: str, trusted_proxies: set[str]) -> None:
    key = f"{bucket}:{client_key(request, trusted_proxies)}"
    allowed, retry_after = limiter.check(key, limit)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Slow down.",
            headers={"Retry-After": str(retry_after)},
        )
