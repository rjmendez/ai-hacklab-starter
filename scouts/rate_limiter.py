#!/usr/bin/env python3
"""
rate_limiter.py — Distributed rate limiting using Redis INCR + TTL.
Shared across all scout workers so rate limits apply mesh-wide.
"""

import asyncio
import time

import redis


# ── Redis connection ──────────────────────────────────────────────────────────

def _get_redis() -> redis.Redis:
    import os
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


# ── RedisRateLimiter ──────────────────────────────────────────────────────────

class RedisRateLimiter:
    """
    Distributed rate limiting using Redis INCR + TTL (sliding fixed window).

    Each call to acquire() increments a counter. If the counter exceeds
    max_calls within the window, the slot is refused or the caller blocks.
    """

    def __init__(self, key: str, max_calls: int, window_seconds: int):
        """
        :param key:            Redis key, e.g. "ratelimit:crtsh"
        :param max_calls:      Maximum calls allowed per window
        :param window_seconds: Window duration in seconds
        """
        self.key = key
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._r = _get_redis()

    def _increment(self) -> int:
        """Increment counter; set TTL on first call in window. Returns new count."""
        pipe = self._r.pipeline()
        pipe.incr(self.key)
        pipe.expire(self.key, self.window_seconds)
        results = pipe.execute()
        return results[0]  # the INCR result

    def acquire(self, block: bool = True) -> bool:
        """
        Try to acquire a rate limit slot.

        If block=True, sleep until a slot is available (max 30s total wait).
        Returns True if acquired, False if timed out or blocked=False and over limit.
        """
        deadline = time.monotonic() + 30  # max 30s blocking

        while True:
            count = self._increment()
            if count <= self.max_calls:
                return True

            # Over limit — decrement to avoid permanently inflating the counter
            self._r.decr(self.key)

            if not block:
                return False

            if time.monotonic() >= deadline:
                return False  # timed out

            # Sleep a fraction of the window before retrying
            sleep_time = self.window_seconds / self.max_calls
            sleep_time = min(sleep_time, deadline - time.monotonic(), 5.0)
            if sleep_time <= 0:
                return False
            time.sleep(sleep_time)

    def remaining(self) -> int:
        """How many calls remaining in the current window."""
        current = self._r.get(self.key)
        if current is None:
            return self.max_calls
        used = int(current)
        return max(0, self.max_calls - used)

    async def acquire_async(self) -> bool:
        """
        Async version: non-blocking check, with asyncio.sleep on backoff.
        Max wait: 30 seconds.
        """
        deadline = asyncio.get_event_loop().time() + 30

        while True:
            count = self._increment()
            if count <= self.max_calls:
                return True

            self._r.decr(self.key)

            now = asyncio.get_event_loop().time()
            if now >= deadline:
                return False

            sleep_time = self.window_seconds / self.max_calls
            sleep_time = min(sleep_time, deadline - now, 5.0)
            if sleep_time <= 0:
                return False
            await asyncio.sleep(sleep_time)

    def reset(self):
        """Manually reset the rate limit counter (for testing)."""
        self._r.delete(self.key)

    def __repr__(self):
        return (
            f"RedisRateLimiter(key={self.key!r}, "
            f"max_calls={self.max_calls}, "
            f"window={self.window_seconds}s, "
            f"remaining={self.remaining()})"
        )


# ── Pre-configured limiters for known external APIs ───────────────────────────

CRTSH_LIMITER    = RedisRateLimiter("ratelimit:crtsh",     max_calls=5,  window_seconds=60)
HACKERONE_LIMITER = RedisRateLimiter("ratelimit:hackerone", max_calls=10, window_seconds=60)
FIREBASE_LIMITER  = RedisRateLimiter("ratelimit:firebase",  max_calls=50, window_seconds=60)
BUGCROWD_LIMITER  = RedisRateLimiter("ratelimit:bugcrowd",  max_calls=10, window_seconds=60)


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== RedisRateLimiter self-test ===")

    TEST_KEY = "ratelimit:selftest"
    rl = RedisRateLimiter(TEST_KEY, max_calls=3, window_seconds=5)
    rl.reset()

    # acquire 3 slots — should all succeed
    for i in range(3):
        ok = rl.acquire(block=False)
        assert ok, f"Expected slot {i+1} to be available"
        print(f"✓ Acquired slot {i+1}")

    # 4th should fail with block=False
    ok = rl.acquire(block=False)
    assert not ok, "Expected 4th acquire to fail (over limit)"
    print("✓ 4th acquire correctly rejected (block=False)")

    # remaining() should be 0
    assert rl.remaining() == 0, f"Expected 0 remaining, got {rl.remaining()}"
    print("✓ remaining() = 0")

    # Wait for window to expire
    print("  Waiting 6s for window to expire...")
    time.sleep(6)

    # Should be able to acquire again
    ok = rl.acquire(block=False)
    assert ok, "Expected slot after window reset"
    print("✓ Slot available after window reset")

    assert rl.remaining() == 2, f"Expected 2 remaining, got {rl.remaining()}"
    print(f"✓ remaining() = {rl.remaining()}")

    rl.reset()

    # Async test
    async def async_test():
        rl2 = RedisRateLimiter(TEST_KEY + ":async", max_calls=2, window_seconds=5)
        rl2.reset()
        ok1 = await rl2.acquire_async()
        ok2 = await rl2.acquire_async()
        assert ok1 and ok2, "Both async acquires should succeed"
        print("✓ acquire_async() works")
        rl2.reset()

    asyncio.run(async_test())

    # Print pre-configured limiters
    print("\nPre-configured limiters:")
    for lim in [CRTSH_LIMITER, HACKERONE_LIMITER, FIREBASE_LIMITER, BUGCROWD_LIMITER]:
        print(f"  {lim}")

    print("\n✅ All RedisRateLimiter tests passed.")
