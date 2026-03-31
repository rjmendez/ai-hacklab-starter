"""
scouts/rate_limiter.py — Distributed rate limiting via Redis INCR + TTL.

Shared across all scout workers so rate limits apply mesh-wide, not
per-process. A crt.sh rate limit applies regardless of how many
gamma agents are running.

Usage:
    from scouts.rate_limiter import RedisRateLimiter

    limiter = RedisRateLimiter("ratelimit:crtsh", max_calls=10, window_seconds=60)

    # Blocking style (waits until a slot is available)
    with limiter.throttle():
        result = requests.get("https://crt.sh/...")

    # Non-blocking style (returns False if rate limited)
    if limiter.try_acquire():
        result = requests.get(...)
    else:
        print("Rate limited — skipping this cycle")
"""

import os
import time
import redis


def _get_client() -> redis.Redis:
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


class RedisRateLimiter:
    """
    Distributed rate limiter using Redis INCR + TTL (fixed window per interval).

    Each call to acquire() atomically increments a counter. If the counter
    exceeds max_calls within the window, the slot is refused or the caller
    blocks until the window resets.

    Args:
        key:            Redis key (e.g. "ratelimit:crtsh" or "ratelimit:github-api")
        max_calls:      Maximum calls allowed per window.
        window_seconds: Window duration in seconds.
    """

    def __init__(self, key: str, max_calls: int, window_seconds: int):
        self.key            = key
        self.max_calls      = max_calls
        self.window_seconds = window_seconds
        self._r             = _get_client()

    def _increment(self) -> int:
        """Increment counter; set TTL on first call in window. Returns new count."""
        pipe = self._r.pipeline()
        pipe.incr(self.key)
        pipe.expire(self.key, self.window_seconds)
        results = pipe.execute()
        return results[0]

    def try_acquire(self) -> bool:
        """
        Try to acquire one slot. Returns True if allowed, False if rate limited.
        Non-blocking.
        """
        count = self._increment()
        if count > self.max_calls:
            # We over-incremented — decrement back to avoid inflating the count
            self._r.decr(self.key)
            return False
        return True

    def throttle(self, poll_interval: float = 0.5):
        """
        Context manager that blocks until a slot is available.

        Usage:
            with limiter.throttle():
                make_api_call()
        """
        return _ThrottleContext(self, poll_interval)

    def current_count(self) -> int:
        """Return the current call count in this window."""
        val = self._r.get(self.key)
        return int(val) if val else 0

    def reset(self) -> None:
        """Reset the counter. Use for testing or emergency unblocking."""
        self._r.delete(self.key)

    def remaining(self) -> int:
        """Return remaining slots in this window."""
        return max(0, self.max_calls - self.current_count())


class _ThrottleContext:
    def __init__(self, limiter: RedisRateLimiter, poll_interval: float):
        self._limiter       = limiter
        self._poll_interval = poll_interval

    def __enter__(self):
        while not self._limiter.try_acquire():
            time.sleep(self._poll_interval)

    def __exit__(self, *_):
        pass
