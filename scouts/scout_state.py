#!/usr/bin/env python3
"""
scout_state.py — Shared Redis state-reporting module for all scouts
===================================================================
Import this at the top of any scout to get set_running / set_idle / set_error / publish_alert.

Key schema:
  scout:{name}:status   = "running" | "idle" | "error"
  scout:{name}:last_run = ISO timestamp
  scout:{name}:result   = JSON {count, details}
  scout:{name}:error    = error message
  scout:{name}:pid      = process id

Pub/sub channel: mnemosyne:alerts
Alert format: {"severity": "info|warning|critical", "source": "scout_name", "message": "...", "ts": "ISO"}
"""

import json
import os
import sys
from datetime import datetime, timezone

_REDIS_HOSTS = [
    # Primary: local audit-redis (always available in container)
    {"host": os.environ.get("REDIS_HOST","localhost"), "port": int(os.environ.get("REDIS_PORT","6379")), "password": os.environ.get("REDIS_PASSWORD") or None},
    # Fallback: Oxalis Redis master (available when Oxalis is online)
    {"host": __import__("os").environ.get("REDIS_HOST2","localhost"), "port": int(__import__("os").environ.get("REDIS_PORT2","6379")), "password": __import__("os").environ.get("REDIS_PASSWORD") or None},
]

STATE_TTL = 86400  # 24 hours


def get_redis():
    """Return a connected Redis client, trying hosts in priority order."""
    import redis
    last_exc = None
    for cfg in _REDIS_HOSTS:
        try:
            r = redis.Redis(
                host=cfg["host"],
                port=cfg["port"],
                password=cfg["password"],
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            r.ping()
            return r
        except Exception as exc:
            last_exc = exc
    raise last_exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_keys(r, scout_name: str, mapping: dict) -> None:
    """Write multiple keys for a scout, applying TTL to each."""
    prefix = f"scout:{scout_name}"
    for field, value in mapping.items():
        key = f"{prefix}:{field}"
        r.set(key, value, ex=STATE_TTL)


# ── Public API ──────────────────────────────────────────────────────────────────

def set_running(scout_name: str, details: dict = None) -> None:
    """Call at the start of a scout run."""
    try:
        r = get_redis()
        mapping = {
            "status":   "running",
            "last_run": _now_iso(),
            "pid":      str(os.getpid()),
            "error":    "",  # clear any previous error
        }
        if details:
            mapping["result"] = json.dumps(details)
        _set_keys(r, scout_name, mapping)
    except Exception as exc:
        # Never block a scout because Redis is down
        print(f"[scout_state] set_running({scout_name}) failed: {exc}", file=sys.stderr)


def set_idle(scout_name: str, result: dict = None) -> None:
    """Call at the end of a successful scout run."""
    try:
        r = get_redis()
        mapping = {
            "status":   "idle",
            "last_run": _now_iso(),
            "error":    "",
        }
        if result:
            mapping["result"] = json.dumps(result)
        _set_keys(r, scout_name, mapping)
    except Exception as exc:
        print(f"[scout_state] set_idle({scout_name}) failed: {exc}", file=sys.stderr)


def set_error(scout_name: str, error: str) -> None:
    """Call on failure."""
    try:
        r = get_redis()
        mapping = {
            "status":   "error",
            "last_run": _now_iso(),
            "error":    str(error)[:1000],  # cap at 1k chars
        }
        _set_keys(r, scout_name, mapping)
    except Exception as exc:
        print(f"[scout_state] set_error({scout_name}) failed: {exc}", file=sys.stderr)


def get_state(scout_name: str) -> dict:
    """Get the current state dict for a single scout."""
    try:
        r = get_redis()
        prefix = f"scout:{scout_name}"
        return {
            "name":     scout_name,
            "status":   r.get(f"{prefix}:status")   or "unknown",
            "last_run": r.get(f"{prefix}:last_run")  or None,
            "result":   r.get(f"{prefix}:result")    or None,
            "error":    r.get(f"{prefix}:error")     or None,
            "pid":      r.get(f"{prefix}:pid")       or None,
        }
    except Exception as exc:
        return {
            "name":   scout_name,
            "status": f"redis_error: {exc}",
        }


def get_all_states() -> dict:
    """
    Collect state for every scout currently tracked in Redis.
    Discovers scouts by scanning for scout:*:status keys, then merges
    with a hardcoded list of known scouts so offline scouts show as 'unknown'.
    """
    KNOWN_SCOUTS = ["iris", "rex", "atlas", "hermes", "rate_scout", "ratchet", "mnemosyne"]

    states = {}
    try:
        r = get_redis()

        # Discover dynamic scouts from Redis keys
        live_keys = r.keys("scout:*:status")
        discovered = set()
        for k in live_keys:
            parts = k.split(":")
            if len(parts) == 3:
                discovered.add(parts[1])

        all_scouts = sorted(set(KNOWN_SCOUTS) | discovered)

        for name in all_scouts:
            states[name] = get_state(name)

    except Exception as exc:
        # Redis down — return known scouts with error status
        for name in KNOWN_SCOUTS:
            states[name] = {"name": name, "status": f"redis_error: {exc}"}

    return states


# ── Pub/Sub ─────────────────────────────────────────────────────────────────────

ALERT_CHANNEL = "mnemosyne:alerts"


def publish_alert(message: str, severity: str = "info", source: str = "unknown") -> None:
    """
    Publish an alert to the mnemosyne:alerts pub/sub channel.
    Also appends to the mnemosyne:alerts list for durability (consumers that
    aren't live can still read it on next wake-up).
    """
    payload = json.dumps({
        "severity": severity,
        "source":   source,
        "message":  message,
        "ts":       _now_iso(),
    })
    try:
        r = get_redis()
        r.publish(ALERT_CHANNEL, payload)
        # Also keep a bounded list (last 500 alerts)
        r.lpush(ALERT_CHANNEL, payload)
        r.ltrim(ALERT_CHANNEL, 0, 499)
    except Exception as exc:
        print(f"[scout_state] publish_alert failed: {exc}", file=sys.stderr)


def subscribe_alerts(callback) -> None:
    """
    Subscribe to mnemosyne:alerts.  Calls callback(parsed_dict) for each message.
    Blocks forever — run in a thread or via --listen CLI mode.

    callback signature: callback(alert: dict) -> None
      alert = {"severity": ..., "source": ..., "message": ..., "ts": ...}
    """
    try:
        r = get_redis()
        pubsub = r.pubsub()
        pubsub.subscribe(ALERT_CHANNEL)
        print(f"[scout_state] Subscribed to {ALERT_CHANNEL} — listening …", file=sys.stderr)
        for raw in pubsub.listen():
            if raw["type"] != "message":
                continue
            try:
                alert = json.loads(raw["data"])
            except (json.JSONDecodeError, TypeError):
                alert = {
                    "severity": "unknown",
                    "source":   "unknown",
                    "message":  str(raw["data"]),
                    "ts":       _now_iso(),
                }
            try:
                callback(alert)
            except Exception as cb_exc:
                print(f"[scout_state] callback error: {cb_exc}", file=sys.stderr)
    except Exception as exc:
        print(f"[scout_state] subscribe_alerts failed: {exc}", file=sys.stderr)


# ── __main__ : print all scout states ──────────────────────────────────────────

if __name__ == "__main__":
    states = get_all_states()

    header = f"{'SCOUT':<16} {'STATUS':<14} {'LAST RUN':<32} {'PID':<8} {'RESULT / ERROR'}"
    print("=" * 100)
    print("  scout_state — All Scout States")
    print(f"  Queried: {_now_iso()}")
    print("=" * 100)
    print(header)
    print("-" * 100)

    for name, s in sorted(states.items()):
        status   = s.get("status", "unknown")
        last_run = (s.get("last_run") or "never")[:30]
        pid      = s.get("pid") or "-"
        result   = s.get("result") or ""
        error    = s.get("error") or ""
        detail   = (error[:60] if error else result[:60]) or "-"

        status_icon = {
            "running": "🟡",
            "idle":    "✅",
            "error":   "🔴",
            "unknown": "⚪",
        }.get(status, "❓")

        print(f"{name:<16} {status_icon} {status:<12} {last_run:<32} {pid:<8} {detail}")

    print("=" * 100)
