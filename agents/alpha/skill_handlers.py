"""
Alpha agent skill handlers — coordinator and sysadmin capabilities.

Handles task orchestration, Docker management, queue monitoring, and report generation.
"""

import json
import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

_KNOWN_AGENTS = ["alpha", "beta", "gamma", "delta"]


def _get_redis():
    try:
        import redis
        r = redis.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=2, socket_timeout=2,
        )
        r.ping()
        return r
    except Exception:
        return None


def handle_task_status(input_data: dict) -> dict:
    """
    Show recent dispatch activity and queue depths.
    Input: {} (no args needed)
    """
    r = _get_redis()
    recent = []
    queue_depths = {}

    if r:
        try:
            entries = r.lrange("dispatch:log", 0, 9)
            recent = [json.loads(e) for e in entries]
        except Exception:
            pass
        for agent in _KNOWN_AGENTS:
            try:
                queue_depths[agent] = r.llen(f"mesh:inbox:{agent}")
            except Exception:
                queue_depths[agent] = -1

    return {
        "status":       "ok",
        "redis":        r is not None,
        "recent_calls": recent,
        "queue_depths": queue_depths,
    }


def handle_docker_status(input_data: dict) -> dict:
    """List all Docker containers and their status."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=15,
        )
        containers = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            containers.append({
                "name":   parts[0] if len(parts) > 0 else "",
                "status": parts[1] if len(parts) > 1 else "",
                "image":  parts[2] if len(parts) > 2 else "",
            })
        return {"status": "ok", "containers": containers, "count": len(containers)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def handle_docker_restart(input_data: dict) -> dict:
    """
    Restart a Docker container by name.
    Input: {"container": "agent-gamma"}
    """
    container = input_data.get("container", "")
    if not container:
        return {"status": "error", "message": "container name required"}
    try:
        result = subprocess.run(
            ["docker", "restart", container],
            capture_output=True, text=True, timeout=60,
        )
        ok = result.returncode == 0
        return {
            "status":   "ok" if ok else "error",
            "container": container,
            "stdout":   result.stdout.strip(),
            "stderr":   result.stderr.strip(),
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def handle_docker_logs(input_data: dict) -> dict:
    """
    Fetch recent logs from a Docker container.
    Input: {"container": "agent-gamma", "tail": 100}
    """
    container = input_data.get("container", "")
    tail      = str(input_data.get("tail", 100))
    if not container:
        return {"status": "error", "message": "container name required"}
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", tail, container],
            capture_output=True, text=True, timeout=30,
        )
        logs = result.stdout + result.stderr
        return {
            "status":    "ok",
            "container": container,
            "logs":      logs[-20000:],  # cap at 20 KB
            "truncated": len(logs) > 20000,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def handle_report_generation(input_data: dict) -> dict:
    """
    Compile structured input into a markdown report.
    Input: {"title": "...", "sections": [{"heading": "...", "content": "..."}]}
    """
    title    = input_data.get("title", "Report")
    sections = input_data.get("sections", [])
    lines    = [f"# {title}", f"", f"*Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}*", ""]

    for sec in sections:
        heading = sec.get("heading", "")
        content = sec.get("content", "")
        if heading:
            lines.append(f"## {heading}")
        lines.append(content)
        lines.append("")

    report = "\n".join(lines)
    return {"status": "ok", "report": report, "chars": len(report)}


def handle_queue_status(input_data: dict) -> dict:
    """
    Check queue depths for all known agents.
    Input: {} (optional: {"agents": ["gamma", "delta"]})
    """
    r = _get_redis()
    if not r:
        return {"status": "error", "message": "Redis unavailable"}

    agents = input_data.get("agents", _KNOWN_AGENTS)
    depths = {}
    dead_letters = {}
    for agent in agents:
        try:
            depths[agent]      = r.llen(f"mesh:inbox:{agent}")
            dead_letters[agent] = r.llen(f"mesh:dead_letter:{agent}")
        except Exception:
            depths[agent]       = -1
            dead_letters[agent] = -1

    return {
        "status":       "ok",
        "queue_depths": depths,
        "dead_letters": dead_letters,
        "total_queued": sum(v for v in depths.values() if v >= 0),
    }


ALPHA_SKILL_HANDLERS = {
    "task_status":       handle_task_status,
    "docker_status":     handle_docker_status,
    "docker_restart":    handle_docker_restart,
    "docker_logs":       handle_docker_logs,
    "report_generation": handle_report_generation,
    "queue_status":      handle_queue_status,
}
