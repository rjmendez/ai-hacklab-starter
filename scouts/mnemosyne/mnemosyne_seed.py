#!/usr/bin/env python3
"""
Mnemosyne 🏛️ — Memory Keeper, Dashboard Manager, Human Interface
=================================================================
She remembers everything. She tells the humans.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis
import requests

# ── scout_state integration (optional — degrades gracefully) ───────────────────
try:
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from scout_state import publish_alert as _publish_alert, subscribe_alerts as _subscribe_alerts
    _SCOUT_STATE_AVAILABLE = True
except Exception:
    _SCOUT_STATE_AVAILABLE = False
    def _publish_alert(message, severity="info", source="unknown"): pass
    def _subscribe_alerts(callback): pass

# ── Credentials ────────────────────────────────────────────────────────────────
DB_DSN = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/audit_framework"

REDIS_HOST     = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT     = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

GRAFANA_URL   = "http://openclaw-grafana:3000"
GRAFANA_TOKEN = os.environ.get("GRAFANA_TOKEN", "")

MESHTASTIC_HOST = "100.73.200.19"
MESHTASTIC_PORT = 4403

WORKSPACE = Path("/home/openclaw/.openclaw/workspace")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mnemosyne] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mnemosyne")


# ── Redis helpers ──────────────────────────────────────────────────────────────
def get_redis() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. mesh_status()
# ═══════════════════════════════════════════════════════════════════════════════
KNOWN_AGENTS = {
    "charlie": {"ip": "100.95.177.44"},
    "oxalis":  {"ip": "100.73.200.19"},
    "mrpink":  {"ip": "100.115.69.88"},
}

KNOWN_SCOUTS = ["iris", "rex", "atlas", "hermes", "rate_scout", "ratchet"]


def _check_a2a(ip: str, timeout: float = 2.0) -> str:
    """Hit agent card endpoint; return 'up' or 'down'."""
    try:
        r = requests.get(
            f"http://{ip}:8200/.well-known/agent-card.json",
            timeout=timeout,
        )
        return "up" if r.status_code == 200 else "down"
    except Exception:
        return "down"


def mesh_status() -> dict:
    """Return current status of all agents and scouts."""
    status = {"agents": {}, "scouts": {}}

    # ── Agents ──────────────────────────────────────────────────────────────
    for name, meta in KNOWN_AGENTS.items():
        a2a = _check_a2a(meta["ip"])
        entry = {"a2a": a2a, "ip": meta["ip"]}
        if name == "oxalis":
            entry["gpu"] = "unknown"
        status["agents"][name] = entry

    # ── Scouts ── pull from Redis, fall back to idle ─────────────────────
    try:
        r = get_redis()
        for scout in KNOWN_SCOUTS:
            scout_status = r.hgetall(f"{scout}:status") or {}
            status["scouts"][scout] = {
                "status":   scout_status.get("status", "idle"),
                "last_run": scout_status.get("last_run"),
                "last_msg": scout_status.get("last_msg"),
            }
    except Exception as e:
        log.warning("Redis unavailable for scout status: %s", e)
        for scout in KNOWN_SCOUTS:
            status["scouts"][scout] = {"status": "unknown (redis down)"}

    return status


# ═══════════════════════════════════════════════════════════════════════════════
# 2. send_meshtastic_alert()
# ═══════════════════════════════════════════════════════════════════════════════
def send_meshtastic_alert(message: str, priority: str = "normal") -> bool:
    """Send a message via Meshtastic TCP bridge. Never raises — best-effort."""
    prefix = {"critical": "🚨 CRITICAL", "warning": "⚠️ WARN", "normal": "📡"}.get(
        priority, "📡"
    )
    full_msg = f"{prefix} [mesh] {message}"

    try:
        import meshtastic
        import meshtastic.tcp_interface

        log.info("Connecting to Meshtastic at %s:%s …", MESHTASTIC_HOST, MESHTASTIC_PORT)
        iface = meshtastic.tcp_interface.TCPInterface(
            hostname=MESHTASTIC_HOST, portNumber=MESHTASTIC_PORT
        )
        iface.sendText(full_msg)
        iface.close()
        log.info("Meshtastic alert sent: %s", full_msg)
        return True
    except ImportError:
        log.warning("meshtastic library not installed — pip install meshtastic")
        return False
    except Exception as e:
        log.warning("Meshtastic alert failed (best-effort, continuing): %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# 3. daily_brief()
# ═══════════════════════════════════════════════════════════════════════════════
def daily_brief() -> str:
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🏛️  Mnemosyne Daily Brief — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # ── Pipeline stats ──────────────────────────────────────────────────────
    try:
        import psycopg2

        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM scan_results WHERE created_at::date = CURRENT_DATE")
        scans_today = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM scan_results "
            "WHERE created_at::date = CURRENT_DATE AND severity = 'critical'"
        )
        critical_today = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM scan_results WHERE reviewed = false")
        pending_review = cur.fetchone()[0]

        conn.close()

        lines += [
            "",
            "📊 Pipeline",
            f"  Scans today:       {scans_today}",
            f"  Critical (today):  {critical_today}",
            f"  Pending review:    {pending_review}",
        ]
    except Exception as e:
        lines += ["", f"📊 Pipeline — DB unavailable ({e})"]

    # ── Redis / queue depth ─────────────────────────────────────────────────
    try:
        r = get_redis()
        queue_depth = r.llen("scan:queue") or 0
        error_count = r.llen("scan:errors") or 0
        lines += [
            "",
            "📬 Queue",
            f"  Queue depth:  {queue_depth}",
            f"  Error count:  {error_count}",
        ]
    except Exception as e:
        lines += ["", f"📬 Queue — Redis unavailable ({e})"]

    # ── Scout activity ──────────────────────────────────────────────────────
    st = mesh_status()
    lines += ["", "🤖 Scouts"]
    for scout, info in st["scouts"].items():
        status = info.get("status", "?")
        last_run = info.get("last_run") or "never"
        lines.append(f"  {scout:<16} {status:<12} last: {last_run}")

    # ── Agent A2A health ────────────────────────────────────────────────────
    lines += ["", "🌐 Agents (A2A)"]
    for agent, info in st["agents"].items():
        a2a = info.get("a2a", "?")
        icon = "✅" if a2a == "up" else "❌"
        lines.append(f"  {icon} {agent:<10} {info['ip']}")

    # ── Blocked items ───────────────────────────────────────────────────────
    blocked = []
    if st["agents"].get("mrpink", {}).get("a2a") == "down":
        blocked.append("MrPink A2A is down — check laptop")
    try:
        r = get_redis()
        if int(r.get("hashcat:blocked") or 0):
            blocked.append("Hashcat CWD issue — check oxalis")
    except Exception:
        pass

    if blocked:
        lines += ["", "🚧 Blocked"]
        for b in blocked:
            lines.append(f"  ⚠️  {b}")
    else:
        lines += ["", "✅ Nothing blocked"]

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. project_status()
# ═══════════════════════════════════════════════════════════════════════════════
def project_status() -> str:
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🏛️  Mnemosyne Project Status — {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # ── Active runs ─────────────────────────────────────────────────────────
    try:
        r = get_redis()
        active_keys = r.keys("*:active_run") or []
        if active_keys:
            lines += ["", "🔄 Active Runs"]
            for k in active_keys:
                val = r.get(k) or "running"
                lines.append(f"  {k}: {val}")
        else:
            lines += ["", "🔄 Active Runs: none"]
    except Exception as e:
        lines += ["", f"🔄 Active Runs — Redis unavailable ({e})"]

    # ── Mesh status ─────────────────────────────────────────────────────────
    st = mesh_status()
    lines += ["", "🌐 Agent A2A Health"]
    for agent, info in st["agents"].items():
        icon = "✅" if info.get("a2a") == "up" else "❌"
        lines.append(f"  {icon} {agent} ({info['ip']})")

    # ── Known blockers ──────────────────────────────────────────────────────
    lines += [
        "",
        "🚧 Known Issues / Blockers",
        "  • MrPink A2A: intermittent — laptop may sleep",
        "  • Hashcat CWD: check oxalis workspace path",
        "  • Grafana permissions: token may need re-scoping for admin ops",
        "  • Meshtastic: requires TCP bridge active on Oxalis (meshtastic --host)",
    ]

    # ── Next actions ────────────────────────────────────────────────────────
    lines += [
        "",
        "▶️  Next Actions",
        "  1. Verify Meshtastic TCP bridge on Oxalis (port 4403)",
        "  2. Build Grafana mesh-overview dashboard",
        "  3. Wire iris / rex scouts to report to mnemosyne:alerts",
        "  4. Index playbooks into searchable Redis store",
    ]

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. notify()
# ═══════════════════════════════════════════════════════════════════════════════
def notify(
    message: str,
    severity: str = "info",
    channels: Optional[list] = None,
    source: str = "mnemosyne",
) -> None:
    """Route a notification by severity (or explicit channels list).

    Always publishes to the mnemosyne:alerts pub/sub channel via scout_state
    when severity is 'warning' or 'critical', so all subscribers are notified.
    """
    if channels is None:
        channels = {
            "critical": ["meshtastic", "redis", "log"],
            "warning":  ["redis", "log"],
            "info":     ["log"],
        }.get(severity, ["log"])

    # Always publish to the shared pub/sub channel for warning/critical
    if severity in ("warning", "critical", "info"):
        _publish_alert(message, severity=severity, source=source)

    for ch in channels:
        if ch == "log":
            lvl = {"critical": logging.CRITICAL, "warning": logging.WARNING}.get(
                severity, logging.INFO
            )
            log.log(lvl, "[notify/%s] %s", severity, message)

        elif ch == "redis":
            try:
                r = get_redis()
                payload = json.dumps({
                    "message":  message,
                    "severity": severity,
                    "ts":       datetime.now(timezone.utc).isoformat(),
                })
                r.lpush("mnemosyne:alerts", payload)
                r.publish("mnemosyne:alerts", payload)
                log.info("Alert published to Redis mnemosyne:alerts")
            except Exception as e:
                log.warning("Redis notify failed: %s", e)

        elif ch == "meshtastic":
            send_meshtastic_alert(message, priority=severity)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. index_document()
# ═══════════════════════════════════════════════════════════════════════════════
def _slugify(path: str) -> str:
    s = Path(path).stem.lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def index_document(path: str, tags: Optional[list] = None) -> None:
    """Add a file to the searchable Redis index."""
    p = Path(path)
    if not p.exists():
        log.warning("index_document: file not found: %s", path)
        return

    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        log.warning("index_document: cannot read %s: %s", path, e)
        return

    # Extract title from first H1 or filename
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else p.name

    # Short snippet (first 200 non-heading chars)
    snippet_lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    snippet = " ".join(snippet_lines)[:200]

    slug = _slugify(str(path))
    key  = f"mnemosyne:index:{slug}"

    doc = {
        "path":       str(path),
        "title":      title,
        "tags":       json.dumps(tags or []),
        "summary":    snippet,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        r = get_redis()
        r.hset(key, mapping=doc)
        log.info("Indexed: %s → %s", path, key)
    except Exception as e:
        log.warning("index_document: Redis write failed for %s: %s", path, e)


def index_all() -> int:
    """Index playbooks, planning, and all scout SCOUT.md files."""
    count = 0
    patterns = [
        (WORKSPACE / "playbooks",      ["playbook"]),
        (WORKSPACE / "planning",       ["planning"]),
    ]

    for base, tags in patterns:
        if base.exists():
            for f in base.rglob("*.md"):
                index_document(str(f), tags=tags)
                count += 1

    # Scout SCOUT.md files
    scouts_dir = WORKSPACE / "agent-mesh" / "scouts"
    if scouts_dir.exists():
        for scout_md in scouts_dir.rglob("SCOUT.md"):
            index_document(str(scout_md), tags=["scout", scout_md.parent.name])
            count += 1

    log.info("Indexed %d documents total", count)
    return count


# ═══════════════════════════════════════════════════════════════════════════════
# 7. search()
# ═══════════════════════════════════════════════════════════════════════════════
def search(query: str) -> list:
    """Simple keyword search over the Redis index."""
    results = []
    query_lower = query.lower()

    try:
        r = get_redis()
        keys = r.keys("mnemosyne:index:*")
        for key in keys:
            doc = r.hgetall(key)
            haystack = " ".join([
                doc.get("title", ""),
                doc.get("summary", ""),
                doc.get("tags", ""),
                doc.get("path", ""),
            ]).lower()
            if query_lower in haystack:
                tags = []
                try:
                    tags = json.loads(doc.get("tags", "[]"))
                except Exception:
                    pass
                results.append({
                    "path":    doc.get("path"),
                    "title":   doc.get("title"),
                    "tags":    tags,
                    "snippet": doc.get("summary", "")[:150],
                })
    except Exception as e:
        log.warning("search: Redis unavailable: %s", e)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Grafana helpers (bonus — dashboard provisioning stub)
# ═══════════════════════════════════════════════════════════════════════════════
def _grafana_headers() -> dict:
    return {
        "Authorization": f"Bearer {GRAFANA_TOKEN}",
        "Content-Type":  "application/json",
    }


def grafana_check() -> dict:
    """Verify Grafana is reachable and return server info."""
    try:
        r = requests.get(
            f"{GRAFANA_URL}/api/health",
            headers=_grafana_headers(),
            timeout=4,
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def grafana_ensure_mesh_dashboard() -> bool:
    """Create a basic mesh-overview dashboard if it doesn't exist."""
    title = "Mesh Overview"
    try:
        headers = _grafana_headers()
        # Check if dashboard exists
        r = requests.get(
            f"{GRAFANA_URL}/api/search?query={title}",
            headers=headers,
            timeout=4,
        )
        existing = [d for d in r.json() if d.get("title") == title]
        if existing:
            log.info("Grafana dashboard '%s' already exists", title)
            return True

        dashboard = {
            "dashboard": {
                "title": title,
                "tags":  ["mesh", "mnemosyne"],
                "panels": [
                    {
                        "type":       "stat",
                        "title":      "A2A Agents Up",
                        "gridPos":    {"h": 4, "w": 6, "x": 0, "y": 0},
                        "datasource": "-- Grafana --",
                        "options":    {"reduceOptions": {"calcs": ["lastNotNull"]}},
                    },
                    {
                        "type":     "logs",
                        "title":    "Mnemosyne Alerts",
                        "gridPos":  {"h": 8, "w": 24, "x": 0, "y": 4},
                        "datasource": "-- Grafana --",
                    },
                ],
                "schemaVersion": 36,
            },
            "overwrite": False,
        }
        resp = requests.post(
            f"{GRAFANA_URL}/api/dashboards/db",
            headers=headers,
            json=dashboard,
            timeout=6,
        )
        if resp.status_code in (200, 412):
            log.info("Grafana mesh dashboard created/exists")
            return True
        log.warning("Grafana dashboard create returned %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.warning("Grafana dashboard creation failed: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(
        description="Mnemosyne 🏛️ — Mesh Memory Keeper & Human Interface"
    )
    ap.add_argument("--status",     action="store_true", help="Print mesh status")
    ap.add_argument("--brief",      action="store_true", help="Print daily brief")
    ap.add_argument("--project",    action="store_true", help="Print project status")
    ap.add_argument("--notify",     metavar="MSG",       help="Send a notification")
    ap.add_argument("--severity",   default="info",      help="Severity for --notify")
    ap.add_argument("--index",      action="store_true", help="Index all docs to Redis")
    ap.add_argument("--search",     metavar="QUERY",     help="Search indexed docs")
    ap.add_argument("--meshtastic", metavar="MSG",       help="Send Meshtastic alert directly")
    ap.add_argument("--grafana",    action="store_true", help="Check Grafana + ensure dashboard")
    ap.add_argument("--listen",     action="store_true",
                    help="Subscribe to mnemosyne:alerts pub/sub and route by severity (blocking)")
    args = ap.parse_args()

    if args.status:
        st = mesh_status()
        print(json.dumps(st, indent=2))

    if args.brief:
        print(daily_brief())

    if args.project:
        print(project_status())

    if args.notify:
        notify(args.notify, severity=args.severity)

    if args.index:
        n = index_all()
        print(f"✅ Indexed {n} documents")

    if args.search:
        hits = search(args.search)
        if hits:
            for h in hits:
                print(f"\n📄 {h['title']}\n   {h['path']}\n   Tags: {h['tags']}\n   {h['snippet']}")
        else:
            print("No results found.")

    if args.meshtastic:
        ok = send_meshtastic_alert(args.meshtastic, priority="normal")
        print("✅ Sent" if ok else "❌ Failed (check logs)")

    if args.grafana:
        info = grafana_check()
        print("Grafana health:", json.dumps(info, indent=2))
        ok = grafana_ensure_mesh_dashboard()
        print("Dashboard:", "✅ ready" if ok else "❌ failed")

    if args.listen:
        _listen_and_route()

    if not any(vars(args).values()):
        ap.print_help()


def _listen_and_route() -> None:
    """
    Subscribe to mnemosyne:alerts and route incoming alerts by severity.
    - critical → log CRITICAL + send Meshtastic
    - warning  → log WARNING  + redis list append
    - info      → log INFO
    Runs forever (blocking). Use Ctrl+C to stop.
    """
    log.info("🏛️  Mnemosyne --listen mode active. Subscribed to mnemosyne:alerts …")

    def _route(alert: dict) -> None:
        sev     = alert.get("severity", "info")
        source  = alert.get("source", "unknown")
        message = alert.get("message", "")
        ts      = alert.get("ts", "")

        prefix = {
            "critical": "🚨 CRITICAL",
            "warning":  "⚠️  WARNING",
            "info":     "ℹ️  INFO",
        }.get(sev, "❓ UNKNOWN")

        log_line = f"[{ts}] {prefix} [{source}] {message}"

        if sev == "critical":
            log.critical(log_line)
            send_meshtastic_alert(f"[{source}] {message}", priority="critical")
        elif sev == "warning":
            log.warning(log_line)
            # Append to a durable warning list in Redis
            try:
                r = get_redis()
                r.lpush("mnemosyne:warnings", json.dumps(alert))
                r.ltrim("mnemosyne:warnings", 0, 199)
            except Exception as e:
                log.warning("Could not persist warning to Redis: %s", e)
        else:
            log.info(log_line)

    try:
        _subscribe_alerts(_route)
    except KeyboardInterrupt:
        log.info("🏛️  Mnemosyne --listen stopped.")


if __name__ == "__main__":
    main()
