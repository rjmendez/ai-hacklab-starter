"""
distributed_lock.py — Redis-backed distributed lock for audit pipeline workers.

Uses SET NX EX pattern. Faster than Postgres advisory locks, no DB load.
Token-based release ensures only the owner can unlock.
"""

import contextlib
import time
import uuid

import redis

# ---------------------------------------------------------------------------
# Module-level Redis client (lazy, shared across lock instances)
# ---------------------------------------------------------------------------
_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    import os
    global _client
    if _client is None:
        _client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    return _client


# ---------------------------------------------------------------------------
# Lua script — atomic check-and-delete (release)
# ---------------------------------------------------------------------------
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Lua script — atomic check-and-extend (extend TTL only if we own the lock)
EXTEND_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class RedisLock:
    """
    Distributed lock using Redis SET NX EX.

    Usage (context manager):
        lock = RedisLock("bucket:s3://example.com/path")
        with lock(timeout_seconds=10):
            # critical section

    Usage (manual):
        lock = RedisLock("some-key")
        if lock.acquire(timeout_seconds=5):
            try:
                ...
            finally:
                lock.release()
    """

    def __init__(
        self,
        key: str,
        ttl_seconds: int = 300,
        retry_interval: float = 0.1,
    ):
        self.key = f"lock:{key}"
        self.ttl = ttl_seconds
        self.retry_interval = retry_interval
        # Unique token — guarantees only the owner can release
        self.token = str(uuid.uuid4())
        self._release_script: redis.client.Script | None = None
        self._extend_script: redis.client.Script | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _r(self) -> redis.Redis:
        return _get_client()

    def _get_release_script(self) -> redis.client.Script:
        if self._release_script is None:
            self._release_script = self._r().register_script(RELEASE_SCRIPT)
        return self._release_script

    def _get_extend_script(self) -> redis.client.Script:
        if self._extend_script is None:
            self._extend_script = self._r().register_script(EXTEND_SCRIPT)
        return self._extend_script

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, timeout_seconds: float = 30.0) -> bool:
        """
        Try to acquire the lock, retrying until timeout_seconds elapses.

        Returns True if lock was acquired, False if timed out.
        """
        deadline = time.monotonic() + timeout_seconds
        r = self._r()

        while True:
            acquired = r.set(self.key, self.token, nx=True, ex=self.ttl)
            if acquired:
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            sleep_time = min(self.retry_interval, remaining)
            time.sleep(sleep_time)

    def release(self) -> bool:
        """
        Release the lock atomically. Only works if this instance holds it.

        Returns True if released, False if lock was not held by this instance.
        """
        try:
            result = self._get_release_script()(
                keys=[self.key], args=[self.token]
            )
            return bool(result)
        except redis.RedisError:
            return False

    def extend(self, additional_seconds: int) -> bool:
        """
        Extend the lock TTL by additional_seconds if we still hold it.

        Returns True on success.
        """
        try:
            result = self._get_extend_script()(
                keys=[self.key],
                args=[self.token, str(self.ttl + additional_seconds)],
            )
            return bool(result)
        except redis.RedisError:
            return False

    def is_held(self) -> bool:
        """Return True if this instance currently holds the lock."""
        try:
            return self._r().get(self.key) == self.token
        except redis.RedisError:
            return False

    @contextlib.contextmanager
    def __call__(self, timeout_seconds: float = 30.0):
        """
        Context manager usage::

            lock = RedisLock("my-key")
            with lock(timeout_seconds=10):
                do_work()

        Raises TimeoutError if the lock cannot be acquired within timeout.
        """
        acquired = self.acquire(timeout_seconds)
        if not acquired:
            raise TimeoutError(
                f"Could not acquire lock {self.key} within {timeout_seconds}s"
            )
        try:
            yield self
        finally:
            self.release()


# ---------------------------------------------------------------------------
# Self-test (run with: python3 distributed_lock.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import threading

    print("=== RedisLock self-test ===\n")

    # ---- Test 1: basic acquire / release ----
    print("[1] Basic acquire/release")
    lock = RedisLock("test:basic", ttl_seconds=10)
    assert lock.acquire(timeout_seconds=5), "Should acquire"
    assert lock.is_held(), "Should be held after acquire"
    assert lock.release(), "Should release cleanly"
    assert not lock.is_held(), "Should not be held after release"
    print("    PASS\n")

    # ---- Test 2: context manager ----
    print("[2] Context manager")
    lock2 = RedisLock("test:ctx", ttl_seconds=10)
    with lock2(timeout_seconds=5):
        assert lock2.is_held(), "Should be held inside context"
    assert not lock2.is_held(), "Should be released after context"
    print("    PASS\n")

    # ---- Test 3: extend ----
    print("[3] Extend TTL")
    lock3 = RedisLock("test:extend", ttl_seconds=10)
    assert lock3.acquire(timeout_seconds=5)
    assert lock3.extend(30), "Extend should succeed while held"
    assert lock3.release()
    print("    PASS\n")

    # ---- Test 4: contention — second lock on same key should fail fast ----
    print("[4] Contention (second lock same key, short timeout)")
    lock_a = RedisLock("test:contention", ttl_seconds=15)
    lock_b = RedisLock("test:contention", ttl_seconds=15)
    assert lock_a.acquire(timeout_seconds=5), "lock_a should acquire"
    result_b = lock_b.acquire(timeout_seconds=0.3)  # should fail fast
    assert not result_b, "lock_b should NOT acquire while lock_a holds it"
    assert lock_a.release(), "lock_a releases"
    # Now lock_b should succeed
    assert lock_b.acquire(timeout_seconds=5), "lock_b should acquire after release"
    lock_b.release()
    print("    PASS\n")

    # ---- Test 5: only owner can release ----
    print("[5] Non-owner cannot release")
    lock_owner = RedisLock("test:owner", ttl_seconds=10)
    lock_other = RedisLock("test:owner", ttl_seconds=10)
    lock_other.token = "fake-token"  # tamper
    assert lock_owner.acquire(timeout_seconds=5)
    assert not lock_other.release(), "Non-owner release should return False"
    assert lock_owner.is_held(), "Owner lock should still be held"
    lock_owner.release()
    print("    PASS\n")

    # ---- Test 6: concurrent contention via threads ----
    print("[6] Thread contention — only one thread enters critical section at a time")
    counter = {"value": 0, "violations": 0}
    counter_lock = threading.Lock()
    inside = {"count": 0}

    def worker(worker_id: int):
        lk = RedisLock("test:threads", ttl_seconds=10)
        if lk.acquire(timeout_seconds=10):
            try:
                with counter_lock:
                    inside["count"] += 1
                    if inside["count"] > 1:
                        counter["violations"] += 1
                time.sleep(0.05)
                with counter_lock:
                    inside["count"] -= 1
                    counter["value"] += 1
            finally:
                lk.release()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert counter["violations"] == 0, f"Mutual exclusion violated {counter['violations']} times"
    print(f"    {counter['value']}/8 workers completed, 0 violations")
    print("    PASS\n")

    print("=== All tests passed ===")
