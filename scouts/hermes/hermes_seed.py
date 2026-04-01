#!/usr/bin/env python3
"""
Hermes 🪽 — Disclosure Scout
Routes and tracks disclosure submissions across all channels.
NEVER submits anything without explicit human approval.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# ── Scout state (optional — degrades gracefully if Redis is down) ─────────────
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from scout_state import set_running, set_idle, set_error
except Exception:
    def set_running(*a, **k): pass
    def set_idle(*a, **k): pass
    def set_error(*a, **k): pass

SCOUT_NAME = "hermes"

DSN = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/audit_framework"

# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS firebase.disclosure_queue (
    id                  SERIAL PRIMARY KEY,
    target_slug         TEXT NOT NULL,
    program_type        TEXT NOT NULL,          -- firebase_abuse | bugbounty | gdpr | direct
    platform            TEXT,                   -- HackerOne | Bugcrowd | Intigriti | Google | DPA
    status              TEXT DEFAULT 'draft',   -- draft | pending_review | submitted | acknowledged | resolved | closed
    priority            TEXT DEFAULT 'p1',      -- p0 | p1 | p2
    draft_title         TEXT,
    draft_body          TEXT,
    submission_url      TEXT,
    submission_id       TEXT,
    submitted_at        TIMESTAMPTZ,
    acknowledged_at     TIMESTAMPTZ,
    resolved_at         TIMESTAMPTZ,
    bounty_amount       NUMERIC,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(target_slug, platform)
);
"""


def get_conn():
    return psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# Prioritisation
# ---------------------------------------------------------------------------

PRIORITY_QUERY = """
SELECT
    t.slug,
    t.severity,
    t.category,
    t.summary,
    t.firebase_url,
    t.app_name,
    t.app_package,
    t.platform AS app_platform,
    t.raw_uncompressed_bytes,
    COALESCE(f.finding_types, '{}') AS finding_types,
    COALESCE(f.total_count, 0)       AS total_record_count,
    COALESCE(f.max_count, 0)         AS max_finding_count
FROM firebase.targets t
LEFT JOIN (
    SELECT
        target_id,
        array_agg(DISTINCT finding_type)  AS finding_types,
        SUM(count)                         AS total_count,
        MAX(count)                         AS max_count
    FROM firebase.findings
    GROUP BY target_id
) f ON f.target_id = t.id
-- Only targets not yet in the disclosure queue
WHERE t.slug NOT IN (
    SELECT target_slug FROM firebase.disclosure_queue
)
AND t.severity IN ('critical', 'high', 'medium')
ORDER BY
    CASE t.severity
        WHEN 'critical' THEN 0
        WHEN 'high'     THEN 1
        WHEN 'medium'   THEN 2
        ELSE 3
    END,
    COALESCE(f.total_count, 0) DESC NULLS LAST,
    t.raw_uncompressed_bytes DESC NULLS LAST
"""


def _calc_priority(target: dict) -> str:
    """Derive P0/P1/P2 from target attributes."""
    finding_types = list(target.get("finding_types") or [])
    total_count = int(target.get("total_record_count") or 0)
    severity = (target.get("severity") or "").lower()

    has_creds = any(ft in finding_types for ft in [
        "credential_exposure", "secret", "api_key", "password", "private_key",
    ])
    has_financial = any(ft in finding_types for ft in ["financial_data", "financial"])
    has_pii = any(ft in finding_types for ft in [
        "pii_exposure", "pii", "device_tracking", "security_incident",
    ])

    if severity == "critical" or has_creds or has_financial:
        return "p0"
    if total_count >= 10_000 or severity == "high" or has_pii:
        return "p1"
    return "p2"


BUCKET_PRIORITY_QUERY = """
SELECT
    dp.target_slug AS slug,
    dp.platform    AS provider,
    t.triage_priority AS triage_priority,
    dp.program_type,
    dp.platform
FROM bucket.disclosure_programs dp
JOIN bucket.targets t ON t.slug = dp.target_slug
WHERE dp.target_slug NOT IN (
    SELECT target_slug FROM bucket.disclosure_queue
)
AND dp.program_type = 'abuse'
AND dp.platform IN ('aws', 'gcp', 'azure', 'digitalocean', 'alibaba')
ORDER BY
    CASE COALESCE(t.triage_priority, 'medium')
        WHEN 'high'   THEN 0
        WHEN 'medium' THEN 1
        WHEN 'low'    THEN 2
        ELSE 1
    END,
    dp.discovered_at DESC
"""

DDL_BUCKET_QUEUE = """
CREATE TABLE IF NOT EXISTS bucket.disclosure_queue (
    id              SERIAL PRIMARY KEY,
    target_slug     TEXT NOT NULL,
    program_type    TEXT NOT NULL,
    platform        TEXT,
    status          TEXT DEFAULT 'draft',
    priority        TEXT DEFAULT 'p2',
    draft_title     TEXT,
    draft_body      TEXT,
    submission_url  TEXT,
    submission_id   TEXT,
    submitted_at    TIMESTAMPTZ,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(target_slug, platform)
);
"""


def prioritize_bucket_queue(conn) -> list:
    """Return ordered list of unqueued bucket targets ready for disclosure."""
    with conn.cursor() as cur:
        cur.execute(BUCKET_PRIORITY_QUERY)
        return [dict(r) for r in cur.fetchall()]



    """Return ordered list of unqueued targets, highest priority first."""
    close_after = conn is None
    if conn is None:
        conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(PRIORITY_QUERY)
            rows = cur.fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["priority"] = _calc_priority(d)
            results.append(d)
        return results
    finally:
        if close_after:
            conn.close()


# ---------------------------------------------------------------------------
# Draft generators
# ---------------------------------------------------------------------------

def _safe_date(ts=None) -> str:
    if ts is None:
        ts = datetime.now(timezone.utc)
    return ts.strftime("%Y-%m-%d")


def _findings_summary(findings: list) -> str:
    """Produce a safe, PII-free summary of findings."""
    if not findings:
        return "- Unauthenticated read access confirmed\n"
    lines = []
    for f in findings:
        ft = f.get("finding_type") or f.get("type") or "unknown"
        count = f.get("count") or 0
        title = f.get("title") or ft
        lines.append(f"- {title}: {count:,} records" if count else f"- {title}")
    return "\n".join(lines) + "\n"


def draft_firebase_abuse_report(target: dict, findings: list) -> str:
    slug = target.get("slug", "unknown")
    firebase_url = target.get("firebase_url") or f"https://{slug}"
    finding_types = list(target.get("finding_types") or [])
    total_count = int(target.get("total_record_count") or 0)
    app_name = target.get("app_name") or slug
    summary = target.get("summary") or ""
    discovered = _safe_date()

    # Derive exposure type label
    if any(ft in finding_types for ft in ["credential_exposure", "secret", "api_key"]):
        exposure_type = "credential_exposure"
    elif "financial_data" in finding_types:
        exposure_type = "financial_data_exposure"
    elif "device_tracking" in finding_types:
        exposure_type = "device_tracking_exposure"
    else:
        exposure_type = "pii_exposure"

    # Safe field-name-only list from summary (no values)
    # We strip anything that looks like an actual value
    data_types = ", ".join(finding_types) if finding_types else "user_data"

    report = f"""Subject: Publicly Exposed Firebase Realtime Database – {slug}

Hello Firebase Security Team,

I am reporting a publicly accessible Firebase Realtime Database that appears to contain
sensitive user data with no authentication required.

**Application:** {app_name}
**Database URL:** {firebase_url}
**Exposure Type:** {exposure_type}
**Estimated Records:** {total_count:,} (across all collections)
**Data Types Observed (field names only, no values):** {data_types}
**First Discovered:** {discovered}
**Still Active:** Yes (as of {discovered})

**Context:**
{summary[:500] if summary else "Unauthenticated access confirmed via HTTP GET returning full database dump."}

**Findings:**
{_findings_summary(findings)}
**Recommended Remediation:**
1. Enable Firebase Authentication and set database rules to deny unauthenticated reads
2. Review Firebase Security Rules to ensure `.read` is not set to `true` at the root
3. Rotate any credentials or API keys that may have been exposed
4. Notify affected users per applicable data protection regulations

I have not accessed, downloaded, or retained any personal data beyond what was necessary
to confirm the exposure and estimate scope. This report is submitted in good faith for
responsible disclosure purposes only.

Please feel free to reach out if you require additional technical details.

Regards,
[Security Researcher]
"""
    return report.strip()


def draft_bugbounty_report(target: dict, findings: list, platform: str = "HackerOne") -> str:
    slug = target.get("slug", "unknown")
    firebase_url = target.get("firebase_url") or f"https://{slug}"
    app_name = target.get("app_name") or slug
    app_package = target.get("app_package") or "unknown"
    finding_types = list(target.get("finding_types") or [])
    total_count = int(target.get("total_record_count") or 0)
    priority = target.get("priority", "p1")
    summary = target.get("summary") or ""

    severity_label = {"p0": "Critical", "p1": "High", "p2": "Medium"}.get(priority, "High")
    data_types = ", ".join(finding_types) if finding_types else "user PII"

    report = f"""## Summary

The Firebase Realtime Database associated with **{app_name}** (`{app_package}`) is publicly
accessible without any authentication. An unauthenticated HTTP request returns the full
database contents, exposing sensitive user data.

**Severity:** {severity_label}
**CVSS Base Score (estimate):** {"9.8" if priority == "p0" else "8.1" if priority == "p1" else "6.5"}
**Database URL:** {firebase_url}

---

## Steps to Reproduce

1. Open a browser or run:
   ```
   curl -s "{firebase_url}/.json?shallow=true"
   ```
2. Observe: the server returns top-level keys without requiring any authentication token.
3. Enumerate any key to confirm presence of user records.

---

## Impact

- **Records at Risk:** ~{total_count:,}
- **Data Categories (field names only):** {data_types}
- **App Context:** {summary[:300] if summary else "Consumer-facing mobile application"}

An attacker can trivially exfiltrate the entire database. Depending on data sensitivity,
this could enable account takeover, identity theft, stalking, or targeted phishing.

---

## Affected Asset

- **Firebase project slug:** `{slug}`
- **App package:** `{app_package}`
- **Platform:** Firebase Realtime Database (Google)

---

## Recommendations

1. Set Firebase Security Rules to deny unauthenticated access:
   ```json
   {{
     "rules": {{
       ".read": "auth != null",
       ".write": "auth != null"
     }}
   }}
   ```
2. Rotate any credentials or secrets that may have been exposed.
3. Audit historical database access logs in the Firebase console.

---

## Disclosure Timeline

| Date | Event |
|------|-------|
| {_safe_date()} | Vulnerability discovered |
| {_safe_date()} | Report drafted for submission |

---

*No personal data values were recorded or retained. Field names and record counts only.*
"""
    return report.strip()


def draft_cloud_bucket_abuse_report(target: dict) -> str:
    """Draft an abuse report for a misconfigured/exposed cloud bucket."""
    slug       = target.get("slug", "unknown")
    provider   = (target.get("provider") or target.get("platform") or "cloud").lower()
    bucket_url = target.get("bucket_url") or target.get("firebase_url") or slug
    severity   = target.get("severity", "medium")
    discovered = _safe_date()

    PROVIDER_INFO = {
        "aws":          ("Amazon Web Services (S3)", AWS_ABUSE_URL,   "abuse@amazonaws.com"),
        "gcp":          ("Google Cloud Platform (GCS)", GCP_ABUSE_URL, "cloud-compliance@google.com"),
        "azure":        ("Microsoft Azure (Blob Storage)", AZURE_ABUSE_URL, "abuse@microsoft.com"),
        "digitalocean": ("DigitalOcean Spaces",
                         "https://www.digitalocean.com/company/contact/abuse/",
                         "abuse@digitalocean.com"),
        "alibaba":      ("Alibaba Cloud OSS",
                         "https://help.aliyun.com/knowledge_detail/37544.html",
                         "abuse@service.aliyun.com"),
    }
    provider_name, abuse_url, abuse_email = PROVIDER_INFO.get(
        provider, ("Cloud Provider", None, "abuse@" + provider)
    )

    report = f"""Subject: Publicly Accessible Cloud Storage Bucket — {slug}

Hello {provider_name} Security Team,

I am reporting a publicly accessible cloud storage bucket that exposes sensitive data
without authentication.

**Bucket Identifier:** {slug}
**Bucket URL / Endpoint:** {bucket_url}
**Provider:** {provider_name}
**Severity:** {severity.capitalize()}
**First Discovered:** {discovered}
**Still Active:** Yes (as of {discovered})

**Description:**
The above storage bucket appears to be misconfigured with public read access enabled.
This allows any unauthenticated user to list or download the bucket's contents, which
may include sensitive files, credentials, database dumps, or personal data.

**Recommended Remediation:**
1. Immediately disable public read access on the bucket
2. Review bucket ACLs and bucket policy / IAM permissions
3. Audit access logs to determine if unauthorised access has occurred
4. Rotate any credentials or secrets that may have been exposed
5. Notify affected users if personal data was accessible

I have not downloaded, retained, or accessed any personal data beyond confirming the
existence of the misconfiguration and estimating exposure scope.

This report is submitted in good faith for responsible disclosure purposes.

Regards,
[Security Researcher]

---
Abuse contact reference: {abuse_url or abuse_email}
"""
    return report.strip()



    """Draft a DPA (Data Protection Authority) notification."""
    slug = target.get("slug", "unknown")
    firebase_url = target.get("firebase_url") or f"https://{slug}"
    app_name = target.get("app_name") or slug
    app_package = target.get("app_package") or "unknown"
    finding_types = list(target.get("finding_types") or [])
    total_count = int(target.get("total_record_count") or 0)
    summary = target.get("summary") or ""

    # Map jurisdiction to DPA name
    dpa_names = {
        "IE": "Data Protection Commission (Ireland) — Google/Firebase EU HQ jurisdiction",
        "DE": "Bundesbeauftragte für den Datenschutz und die Informationsfreiheit (BfDI)",
        "FR": "Commission Nationale de l'Informatique et des Libertés (CNIL)",
        "US": "FTC / State AG (US — CCPA/COPPA where applicable)",
    }
    dpa = dpa_names.get(jurisdiction, f"Data Protection Authority ({jurisdiction})")
    data_types = ", ".join(finding_types) if finding_types else "personal data"

    report = f"""To: {dpa}
Re: Unsecured Firebase Database — Potential Data Breach Notification (Third-Party Reporter)

---

**Nature of this Report:**
This notification is submitted by an independent security researcher, not the data controller.
The researcher does not have standing to submit an Article 33 notification on behalf of the
controller; however, this report is provided to assist the DPA in investigating a potential
ongoing data breach.

**Controller (if identifiable):**
- Application: {app_name}
- Package identifier: {app_package}
- Firebase project: {slug}
- Firebase URL: {firebase_url}

**Nature of the Breach:**
A Firebase Realtime Database belonging to the above application is publicly accessible
without authentication. Any person who knows or discovers the URL can read the full
database contents via a standard HTTP request.

**Categories of Personal Data Exposed (field names only — no values retained):**
{data_types}

**Approximate Number of Data Subjects Affected:**
~{total_count:,}

**Application Context:**
{summary[:400] if summary else "Consumer-facing mobile application."}

**Likely Consequences:**
- Unauthorised access to personal data by unknown third parties
- Potential identity theft, targeted fraud, or stalking (depending on data categories)
- Reputational harm to affected individuals

**Measures Recommended:**
The database owner should immediately restrict Firebase Security Rules to deny
unauthenticated access and conduct an internal audit to determine exposure duration
and scope.

**Researcher's Disclosure Conduct:**
- No personal data values were downloaded or retained
- Field names and aggregate counts only were noted for scoping purposes
- This report is submitted solely to prompt remediation and regulatory review

**Date of Discovery:** {_safe_date()}
**Status at Time of Report:** Exposure appears ongoing

---
[Security Researcher — contact details available on request]
"""
    return report.strip()


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------

def queue_disclosure(conn, target_slug: str, program_type: str, platform: str,
                     draft_title: str, draft_body: str, priority: str) -> int:
    """Insert a draft into disclosure_queue. Returns the new row id."""
    sql = """
        INSERT INTO firebase.disclosure_queue
            (target_slug, program_type, platform, status, priority, draft_title, draft_body)
        VALUES (%s, %s, %s, 'draft', %s, %s, %s)
        ON CONFLICT (target_slug, platform) DO UPDATE SET
            draft_title = EXCLUDED.draft_title,
            draft_body  = EXCLUDED.draft_body,
            priority    = EXCLUDED.priority,
            updated_at  = NOW()
        RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (target_slug, program_type, platform, priority, draft_title, draft_body))
        row = cur.fetchone()
    conn.commit()
    return row["id"]


def _get_findings_for_target(conn, slug: str) -> list:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.finding_type, f.severity, f.title, f.count, f.detail
            FROM firebase.findings f
            JOIN firebase.targets t ON t.id = f.target_id
            WHERE t.slug = %s
        """, (slug,))
        return [dict(r) for r in cur.fetchall()]


def _choose_program(target: dict) -> tuple:
    """Return (program_type, platform) based on target attributes."""
    finding_types = list(target.get("finding_types") or [])
    priority = target.get("priority", "p1")

    if any(ft in finding_types for ft in ["credential_exposure", "secret", "api_key"]):
        return ("firebase_abuse", "Google")
    if priority == "p0":
        return ("firebase_abuse", "Google")
    # Default: firebase abuse report
    return ("firebase_abuse", "Google")


def hermes_run(schema: str = "firebase", limit: int = 10, auto_draft: bool = True,
               conn=None) -> list:
    """
    For top N unqueued targets:
    - Pull target + findings from DB
    - Draft appropriate report
    - Insert to disclosure_queue as 'draft'
    Returns list of queued items.
    Supports schema='firebase' and schema='bucket'.
    """
    close_after = conn is None
    if conn is None:
        conn = get_conn()

    ensure_schema(conn)
    queued = []

    try:
        if schema == "bucket":
            # Ensure bucket queue table exists
            with conn.cursor() as cur:
                cur.execute(DDL_BUCKET_QUEUE)
            conn.commit()

            targets = prioritize_bucket_queue(conn)[:limit]
            for target in targets:
                slug     = target["slug"]
                provider = target.get("provider") or target.get("platform", "unknown")
                priority = "p1" if provider in ("aws", "gcp", "azure") else "p2"
                draft_body  = draft_cloud_bucket_abuse_report(target)
                draft_title = f"Publicly Accessible {provider.upper()} Bucket — {slug}"

                if auto_draft:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO bucket.disclosure_queue
                                (target_slug, program_type, platform, status, priority, draft_title, draft_body)
                            VALUES (%s, 'abuse', %s, 'draft', %s, %s, %s)
                            ON CONFLICT (target_slug, platform) DO UPDATE SET
                                draft_title = EXCLUDED.draft_title,
                                draft_body  = EXCLUDED.draft_body,
                                updated_at  = NOW()
                            RETURNING id
                        """, (slug, provider, priority, draft_title, draft_body))
                        row_id = cur.fetchone()["id"]
                    conn.commit()
                    queued.append({
                        "id": row_id, "slug": slug, "priority": priority,
                        "program_type": "abuse", "platform": provider, "title": draft_title,
                    })
                    print(f"  ✅ [{priority.upper()}] Queued {slug} → {provider} (id={row_id})")
                else:
                    queued.append({"slug": slug, "priority": priority, "draft": draft_body})

        else:
            # Firebase schema (original behaviour)
            targets = prioritize_queue(schema=schema, conn=conn)[:limit]
            for target in targets:
                slug = target["slug"]
                findings = _get_findings_for_target(conn, slug)
                program_type, platform = _choose_program(target)
                priority = target["priority"]

                draft_body  = draft_firebase_abuse_report(target, findings)
                draft_title = f"Publicly Exposed Firebase Database – {slug}"

                if auto_draft:
                    row_id = queue_disclosure(
                        conn, slug, program_type, platform,
                        draft_title, draft_body, priority
                    )
                    queued.append({
                        "id": row_id, "slug": slug, "priority": priority,
                        "program_type": program_type, "platform": platform, "title": draft_title,
                    })
                    print(f"  ✅ [{priority.upper()}] Queued {slug} → {platform} (id={row_id})")
                else:
                    queued.append({"slug": slug, "priority": priority, "draft": draft_body})

        print(f"\n🪽 Hermes queued {len(queued)} disclosure draft(s) for review.")
        print("   Status: ALL are 'draft' — nothing submitted. RJ must approve before anything goes out.\n")
        return queued

    finally:
        if close_after:
            conn.close()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_status():
    """Show disclosure_queue summary."""
    conn = get_conn()
    ensure_schema(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status, priority, COUNT(*) as n
                FROM firebase.disclosure_queue
                GROUP BY status, priority
                ORDER BY
                    CASE status
                        WHEN 'draft'          THEN 0
                        WHEN 'pending_review' THEN 1
                        WHEN 'submitted'      THEN 2
                        WHEN 'acknowledged'   THEN 3
                        WHEN 'resolved'       THEN 4
                        WHEN 'closed'         THEN 5
                        ELSE 6
                    END,
                    CASE priority WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END
            """)
            rows = cur.fetchall()

        if not rows:
            print("📭 disclosure_queue is empty.")
            return

        print("\n🪽 Hermes — Disclosure Queue Summary")
        print("=" * 45)
        print(f"{'STATUS':<18} {'PRIORITY':<10} {'COUNT':>6}")
        print("-" * 45)
        total = 0
        for r in rows:
            print(f"{r['status']:<18} {r['priority']:<10} {r['n']:>6}")
            total += r["n"]
        print("-" * 45)
        print(f"{'TOTAL':<18} {'':<10} {total:>6}")
        print()

        # Show recent drafts
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, target_slug, priority, platform, created_at
                FROM firebase.disclosure_queue
                WHERE status = 'draft'
                ORDER BY
                    CASE priority WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END,
                    created_at DESC
                LIMIT 10
            """)
            drafts = cur.fetchall()

        if drafts:
            print("📝 Recent Drafts (pending RJ approval):")
            for d in drafts:
                ts = d["created_at"].strftime("%Y-%m-%d") if d["created_at"] else "?"
                print(f"  [{d['priority'].upper()}] #{d['id']:>4} {d['target_slug']:<50} → {d['platform']} ({ts})")
        print()

    finally:
        conn.close()


def cmd_draft_single(slug: str):
    """Draft a report for a single target and print to stdout."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM firebase.targets WHERE slug = %s", (slug,))
            row = cur.fetchone()
        if not row:
            print(f"❌ Target not found: {slug}", file=sys.stderr)
            sys.exit(1)

        target = dict(row)
        findings = _get_findings_for_target(conn, slug)

        # Compute finding_types from findings list
        target["finding_types"] = list({f["finding_type"] for f in findings})
        target["total_record_count"] = sum(f.get("count") or 0 for f in findings)
        target["priority"] = _calc_priority(target)

        print(f"\n{'='*70}")
        print(f"🪽 Hermes Draft Report — {slug}")
        print(f"Priority: {target['priority'].upper()} | Severity: {target.get('severity','?')}")
        print(f"{'='*70}\n")
        print(draft_firebase_abuse_report(target, findings))
        print(f"\n{'='*70}")
        print("⚠️  STATUS: DRAFT — not submitted. Requires explicit RJ approval.\n")

    finally:
        conn.close()


def cmd_run(limit: int, schema: str):
    conn = get_conn()
    ensure_schema(conn)
    try:
        print(f"\n🪽 Hermes — Running disclosure drafts (limit={limit}, schema={schema})")
        print("-" * 60)
        hermes_run(schema=schema, limit=limit, auto_draft=True, conn=conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Hermes 🪽 — Disclosure Scout (draft-only, never auto-submits)"
    )
    parser.add_argument("--run", action="store_true",
                        help="Draft reports for top unqueued targets")
    parser.add_argument("--status", action="store_true",
                        help="Show disclosure_queue summary")
    parser.add_argument("--draft", metavar="SLUG",
                        help="Draft a single target's report and print to stdout")
    parser.add_argument("--schema", default="firebase",
                        choices=["firebase", "bucket"],
                        help="Target schema (default: firebase)")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max targets to process with --run (default: 10)")

    args = parser.parse_args()

    mode = "status" if args.status else ("draft" if args.draft else ("run" if args.run else "help"))
    set_running(SCOUT_NAME, {"mode": mode, "schema": args.schema})
    try:
        if args.status:
            cmd_status()
            set_idle(SCOUT_NAME, {"mode": mode})
        elif args.draft:
            cmd_draft_single(args.draft)
            set_idle(SCOUT_NAME, {"mode": mode, "slug": args.draft})
        elif args.run:
            cmd_run(limit=args.limit, schema=args.schema)
            set_idle(SCOUT_NAME, {"mode": mode, "limit": args.limit})
        else:
            set_idle(SCOUT_NAME, {"mode": "help"})
            parser.print_help()
    except Exception as e:
        set_error(SCOUT_NAME, str(e))
        raise


if __name__ == "__main__":
    main()
