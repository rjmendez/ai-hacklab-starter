"""
scouts/checkpoint.py — Redis-backed checkpoint for long-running scouts.

Solves the timeout-and-resume problem: scouts save progress mid-run and
resume from where they left off after a restart, crash, or timeout.

Usage:
    from scouts.checkpoint import RedisCheckpoint

    cp = RedisCheckpoint("iris:scan-run-001")

    # Save progress
    cp.save({"last_offset": 1500, "processed": 1500, "total": 6000})

    # Resume on next run
    state = cp.load()
    if state:
        start_from = state["last_offset"]
    else:
        start_from = 0  # fresh start

    # Mark complete (clears checkpoint)
    cp.complete()
"""

import json
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


class RedisCheckpoint:
    """
    Redis-backed checkpoint for resumable scout runs.

    State is stored as JSON. A checkpoint that is never completed will
    expire automatically after ttl_hours (default 48h) — preventing
    stale state from blocking a fresh run indefinitely.

    Args:
        run_id:    Unique ID for this run (e.g. "iris:scan:2026-04-01")
        ttl_hours: How long to keep incomplete checkpoints.
    """

    def __init__(self, run_id: str, ttl_hours: int = 48):
        self.key      = f"checkpoint:{run_id}"
        self.ttl_secs = ttl_hours * 3600
        self._r       = _get_client()

    def save(self, state: dict) -> None:
        """
        Save checkpoint state. Merges with existing state (partial updates ok).

        Args:
            state: Dict of any JSON-serializable values.
        """
        existing = self.load() or {}
        existing.update(state)
        existing["_saved_at"] = time.time()
        self._r.setex(self.key, self.ttl_secs, json.dumps(existing))

    def load(self) -> dict | None:
        """
        Load checkpoint state. Returns None if no checkpoint exists.
        """
        raw = self._r.get(self.key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def exists(self) -> bool:
        """Return True if a checkpoint exists for this run."""
        return bool(self._r.exists(self.key))

    def complete(self) -> None:
        """
        Mark this run as complete — deletes the checkpoint.
        Call this at the end of a successful run.
        """
        self._r.delete(self.key)

    def ttl(self) -> int:
        """Return seconds until this checkpoint expires (-1 = no TTL, -2 = not found)."""
        return self._r.ttl(self.key)
