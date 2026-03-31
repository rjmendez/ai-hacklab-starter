"""
scouts/dedup.py — Distributed deduplication using Redis SETs.

Scouts use this to avoid re-processing already-seen items across restarts,
crashes, or when multiple workers run in parallel.

Usage:
    from scouts.dedup import RedisDedup

    seen = RedisDedup("iris:seen_domains", ttl_days=30)

    for domain in candidate_list:
        if seen.is_new(domain):
            process(domain)
            seen.mark(domain)

    # Or in bulk:
    new_items = seen.filter_new(candidate_list)
    for item in new_items:
        process(item)
        seen.mark(item)
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


class RedisDedup:
    """
    Distributed deduplication using a Redis SET with TTL.

    The set auto-expires after ttl_days of inactivity. TTL is refreshed
    on every write so an active scout won't lose its dedup state.

    Args:
        key:      Redis key (e.g. "scout:seen:domains")
        ttl_days: Days before the set expires if no writes occur.
    """

    def __init__(self, key: str, ttl_days: int = 30):
        self.key         = key
        self.ttl_seconds = ttl_days * 86400
        self._r          = _get_client()

    def _refresh_ttl(self):
        self._r.expire(self.key, self.ttl_seconds)

    def is_new(self, value: str) -> bool:
        """Return True if this value has NOT been seen before."""
        return not bool(self._r.sismember(self.key, value))

    def mark(self, value: str) -> None:
        """Mark a value as seen."""
        self._r.sadd(self.key, value)
        self._refresh_ttl()

    def mark_many(self, values: list[str]) -> None:
        """Mark multiple values as seen in a single pipeline call."""
        if not values:
            return
        pipe = self._r.pipeline()
        pipe.sadd(self.key, *values)
        pipe.expire(self.key, self.ttl_seconds)
        pipe.execute()

    def filter_new(self, values: list[str]) -> list[str]:
        """Return only the values not yet seen. Does NOT mark them."""
        if not values:
            return []
        pipe = self._r.pipeline()
        for v in values:
            pipe.sismember(self.key, v)
        results = pipe.execute()
        return [v for v, seen in zip(values, results) if not seen]

    def count(self) -> int:
        """Return the number of items in the dedup set."""
        return self._r.scard(self.key)

    def reset(self) -> None:
        """Clear all dedup state. Use with caution — scouts will re-process everything."""
        self._r.delete(self.key)
