#!/usr/bin/env python3
"""
Q 🔬 — The Mesh's Research Specialist & Mad Scientist
Cross-links intelligence across all scouts, surfaces hidden patterns,
and proposes tools that don't exist yet.

Usage:
    python3 q_seed.py [--cross-link] [--patterns] [--wordlist "org1,org2"]
                      [--hypothesis "text"] [--report] [--propose "problem"]
"""

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg2
import redis

# ─── Config ──────────────────────────────────────────────────────────────────

DSN_AUDIT   = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/audit_framework"
DSN_LITELLM = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/litellm_proxy"
REDIS_HOST  = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT  = 6381
REDIS_PASS  = os.environ.get("REDIS_PASSWORD", "")
WORKSPACE   = Path("/home/openclaw/.openclaw/workspace")

# ─── DB helpers ──────────────────────────────────────────────────────────────

def get_conn(dsn=DSN_AUDIT):
    return psycopg2.connect(dsn)

def get_redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS, decode_responses=True)

def q(sql, params=None, dsn=DSN_AUDIT):
    """Run a query, return list of dicts."""
    conn = get_conn(dsn)
    cur  = conn.cursor()
    if params:
        cur.execute(sql, params)
    else:
        # No params: execute as a plain string (avoids %s / ILIKE confusion)
        cur.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows

# ─── 1. cross_link_findings ───────────────────────────────────────────────────

def cross_link_findings() -> list[dict]:
    """
    Find connections across data sources.

    Strategy:
    - Pull OpenAI / GitHub / Google keys from bucket.scan_secrets
    - Pull firebase targets that look like the same org (by slug / app_package domain)
    - Check if the same raw_secret appears in firebase.findings
    - Use domain/package heuristics to link target records across schemas
    """
    links = []

    # --- bucket: API keys of interest
    key_rows = q("""
        SELECT ss.id, ss.detector_name, ss.detector_type,
               ss.raw_secret, ss.redacted, ss.verified,
               sf.bucket_url, sf.filename, sf.full_path,
               bt.slug AS bucket_slug, bt.app_name AS bucket_app,
               bt.app_package AS bucket_package
        FROM   bucket.scan_secrets ss
        JOIN   bucket.scan_files   sf ON sf.id = ss.scan_file_id
        LEFT   JOIN bucket.targets bt ON bt.id = sf.bucket_id
        WHERE  ss.detector_name ILIKE '%openai%'
            OR ss.detector_name ILIKE '%github%'
            OR ss.detector_name ILIKE '%google%'
        LIMIT  2000
    """)

    # --- firebase: targets with known slugs / packages
    fb_targets = q("""
        SELECT id, firebase_url, slug, app_name, app_package
        FROM   firebase.targets
        WHERE  slug IS NOT NULL OR app_package IS NOT NULL
        LIMIT  5000
    """)

    # --- firebase: credential-type findings
    fb_creds = q("""
        SELECT ff.id, ff.target_id, ff.finding_type, ff.title, ff.detail,
               ft.slug, ft.app_package, ft.firebase_url
        FROM   firebase.findings ff
        JOIN   firebase.targets  ft ON ft.id = ff.target_id
        WHERE  ff.finding_type ILIKE '%secret%'
            OR ff.finding_type ILIKE '%credential%'
            OR ff.finding_type ILIKE '%key%'
            OR ff.finding_type ILIKE '%token%'
        LIMIT  2000
    """)

    # Build lookup: org token → firebase targets
    def org_tokens(text: str) -> set[str]:
        """Extract lowercase alpha tokens ≥4 chars from a string."""
        if not text:
            return set()
        return {t.lower() for t in re.findall(r'[a-zA-Z]{4,}', text)}

    fb_index = {}  # token → list of fb target rows
    for ft in fb_targets:
        tokens = org_tokens(ft.get("slug", "") or "") | org_tokens(ft.get("app_package", "") or "")
        for tok in tokens:
            fb_index.setdefault(tok, []).append(ft)

    # Build raw_secret set from firebase findings details
    fb_secret_values = set()
    for fc in fb_creds:
        detail = fc.get("detail", "") or ""
        # crude extraction: grab anything that looks like a token/key (≥20 chars no spaces)
        for m in re.findall(r'[A-Za-z0-9_\-\.]{20,}', detail):
            fb_secret_values.add(m)

    seen = set()

    for row in key_rows:
        raw = row.get("raw_secret", "") or ""
        det = row.get("detector_name", "")
        slug = row.get("bucket_slug", "") or ""
        pkg  = row.get("bucket_package", "") or ""
        burl = row.get("bucket_url", "") or ""

        # --- Link type A: same raw secret found in firebase findings
        if raw and raw in fb_secret_values:
            key = ("secret_overlap", raw[:40])
            if key not in seen:
                seen.add(key)
                links.append({
                    "source_a":    f"bucket.scan_secrets id={row['id']} ({det})",
                    "source_b":    "firebase.findings (detail match)",
                    "link_type":   "secret_value_overlap",
                    "confidence":  0.90,
                    "description": (
                        f"Raw secret value ({det}) found in a bucket file "
                        f"also appears verbatim in a Firebase finding. "
                        f"Bucket: {burl or slug}"
                    ),
                })

        # --- Link type B: org name overlap between bucket and firebase
        bucket_tokens = org_tokens(slug) | org_tokens(pkg) | org_tokens(burl)
        matched_fb = set()
        for tok in bucket_tokens:
            for ft in fb_index.get(tok, []):
                matched_fb.add(ft["id"])
        if matched_fb:
            # Dedup by bucket_url+firebase_ids combo (not per secret id)
            fb_key = frozenset(matched_fb)
            key = ("org_overlap", (burl or slug or pkg), fb_key)
            if key not in seen:
                seen.add(key)
                links.append({
                    "source_a":    f"bucket.scan_secrets detector={det} bucket={burl or slug or pkg}",
                    "source_b":    f"firebase.targets ids={sorted(matched_fb)[:5]}",
                    "link_type":   "org_name_overlap",
                    "confidence":  0.65,
                    "description": (
                        f"Org tokens from bucket ({slug or pkg or burl}) match "
                        f"{len(matched_fb)} Firebase target(s). "
                        f"Key type: {det}"
                    ),
                })

    # --- Link type C: firebase targets whose slugs match bucket slugs directly
    bucket_slugs = q("SELECT DISTINCT slug FROM bucket.targets WHERE slug IS NOT NULL LIMIT 5000")
    bslugs = {r["slug"].lower() for r in bucket_slugs if r["slug"]}

    for ft in fb_targets:
        fs = (ft.get("slug") or "").lower()
        if fs and fs in bslugs:
            key = ("slug_match", fs)
            if key not in seen:
                seen.add(key)
                links.append({
                    "source_a":    f"bucket.targets slug={fs}",
                    "source_b":    f"firebase.targets id={ft['id']} slug={fs}",
                    "link_type":   "slug_exact_match",
                    "confidence":  0.85,
                    "description": (
                        f"Slug '{fs}' exists in BOTH bucket.targets and firebase.targets — "
                        f"same app has both an S3 bucket and a Firebase DB. "
                        f"High-value pivot point."
                    ),
                })

    return links


# ─── 2. generate_bucket_wordlist ──────────────────────────────────────────────

def generate_bucket_wordlist(org_names: list[str], rules: str = "suffix") -> list[str]:
    """
    Given org names, generate bucket name candidates using common patterns.
    CPU fallback (no hashcat dependency).
    For GPU acceleration: hashcat --stdout -r /rules/best64.rule wordlist.txt
    """
    suffixes = [
        "-backup", "-dev", "-prod", "-data", "-assets",
        "-internal", "-staging", "-db", "-logs", "-uploads",
        "-media", "-archive", "-export", "-import", "-raw",
        "-api", "-static", "-config", "-secrets", "-private",
        ".com", "-store", "-files", "-bucket", "-s3",
    ]
    prefixes = ["dev-", "prod-", "staging-", "backup-", "data-", "logs-"]

    candidates = []
    for org in org_names:
        org = org.lower().strip().replace(" ", "-").replace("_", "-")
        if not org:
            continue
        candidates.append(org)  # bare name
        for sfx in suffixes:
            candidates.append(f"{org}{sfx}")
        for pfx in prefixes:
            candidates.append(f"{pfx}{org}")
        # dot-com variants
        candidates.append(f"{org}.com")
        candidates.append(f"www.{org}.com")

    return sorted(set(candidates))


# ─── 3. hypothesis_test ───────────────────────────────────────────────────────

def hypothesis_test(hypothesis: str) -> dict:
    """
    Test a natural-language hypothesis against the DB.
    Supports a small set of known hypothesis templates.
    """
    h = hypothesis.lower()
    result = {}

    # Template 1: same credentials in both Firebase and S3
    if any(kw in h for kw in ["same credential", "both firebase and s3", "firebase and bucket",
                               "appear in both", "cross-schema secret"]):
        rows = q("""
            SELECT ss.detector_name, ss.raw_secret, ss.redacted,
                   sf.bucket_url, bt.slug
            FROM   bucket.scan_secrets ss
            JOIN   bucket.scan_files   sf ON sf.id = ss.scan_file_id
            LEFT   JOIN bucket.targets bt ON bt.id = sf.bucket_id
            WHERE  ss.raw_secret IS NOT NULL
              AND  LENGTH(ss.raw_secret) > 15
        """)
        fb_rows = q("""
            SELECT ff.detail, ff.finding_type, ft.slug
            FROM   firebase.findings ff
            JOIN   firebase.targets  ft ON ft.id = ff.target_id
            WHERE  ff.detail IS NOT NULL
        """)
        fb_details = " ".join(r["detail"] or "" for r in fb_rows)
        matches = []
        for r in rows:
            raw = r.get("raw_secret", "") or ""
            if len(raw) > 15 and raw in fb_details:
                matches.append(r)
        confidence = min(0.99, 0.5 + 0.1 * len(matches)) if matches else 0.05
        result = {
            "hypothesis": hypothesis,
            "result":     "SUPPORTED" if matches else "NOT SUPPORTED",
            "evidence":   matches[:5],
            "confidence": confidence,
            "query_used": "Cross-matched bucket.scan_secrets.raw_secret vs firebase.findings.detail",
        }

    # Template 2: file size vs finding rate
    elif any(kw in h for kw in ["file size", "bigger file", "larger file", "size correlation"]):
        rows = q("""
            SELECT
                CASE
                    WHEN sf.file_size < 10000   THEN '<10KB'
                    WHEN sf.file_size < 100000  THEN '10-100KB'
                    WHEN sf.file_size < 1000000 THEN '100KB-1MB'
                    ELSE '>1MB'
                END AS size_bucket,
                COUNT(*) AS file_count,
                SUM(sf.trufflehog_count) AS total_secrets,
                ROUND(AVG(sf.trufflehog_count)::numeric, 2) AS avg_secrets
            FROM bucket.scan_files sf
            WHERE sf.trufflehog_count IS NOT NULL
            GROUP BY 1
            ORDER BY MIN(sf.file_size)
        """)
        result = {
            "hypothesis": hypothesis,
            "result":     "DATA COLLECTED",
            "evidence":   rows,
            "confidence": 0.80,
            "query_used": "Grouped bucket.scan_files by file_size, AVG(trufflehog_count)",
        }

    # Template 3: verified keys
    elif any(kw in h for kw in ["verified", "live key", "active key", "real credential"]):
        rows = q("""
            SELECT detector_name, COUNT(*) AS total,
                   SUM(CASE WHEN verified THEN 1 ELSE 0 END) AS verified_count
            FROM   bucket.scan_secrets
            GROUP  BY detector_name
            ORDER  BY verified_count DESC NULLS LAST
            LIMIT  20
        """)
        result = {
            "hypothesis": hypothesis,
            "result":     "DATA COLLECTED",
            "evidence":   rows,
            "confidence": 0.85,
            "query_used": "Grouped bucket.scan_secrets by detector_name, counted verified=true",
        }

    else:
        # Fallback: summary stats
        rows = q("""
            SELECT 'bucket_secrets' AS source, COUNT(*) AS count FROM bucket.scan_secrets
            UNION ALL
            SELECT 'firebase_findings', COUNT(*) FROM firebase.findings
            UNION ALL
            SELECT 'firebase_targets', COUNT(*) FROM firebase.targets
            UNION ALL
            SELECT 'bucket_targets', COUNT(*) FROM bucket.targets
        """)
        result = {
            "hypothesis": hypothesis,
            "result":     "UNKNOWN — no template matched. Returned summary stats.",
            "evidence":   rows,
            "confidence": 0.1,
            "query_used": "Summary count across major tables",
        }

    return result


# ─── 4. scan_for_novel_patterns ───────────────────────────────────────────────

def scan_for_novel_patterns() -> list[dict]:
    """
    Look for things nobody programmed Q to find.
    """
    patterns = []

    # Pattern A: Unknown detector types (not in a well-known list)
    known_detectors = {
        "openai", "github", "google", "aws", "slack", "stripe",
        "twilio", "sendgrid", "jwt", "generic", "private key",
        "azure", "gcp", "firebase", "heroku", "mailgun", "shopify",
        "datadog", "newrelic", "pagerduty", "npm", "pypi",
    }
    unknown = q("""
        SELECT LOWER(detector_name) AS dname, COUNT(*) AS cnt
        FROM   bucket.scan_secrets
        GROUP  BY 1
        ORDER  BY cnt DESC
    """)
    novel_detectors = [
        r for r in unknown
        if not any(k in (r["dname"] or "") for k in known_detectors)
    ]
    if novel_detectors:
        patterns.append({
            "pattern":          "novel_detector_types",
            "description":      f"Found {len(novel_detectors)} detector type(s) not in Q's known list.",
            "supporting_data":  novel_detectors[:10],
            "suggested_action": "Review these detector names. Add to known list or flag for Ratchet.",
        })

    # Pattern B: Rare finding types in firebase.findings
    fb_types = q("""
        SELECT finding_type, COUNT(*) AS cnt
        FROM   firebase.findings
        GROUP  BY finding_type
        ORDER  BY cnt ASC
        LIMIT  20
    """)
    rare_types = [r for r in fb_types if r["cnt"] <= 3]
    if rare_types:
        patterns.append({
            "pattern":          "rare_firebase_finding_types",
            "description":      f"{len(rare_types)} Firebase finding type(s) seen ≤3 times — possibly novel or misclassified.",
            "supporting_data":  rare_types,
            "suggested_action": "Inspect these findings manually. Could be new credential classes.",
        })

    # Pattern C: File size vs finding rate
    size_data = q("""
        SELECT
            CASE
                WHEN file_size < 10000   THEN '<10KB'
                WHEN file_size < 100000  THEN '10-100KB'
                WHEN file_size < 1000000 THEN '100KB-1MB'
                ELSE '>1MB'
            END AS size_bucket,
            COUNT(*) AS files,
            ROUND(AVG(COALESCE(trufflehog_count,0))::numeric, 3) AS avg_secrets
        FROM bucket.scan_files
        GROUP BY 1
        ORDER BY MIN(file_size)
    """)
    if size_data:
        avg_vals = [r["avg_secrets"] for r in size_data if r["avg_secrets"] is not None]
        if avg_vals and (max(avg_vals) - min(avg_vals)) > 0.5:
            patterns.append({
                "pattern":          "file_size_finding_correlation",
                "description":      "File size correlates with secret density — not all size bands are equal.",
                "supporting_data":  size_data,
                "suggested_action": "Prioritize scanning files in the high-yield size band.",
            })

    # Pattern D: Targets in BOTH bucket and firebase schemas (slug overlap)
    overlap = q("""
        SELECT bt.slug, bt.app_name,
               ft.firebase_url, ft.severity AS fb_severity
        FROM   bucket.targets bt
        JOIN   firebase.targets ft ON LOWER(bt.slug) = LOWER(ft.slug)
        WHERE  bt.slug IS NOT NULL
        LIMIT  50
    """)
    if overlap:
        patterns.append({
            "pattern":          "cross_schema_target_overlap",
            "description":      f"{len(overlap)} target(s) appear in both bucket.targets and firebase.targets.",
            "supporting_data":  overlap[:10],
            "suggested_action": "These orgs have both S3 and Firebase exposure. High-priority combined audit.",
        })

    # Pattern E: Verified secrets that have never triggered a disclosure
    # Join path: scan_secrets → scan_files → bucket.targets → firebase.disclosure_queue via slug
    disc_check = q("""
        SELECT ss.detector_name, COUNT(*) AS verified_no_disclosure
        FROM   bucket.scan_secrets ss
        JOIN   bucket.scan_files   sf  ON sf.id = ss.scan_file_id
        JOIN   bucket.targets      bt  ON bt.id = sf.bucket_id
        LEFT   JOIN firebase.disclosure_queue dq
               ON LOWER(dq.target_slug) = LOWER(bt.slug)
        WHERE  ss.verified = TRUE
          AND  dq.id IS NULL
        GROUP  BY ss.detector_name
        ORDER  BY 2 DESC
        LIMIT  10
    """)
    if disc_check:
        patterns.append({
            "pattern":          "verified_secrets_without_disclosure",
            "description":      "Verified-live secrets with no corresponding disclosure queue entry.",
            "supporting_data":  disc_check,
            "suggested_action": "Hand to Ratchet for disclosure workflow.",
        })

    return patterns


# ─── 5. propose_tool ──────────────────────────────────────────────────────────

def propose_tool(problem: str) -> dict:
    """
    Given a problem description, propose a new scout/tool.
    """
    p = problem.lower()

    # Heuristic matching
    if any(kw in p for kw in ["github", "git", "repo", "source code", "commit"]):
        return {
            "name":           "Mosaic 🧩",
            "emoji":          "🧩",
            "purpose":        "GitHub org/repo scanner — find leaked secrets in public commits, gists, and Actions logs",
            "reads":          ["GitHub API", "bucket.targets.app_package for org names"],
            "produces":       ["bucket.scan_secrets (detector=github_trufflehog)", "bucket.scan_files"],
            "feeds":          ["Q 🔬 (cross-linking)", "Ratchet ⚙️ (disclosure)"],
            "complexity":     "medium",
            "rationale":      "Current mesh covers S3 + Firebase but not source repos. Many creds live in code.",
        }
    elif any(kw in p for kw in ["npm", "pypi", "package", "dependency", "registry"]):
        return {
            "name":           "Venom 🐍",
            "emoji":          "🐍",
            "purpose":        "Package registry scanner — scan npm/PyPI packages for embedded secrets",
            "reads":          ["npm registry API", "PyPI JSON API", "bucket.targets for package names"],
            "produces":       ["bucket.scan_secrets", "novel findings in public package metadata"],
            "feeds":          ["Q 🔬", "Atlas 🗺️ (target discovery)"],
            "complexity":     "medium",
            "rationale":      "Devs accidentally publish .env files inside npm tarballs. High signal.",
        }
    elif any(kw in p for kw in ["ios", "apk", "android", "mobile", "app store", "ipa"]):
        return {
            "name":           "Splinter 📱",
            "emoji":          "📱",
            "purpose":        "Mobile binary scanner — extract hardcoded secrets from APKs and IPAs",
            "reads":          ["app_package from firebase.targets", "Google Play / App Store metadata"],
            "produces":       ["bucket.scan_secrets with detector=mobile_hardcoded", "firebase.findings"],
            "feeds":          ["Q 🔬 (cross-linking app_package ↔ bucket slugs)", "Ratchet ⚙️"],
            "complexity":     "complex",
            "rationale":      "Firebase targets already have app_package. Splinter decompiles and scans.",
        }
    elif any(kw in p for kw in ["dedup", "duplicate", "same file", "hash", "identical"]):
        return {
            "name":           "Prism 🔷",
            "emoji":          "🔷",
            "purpose":        "Deduplication engine — hash-based file dedup across all scan sources",
            "reads":          ["bucket.scan_files.content_sha256", "bucket.scan_files.etag"],
            "produces":       ["bucket.scan_files.duplicate_of (populated)", "dedup stats in Redis"],
            "feeds":          ["Charlie 🐀 (queue management)", "Atlas 🗺️"],
            "complexity":     "simple",
            "rationale":      "Schema has duplicate_of column but it's not being populated. Quick win.",
        }
    elif any(kw in p for kw in ["alert", "notify", "slack", "discord", "webhook", "real-time"]):
        return {
            "name":           "Flare 🚨",
            "emoji":          "🚨",
            "purpose":        "Real-time alerting scout — watch for high-confidence verified secrets and fire webhooks",
            "reads":          ["bucket.scan_secrets WHERE verified=TRUE", "Redis pub/sub new_verified_secret"],
            "produces":       ["Slack/Discord/webhook notifications", "audit log in Redis"],
            "feeds":          ["Ratchet ⚙️ (triggers disclosure)", "RJ directly via alert channel"],
            "complexity":     "simple",
            "rationale":      "Verified secrets need human eyes fast. Flare bridges scan → human in <60s.",
        }
    else:
        # Generic proposal
        return {
            "name":           "Unknown Scout ❓",
            "emoji":          "❓",
            "purpose":        f"Scout to solve: {problem}",
            "reads":          ["To be determined"],
            "produces":       ["To be determined"],
            "feeds":          ["Q 🔬 (all scouts feed Q)"],
            "complexity":     "unknown",
            "rationale":      "Problem didn't match a known pattern. Q needs more data to propose a specific tool.",
        }


# ─── 6. q_report ─────────────────────────────────────────────────────────────

def q_report() -> str:
    today = date.today().isoformat()
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    print(f"[Q] Running cross_link_findings()...")
    links = cross_link_findings()

    print(f"[Q] Running scan_for_novel_patterns()...")
    patterns = scan_for_novel_patterns()

    # Check Redis for queued hypotheses
    r = get_redis()
    queued_hyps = r.lrange("q:hypothesis_queue", 0, -1)
    hyp_results = []
    for hyp in queued_hyps:
        print(f"[Q] Testing hypothesis: {hyp}")
        hyp_results.append(hypothesis_test(hyp))
    if queued_hyps:
        r.delete("q:hypothesis_queue")

    # Build report
    lines = [
        f"# Q 🔬 Report — {today}",
        f"_Generated: {ts}_",
        "",
        "---",
        "",
        f"## Cross-Links Found ({len(links)})",
        "",
    ]
    if links:
        for i, lnk in enumerate(links[:20], 1):
            lines += [
                f"### Link {i} — `{lnk['link_type']}` (confidence: {lnk['confidence']:.0%})",
                f"- **A:** {lnk['source_a']}",
                f"- **B:** {lnk['source_b']}",
                f"- {lnk['description']}",
                "",
            ]
        if len(links) > 20:
            lines.append(f"_…and {len(links) - 20} more. Run `--cross-link` for full output._\n")
    else:
        lines.append("_No cross-links found. Either data is sparse or schemas are well-isolated._\n")

    lines += [
        "---",
        "",
        f"## Novel Patterns ({len(patterns)})",
        "",
    ]
    for pat in patterns:
        lines += [
            f"### 🔍 `{pat['pattern']}`",
            f"{pat['description']}",
            f"**Suggested action:** {pat['suggested_action']}",
            "",
            "```json",
            json.dumps(pat['supporting_data'][:3], indent=2, default=str),
            "```",
            "",
        ]
    if not patterns:
        lines.append("_No novel patterns detected._\n")

    if hyp_results:
        lines += ["---", "", "## Hypothesis Tests", ""]
        for hr in hyp_results:
            lines += [
                f"### Hypothesis: _{hr['hypothesis']}_",
                f"**Result:** {hr['result']} (confidence: {hr.get('confidence', '?'):.0%})",
                f"**Query:** `{hr['query_used']}`",
                "",
                "```json",
                json.dumps(hr['evidence'][:3], indent=2, default=str),
                "```",
                "",
            ]

    lines += [
        "---",
        "",
        "## Q's Notes",
        "",
        "- Q proposes, Ratchet deploys. Nothing leaves scratch space without review.",
        "- To queue a hypothesis: `redis-cli -h audit-redis -a <pass> RPUSH q:hypothesis_queue 'your hypothesis'`",
        "- Cross-links with confidence ≥0.85 and verified=TRUE → hand to Ratchet immediately.",
        "",
    ]

    report = "\n".join(lines)

    # Save
    out_path = WORKSPACE / "planning" / f"q-report-{today}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"[Q] Report saved → {out_path}")

    # Cache summary in Redis
    try:
        r.set("q:last_report_date", today, ex=86400 * 7)
        r.set("q:last_link_count",  str(len(links)), ex=86400 * 7)
        r.set("q:last_pattern_count", str(len(patterns)), ex=86400 * 7)
    except Exception as e:
        print(f"[Q] Redis cache warning: {e}", file=sys.stderr)

    return report


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Q 🔬 — Mesh Research Specialist")
    parser.add_argument("--cross-link", action="store_true", help="Run cross_link_findings()")
    parser.add_argument("--patterns",   action="store_true", help="Run scan_for_novel_patterns()")
    parser.add_argument("--wordlist",   metavar="ORGS",      help="Generate bucket wordlist for comma-separated org names")
    parser.add_argument("--hypothesis", metavar="TEXT",      help="Test a hypothesis against the DB")
    parser.add_argument("--report",     action="store_true", help="Full Q report (saves to planning/)")
    parser.add_argument("--propose",    metavar="PROBLEM",   help="Propose a new scout for a problem")
    args = parser.parse_args()

    if args.cross_link:
        print("[Q] Cross-linking findings across schemas…")
        links = cross_link_findings()
        print(f"\n[Q] Found {len(links)} unique cross-link(s):\n")
        # Group by link_type for summary
        from collections import Counter
        type_counts = Counter(lnk["link_type"] for lnk in links)
        for lt, cnt in type_counts.most_common():
            print(f"  {cnt:4d}x  [{lt}]")
        print()
        print("Top links (up to 10):")
        for lnk in links[:10]:
            print(f"  [{lnk['link_type']}] conf={lnk['confidence']:.0%}")
            print(f"    A: {lnk['source_a']}")
            print(f"    B: {lnk['source_b']}")
            print(f"    → {lnk['description']}")
            print()

    if args.patterns:
        print("[Q] Scanning for novel patterns…")
        patterns = scan_for_novel_patterns()
        print(f"\n[Q] Found {len(patterns)} pattern(s):\n")
        for pat in patterns:
            print(f"  🔍 {pat['pattern']}")
            print(f"     {pat['description']}")
            print(f"     Action: {pat['suggested_action']}")
            print(f"     Data:   {json.dumps(pat['supporting_data'][:2], default=str)}")
            print()

    if args.wordlist:
        orgs = [o.strip() for o in args.wordlist.split(",") if o.strip()]
        print(f"[Q] Generating bucket wordlist for {len(orgs)} org(s)…")
        candidates = generate_bucket_wordlist(orgs)
        print(f"[Q] {len(candidates)} candidates:\n")
        for c in candidates:
            print(c)

    if args.hypothesis:
        print(f"[Q] Testing hypothesis: {args.hypothesis!r}")
        result = hypothesis_test(args.hypothesis)
        print(f"\nResult:     {result['result']}")
        print(f"Confidence: {result['confidence']:.0%}")
        print(f"Query:      {result['query_used']}")
        print(f"Evidence ({len(result['evidence'])} rows):")
        print(json.dumps(result['evidence'][:5], indent=2, default=str))

    if args.propose:
        print(f"[Q] Proposing tool for: {args.propose!r}")
        proposal = propose_tool(args.propose)
        print(json.dumps(proposal, indent=2))

    if args.report:
        print("[Q] Building full report…\n")
        report = q_report()
        # Print first 80 lines to stdout for visibility
        for line in report.split("\n")[:80]:
            print(line)
        print("\n[Q] Full report saved to planning/.")

    if not any([args.cross_link, args.patterns, args.wordlist,
                args.hypothesis, args.report, args.propose]):
        parser.print_help()


if __name__ == "__main__":
    main()
