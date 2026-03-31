#!/usr/bin/env python3
"""
scouts/hermes/hermes_seed.py — Hermes: disclosure drafting scout.

Hermes drafts professional, PII-safe disclosure reports and queues them
for human review. He NEVER submits anything. Every draft stays in `draft`
status until your operator explicitly approves it.

The human approval gate is non-negotiable and cannot be bypassed.

What Hermes does:
  - Reads high-confidence findings from the research notes DB
  - Drafts professional disclosure reports (sanitized, no raw secrets/PII)
  - Queues drafts with status `draft` in a local SQLite tracking DB
  - Tracks submission lifecycle: draft → approved → submitted → acknowledged

What Hermes NEVER does:
  - Auto-submit to any platform, provider, or contact
  - Include raw secret values, PII records, or credentials in any report
  - Promote a draft past `approved` without human action
  - Guess at program scope or eligibility

Usage:
    python3 scouts/hermes/hermes_seed.py --draft
    python3 scouts/hermes/hermes_seed.py --list
    python3 scouts/hermes/hermes_seed.py --show <draft-id>
    python3 scouts/hermes/hermes_seed.py --approve <draft-id>
    python3 scouts/hermes/hermes_seed.py --mark-submitted <draft-id> --ref "TICKET-1234"
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

HERMES_DB = Path(os.environ.get(
    "HERMES_DB_PATH",
    Path.home() / ".agent-mesh" / "hermes.db"
))

RESEARCH_DB = Path(os.environ.get(
    "RESEARCH_DB_PATH",
    Path.home() / ".agent-mesh" / "research.db"
))

REPORTS_DIR = Path(__file__).parent.parent.parent / "workspace" / "disclosures"

VALID_STATUSES = ("draft", "approved", "submitted", "acknowledged", "resolved", "closed")


# ── Hermes DB ─────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    HERMES_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(HERMES_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS disclosures (
            id              TEXT PRIMARY KEY,
            target          TEXT NOT NULL,
            finding_type    TEXT NOT NULL,
            title           TEXT NOT NULL,
            draft_report    TEXT NOT NULL,
            status          TEXT DEFAULT 'draft',
            finding_ids     TEXT,
            disclosure_path TEXT,
            ref             TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


# ── Report drafting ───────────────────────────────────────────────────────────

def _get_findings_for_target(target: str) -> list[dict]:
    """Pull high-confidence findings for a target from the research notes DB."""
    if not RESEARCH_DB.exists():
        return []
    try:
        conn = sqlite3.connect(RESEARCH_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, value, type, confidence, source, notes, investigation_id, reported_by, created_at
            FROM findings
            WHERE LOWER(target) LIKE LOWER(?)
              AND confidence IN ('high', 'medium')
            ORDER BY confidence DESC, created_at DESC
            LIMIT 20
        """, (f"%{target}%",)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def draft_report(
    target:       str,
    finding_type: str,
    findings:     list[dict],
    disclosure_path: str = "",
) -> str:
    """
    Generate a sanitized disclosure report.

    IMPORTANT: This report contains no raw secret values, no PII records,
    and no internal tooling details. It describes the class of issue and
    recommended remediation only.
    """
    ts       = time.strftime("%Y-%m-%d", time.gmtime())
    count    = len(findings)
    types    = list({f["type"] for f in findings})
    high     = sum(1 for f in findings if f.get("confidence") == "high")

    # Sanitize: count and types only, never raw values
    lines = [
        f"# Security Disclosure Report",
        f"",
        f"**Date:** {ts}",
        f"**Target:** {target}",
        f"**Finding Type:** {finding_type}",
        f"**Severity:** {'Critical' if high > 0 else 'High'}",
        f"",
        f"## Summary",
        f"",
        f"During routine security research, {count} finding(s) of type(s) "
        f"{', '.join(types)} were identified associated with `{target}`.",
        f"",
        f"## Impact",
        f"",
        f"- {high} high-confidence finding(s) identified",
        f"- Finding types: {', '.join(types)}",
        f"- Recommended action: immediate review and remediation",
        f"",
        f"## Recommended Remediation",
        f"",
        f"1. Rotate or revoke any exposed credentials immediately",
        f"2. Review access logs for the affected resources",
        f"3. Implement secret scanning in your CI/CD pipeline",
        f"4. Consider enabling cloud provider security notifications",
        f"",
        f"## Researcher Notes",
        f"",
        f"This report was generated as part of responsible security research.",
        f"No data was accessed, modified, or exfiltrated beyond what was needed",
        f"to identify and confirm the finding.",
        f"",
        f"---",
        f"*This is a draft disclosure report. It has NOT been submitted.*",
        f"*Review and approve before sending.*",
    ]
    return "\n".join(lines)


# ── Lifecycle management ──────────────────────────────────────────────────────

def create_draft(target: str, finding_type: str = "credential",
                 disclosure_path: str = "") -> dict:
    """Create a draft disclosure for a target."""
    findings = _get_findings_for_target(target)
    if not findings:
        return {"status": "error", "message": f"No findings for target: {target}"}

    draft_id  = str(uuid.uuid4())[:8]
    title     = f"Security finding: {finding_type} — {target}"
    report    = draft_report(target, finding_type, findings, disclosure_path)
    now       = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fids      = json.dumps([f["id"] for f in findings])

    conn = _get_db()
    conn.execute(
        """INSERT INTO disclosures (id, target, finding_type, title, draft_report,
           status, finding_ids, disclosure_path, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (draft_id, target, finding_type, title, report, "draft", fids,
         disclosure_path, now, now)
    )
    conn.commit()
    conn.close()

    # Save to file too
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{draft_id}_{target.replace('/', '_')}.md"
    report_path.write_text(report)

    return {
        "status":      "ok",
        "draft_id":    draft_id,
        "target":      target,
        "findings":    len(findings),
        "report_path": str(report_path),
        "note":        "⛔ DRAFT — NOT submitted. Operator approval required before any submission.",
    }


def list_drafts(status: str = None) -> list[dict]:
    conn   = _get_db()
    query  = "SELECT id, target, finding_type, title, status, created_at FROM disclosures"
    params = []
    if status:
        query += " WHERE status = ?"; params.append(status)
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_draft(draft_id: str) -> dict | None:
    conn = _get_db()
    row  = conn.execute("SELECT * FROM disclosures WHERE id = ?", (draft_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def approve_draft(draft_id: str) -> dict:
    """Mark a draft as approved by the operator. Does NOT submit."""
    conn = _get_db()
    now  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute("UPDATE disclosures SET status='approved', updated_at=? WHERE id=?",
                 (now, draft_id))
    conn.commit()
    conn.close()
    return {
        "status":   "ok",
        "draft_id": draft_id,
        "note":     "Approved. Still not submitted — Hermes never submits. You must submit manually.",
    }


def mark_submitted(draft_id: str, ref: str = "") -> dict:
    """Record that the operator submitted this disclosure (manual action)."""
    conn = _get_db()
    now  = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute("UPDATE disclosures SET status='submitted', ref=?, updated_at=? WHERE id=?",
                 (ref, now, draft_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "draft_id": draft_id, "ref": ref}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes — disclosure drafting scout")
    parser.add_argument("--draft",          metavar="TARGET",   help="Create a draft for a target")
    parser.add_argument("--finding-type",   default="credential")
    parser.add_argument("--list",           action="store_true", help="List all drafts")
    parser.add_argument("--list-approved",  action="store_true", help="List approved drafts")
    parser.add_argument("--show",           metavar="DRAFT_ID",  help="Show a draft report")
    parser.add_argument("--approve",        metavar="DRAFT_ID",  help="Approve a draft (operator action)")
    parser.add_argument("--mark-submitted", metavar="DRAFT_ID",  help="Record manual submission")
    parser.add_argument("--ref",            metavar="TICKET",    help="Ticket/reference number")
    args = parser.parse_args()

    if args.draft:
        result = create_draft(args.draft, args.finding_type)
        print(json.dumps(result, indent=2))
        if result.get("status") == "ok":
            print(f"\n📄 Draft saved. View with: --show {result['draft_id']}")
            print("⛔ NOT submitted. Operator approval required.")

    elif args.list or args.list_approved:
        status = "approved" if args.list_approved else None
        drafts = list_drafts(status)
        if not drafts:
            print("No drafts found.")
        for d in drafts:
            print(f"  [{d['status']:12s}] {d['id']} — {d['target']} ({d['finding_type']}) — {d['created_at'][:10]}")

    elif args.show:
        draft = get_draft(args.show)
        if not draft:
            print(f"Draft {args.show} not found.")
        else:
            print(draft["draft_report"])
            print(f"\n--- Status: {draft['status']} | ID: {draft['id']} ---")

    elif args.approve:
        result = approve_draft(args.approve)
        print(json.dumps(result, indent=2))

    elif args.mark_submitted:
        result = mark_submitted(args.mark_submitted, args.ref or "")
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
