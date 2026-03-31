#!/usr/bin/env python3
"""
scouts/q/q_seed.py — Q: Research specialist and intelligence cross-linker.

Q is the mesh's analysis layer. He reads what every other scout produces,
surfaces connections nobody else noticed, and proposes new tools.

Q is read-mostly. His only write operations are:
  - Markdown reports → workspace/planning/
  - State keys       → Redis q:* namespace only

Q never commands other scouts or touches production data directly.

What Q does:
  - Cross-link findings across data sources (find the same artifact appearing
    via multiple discovery paths — highest confidence findings)
  - Identify novel patterns that weren't explicitly programmed
  - Test hypotheses with real data and return evidence + confidence scores
  - Propose new scouts (name, purpose, complexity, integration plan)

Usage:
    python3 scouts/q/q_seed.py --cross-link
    python3 scouts/q/q_seed.py --patterns
    python3 scouts/q/q_seed.py --hypothesis "high-entropy values are more likely live"
    python3 scouts/q/q_seed.py --propose "we need to scan git repos for leaked secrets"
    python3 scouts/q/q_seed.py --report
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [q] %(levelname)s %(message)s")
log = logging.getLogger("q")

REPO_ROOT   = Path(__file__).parent.parent.parent
REPORTS_DIR = REPO_ROOT / "workspace" / "planning"
REDIS_HOST  = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT  = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASS  = os.environ.get("REDIS_PASSWORD") or None


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


# ── MCP findings DB ───────────────────────────────────────────────────────────

def _get_findings_db():
    """Connect to the research notes SQLite DB if available."""
    db_path = os.environ.get("RESEARCH_DB_PATH", "")
    if not db_path:
        from pathlib import Path as _P
        db_path = str(_P.home() / ".agent-mesh" / "research.db")
    if not os.path.exists(db_path):
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


# ── Cross-linking ─────────────────────────────────────────────────────────────

def cross_link_findings() -> list[dict]:
    """
    Find findings that appear via multiple independent discovery paths.
    These are highest-confidence because two different scouts found the same artifact.
    """
    conn = _get_findings_db()
    if not conn:
        log.warning("No findings DB available — cross-link requires mcp/research_notes.py")
        return []

    links = []
    try:
        # Group by value — same value seen from multiple sources = cross-link
        rows = conn.execute("""
            SELECT value, GROUP_CONCAT(DISTINCT source) as sources,
                   GROUP_CONCAT(DISTINCT type) as types,
                   GROUP_CONCAT(DISTINCT reported_by) as reporters,
                   COUNT(*) as count
            FROM findings
            WHERE value IS NOT NULL
            GROUP BY value
            HAVING COUNT(DISTINCT source) > 1 OR COUNT(DISTINCT reported_by) > 1
            ORDER BY count DESC
            LIMIT 50
        """).fetchall()

        for row in rows:
            links.append({
                "value":       row["value"][:80] + ("..." if len(row["value"]) > 80 else ""),
                "sources":     row["sources"],
                "types":       row["types"],
                "reporters":   row["reporters"],
                "seen_count":  row["count"],
                "confidence":  "high" if row["count"] >= 3 else "medium",
            })
    except Exception as exc:
        log.warning("cross_link_findings failed: %s", exc)
    finally:
        conn.close()

    return links


# ── Pattern analysis ──────────────────────────────────────────────────────────

def analyze_patterns() -> list[dict]:
    """Find novel patterns in the findings DB."""
    conn = _get_findings_db()
    if not conn:
        return []

    patterns = []
    try:
        # Finding type distribution
        rows = conn.execute("""
            SELECT type, COUNT(*) as count, 
                   SUM(CASE WHEN confidence='high' THEN 1 ELSE 0 END) as high_conf
            FROM findings GROUP BY type ORDER BY count DESC
        """).fetchall()
        for row in rows:
            if row["count"] > 0:
                patterns.append({
                    "pattern":    f"finding_type:{row['type']}",
                    "count":      row["count"],
                    "high_conf":  row["high_conf"],
                    "note":       f"{row['type']}: {row['count']} findings, {row['high_conf']} high-confidence",
                })

        # Agents with most findings
        rows = conn.execute("""
            SELECT reported_by, COUNT(*) as count
            FROM findings WHERE reported_by IS NOT NULL
            GROUP BY reported_by ORDER BY count DESC LIMIT 5
        """).fetchall()
        for row in rows:
            patterns.append({
                "pattern":  f"top_reporter:{row['reported_by']}",
                "count":    row["count"],
                "note":     f"Agent '{row['reported_by']}' has {row['count']} findings",
            })

    except Exception as exc:
        log.warning("analyze_patterns failed: %s", exc)
    finally:
        conn.close()

    return patterns


# ── Hypothesis testing ────────────────────────────────────────────────────────

def test_hypothesis(hypothesis: str) -> dict:
    """
    Test a natural language hypothesis against findings data.
    Returns evidence and confidence score.
    """
    conn = _get_findings_db()
    h    = hypothesis.lower()
    result = {
        "hypothesis": hypothesis,
        "evidence":   [],
        "confidence": "unknown",
        "verdict":    "insufficient data",
    }

    if not conn:
        result["verdict"] = "no findings DB available"
        return result

    try:
        if any(w in h for w in ["same", "both", "multiple", "cross"]):
            links = cross_link_findings()
            if links:
                result["evidence"]   = [f"{l['value']}: seen via {l['sources']}" for l in links[:5]]
                result["confidence"] = "medium"
                result["verdict"]    = f"Supported — {len(links)} cross-linked findings found"
            else:
                result["verdict"] = "Not supported — no cross-links found"

        elif any(w in h for w in ["high", "more", "correlation", "rate"]):
            rows = conn.execute("""
                SELECT confidence, COUNT(*) as count FROM findings GROUP BY confidence
            """).fetchall()
            result["evidence"]   = [f"{r['confidence']}: {r['count']}" for r in rows]
            result["confidence"] = "medium"
            result["verdict"]    = "Distribution data available — inspect evidence"

        else:
            # Generic stats
            total = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
            result["evidence"]   = [f"Total findings: {total}"]
            result["confidence"] = "low"
            result["verdict"]    = "No matching hypothesis template — generic stats returned"

    except Exception as exc:
        result["verdict"] = f"Error: {exc}"
    finally:
        conn.close()

    return result


# ── Scout proposals ───────────────────────────────────────────────────────────

def propose_scout(problem: str) -> dict:
    """Generate a structured scout proposal for a new capability."""
    ts = time.strftime("%Y-%m-%d", time.gmtime())
    return {
        "proposed_by": "q",
        "date":        ts,
        "problem":     problem,
        "template": {
            "name":         "new-scout",
            "purpose":      f"Automated capability to address: {problem}",
            "inputs":       ["target", "context"],
            "outputs":      ["findings", "recommendations"],
            "complexity":   "medium",
            "dependencies": ["redis", "requests"],
            "integration":  "Add to agents/README.md skills matrix, create scouts/<name>/<name>_seed.py",
            "review_gate":  "All output to workspace/planning/ only. No production writes without operator approval.",
        },
        "note": "This is a proposal only. Review before implementing.",
    }


# ── Report generation ─────────────────────────────────────────────────────────

def produce_report(
    links:    list[dict],
    patterns: list[dict],
    hypothesis_result: dict | None = None,
    proposal: dict | None = None,
) -> str:
    ts    = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [f"# Q Report — {ts}", ""]

    lines += ["## Cross-Links Found", ""]
    if links:
        lines.append(f"*{len(links)} artifacts seen via multiple independent discovery paths:*\n")
        for l in links[:20]:
            lines.append(f"- `{l['value']}` — sources: {l['sources']}, confidence: {l['confidence']}")
    else:
        lines.append("*No cross-links found in current dataset.*")
    lines.append("")

    lines += ["## Pattern Analysis", ""]
    if patterns:
        for p in patterns[:10]:
            lines.append(f"- {p['note']}")
    else:
        lines.append("*No patterns extracted.*")
    lines.append("")

    if hypothesis_result:
        lines += ["## Hypothesis Test", ""]
        lines.append(f"**Hypothesis:** {hypothesis_result['hypothesis']}")
        lines.append(f"**Verdict:** {hypothesis_result['verdict']}")
        lines.append(f"**Confidence:** {hypothesis_result['confidence']}")
        if hypothesis_result.get("evidence"):
            lines.append("**Evidence:**")
            for e in hypothesis_result["evidence"]:
                lines.append(f"  - {e}")
        lines.append("")

    if proposal:
        lines += ["## Scout Proposal", ""]
        lines.append(f"**Problem:** {proposal['problem']}")
        lines.append(f"**Proposed scout:** {proposal['template']['name']}")
        lines.append(f"**Integration:** {proposal['template']['integration']}")
        lines.append(f"**Review gate:** {proposal['template']['review_gate']}")
        lines.append("")

    lines += [
        "---",
        "",
        "*Q proposes. Ratchet deploys. Operator decides.*",
        "*Output is read-only analysis. Nothing here executes automatically.*",
    ]

    return "\n".join(lines)


def save_report(report: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts   = time.strftime("%Y-%m-%d_%H%M")
    path = REPORTS_DIR / f"q-report-{ts}.md"
    path.write_text(report)
    log.info("Report saved to %s", path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Q — research specialist scout")
    parser.add_argument("--cross-link", action="store_true", help="Find cross-linked findings")
    parser.add_argument("--patterns",   action="store_true", help="Analyze patterns in findings")
    parser.add_argument("--hypothesis", metavar="TEXT",      help="Test a hypothesis")
    parser.add_argument("--propose",    metavar="PROBLEM",   help="Propose a new scout")
    parser.add_argument("--report",     action="store_true", help="Full report (all analyses)")
    parser.add_argument("--save",       action="store_true", help="Save report to workspace/planning/")
    args = parser.parse_args()

    links = cross_link_findings()  if (args.cross_link or args.report) else []
    pats  = analyze_patterns()     if (args.patterns   or args.report) else []
    hyp   = test_hypothesis(args.hypothesis) if args.hypothesis else None
    prop  = propose_scout(args.propose)      if args.propose    else None

    if any([args.cross_link, args.patterns, args.hypothesis, args.propose, args.report]):
        report = produce_report(links, pats, hyp, prop)
        print(report)
        if args.save:
            save_report(report)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
