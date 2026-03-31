#!/usr/bin/env python3
"""
queue/monitor.py — Live mesh queue depth monitor.

Shows inbox and dead-letter queue sizes for all known agents.
Optionally refreshes on a watch interval.

Usage:
    python3 queue/monitor.py
    python3 queue/monitor.py --watch
    python3 queue/monitor.py --watch --interval 3
    python3 queue/monitor.py --agent gamma
"""

import argparse
import os
import sys
import time

try:
    import redis
except ImportError:
    print("ERROR: redis-py not installed. Run: pip install redis")
    sys.exit(1)

KNOWN_AGENTS: list[str] = os.environ.get(
    "MESH_AGENTS", "alpha,beta,gamma,delta"
).split(",")


def get_client() -> redis.Redis:
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "redis"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


def render_table(r: redis.Redis, agents: list[str]) -> str:
    col_w = 14
    header = f"{'AGENT':<{col_w}} {'INBOX':>{col_w}} {'DEAD-LETTER':>{col_w}}"
    sep    = "─" * len(header)
    lines  = [header, sep]

    total_inbox = 0
    total_dead  = 0

    for agent in agents:
        try:
            inbox = r.llen(f"mesh:inbox:{agent}")
            dead  = r.llen(f"mesh:dead_letter:{agent}")
        except Exception:
            inbox, dead = -1, -1

        inbox_s = str(inbox) if inbox >= 0 else "ERR"
        dead_s  = str(dead)  if dead  >= 0 else "ERR"

        # Highlight non-zero dead-letter queues
        if dead > 0:
            dead_s = f"⚠️  {dead_s}"

        lines.append(f"{agent:<{col_w}} {inbox_s:>{col_w}} {dead_s:>{col_w}}")

        if inbox >= 0:
            total_inbox += inbox
        if dead >= 0:
            total_dead  += dead

    lines.append(sep)
    lines.append(f"{'TOTAL':<{col_w}} {total_inbox:>{col_w}} {str(total_dead):>{col_w}}")
    lines.append(f"\n  Updated: {time.strftime('%H:%M:%S')}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mesh queue monitor")
    parser.add_argument("--watch",    action="store_true", help="Refresh continuously")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Refresh interval in seconds (default: 5)")
    parser.add_argument("--agent",    help="Show only a specific agent")
    args = parser.parse_args()

    agents = [args.agent] if args.agent else KNOWN_AGENTS

    try:
        r = get_client()
        r.ping()
    except Exception as exc:
        print(f"ERROR: Cannot connect to Redis — {exc}")
        print("Check REDIS_HOST / REDIS_PORT / REDIS_PASSWORD env vars.")
        sys.exit(1)

    if args.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")  # clear screen
                print(render_table(r, agents))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        print(render_table(r, agents))


if __name__ == "__main__":
    main()
