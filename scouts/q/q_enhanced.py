#!/usr/bin/env python3
"""
q_enhanced.py - Organization Analyzer with Database Integration
Writes findings to pipeline.org_findings instead of memory

Updated 2026-04-01:
  - Mine firebase.targets (3,913 rows) instead of firebase.disclosure_metadata (42 rows)
  - Also mine firebase.disclosure_programs contact_email for org signals
  - Added RedisDedup + RedisCheckpoint from scouts/ utilities
"""

import psycopg2
import json
import re
from datetime import datetime
import sys

sys.path.insert(0, '/home/openclaw/.openclaw/workspace/agent-mesh/scouts')
from dedup import RedisDedup
from checkpoint import RedisCheckpoint

DB_DSN = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/audit_framework"

def get_db():
    return psycopg2.connect(DB_DSN)

def get_redis_state():
    try:
        return RedisDedup("q:seen_domains", ttl_days=30), RedisCheckpoint("q:org-analysis", ttl_hours=24)
    except Exception as e:
        print(f"[Q] Redis unavailable: {e} (dedup disabled)", file=sys.stderr)
        return None, None

def analyze_org_links():
    """
    Scan firebase.targets for organization cross-links:
    - app_name / app_package domain overlap
    - contact_email from disclosure_programs
    - API key / infrastructure patterns
    """
    dedup, checkpoint = get_redis_state()
    conn = get_db()
    cur = conn.cursor()

    start_offset = 0
    if checkpoint and checkpoint.exists():
        state = checkpoint.load()
        start_offset = state.get('last_offset', 0)
        print(f"[Q] Resuming from checkpoint offset={start_offset}")

    # Primary source: firebase.targets (3,913 rows, much richer than disclosure_metadata)
    cur.execute("""
        SELECT DISTINCT
            t.slug,
            t.app_name,
            t.app_package,
            t.platform,
            dp.contact_email
        FROM firebase.targets t
        LEFT JOIN firebase.disclosure_programs dp
            ON dp.target_slug = t.slug
            AND dp.contact_email IS NOT NULL
        WHERE t.app_name IS NOT NULL
        ORDER BY t.slug
        LIMIT 500 OFFSET %s
    """, (start_offset,))

    rows = cur.fetchall()
    org_links = []
    processed = 0

    for slug, app_name, app_package, platform, contact_email in rows:
        # Extract domain signals
        email_domain = None
        if contact_email and '@' in contact_email:
            email_domain = contact_email.split('@')[1].lower()

        package_domain = None
        if app_package:
            # com.mycompany.app → mycompany.com
            parts = app_package.split('.')
            if len(parts) >= 2 and parts[0] in ('com', 'org', 'net', 'io', 'co'):
                package_domain = f"{parts[1]}.{parts[0]}"

        domain = email_domain or package_domain
        if not domain:
            processed += 1
            continue

        # Skip already-seen domains
        if dedup and not dedup.is_new(domain):
            processed += 1
            continue

        # Find other apps with same domain (email or package)
        cur.execute("""
            SELECT DISTINCT t2.slug, t2.app_name
            FROM firebase.targets t2
            LEFT JOIN firebase.disclosure_programs dp2
                ON dp2.target_slug = t2.slug
            WHERE (
                dp2.contact_email ILIKE %s
                OR t2.app_package ILIKE %s
                OR t2.app_package ILIKE %s
            )
            AND t2.slug != %s
            LIMIT 10
        """, (
            f"%@{domain}",
            f"%.{domain.split('.')[0]}.%",
            f"{'.'.join(reversed(domain.split('.')))}.%",
            slug,
        ))
        related = cur.fetchall()

        if related:
            related_names = ', '.join(r[1] or r[0] for r in related[:3])
            org_links.append({
                'org_name_1': app_name or slug,
                'org_name_2': related_names,
                'link_type':  'domain_overlap',
                'evidence':   domain,
                'confidence': 0.85 if len(related) > 2 else 0.65,
                'severity':   'info'
            })

        processed += 1
        if checkpoint and processed % 50 == 0:
            checkpoint.save({'last_offset': start_offset + processed})

    conn.close()
    if checkpoint:
        checkpoint.clear()
    return org_links

def write_findings(org_links):
    if not org_links:
        return 0
    conn = get_db()
    cur = conn.cursor()
    count = 0
    for link in org_links:
        try:
            cur.execute("""
                INSERT INTO pipeline.org_findings
                (org_name_1, org_name_2, link_type, evidence, confidence, severity)
                VALUES (%(org_name_1)s, %(org_name_2)s, %(link_type)s,
                        %(evidence)s, %(confidence)s, %(severity)s)
                ON CONFLICT DO NOTHING
            """, link)
            count += 1
        except Exception as e:
            print(f"[Q] Error inserting: {e}", file=sys.stderr)
    conn.commit()
    if count > 0:
        try:
            cur.execute("""
                INSERT INTO pipeline.events (agent, scout, event_type, count, detail)
                VALUES ('charlie', 'q', 'org_link_discovered', %s, %s)
            """, (count, json.dumps({'links': count})))
            conn.commit()
        except Exception:
            pass
    conn.close()
    return count

def main():
    print(f"[Q] Analyzing organization links... {datetime.utcnow().isoformat()}")
    org_links = analyze_org_links()
    print(f"[Q] Found {len(org_links)} potential org links")
    if org_links:
        count = write_findings(org_links)
        print(f"[Q] Wrote {count} findings to pipeline.org_findings")
        high_conf = [l for l in org_links if l['confidence'] >= 0.75]
        if high_conf:
            print(f"[Q] 🚨 {len(high_conf)} high-confidence org links (>75%)")

if __name__ == "__main__":
    main()
