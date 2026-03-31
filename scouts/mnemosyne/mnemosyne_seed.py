#!/usr/bin/env python3
"""
scouts/mnemosyne/mnemosyne_seed.py — Mnemosyne: dashboard and notifications.

Mnemosyne is the mesh's memory keeper and human interface. She turns
machine churn into something readable, routes alerts to the right channel,
and maintains a daily operational summary.

What Mnemosyne does:
  - Mesh status: poll all agent health endpoints, summarize
  - Daily brief: compile agent health + queue depths + recent activity
  - Notifications: route alerts to configured channels (webhook, file, stdout)
  - Memory index: summarize what's in Redis for quick operator reference

Usage:
    python3 scouts/mnemosyne/mnemosyne_seed.py --mesh-status
    python3 scouts/mnemosyne/mnemosyne_seed.py --daily-brief
    python3 scouts/mnemosyne/mnemosyne_seed.py --notify "Queue depth critical on gamma" --severity critical
    python3 scouts/mnemosyne/mnemosyne_seed.py --memory-index
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [mnemosyne] %(levelname)s %(message)s")
log = logging.getLogger("mnemosyne")

REPO_ROOT   = Path(__file__).parent.parent.parent
REPORTS_DIR = REPO_ROOT / "workspace" / "planning"

REDIS_HOST  = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT  = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASS  = os.environ.get("REDIS_PASSWORD") or None

NOTIFY_WEBHOOK = os.environ.get("MNEMOSYNE_WEBHOOK_URL", "")
NOTIFY_FILE    = os.environ.get("MNEMOSYNE_NOTIFY_FILE", "")

AGENT_PORTS = {
    "alpha": int(os.environ.get("ALPHA_PORT", "8201")),
    "beta":  int(os.environ.get("BETA_PORT",  "8202")),
    "gamma": int(os.environ.get("GAMMA_PORT", "8203")),
    "delta": int(os.environ.get("DELTA_PORT", "8204")),
}
AGENT_TOKEN  = os.environ.get("AGENT_TOKEN", "")
AGENT_HOST   = os.environ.get("AGENT_HOST", "localhost")


def _get_redis():
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS,
                        decode_responses=True, socket_connect_timeout=3)
        r.ping()
        return r
    except Exception:
        return None


# ── Mesh status ───────────────────────────────────────────────────────────────

def _check_agent(name: str, port: int) -> dict:
    url = f"http://{AGENT_HOST}:{port}/health"
    headers = {}
    if AGENT_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    t0  = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data    = json.loads(resp.read().decode())
            latency = int((time.monotonic() - t0) * 1000)
            return {
                "agent":    name,
                "status":   "up",
                "latency_ms": latency,
                "skills":   data.get("skills_loaded", "?"),
                "totp":     data.get("totp_2fa", False),
            }
    except Exception as exc:
        return {"agent": name, "status": "down", "error": str(exc)[:80]}


def mesh_status() -> dict:
    """Poll all agents and return health summary."""
    agents = []
    for name, port in AGENT_PORTS.items():
        agents.append(_check_agent(name, port))
    up   = sum(1 for a in agents if a["status"] == "up")
    down = len(agents) - up
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary":   f"{up}/{len(agents)} agents up",
        "healthy":   down == 0,
        "agents":    agents,
    }


# ── Queue summary ─────────────────────────────────────────────────────────────

def queue_summary(r) -> dict:
    if not r:
        return {"status": "redis_unavailable"}
    depths = {}
    dead   = {}
    for agent in AGENT_PORTS:
        try:
            depths[agent] = r.llen(f"mesh:inbox:{agent}")
            dead[agent]   = r.llen(f"mesh:dead_letter:{agent}")
        except Exception:
            depths[agent] = dead[agent] = -1
    return {
        "queue_depths": depths,
        "dead_letters": dead,
        "total_queued": sum(v for v in depths.values() if v >= 0),
        "total_dead":   sum(v for v in dead.values() if v >= 0),
    }


# ── Daily brief ───────────────────────────────────────────────────────────────

def daily_brief() -> str:
    r      = _get_redis()
    status = mesh_status()
    queues = queue_summary(r)
    ts     = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    lines = [f"# Mesh Daily Brief — {ts}", ""]

    # Agent health
    lines += ["## Agent Health", ""]
    for a in status["agents"]:
        icon = "✅" if a["status"] == "up" else "❌"
        if a["status"] == "up":
            lines.append(f"  {icon} **{a['agent']}** — {a['latency_ms']}ms, {a['skills']} skills, totp={a['totp']}")
        else:
            lines.append(f"  {icon} **{a['agent']}** — DOWN: {a.get('error', '')}")
    lines.append("")

    # Queue depths
    lines += ["## Queue Depths", ""]
    if "queue_depths" in queues:
        for agent, depth in queues["queue_depths"].items():
            dead = queues["dead_letters"].get(agent, 0)
            flag = " ⚠️ dead-letter" if dead > 0 else ""
            lines.append(f"  {agent}: inbox={depth}, dead={dead}{flag}")
    else:
        lines.append("  Redis unavailable — queue depths unknown")
    lines.append("")

    # Recent A2A activity
    if r:
        lines += ["## Recent A2A Activity (last 5 calls per agent)", ""]
        for agent in AGENT_PORTS:
            try:
                entries = r.lrange(f"a2a:log:{agent}", 0, 4)
                if entries:
                    lines.append(f"  **{agent}:**")
                    for e in entries:
                        try:
                            rec = json.loads(e)
                            ok  = "✅" if rec.get("success") else "❌"
                            lines.append(f"    {ok} {rec.get('skill','?')} from {rec.get('caller','?')} ({rec.get('latency_ms','?')}ms)")
                        except Exception:
                            pass
            except Exception:
                pass
        lines.append("")

    # Health summary
    lines += [
        "---",
        "",
        f"**Status:** {status['summary']}",
        f"**Total queued:** {queues.get('total_queued', '?')}",
        f"**Dead letters:** {queues.get('total_dead', '?')}",
    ]

    return "\n".join(lines)


# ── Notifications ─────────────────────────────────────────────────────────────

SEVERITY_LEVELS = {"debug": 0, "info": 1, "warning": 2, "critical": 3}


def notify(message: str, severity: str = "info", agent: str = "mnemosyne") -> None:
    """
    Route a notification to configured channels.
    Configure via MNEMOSYNE_WEBHOOK_URL and/or MNEMOSYNE_NOTIFY_FILE env vars.
    """
    ts      = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = {
        "ts":       ts,
        "agent":    agent,
        "severity": severity,
        "message":  message,
    }

    # Always log locally
    level = {"debug": log.debug, "info": log.info,
             "warning": log.warning, "critical": log.error}.get(severity, log.info)
    level("[%s] %s", severity.upper(), message)

    # Webhook delivery
    if NOTIFY_WEBHOOK:
        try:
            data = json.dumps(payload).encode("utf-8")
            req  = urllib.request.Request(
                NOTIFY_WEBHOOK, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            log.warning("Webhook delivery failed: %s", exc)

    # File delivery
    if NOTIFY_FILE:
        try:
            with open(NOTIFY_FILE, "a") as f:
                f.write(json.dumps(payload) + "\n")
        except Exception as exc:
            log.warning("File notification failed: %s", exc)


# ── Memory index ──────────────────────────────────────────────────────────────

def memory_index(r) -> dict:
    """Summarize what's in Redis: key counts by prefix."""
    if not r:
        return {"status": "redis_unavailable"}
    try:
        prefixes = ["mesh:inbox:", "mesh:dead_letter:", "a2a:log:", "memory:",
                    "checkpoint:", "ratelimit:", "dispatch:", "spend:"]
        summary = {}
        for prefix in prefixes:
            keys = list(r.scan_iter(f"{prefix}*", count=100))
            if keys:
                summary[prefix.rstrip(":")] = len(keys)
        return {"status": "ok", "key_counts": summary, "total_keys": sum(summary.values())}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Mnemosyne — dashboard and notifications")
    parser.add_argument("--mesh-status",  action="store_true", help="Show mesh health")
    parser.add_argument("--daily-brief",  action="store_true", help="Generate daily brief")
    parser.add_argument("--notify",       metavar="MSG",       help="Send a notification")
    parser.add_argument("--severity",     default="info",
                        choices=["debug", "info", "warning", "critical"])
    parser.add_argument("--memory-index", action="store_true", help="Summarize Redis key space")
    parser.add_argument("--save",         action="store_true", help="Save brief to workspace/planning/")
    args = parser.parse_args()

    if args.mesh_status:
        result = mesh_status()
        print(json.dumps(result, indent=2))

    elif args.daily_brief:
        brief = daily_brief()
        print(brief)
        if args.save:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            ts   = time.strftime("%Y-%m-%d_%H%M")
            path = REPORTS_DIR / f"daily-brief-{ts}.md"
            path.write_text(brief)
            print(f"\n✅ Saved to {path}")

    elif args.notify:
        notify(args.notify, args.severity)

    elif args.memory_index:
        r      = _get_redis()
        result = memory_index(r)
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
