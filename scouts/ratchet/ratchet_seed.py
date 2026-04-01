#!/usr/bin/env python3
"""
Ratchet 🔧 — Meta-Scout for Continuous System Improvement
=========================================================
Reads failure signals across the mesh and produces actionable improvement
patches. Only turns one way: forward. Never breaks things, only tightens them.

Usage:
    python3 ratchet_seed.py [--report] [--patch] [--since ISO] [--dry-run]

Options:
    --report    Analyze everything, print findings
    --patch     Apply safe playbook patches automatically
    --since     ISO 8601 timestamp to filter lookback window (default: 24h)
    --dry-run   Show what would be patched without writing
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Scout state (optional — degrades gracefully if Redis is down) ─────────────
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from scout_state import set_running, set_idle, set_error
except Exception:
    def set_running(*a, **k): pass
    def set_idle(*a, **k): pass
    def set_error(*a, **k): pass

SCOUT_NAME = "ratchet"

# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------

DSN_AUDIT = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/audit_framework"
DSN_LITELLM = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/litellm_proxy"

WORKSPACE = "/home/openclaw/.openclaw/workspace"
PLAYBOOK = f"{WORKSPACE}/agent-mesh/scouts/PLAYBOOK.md"
AAR_DIR = f"{WORKSPACE}/playbooks"
IMPROVEMENTS_DIR = f"{WORKSPACE}/playbooks/improvements"

CHARLIE_A2A_LOG = "/tmp/charlie-a2a.log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_connect(dsn: str):
    """Return a psycopg2 connection or None on failure."""
    try:
        import psycopg2
        return psycopg2.connect(dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"[ratchet] DB connect failed ({dsn.split('@')[-1]}): {exc}", file=sys.stderr)
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _since_dt(hours: int = 24, since_iso: str | None = None) -> datetime:
    if since_iso:
        try:
            return datetime.fromisoformat(since_iso).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _now_utc() - timedelta(hours=hours)


# ---------------------------------------------------------------------------
# 1. Spend analysis
# ---------------------------------------------------------------------------


def analyze_spend(hours: int = 24, since_iso: str | None = None) -> list[dict]:
    """
    Query LiteLLM_SpendLogs for spend anomalies.

    Returns list of findings:
        {type, model, cost, calls, recommendation, estimated_savings}
    """
    findings = []
    since = _since_dt(hours, since_iso)

    conn = _db_connect(DSN_LITELLM)
    if conn is None:
        findings.append({
            "type": "db_unreachable",
            "model": None,
            "cost": None,
            "calls": None,
            "recommendation": "LiteLLM DB unreachable — cannot perform spend analysis. Check audit-postgres health.",
            "estimated_savings": None,
        })
        return findings

    try:
        with conn.cursor() as cur:
            # Total spend + per-model breakdown
            cur.execute(
                """
                SELECT
                    model,
                    COUNT(*) AS calls,
                    SUM(spend) AS total_spend,
                    AVG(total_tokens) AS avg_tokens
                FROM "LiteLLM_SpendLogs"
                WHERE "startTime" >= %s
                GROUP BY model
                ORDER BY total_spend DESC
                """,
                (since,),
            )
            rows = cur.fetchall()

        if not rows:
            return findings

        total_spend = sum(r[2] or 0 for r in rows)
        total_calls = sum(r[1] or 0 for r in rows)

        for model, calls, spend, avg_tokens in rows:
            spend = spend or 0
            calls = calls or 0
            avg_tokens = avg_tokens or 0

            pct = (spend / total_spend * 100) if total_spend > 0 else 0

            # Flag: single model eating >50% of spend
            if pct > 50 and total_spend > 0.10:
                findings.append({
                    "type": "spend_concentration",
                    "model": model,
                    "cost": round(spend, 4),
                    "calls": calls,
                    "recommendation": (
                        f"{model} consumed {pct:.1f}% of spend (${spend:.4f}) in the last {hours}h. "
                        "Review if cheaper model tier is viable for this workload."
                    ),
                    "estimated_savings": round(spend * 0.6, 4),
                })

            # Flag: cron pattern — high call count, very low avg tokens = cheap task on expensive model
            EXPENSIVE_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-5",
                                 "gpt-4o", "gpt-4.1"}
            model_base = (model or "").split("/")[-1].lower()
            if calls > 20 and avg_tokens < 500 and any(m in model_base for m in EXPENSIVE_MODELS):
                findings.append({
                    "type": "expensive_model_for_simple_task",
                    "model": model,
                    "cost": round(spend, 4),
                    "calls": calls,
                    "recommendation": (
                        f"{model} ran {calls} times with avg {avg_tokens:.0f} tokens — "
                        "looks like a cron/heartbeat job. Switch to gpt-4.1-nano or gemini-2.5-flash."
                    ),
                    "estimated_savings": round(spend * 0.85, 4),
                })

        # Flag: free models (github-copilot) had 0 calls while expensive models ran
        free_models_used = [r for r in rows if "github-copilot" in (r[0] or "")]
        if not free_models_used and total_spend > 0.50:
            findings.append({
                "type": "free_quota_unused",
                "model": "openai/github-copilot/*",
                "cost": round(total_spend, 4),
                "calls": 0,
                "recommendation": (
                    f"${total_spend:.4f} spent in {hours}h with zero github-copilot/* calls. "
                    "Route eligible tasks to free GitHub Copilot quota first."
                ),
                "estimated_savings": round(total_spend * 0.3, 4),
            })

    except Exception as exc:  # noqa: BLE001
        findings.append({
            "type": "spend_query_error",
            "model": None,
            "cost": None,
            "calls": None,
            "recommendation": f"Spend query failed: {exc}",
            "estimated_savings": None,
        })
    finally:
        conn.close()

    return findings


# ---------------------------------------------------------------------------
# 2. Cron failure analysis
# ---------------------------------------------------------------------------


def analyze_cron_failures() -> list[dict]:
    """
    Check known failure signals: charlie-a2a.log + PLAYBOOK known patterns.
    Returns list of {job_name, failure_type, root_cause, fix}
    """
    findings = []

    # Check A2A log
    if os.path.exists(CHARLIE_A2A_LOG):
        try:
            log_text = Path(CHARLIE_A2A_LOG).read_text(errors="replace")
            error_lines = [l for l in log_text.splitlines() if "error" in l.lower() or "fail" in l.lower() or "exception" in l.lower()]
            if error_lines:
                sample = error_lines[-5:]  # last 5 errors
                findings.append({
                    "job_name": "charlie_a2a_server",
                    "failure_type": "a2a_errors_in_log",
                    "root_cause": f"{len(error_lines)} error lines found in A2A log.",
                    "fix": f"Recent errors:\n" + "\n".join(f"  {l}" for l in sample),
                })
        except Exception as exc:
            findings.append({
                "job_name": "charlie_a2a_server",
                "failure_type": "log_read_error",
                "root_cause": str(exc),
                "fix": "Check file permissions on /tmp/charlie-a2a.log",
            })
    else:
        findings.append({
            "job_name": "charlie_a2a_server",
            "failure_type": "log_missing",
            "root_cause": f"{CHARLIE_A2A_LOG} does not exist.",
            "fix": "Verify A2A server is running: ps aux | grep charlie_server. Start if needed.",
        })

    # Check playbook for known broken states
    if os.path.exists(PLAYBOOK):
        playbook_text = Path(PLAYBOOK).read_text(errors="replace")

        # MrPink A2A down
        if "MrPink" in playbook_text and "A2A: DOWN" in playbook_text:
            findings.append({
                "job_name": "mrpink_a2a",
                "failure_type": "a2a_server_down",
                "root_cause": "MrPink A2A server not running (documented in PLAYBOOK).",
                "fix": "Ask RJ to restart MrPink A2A server. Reach via IRC only until resolved.",
            })

        # Oxalis hashcat blocked
        if "hashcat" in playbook_text.lower() and "BLOCKED" in playbook_text:
            findings.append({
                "job_name": "oxalis_hashcat",
                "failure_type": "binary_path_wrong",
                "root_cause": "Hashcat binary at C:\\hashcat-7.1.2, needs rename to C:\\hashcat (Windows manual action).",
                "fix": "Flag to RJ — needs manual rename on Windows host.",
            })

    return findings


# ---------------------------------------------------------------------------
# 3. Subagent timeout analysis
# ---------------------------------------------------------------------------


def analyze_subagent_timeouts() -> list[dict]:
    """
    Read AAR files in playbooks/ for timeout patterns.
    Returns list of {task, timeout_ms, root_cause, recommendation}
    """
    findings = []
    aar_path = Path(AAR_DIR)

    if not aar_path.exists():
        return findings

    md_files = list(aar_path.glob("*.md")) + list(aar_path.glob("**/*.md"))

    timeout_pattern = re.compile(r"timeout", re.IGNORECASE)
    task_pattern = re.compile(r"(subagent|task|job|cron)[:\s]+([^\n]{5,80})", re.IGNORECASE)
    ms_pattern = re.compile(r"(\d{4,7})\s*ms", re.IGNORECASE)

    for f in md_files[:50]:  # cap at 50 files
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue

        if not timeout_pattern.search(text):
            continue

        task_match = task_pattern.search(text)
        task_name = task_match.group(2).strip() if task_match else f.stem

        ms_match = ms_pattern.search(text)
        timeout_ms = int(ms_match.group(1)) if ms_match else None

        # Heuristic: if file mentions chaining / multiple ops, flag it
        chaining = any(kw in text.lower() for kw in ["read + write", "read and write", "chain", "multiple ops"])

        findings.append({
            "task": task_name,
            "timeout_ms": timeout_ms,
            "root_cause": "Scope too broad (chained ops)" if chaining else "Timeout without clear scope issue",
            "recommendation": (
                "Split into single-operation subagents (read OR write OR analyze). "
                "Use 600s timeout for DB/file ops."
                if chaining
                else "Review scope and increase timeout if needed. Ensure model is set explicitly."
            ),
        })

    return findings


# ---------------------------------------------------------------------------
# 4. Playbook gap analysis
# ---------------------------------------------------------------------------


def analyze_playbook_gaps() -> list[dict]:
    """
    Compare PLAYBOOK.md against known issues.
    Returns list of {gap, current_state, recommended_addition}
    """
    findings = []

    if not os.path.exists(PLAYBOOK):
        findings.append({
            "gap": "PLAYBOOK.md missing",
            "current_state": "File does not exist.",
            "recommended_addition": "Create PLAYBOOK.md from template.",
        })
        return findings

    text = Path(PLAYBOOK).read_text(errors="replace")

    checks = [
        {
            "gap": "broken_models_documented",
            "keywords": ["gpt-4.1", "instant-fail", "NEVER use"],
            "recommended_addition": "Document: gpt-4.1, gpt-4.1-mini, claude-opus-4-5 instant-fail in subagent mode.",
        },
        {
            "gap": "redis_credentials_documented",
            "keywords": ["redis", "pyGzOHV", "audit-redis"],
            "recommended_addition": (
                "Add Redis credentials via env vars: REDIS_HOST, REDIS_PORT, REDIS_PASSWORD"
            ),
        },
        {
            "gap": "meshtastic_com20_documented",
            "keywords": ["meshtastic", "COM20", "com20"],
            "recommended_addition": "Document Meshtastic on COM20 if this is a known integration point.",
        },
        {
            "gap": "ratchet_scout_documented",
            "keywords": ["ratchet", "Ratchet"],
            "recommended_addition": (
                "Add Ratchet 🔧 to Scout Registry:\n"
                "  - Purpose: meta-scout, continuous system improvement\n"
                "  - File: agent-mesh/scouts/ratchet/ratchet_seed.py\n"
                "  - Run: python3 ratchet_seed.py --report"
            ),
        },
        {
            "gap": "mrpink_a2a_down_documented",
            "keywords": ["MrPink", "A2A: DOWN"],
            "recommended_addition": "Document MrPink A2A is DOWN; IRC only until server restarted.",
        },
    ]

    for check in checks:
        present = all(kw.lower() in text.lower() for kw in check["keywords"])
        if not present:
            findings.append({
                "gap": check["gap"],
                "current_state": f"Keywords not found: {check['keywords']}",
                "recommended_addition": check["recommended_addition"],
            })

    return findings


# ---------------------------------------------------------------------------
# 5. Produce improvement report
# ---------------------------------------------------------------------------


def produce_improvements(findings: list[dict]) -> str:
    date_str = _now_utc().strftime("%Y-%m-%d %H:%M UTC")

    critical = [f for f in findings if f.get("type") in ("db_unreachable", "a2a_server_down", "spend_query_error")
                or f.get("failure_type") in ("a2a_server_down", "log_missing")]
    recommended = [f for f in findings if f not in critical and f.get("type") in
                   ("spend_concentration", "expensive_model_for_simple_task", "free_quota_unused")]
    nice_to_have = [f for f in findings if f not in critical and f not in recommended]

    lines = [f"# Ratchet Report — {date_str}", ""]

    # Critical
    lines.append("## 🔴 Critical (act now)")
    if critical:
        for f in critical:
            label = f.get("type") or f.get("failure_type") or f.get("gap") or "issue"
            desc = f.get("recommendation") or f.get("fix") or f.get("recommended_addition") or str(f)
            lines.append(f"\n### {label}")
            lines.append(desc)
    else:
        lines.append("_No critical issues found._")

    lines.append("")

    # Recommended
    lines.append("## 🟡 Recommended (act soon)")
    if recommended:
        for f in recommended:
            label = f.get("type") or f.get("gap") or "issue"
            model = f.get("model", "")
            cost = f.get("cost")
            savings = f.get("estimated_savings")
            desc = f.get("recommendation") or f.get("recommended_addition") or str(f)
            lines.append(f"\n### {label}" + (f" — {model}" if model else ""))
            if cost is not None:
                lines.append(f"- Cost: ${cost:.4f} | Estimated savings: ${savings:.4f}" if savings else f"- Cost: ${cost:.4f}")
            lines.append(desc)
    else:
        lines.append("_No recommended actions._")

    lines.append("")

    # Nice to have
    lines.append("## 🟢 Nice to Have")
    if nice_to_have:
        for f in nice_to_have:
            label = f.get("gap") or f.get("task") or f.get("job_name") or "item"
            desc = (f.get("recommended_addition") or f.get("recommendation")
                    or f.get("fix") or str(f))
            lines.append(f"\n### {label}")
            lines.append(desc)
    else:
        lines.append("_Nothing additional._")

    lines.append("")

    # Playbook patches
    lines.append("## 📋 Playbook Patches (exact text to add to PLAYBOOK.md)")
    gap_findings = [f for f in findings if "recommended_addition" in f]
    if gap_findings:
        for gf in gap_findings:
            lines.append(f"\n### Patch: {gf.get('gap', 'unknown')}")
            lines.append("```")
            lines.append(gf["recommended_addition"])
            lines.append("```")
    else:
        lines.append("_No playbook patches needed._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. Patch playbook
# ---------------------------------------------------------------------------


def patch_playbook(patches: list[str], dry_run: bool = False) -> None:
    date_str = _now_utc().strftime("%Y-%m-%d %H:%M UTC")
    header = f"\n\n---\n\n## Ratchet Patch — {date_str}\n\n"
    body = header + "\n\n".join(patches)

    if dry_run:
        print("[dry-run] Would append to PLAYBOOK.md:")
        print(body)
        return

    with open(PLAYBOOK, "a", encoding="utf-8") as fh:
        fh.write(body)
    print(f"[ratchet] Patched PLAYBOOK.md with {len(patches)} additions.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    since_iso = getattr(args, "since", None)
    dry_run = getattr(args, "dry_run", False)

    print(f"[ratchet] Starting analysis — {_now_utc().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"[ratchet] Lookback: {'since ' + since_iso if since_iso else '24h'}")
    print()
    set_running(SCOUT_NAME, {"dry_run": dry_run})

    all_findings: list[dict] = []

    print("[ratchet] → Analyzing spend …")
    spend = analyze_spend(hours=24, since_iso=since_iso)
    all_findings.extend(spend)
    print(f"          {len(spend)} spend findings")

    print("[ratchet] → Analyzing cron failures …")
    cron = analyze_cron_failures()
    all_findings.extend(cron)
    print(f"          {len(cron)} cron findings")

    print("[ratchet] → Analyzing subagent timeouts …")
    timeouts = analyze_subagent_timeouts()
    all_findings.extend(timeouts)
    print(f"          {len(timeouts)} timeout findings")

    print("[ratchet] → Analyzing playbook gaps …")
    gaps = analyze_playbook_gaps()
    all_findings.extend(gaps)
    print(f"          {len(gaps)} gap findings")

    print()
    report = produce_improvements(all_findings)

    if args.report:
        print(report)
        print()

        # Save report to improvements dir
        os.makedirs(IMPROVEMENTS_DIR, exist_ok=True)
        ts = _now_utc().strftime("%Y-%m-%d_%H%M")
        report_path = f"{IMPROVEMENTS_DIR}/ratchet_{ts}.md"
        if not dry_run:
            Path(report_path).write_text(report, encoding="utf-8")
            print(f"[ratchet] Report saved to {report_path}")
        else:
            print(f"[dry-run] Would save report to {report_path}")

    if args.patch:
        patch_texts = [f["recommended_addition"] for f in all_findings if "recommended_addition" in f]
        if patch_texts:
            patch_playbook(patch_texts, dry_run=dry_run)
        else:
            print("[ratchet] No patches to apply.")

    total = len(all_findings)
    critical = sum(1 for f in all_findings
                   if f.get("type") in ("db_unreachable", "a2a_server_down")
                   or f.get("failure_type") in ("a2a_server_down", "log_missing"))
    print(f"[ratchet] Done. {total} total findings, {critical} critical.")
    set_idle(SCOUT_NAME, {"total_findings": total, "critical": critical})


def main() -> None:
    parser = argparse.ArgumentParser(description="Ratchet 🔧 — Meta-Scout for system improvement")
    parser.add_argument("--report", action="store_true", help="Analyze everything and print findings")
    parser.add_argument("--patch", action="store_true", help="Apply safe playbook patches automatically")
    parser.add_argument("--since", metavar="ISO", help="ISO 8601 timestamp to filter lookback window")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be patched without writing")
    args = parser.parse_args()

    if not args.report and not args.patch:
        # Default: run report
        args.report = True

    run(args)


if __name__ == "__main__":
    main()
