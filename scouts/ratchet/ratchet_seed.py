#!/usr/bin/env python3
"""
scouts/ratchet/ratchet_seed.py — Ratchet: meta-scout for continuous improvement.

Ratchet reads failure signals from across the mesh and appends improvement
patches to PLAYBOOK.md. He is append-only: he never deletes or overwrites
existing content. Every patch is dated and reversible.

What Ratchet monitors:
  - LiteLLM spend logs (expensive models on cheap tasks, free quota unused)
  - A2A server logs (error spikes, server down)
  - Queue dead-letter depths (workers failing silently)
  - PLAYBOOK.md (gaps, undocumented broken states)

Usage:
    python3 scouts/ratchet/ratchet_seed.py --report
    python3 scouts/ratchet/ratchet_seed.py --report --patch
    python3 scouts/ratchet/ratchet_seed.py --report --patch --dry-run
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ratchet] %(levelname)s %(message)s")
log = logging.getLogger("ratchet")

REPO_ROOT    = Path(__file__).parent.parent.parent
PLAYBOOK_MD  = REPO_ROOT / "scouts" / "PLAYBOOK.md"
REPORTS_DIR  = REPO_ROOT / "workspace" / "planning"
REDIS_HOST   = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT   = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASS   = os.environ.get("REDIS_PASSWORD") or None
KNOWN_AGENTS = os.environ.get("MESH_AGENTS", "alpha,beta,gamma,delta").split(",")


def _get_redis():
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS,
                        decode_responses=True, socket_connect_timeout=3)
        r.ping()
        return r
    except Exception as e:
        log.warning("Redis unavailable: %s", e)
        return None


# ── Analysis functions ────────────────────────────────────────────────────────

def analyze_dead_letters(r) -> list[dict]:
    """Flag agents with growing dead-letter queues."""
    findings = []
    if not r:
        return findings
    for agent in KNOWN_AGENTS:
        try:
            depth = r.llen(f"mesh:dead_letter:{agent}")
            if depth > 0:
                findings.append({
                    "type":           "dead_letter",
                    "agent":          agent,
                    "depth":          depth,
                    "severity":       "critical" if depth > 10 else "warning",
                    "recommendation": f"Investigate dead-letter queue for {agent}. Run: python3 queue/monitor.py",
                })
        except Exception:
            pass
    return findings


def analyze_a2a_errors(r) -> list[dict]:
    """Check A2A call logs for error spikes."""
    findings = []
    if not r:
        return findings
    for agent in KNOWN_AGENTS:
        try:
            entries = r.lrange(f"a2a:log:{agent}", 0, 49)
            if not entries:
                continue
            logs    = [json.loads(e) for e in entries]
            errors  = [l for l in logs if not l.get("success", True)]
            total   = len(logs)
            err_pct = len(errors) / total * 100 if total > 0 else 0
            if err_pct > 20:
                findings.append({
                    "type":           "a2a_error_spike",
                    "agent":          agent,
                    "error_rate_pct": round(err_pct, 1),
                    "sample_size":    total,
                    "severity":       "critical" if err_pct > 50 else "warning",
                    "recommendation": f"A2A error rate {err_pct:.0f}% on {agent}. Check agent logs.",
                })
        except Exception:
            pass
    return findings


def analyze_spend(r) -> list[dict]:
    """Check for spend anomalies in LiteLLM Redis keys (if tracked)."""
    findings = []
    if not r:
        return findings
    try:
        # Check if spend tracker keys exist
        total_key = "spend:total"
        val = r.get(total_key)
        if val:
            total = float(val)
            if total > 10.0:  # $10 threshold — adjust to taste
                findings.append({
                    "type":           "spend_anomaly",
                    "total_usd":      total,
                    "severity":       "warning",
                    "recommendation": f"Total spend ${total:.2f} exceeded threshold. Run: python3 dispatch/spend_tracker.py --status",
                })
    except Exception:
        pass
    return findings


def analyze_playbook_gaps() -> list[dict]:
    """Check PLAYBOOK.md for missing sections."""
    findings = []
    if not PLAYBOOK_MD.exists():
        findings.append({
            "type":           "playbook_missing",
            "severity":       "critical",
            "recommendation": "PLAYBOOK.md not found. Create it at scouts/PLAYBOOK.md.",
        })
        return findings

    content = PLAYBOOK_MD.read_text()
    required_sections = [
        ("Scout Registry",        "No Scout Registry section"),
        ("Spend Safety",          "No Spend Safety Checklist"),
        ("Emergency",             "No Emergency Response section"),
        ("Model Routing",         "No Model Routing Rules"),
    ]
    for section, msg in required_sections:
        if section not in content:
            findings.append({
                "type":           "playbook_gap",
                "missing":        section,
                "severity":       "recommended",
                "recommendation": f"Add '{section}' section to PLAYBOOK.md. {msg}.",
            })
    return findings


# ── Report generation ─────────────────────────────────────────────────────────

def produce_report(findings: list[dict]) -> str:
    critical    = [f for f in findings if f.get("severity") == "critical"]
    warnings    = [f for f in findings if f.get("severity") == "warning"]
    recommended = [f for f in findings if f.get("severity") == "recommended"]

    ts    = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [
        f"# Ratchet Report — {ts}",
        "",
        f"**Total findings:** {len(findings)} "
        f"({len(critical)} critical, {len(warnings)} warnings, {len(recommended)} recommended)",
        "",
    ]

    if critical:
        lines += ["## 🔴 Critical", ""]
        for f in critical:
            lines += [f"- **{f.get('type', 'unknown')}**: {f.get('recommendation', '')}", ""]

    if warnings:
        lines += ["## 🟡 Warnings", ""]
        for f in warnings:
            lines += [f"- **{f.get('type', 'unknown')}**: {f.get('recommendation', '')}", ""]

    if recommended:
        lines += ["## 🟢 Recommended", ""]
        for f in recommended:
            lines += [f"- **{f.get('type', 'unknown')}**: {f.get('recommendation', '')}", ""]

    if not findings:
        lines += ["## ✅ All Clear", "", "No issues found in this sweep.", ""]

    return "\n".join(lines)


def patch_playbook(report: str, dry_run: bool = False) -> None:
    """Append a dated patch to PLAYBOOK.md. Append-only — never overwrites."""
    ts    = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    patch = f"\n\n---\n\n## Ratchet Patch — {ts}\n\n{report}\n"

    if dry_run:
        log.info("DRY RUN — would append to %s:\n%s", PLAYBOOK_MD, patch[:300])
        return

    PLAYBOOK_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(PLAYBOOK_MD, "a") as f:
        f.write(patch)
    log.info("Patched PLAYBOOK.md — %d chars appended", len(patch))


def save_report(report: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts   = time.strftime("%Y-%m-%d_%H%M")
    path = REPORTS_DIR / f"ratchet_{ts}.md"
    path.write_text(report)
    log.info("Report saved to %s", path)
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def run(patch: bool = False, dry_run: bool = False) -> list[dict]:
    r = _get_redis()

    all_findings = []
    all_findings += analyze_dead_letters(r)
    all_findings += analyze_a2a_errors(r)
    all_findings += analyze_spend(r)
    all_findings += analyze_playbook_gaps()

    return all_findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Ratchet — mesh improvement scout")
    parser.add_argument("--report",   action="store_true", help="Print improvement report")
    parser.add_argument("--patch",    action="store_true", help="Append findings to PLAYBOOK.md")
    parser.add_argument("--dry-run",  action="store_true", help="Show what would be patched, don't write")
    parser.add_argument("--save",     action="store_true", help="Save report to workspace/planning/")
    args = parser.parse_args()

    findings = run()
    report   = produce_report(findings)

    if args.report:
        print(report)

    if args.save:
        save_report(report)

    if args.patch:
        patch_playbook(report, dry_run=args.dry_run)

    critical = [f for f in findings if f.get("severity") == "critical"]
    sys.exit(1 if critical else 0)


if __name__ == "__main__":
    main()
