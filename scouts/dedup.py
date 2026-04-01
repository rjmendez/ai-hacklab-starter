#!/usr/bin/env python3
"""
dedup.py — Distributed deduplication using Redis SETs.
Used by scouts to avoid re-processing already-seen items across restarts.
"""

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


# ── RedisDedup ────────────────────────────────────────────────────────────────

class RedisDedup:
    """Distributed deduplication using Redis SETs."""

    def __init__(self, key: str, ttl_days: int = 30):
        """
        :param key:      Redis key for the set, e.g. "iris:seen_slugs"
        :param ttl_days: Days before the set expires (refreshed on writes)
        """
        self.key = key
        self.ttl_seconds = ttl_days * 86400
        self._r = _get_redis()

    def _refresh_ttl(self):
        """Refresh TTL after a write so the set doesn't expire mid-use."""
        self._r.expire(self.key, self.ttl_seconds)

    def is_new(self, value: str) -> bool:
        """
        Returns True if value hasn't been seen before (and marks it seen).
        Uses SADD — returns 1 if added (new), 0 if already existed.
        """
        result = self._r.sadd(self.key, value)
        if result:
            self._refresh_ttl()
        return bool(result)

    def filter_new(self, values: list) -> list:
        """
        Return only values not previously seen, and mark all new ones as seen.
        Uses a pipeline for efficiency.
        """
        if not values:
            return []

        pipe = self._r.pipeline()
        for v in values:
            pipe.sadd(self.key, v)
        results = pipe.execute()

        new_values = [v for v, added in zip(values, results) if added]
        if new_values:
            self._refresh_ttl()
        return new_values

    def count(self) -> int:
        """How many items are in the set."""
        return self._r.scard(self.key)

    def clear(self):
        """Reset the dedup set."""
        self._r.delete(self.key)

    def peek(self, count: int = 10) -> list:
        """Sample up to `count` items from the set (for debugging)."""
        return list(self._r.srandmember(self.key, count))

    def add_bulk(self, values: list):
        """Add many values at once without returning new/old status."""
        if not values:
            return
        pipe = self._r.pipeline()
        # Redis SADD accepts multiple members
        pipe.sadd(self.key, *values)
        pipe.expire(self.key, self.ttl_seconds)
        pipe.execute()

    def contains(self, value: str) -> bool:
        """Check membership without modifying the set."""
        return bool(self._r.sismember(self.key, value))


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== RedisDedup self-test ===")

    TEST_KEY = "test:dedup:selftest"
    d = RedisDedup(TEST_KEY, ttl_days=1)

    # Reset
    d.clear()
    assert d.count() == 0, "Expected empty set after clear()"
    print("✓ clear() works")

    # is_new
    assert d.is_new("alpha") is True, "First add should be new"
    assert d.is_new("alpha") is False, "Second add should NOT be new"
    assert d.is_new("beta") is True
    print("✓ is_new() works")

    # filter_new
    new_items = d.filter_new(["alpha", "beta", "gamma", "delta"])
    assert set(new_items) == {"gamma", "delta"}, f"Expected only gamma+delta new, got: {new_items}"
    print(f"✓ filter_new() works: {new_items}")

    # count
    assert d.count() == 4, f"Expected 4, got {d.count()}"
    print(f"✓ count() = {d.count()}")

    # peek
    sample = d.peek(2)
    assert len(sample) == 2
    print(f"✓ peek(2) = {sample}")

    # contains
    assert d.contains("alpha") is True
    assert d.contains("zzz_not_here") is False
    print("✓ contains() works")

    # add_bulk
    d.add_bulk(["x1", "x2", "x3"])
    assert d.count() == 7
    print(f"✓ add_bulk() works, count now {d.count()}")

    # cleanup
    d.clear()
    assert d.count() == 0
    print("✓ Final clear() works")

    print("\n✅ All RedisDedup tests passed.")
