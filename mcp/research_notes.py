#!/usr/bin/env python3
"""
mcp/research_notes.py — Shared research findings MCP server.

A generic MCP stdio server for storing and querying research findings
across the agent mesh. Any agent can submit findings; any can query them.

Storage: SQLite (default: ~/.agent-mesh/research.db)
Config:  RESEARCH_DB_PATH env var overrides the default path.

Finding types: credential, endpoint, config_leak, cert, domain,
               api_key, private_key, other

Usage (stdio MCP):
    python3 mcp/research_notes.py

Wire into your MCP client config:
    {
      "mcpServers": {
        "research-notes": {
          "command": "python3",
          "args": ["mcp/research_notes.py"]
        }
      }
    }
"""

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get(
    "RESEARCH_DB_PATH",
    Path.home() / ".agent-mesh" / "research.db"
))

# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS findings (
            id               TEXT PRIMARY KEY,
            value            TEXT NOT NULL,
            type             TEXT NOT NULL,
            confidence       TEXT DEFAULT 'medium',
            source           TEXT,
            notes            TEXT,
            target           TEXT,
            investigation_id TEXT,
            source_url       TEXT,
            reported_by      TEXT,
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_type       ON findings(type);
        CREATE INDEX IF NOT EXISTS idx_target     ON findings(target);
        CREATE INDEX IF NOT EXISTS idx_confidence ON findings(confidence);
        CREATE INDEX IF NOT EXISTS idx_reported   ON findings(reported_by);

        CREATE TABLE IF NOT EXISTS tags (
            finding_id TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
            tag        TEXT NOT NULL,
            PRIMARY KEY (finding_id, tag)
        );
    """)
    conn.commit()
    return conn

# ── MCP Tool Definitions ──────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "finding_submit",
        "description": (
            "Submit a research finding to the shared database. "
            "Use when you discover a credential, exposed endpoint, misconfiguration, "
            "certificate, domain, or any other notable artifact during research."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["value", "type"],
            "properties": {
                "value":            {"type": "string", "description": "The finding value (URL, key fingerprint, domain, etc.)"},
                "type":             {"type": "string", "enum": ["credential", "endpoint", "config_leak", "cert", "domain", "api_key", "private_key", "other"]},
                "confidence":       {"type": "string", "enum": ["high", "medium", "low"], "default": "medium"},
                "source":           {"type": "string", "description": "How it was found: osint, scan, manual, tip"},
                "notes":            {"type": "string", "description": "Context, impact, or next steps"},
                "target":           {"type": "string", "description": "Target organization or domain (if known)"},
                "investigation_id": {"type": "string", "description": "Group related findings under one investigation ID"},
                "source_url":       {"type": "string", "description": "URL where the finding was discovered"},
                "reported_by":      {"type": "string", "description": "Agent or source that found this"},
                "tags":             {"type": "array", "items": {"type": "string"}, "description": "Free-form labels"},
            }
        }
    },
    {
        "name": "finding_query",
        "description": (
            "Query the research findings database. Filter by type, confidence, target, "
            "source, tag, or free-text search. Returns matching findings with full provenance."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type":             {"type": "string", "description": "Filter by finding type"},
                "confidence":       {"type": "string", "description": "Filter by confidence (high/medium/low)"},
                "target":           {"type": "string", "description": "Filter by target (partial match)"},
                "source":           {"type": "string", "description": "Filter by source"},
                "reported_by":      {"type": "string", "description": "Filter by reporting agent"},
                "investigation_id": {"type": "string", "description": "Filter by investigation ID"},
                "tag":              {"type": "string", "description": "Filter by tag"},
                "search":           {"type": "string", "description": "Free-text search across value, notes, target"},
                "limit":            {"type": "integer", "description": "Max results (default 20, max 100)"},
            }
        }
    },
    {
        "name": "finding_get",
        "description": "Get a single finding by ID with full details including tags.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "string"}
            }
        }
    },
    {
        "name": "finding_stats",
        "description": "Get summary statistics: total count, breakdown by type, confidence, and reporting agent.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "finding_tag",
        "description": "Add one or more tags to an existing finding.",
        "inputSchema": {
            "type": "object",
            "required": ["id", "tags"],
            "properties": {
                "id":   {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            }
        }
    },
]

# ── Handlers ──────────────────────────────────────────────────────────────────

def handle_finding_submit(args: dict) -> dict:
    conn = get_db()
    fid  = str(uuid.uuid4())
    now  = datetime.now(timezone.utc).isoformat()
    tags = args.pop("tags", [])
    conn.execute(
        """INSERT INTO findings (id, value, type, confidence, source, notes,
               target, investigation_id, source_url, reported_by, created_at)
           VALUES (:id, :value, :type, :confidence, :source, :notes,
               :target, :investigation_id, :source_url, :reported_by, :created_at)""",
        {
            "id": fid, "created_at": now,
            "value":            args.get("value"),
            "type":             args.get("type"),
            "confidence":       args.get("confidence", "medium"),
            "source":           args.get("source"),
            "notes":            args.get("notes"),
            "target":           args.get("target"),
            "investigation_id": args.get("investigation_id"),
            "source_url":       args.get("source_url"),
            "reported_by":      args.get("reported_by"),
        }
    )
    for tag in tags:
        conn.execute("INSERT OR IGNORE INTO tags VALUES (?, ?)", (fid, tag))
    conn.commit()
    return {"id": fid, "status": "submitted", "created_at": now}


def handle_finding_query(args: dict) -> dict:
    conn    = get_db()
    clauses = []
    params  = []

    if args.get("type"):
        clauses.append("f.type = ?"); params.append(args["type"])
    if args.get("confidence"):
        clauses.append("f.confidence = ?"); params.append(args["confidence"])
    if args.get("target"):
        clauses.append("LOWER(f.target) LIKE ?"); params.append(f"%{args['target'].lower()}%")
    if args.get("source"):
        clauses.append("f.source = ?"); params.append(args["source"])
    if args.get("reported_by"):
        clauses.append("LOWER(f.reported_by) LIKE ?"); params.append(f"%{args['reported_by'].lower()}%")
    if args.get("investigation_id"):
        clauses.append("f.investigation_id = ?"); params.append(args["investigation_id"])
    if args.get("tag"):
        clauses.append("f.id IN (SELECT finding_id FROM tags WHERE tag = ?)")
        params.append(args["tag"])
    if args.get("search"):
        q = f"%{args['search'].lower()}%"
        clauses.append("(LOWER(f.value) LIKE ? OR LOWER(f.notes) LIKE ? OR LOWER(f.target) LIKE ?)")
        params.extend([q, q, q])

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = min(int(args.get("limit", 20)), 100)

    rows = conn.execute(
        f"SELECT f.* FROM findings f {where} ORDER BY f.created_at DESC LIMIT ?",
        params + [limit]
    ).fetchall()

    results = []
    for row in rows:
        r = dict(row)
        r["tags"] = [t["tag"] for t in conn.execute(
            "SELECT tag FROM tags WHERE finding_id = ?", (r["id"],)
        ).fetchall()]
        results.append(r)

    return {"count": len(results), "findings": results}


def handle_finding_get(args: dict) -> dict:
    conn = get_db()
    row  = conn.execute("SELECT * FROM findings WHERE id = ?", (args["id"],)).fetchone()
    if not row:
        return {"error": "not found", "id": args["id"]}
    r = dict(row)
    r["tags"] = [t["tag"] for t in conn.execute(
        "SELECT tag FROM tags WHERE finding_id = ?", (r["id"],)
    ).fetchall()]
    return r


def handle_finding_stats(args: dict) -> dict:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    def _breakdown(col):
        return dict(conn.execute(
            f"SELECT {col}, COUNT(*) FROM findings WHERE {col} IS NOT NULL GROUP BY {col}"
        ).fetchall())
    return {
        "total":         total,
        "by_type":       _breakdown("type"),
        "by_confidence": _breakdown("confidence"),
        "by_agent":      _breakdown("reported_by"),
    }


def handle_finding_tag(args: dict) -> dict:
    conn = get_db()
    fid  = args["id"]
    row  = conn.execute("SELECT id FROM findings WHERE id = ?", (fid,)).fetchone()
    if not row:
        return {"error": "finding not found", "id": fid}
    added = 0
    for tag in args.get("tags", []):
        try:
            conn.execute("INSERT OR IGNORE INTO tags VALUES (?, ?)", (fid, tag))
            added += 1
        except Exception:
            pass
    conn.commit()
    return {"id": fid, "status": "ok", "tags_added": added}


HANDLERS = {
    "finding_submit": handle_finding_submit,
    "finding_query":  handle_finding_query,
    "finding_get":    handle_finding_get,
    "finding_stats":  handle_finding_stats,
    "finding_tag":    handle_finding_tag,
}

# ── MCP stdio loop ─────────────────────────────────────────────────────────────

def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method", "")
        req_id = req.get("id")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "research-notes", "version": "1.0.0"},
            }})

        elif method == "notifications/initialized":
            pass  # no-op

        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

        elif method == "tools/call":
            name    = req.get("params", {}).get("name")
            args    = req.get("params", {}).get("arguments", {})
            handler = HANDLERS.get(name)
            if not handler:
                _send({"jsonrpc": "2.0", "id": req_id,
                       "error": {"code": -32601, "message": f"Unknown tool: {name}"}})
                continue
            try:
                result = handler(args)
                _send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                }})
            except Exception as exc:
                _send({"jsonrpc": "2.0", "id": req_id,
                       "error": {"code": -32603, "message": str(exc)}})

        else:
            _send({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": -32601, "message": f"Unknown method: {method}"}})


if __name__ == "__main__":
    main()
