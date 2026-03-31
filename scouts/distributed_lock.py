"""
scouts/distributed_lock.py — Redis-backed distributed lock for mesh workers.

Uses the SET NX EX pattern. Faster than DB advisory locks, no DB load.
Token-based release ensures only the owner can unlock (safe under races).

Usage:
    from scouts.distributed_lock import DistributedLock

    with DistributedLock("my-worker:lock", ttl=60) as acquired:
        if not acquired:
            print("Another worker holds the lock — skipping")
        else:
            do_work()

    # Or non-context-manager style:
    lock = DistributedLock("my-worker:lock", ttl=60)
    if lock.acquire():
        try:
            do_work()
        finally:
            lock.release()
"""

import uuid
import time
import os
import redis

# Lua script: atomic check-and-delete (prevents another owner from releasing)
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def _get_client() -> redis.Redis:
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


class DistributedLock:
    """
    Redis SET NX EX distributed lock.

    Args:
        key:         Redis key (e.g. "worker:scan:lock")
        ttl:         Lock TTL in seconds. Lock auto-expires if holder crashes.
        retry:       Number of acquisition retries (0 = try once).
        retry_delay: Seconds between retries.
    """

    def __init__(
        self,
        key: str,
        ttl: int = 30,
        retry: int = 0,
        retry_delay: float = 1.0,
    ):
        self.key         = key
        self.ttl         = ttl
        self.retry       = retry
        self.retry_delay = retry_delay
        self._token: str | None = None
        self._r = _get_client()
        self._release_script = self._r.register_script(_RELEASE_SCRIPT)

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True if acquired."""
        token = str(uuid.uuid4())
        for attempt in range(self.retry + 1):
            if self._r.set(self.key, token, nx=True, ex=self.ttl):
                self._token = token
                return True
            if attempt < self.retry:
                time.sleep(self.retry_delay)
        return False

    def release(self) -> bool:
        """Release the lock. Only works if we own it. Returns True if released."""
        if self._token is None:
            return False
        result = self._release_script(keys=[self.key], args=[self._token])
        self._token = None
        return bool(result)

    def extend(self, additional_ttl: int) -> bool:
        """Extend the lock TTL if we still own it."""
        if self._token is None:
            return False
        current = self._r.get(self.key)
        if current != self._token:
            return False
        self._r.expire(self.key, additional_ttl)
        return True

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
