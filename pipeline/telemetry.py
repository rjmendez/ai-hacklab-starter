"""
pipeline/telemetry.py — Drop-in scout event logging to a shared events table.

Any scout or worker can call log_event() to record what it did. Never raises —
telemetry must not break the pipeline even if the DB is down.

Usage:
    from pipeline.telemetry import log_event

    log_event("gamma",  "ct_enum",      severity="info",     count=142, detail="acme.com → 142 subdomains")
    log_event("delta",  "batch_start",  severity="info",     count=500, detail="processing 500 items")
    log_event("alpha",  "error",        severity="warning",  count=0,   detail="queue depth exceeded 1000")
    log_event("veritas","verify_live",  severity="critical", count=1,   detail="live credential found")

Schema (auto-created on first use):
    CREATE TABLE IF NOT EXISTS pipeline_events (
        id         SERIAL PRIMARY KEY,
        agent      TEXT NOT NULL,
        event_type TEXT NOT NULL,
        severity   TEXT DEFAULT 'info',
        count      INTEGER DEFAULT 0,
        detail     TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Backend selection ─────────────────────────────────────────────────────────
# Set TELEMETRY_DSN to a postgres DSN for production.
# Falls back to SQLite at TELEMETRY_SQLITE_PATH (default: ~/.agent-mesh/telemetry.db)

TELEMETRY_DSN         = os.environ.get("TELEMETRY_DSN", "")
TELEMETRY_SQLITE_PATH = Path(os.environ.get(
    "TELEMETRY_SQLITE_PATH",
    Path.home() / ".agent-mesh" / "telemetry.db"
))

_local = threading.local()
_sqlite_lock = threading.Lock()
_sqlite_initialized = False


# ── SQLite backend ────────────────────────────────────────────────────────────

def _get_sqlite() -> sqlite3.Connection:
    global _sqlite_initialized
    TELEMETRY_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(TELEMETRY_SQLITE_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    if not _sqlite_initialized:
        with _sqlite_lock:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent      TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity   TEXT DEFAULT 'info',
                    count      INTEGER DEFAULT 0,
                    detail     TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent    ON pipeline_events(agent)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_severity ON pipeline_events(severity)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ts       ON pipeline_events(created_at)")
            conn.commit()
            _sqlite_initialized = True
    return conn


def _write_sqlite(agent: str, event_type: str, severity: str, count: int, detail: Optional[str]) -> None:
    conn = _get_sqlite()
    now  = datetime.now(timezone.utc).isoformat()
    with _sqlite_lock:
        conn.execute(
            "INSERT INTO pipeline_events (agent, event_type, severity, count, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent, event_type, severity, count, detail, now)
        )
        conn.commit()


# ── Postgres backend ──────────────────────────────────────────────────────────

def _get_pg():
    if not getattr(_local, "pg", None) or _local.pg.closed:
        import psycopg2
        _local.pg = psycopg2.connect(TELEMETRY_DSN)
        _local.pg.autocommit = True
        with _local.pg.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_events (
                    id         SERIAL PRIMARY KEY,
                    agent      TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity   TEXT DEFAULT 'info',
                    count      INTEGER DEFAULT 0,
                    detail     TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    return _local.pg


def _write_pg(agent: str, event_type: str, severity: str, count: int, detail: Optional[str]) -> None:
    conn = _get_pg()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pipeline_events (agent, event_type, severity, count, detail) "
            "VALUES (%s, %s, %s, %s, %s)",
            (agent, event_type, severity, count, detail)
        )


# ── Public API ────────────────────────────────────────────────────────────────

def log_event(
    agent:      str,
    event_type: str,
    severity:   str = "info",
    count:      int = 0,
    detail:     Optional[str] = None,
    **kwargs:   Any,
) -> None:
    """
    Log a pipeline event. Never raises — telemetry failures are logged locally only.

    Args:
        agent:      Agent name (e.g. "gamma", "delta")
        event_type: Short event label (e.g. "ct_enum", "batch_complete", "error")
        severity:   "debug" | "info" | "warning" | "critical"
        count:      Number of items processed/found (optional but useful for dashboards)
        detail:     Free-text detail string — NO raw secrets or PII
        **kwargs:   Extra fields are JSON-serialized into detail if detail is None
    """
    if detail is None and kwargs:
        detail = json.dumps(kwargs, default=str)

    try:
        if TELEMETRY_DSN:
            _write_pg(agent, event_type, severity, count, detail)
        else:
            _write_sqlite(agent, event_type, severity, count, detail)
    except Exception as exc:
        # Telemetry must never break the pipeline
        log.warning("telemetry write failed: %s", exc)


def recent_events(
    agent:      Optional[str] = None,
    severity:   Optional[str] = None,
    limit:      int = 50,
) -> list[dict]:
    """
    Fetch recent pipeline events. Useful for dashboards and health checks.

    Returns list of dicts with keys: id, agent, event_type, severity, count, detail, created_at
    """
    try:
        conn = _get_sqlite()
        clauses, params = [], []
        if agent:
            clauses.append("agent = ?"); params.append(agent)
        if severity:
            clauses.append("severity = ?"); params.append(severity)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows  = conn.execute(
            f"SELECT * FROM pipeline_events {where} ORDER BY id DESC LIMIT ?",
            params + [limit]
        ).fetchall()
        cols = ["id", "agent", "event_type", "severity", "count", "detail", "created_at"]
        return [dict(zip(cols, row)) for row in rows]
    except Exception as exc:
        log.warning("telemetry read failed: %s", exc)
        return []
