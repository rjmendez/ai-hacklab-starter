#!/usr/bin/env python3
"""
checkpoint.py — Redis-backed checkpoint for long-running scouts.

Solves the Rex timeout problem: scouts can save progress mid-run and
resume from where they left off after a restart or timeout.
"""

import json
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


# ── RedisCheckpoint ───────────────────────────────────────────────────────────

class RedisCheckpoint:
    """
    Allows long-running scouts to save progress and resume after restart/timeout.

    Usage pattern for Rex:
        cp = RedisCheckpoint("rex:bucket-audit:2026-03-28")
        if cp.exists():
            state = cp.load()
            start_from = state['last_table']
        else:
            start_from = None

        # ... do work ...
        cp.save({'last_table': current_table, 'rows_processed': n})

        cp.clear()  # on completion
    """

    def __init__(self, job_id: str, ttl_hours: int = 24):
        """
        :param job_id:    Unique job identifier, e.g. "rex:bucket-audit:2026-03-28"
        :param ttl_hours: How long to keep checkpoint before auto-expiring
        """
        self.job_id = job_id
        self.key = f"checkpoint:{job_id}"
        self.ttl_seconds = ttl_hours * 3600
        self._r = _get_redis()

    def save(self, progress: dict):
        """
        Save current progress state.
        Automatically adds a 'saved_at' timestamp for debugging.
        """
        data = dict(progress)
        data['_saved_at'] = time.time()
        data['_job_id'] = self.job_id
        self._r.setex(self.key, self.ttl_seconds, json.dumps(data))

    def load(self) -> dict | None:
        """
        Load saved progress. Returns None if no checkpoint exists.
        """
        raw = self._r.get(self.key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    def clear(self):
        """Remove checkpoint (call on successful job completion)."""
        self._r.delete(self.key)

    def exists(self) -> bool:
        """Check if a checkpoint exists for this job."""
        return bool(self._r.exists(self.key))

    def ttl(self) -> int:
        """Return remaining TTL in seconds (-1 = no expiry, -2 = key gone)."""
        return self._r.ttl(self.key)

    def update(self, **kwargs):
        """
        Merge kwargs into existing checkpoint state (load → merge → save).
        Creates a new checkpoint if none exists.
        """
        state = self.load() or {}
        state.update(kwargs)
        self.save(state)

    def __repr__(self):
        exists = self.exists()
        ttl = self.ttl() if exists else -2
        return f"RedisCheckpoint(job_id={self.job_id!r}, exists={exists}, ttl={ttl}s)"


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== RedisCheckpoint self-test ===")

    JOB_ID = "test:selftest:checkpoint"
    cp = RedisCheckpoint(JOB_ID, ttl_hours=1)

    # Clean state
    cp.clear()
    assert not cp.exists(), "Expected no checkpoint initially"
    assert cp.load() is None, "Expected None from load() when empty"
    print("✓ No checkpoint on fresh start")

    # Save state
    cp.save({'last_table': 'firebase_targets', 'rows_processed': 42})
    assert cp.exists(), "Expected checkpoint to exist after save()"
    print("✓ save() persists checkpoint")

    # Load state
    state = cp.load()
    assert state is not None
    assert state['last_table'] == 'firebase_targets'
    assert state['rows_processed'] == 42
    assert '_saved_at' in state  # auto-added timestamp
    assert '_job_id' in state
    print(f"✓ load() returns correct state: last_table={state['last_table']}, rows={state['rows_processed']}")

    # TTL check
    ttl = cp.ttl()
    assert 0 < ttl <= 3600, f"Expected TTL ~3600s, got {ttl}"
    print(f"✓ TTL = {ttl}s (correct)")

    # update() merges
    cp.update(rows_processed=100, last_row_id=9999)
    state2 = cp.load()
    assert state2['last_table'] == 'firebase_targets'  # preserved
    assert state2['rows_processed'] == 100             # updated
    assert state2['last_row_id'] == 9999               # new field
    print("✓ update() merges correctly")

    # clear on completion
    cp.clear()
    assert not cp.exists()
    assert cp.load() is None
    print("✓ clear() removes checkpoint")

    # Simulate the Rex usage pattern
    print("\n--- Simulating Rex resume pattern ---")
    cp2 = RedisCheckpoint("rex:bucket-audit:test-run", ttl_hours=24)
    cp2.clear()

    if cp2.exists():
        start_from = cp2.load()['last_table']
        print(f"  Resuming from: {start_from}")
    else:
        start_from = None
        print("  No checkpoint — starting fresh")

    for i, table in enumerate(['targets', 'buckets', 'findings']):
        # simulate work
        cp2.save({'last_table': table, 'rows_processed': i * 100})
        print(f"  Processed table '{table}', checkpoint saved")

    cp2.clear()
    print("  Job complete, checkpoint cleared")

    print(f"\n✅ All RedisCheckpoint tests passed.")
