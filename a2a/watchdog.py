#!/usr/bin/env python3
"""
a2a/watchdog.py — A2A server watchdog for the agent mesh.

Checks each agent's A2A health endpoint. If an agent is unreachable,
attempts to restart it via its configured start command. Logs all events
to a local file and optionally to the telemetry DB.

Designed to run as a cron job every 1–5 minutes:
    */2 * * * * /usr/bin/python3 /app/a2a/watchdog.py >> /tmp/watchdog.log 2>&1

Usage:
    python3 a2a/watchdog.py
    python3 a2a/watchdog.py --agent gamma
    python3 a2a/watchdog.py --dry-run
    python3 a2a/watchdog.py --once

Environment (per-agent, via .env or docker environment):
    AGENT_NAME          — which agent to watch (default: check all)
    AGENT_TOKEN         — bearer token for health check auth
    AGENT_TOTP_SEED     — TOTP seed (if TOTP is enabled on the server)
    A2A_PORT            — port the agent listens on (default: 8200)
    WATCHDOG_LOG_FILE   — log file path (default: /tmp/watchdog-<agent>.log)
    WATCHDOG_INTERVAL   — seconds between checks in daemon mode (default: 30)
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Agent configs ─────────────────────────────────────────────────────────────
# Override any of these via environment variables.
# In Docker: the compose file injects AGENT_NAME, AGENT_TOKEN, etc.

DEFAULT_PORT = int(os.environ.get("A2A_PORT", "8200"))

AGENTS: dict[str, dict] = {
    "alpha": {
        "url":       f"http://localhost:{os.environ.get('A2A_PORT', '8201')}",
        "start_cmd": ["python3", "a2a/server.py"],
        "pid_file":  "/tmp/agent-alpha.pid",
        "log_file":  "/tmp/agent-alpha.log",
    },
    "beta": {
        "url":       f"http://localhost:{os.environ.get('A2A_PORT', '8202')}",
        "start_cmd": ["python3", "a2a/server.py"],
        "pid_file":  "/tmp/agent-beta.pid",
        "log_file":  "/tmp/agent-beta.log",
    },
    "gamma": {
        "url":       f"http://localhost:{os.environ.get('A2A_PORT', '8203')}",
        "start_cmd": ["python3", "a2a/server.py"],
        "pid_file":  "/tmp/agent-gamma.pid",
        "log_file":  "/tmp/agent-gamma.log",
    },
    "delta": {
        "url":       f"http://localhost:{os.environ.get('A2A_PORT', '8204')}",
        "start_cmd": ["python3", "a2a/server.py"],
        "pid_file":  "/tmp/agent-delta.pid",
        "log_file":  "/tmp/agent-delta.log",
    },
}

# If running inside a container, override with env-based single-agent config
_env_agent = os.environ.get("AGENT_NAME")
if _env_agent:
    _port = os.environ.get("A2A_PORT", "8200")
    AGENTS = {
        _env_agent: {
            "url":       f"http://localhost:{_port}",
            "start_cmd": ["python3", "a2a/server.py"],
            "pid_file":  f"/tmp/agent-{_env_agent}.pid",
            "log_file":  f"/tmp/agent-{_env_agent}.log",
        }
    }


# ── Health check ──────────────────────────────────────────────────────────────

def _get_totp_code() -> str | None:
    seed = os.environ.get("AGENT_TOTP_SEED", "")
    if not seed:
        return None
    try:
        import pyotp
        return pyotp.TOTP(seed).now()
    except Exception:
        return None


def check_health(agent_name: str, config: dict) -> tuple[bool, str]:
    """
    Check agent health via /health endpoint.
    Returns (is_healthy, detail_message).
    """
    import urllib.request
    import urllib.error

    url   = config["url"].rstrip("/") + "/health"
    token = os.environ.get("AGENT_TOKEN", "")
    totp  = _get_totp_code()

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if totp:
        headers["X-TOTP"] = totp

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            if resp.status == 200 and data.get("status") == "ok":
                return True, f"healthy — skills={data.get('skills_loaded', '?')}"
            return False, f"unhealthy response: {data}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, f"unreachable: {exc}"


# ── Process management ────────────────────────────────────────────────────────

def _read_pid(pid_file: str) -> int | None:
    try:
        pid = int(Path(pid_file).read_text().strip())
        os.kill(pid, 0)  # signal 0 = check if process exists
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def _write_pid(pid_file: str, pid: int) -> None:
    Path(pid_file).write_text(str(pid))


def stop_agent(agent_name: str, config: dict, dry_run: bool = False) -> bool:
    """Kill the agent process if a PID file exists."""
    pid = _read_pid(config["pid_file"])
    if pid is None:
        log.info("[%s] No PID found — nothing to stop", agent_name)
        return True
    log.info("[%s] Stopping PID %d", agent_name, pid)
    if not dry_run:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            # Force kill if still alive
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except Exception as exc:
            log.warning("[%s] Stop failed: %s", agent_name, exc)
            return False
    return True


def start_agent(agent_name: str, config: dict, dry_run: bool = False) -> bool:
    """Start the agent process using its configured start_cmd."""
    log_path = config["log_file"]
    cmd      = config["start_cmd"]

    log.info("[%s] Starting: %s", agent_name, " ".join(cmd))
    if dry_run:
        log.info("[%s] (dry-run — not actually starting)", agent_name)
        return True

    try:
        with open(log_path, "a") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        _write_pid(config["pid_file"], proc.pid)
        time.sleep(3)  # brief settle time

        healthy, detail = check_health(agent_name, config)
        if healthy:
            log.info("[%s] Started successfully — %s", agent_name, detail)
            return True
        else:
            log.error("[%s] Started but health check failed: %s", agent_name, detail)
            return False
    except Exception as exc:
        log.error("[%s] Failed to start: %s", agent_name, exc)
        return False


# ── Main watchdog loop ────────────────────────────────────────────────────────

def watch_once(agents: dict, dry_run: bool = False) -> dict[str, str]:
    """Check all agents once. Returns {agent_name: status_string}."""
    results = {}
    for name, config in agents.items():
        healthy, detail = check_health(name, config)
        if healthy:
            log.info("[%s] OK — %s", name, detail)
            results[name] = "ok"
        else:
            log.warning("[%s] UNHEALTHY — %s — attempting restart", name, detail)
            results[name] = "restarting"
            stop_agent(name, config, dry_run)
            ok = start_agent(name, config, dry_run)
            results[name] = "recovered" if ok else "failed"
            if not ok:
                log.error("[%s] Could not recover agent — manual intervention required", name)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="A2A agent watchdog")
    parser.add_argument("--agent",    help="Watch only this agent (default: all)")
    parser.add_argument("--dry-run",  action="store_true", help="Check only, no restarts")
    parser.add_argument("--once",     action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("WATCHDOG_INTERVAL", "30")),
                        help="Seconds between checks in daemon mode (default: 30)")
    args = parser.parse_args()

    agents = {args.agent: AGENTS[args.agent]} if args.agent and args.agent in AGENTS else AGENTS

    if not agents:
        log.error("No agents configured. Check AGENT_NAME env var or --agent flag.")
        sys.exit(1)

    if args.once or args.dry_run:
        results = watch_once(agents, dry_run=args.dry_run)
        failed  = [k for k, v in results.items() if v == "failed"]
        sys.exit(1 if failed else 0)

    # Daemon mode
    log.info("Watchdog starting — monitoring: %s every %ds", list(agents.keys()), args.interval)
    try:
        while True:
            watch_once(agents)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Watchdog stopped.")


if __name__ == "__main__":
    main()
