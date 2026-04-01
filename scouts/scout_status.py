#!/usr/bin/env python3
"""
Scout Status Macro - Query central findings database with full schema context

Usage:
    python3 scout_status.py                          # All findings, last 24h
    python3 scout_status.py --hours 720              # Last 30 days
    python3 scout_status.py --severity high          # High+critical only
    python3 scout_status.py --type pii               # Only PII findings
    python3 scout_status.py --json                   # JSON output
    python3 scout_status.py --pii-summary            # PII aggregation report
    python3 scout_status.py --secrets-report         # Verified secrets only
    python3 scout_status.py --targets-critical       # Critical targets only
"""

import sqlite3
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
import argparse

# Database path
DB_PATH = Path.home() / "development" / "audit-framework" / "modules" / "firebase-rtdb-audit" / "findings.db"

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
FINDING_TYPES = ["pii", "secret", "credential", "infrastructure", "content", "financial"]

class FindingsQuery:
    def __init__(self, db_path):
        self.db_path = db_path
        
    def connect(self):
        """Connect to findings database"""
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def get_all_findings(self, severity_min="info", hours=24, finding_type=None):
        """Query findings from database with optional type filter"""
        conn = self.connect()
        cursor = conn.cursor()
        
        # Build query with severity + type filters
        severity_clause = ""
        type_clause = ""
        params = []
        
        if severity_min != "info":
            severity_rank = SEVERITY_ORDER.get(severity_min, 0)
            severity_list = [s for s, r in SEVERITY_ORDER.items() if r >= severity_rank]
            placeholders = ",".join(["?" for _ in severity_list])
            severity_clause = f"AND f.severity IN ({placeholders})"
            params.extend(severity_list)
        
        if finding_type and finding_type in FINDING_TYPES:
            type_clause = f"AND f.finding_type = ?"
            params.append(finding_type)
        
        query = f"""
        SELECT 
            f.id, COALESCE(t.slug, 'unknown') as target, f.finding_type, f.severity, f.title, f.detail,
            f.count, f.verified, f.created_at, t.app_name, t.category
        FROM findings f
        LEFT JOIN targets t ON f.target_id = t.id
        WHERE f.created_at > datetime('now', '-{hours} hours')
        {severity_clause}
        {type_clause}
        ORDER BY f.created_at DESC, 
                 CASE f.severity 
                   WHEN 'critical' THEN 4
                   WHEN 'high' THEN 3
                   WHEN 'medium' THEN 2
                   WHEN 'low' THEN 1
                   ELSE 0
                 END DESC
        """
        
        cursor.execute(query, params)
        findings = cursor.fetchall()
        conn.close()
        
        return findings
    
    def get_findings_summary(self, hours=24):
        """Get summary stats by finding type"""
        conn = self.connect()
        cursor = conn.cursor()
        
        query = f"""
        SELECT 
            f.finding_type,
            COUNT(*) as total_findings,
            SUM(CASE WHEN f.severity = 'critical' THEN 1 ELSE 0 END) as critical_count,
            SUM(CASE WHEN f.severity = 'high' THEN 1 ELSE 0 END) as high_count,
            SUM(CASE WHEN f.severity = 'medium' THEN 1 ELSE 0 END) as medium_count,
            SUM(CASE WHEN f.severity = 'low' THEN 1 ELSE 0 END) as low_count,
            MAX(f.created_at) as last_run
        FROM findings f
        WHERE f.created_at > datetime('now', '-{hours} hours')
        GROUP BY f.finding_type
        ORDER BY critical_count DESC, high_count DESC
        """
        
        cursor.execute(query)
        summary = cursor.fetchall()
        conn.close()
        
        return summary
    
    def get_critical_targets(self, hours=720):
        """Get targets with critical severity"""
        conn = self.connect()
        cursor = conn.cursor()
        
        query = f"""
        SELECT 
            t.id, t.slug, t.app_name, t.category, t.severity, t.summary,
            COUNT(f.id) as finding_count,
            SUM(CASE WHEN f.severity = 'critical' THEN 1 ELSE 0 END) as critical_findings
        FROM targets t
        LEFT JOIN findings f ON f.target_id = t.id
        WHERE t.severity = 'critical'
          AND t.created_at > datetime('now', '-{hours} hours')
        GROUP BY t.id
        ORDER BY critical_findings DESC, t.scan_date DESC
        """
        
        cursor.execute(query)
        targets = cursor.fetchall()
        conn.close()
        
        return targets
    
    def get_pii_summary_report(self, hours=720):
        """Get PII aggregation by target"""
        conn = self.connect()
        cursor = conn.cursor()
        
        query = f"""
        SELECT 
            t.slug, t.app_name, t.category,
            ps.unique_emails, ps.total_email_refs, ps.unique_phones, ps.ipv4_count,
            ps.ssn_like, ps.passwords_plaintext, ps.passwords_hashed,
            ps.has_names, ps.has_location, ps.has_financial
        FROM pii_summary ps
        JOIN targets t ON ps.target_id = t.id
        WHERE t.created_at > datetime('now', '-{hours} hours')
        ORDER BY ps.unique_emails DESC, ps.ipv4_count DESC
        LIMIT 20
        """
        
        cursor.execute(query)
        results = cursor.fetchall()
        conn.close()
        
        return results
    
    def get_verified_secrets(self, hours=720):
        """Get verified secrets (high confidence API keys, etc.)"""
        conn = self.connect()
        cursor = conn.cursor()
        
        query = f"""
        SELECT 
            t.slug, t.app_name, s.detector, s.detail, s.count, s.created_at
        FROM secrets s
        JOIN targets t ON s.target_id = t.id
        WHERE s.verified = 1
          AND t.created_at > datetime('now', '-{hours} hours')
        ORDER BY s.created_at DESC, s.count DESC
        """
        
        cursor.execute(query)
        results = cursor.fetchall()
        conn.close()
        
        return results

def print_summary_table(summary):
    """Print findings summary as table"""
    print("\n┌─ Findings Summary ──────────────────────────────────────────────┐")
    print("│ Type                │ Total │ 🔴 Critical │ 🟠 High │ 🟡 Medium │")
    print("├─────────────────────┼───────┼─────────────┼─────────┼───────────┤")
    
    for row in summary:
        finding_type = str(row['finding_type'] or 'other')[:19].ljust(19)
        total = str(row['total_findings']).rjust(5)
        crit = str(row['critical_count'] or 0).rjust(11)
        high = str(row['high_count'] or 0).rjust(7)
        med = str(row['medium_count'] or 0).rjust(9)
        
        print(f"│ {finding_type} │ {total} │ {crit} │ {high} │ {med} │")
    
    print("└─────────────────────┴───────┴─────────────┴─────────┴───────────┘\n")

def print_findings_table(findings, limit=10):
    """Print findings as table"""
    if not findings:
        print("\n✅ No findings\n")
        return
    
    print(f"\n┌─ Latest Findings ({len(findings)} total, showing {min(limit, len(findings))}) ──────────────┐")
    
    for i, f in enumerate(findings[:limit], 1):
        # Severity emoji
        emoji = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🔵",
            "info": "⚪"
        }.get(f['severity'], "?")
        
        detail = (f['detail'] or f['title'] or 'unknown')[:50]
        print(f"\n│ [{i}] {emoji} {f['severity'].upper():8} | {f['finding_type']:10} | {detail}")
        print(f"│     Target: {(f['target'] or 'unknown')[:55]}")
        print(f"│     Count: {str(f['count'] or 'N/A'):10} | Verified: {str(f['verified']):3} | Time: {f['created_at'][-19:]}")
    
    print("\n└" + "─" * 85 + "┘\n")

def print_json_output(findings, summary):
    """Print as JSON"""
    data = {
        "timestamp": datetime.now().isoformat(),
        "summary": [dict(row) for row in summary],
        "findings": [dict(f) for f in findings],
        "total_findings": len(findings)
    }
    print(json.dumps(data, indent=2))

def main():
    parser = argparse.ArgumentParser(
        description="Findings Status - Query central findings database with full schema context",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  scout_status.py                               # All findings, last 24h
  scout_status.py --severity high --hours 720   # High+Critical, last 30 days
  scout_status.py --type pii                    # PII findings only
  scout_status.py --json                        # JSON output
  scout_status.py --pii-summary                 # PII aggregation report
  scout_status.py --secrets-report              # Verified secrets only
  scout_status.py --targets-critical            # Critical targets summary
        """
    )
    
    parser.add_argument("--severity", choices=list(SEVERITY_ORDER.keys()), 
                       default="info", help="Minimum severity (default: all)")
    parser.add_argument("--type", choices=FINDING_TYPES, help="Filter by finding type")
    parser.add_argument("--hours", type=int, default=24, help="Hours to look back (default: 24)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--limit", type=int, default=10, help="Max findings to display (default: 10)")
    parser.add_argument("--pii-summary", action="store_true", help="Show PII aggregation report")
    parser.add_argument("--secrets-report", action="store_true", help="Show verified secrets")
    parser.add_argument("--targets-critical", action="store_true", help="Show critical targets")
    
    args = parser.parse_args()
    
    # Verify database exists
    if not DB_PATH.exists():
        print(f"Error: Database not found: {DB_PATH}")
        sys.exit(1)
    
    try:
        query = FindingsQuery(DB_PATH)
        
        # Handle special reports
        if args.pii_summary:
            print(f"\n📊 PII Summary Report (Last {args.hours} hours)\n")
            pii_data = query.get_pii_summary_report(hours=args.hours)
            
            if not pii_data:
                print("✅ No PII summaries found\n")
            else:
                print("┌─ Top PII Exposures ────────────────────────────────────────────────┐")
                print("│ Target              │ Emails  │ Phones │ IPv4 │ Passwords │ PHI   │")
                print("├─────────────────────┼─────────┼────────┼──────┼───────────┼───────┤")
                for row in pii_data[:10]:
                    target = str(row['slug'])[:19].ljust(19)
                    emails = str(row['unique_emails'] or 0).rjust(7)
                    phones = str(row['unique_phones'] or 0).rjust(6)
                    ipv4 = str(row['ipv4_count'] or 0).rjust(4)
                    pwd = str((row['passwords_plaintext'] or 0) + (row['passwords_hashed'] or 0)).rjust(9)
                    phi = "Yes" if row['has_names'] or row['has_location'] else "No"
                    print(f"│ {target} │ {emails} │ {phones} │ {ipv4} │ {pwd} │ {phi:3} │")
                print("└─────────────────────┴─────────┴────────┴──────┴───────────┴───────┘\n")
            return
        
        if args.secrets_report:
            print(f"\n🔑 Verified Secrets Report (Last {args.hours} hours)\n")
            secrets_data = query.get_verified_secrets(hours=args.hours)
            
            if not secrets_data:
                print("✅ No verified secrets found\n")
            else:
                print("┌─ Verified API Keys & Credentials ──────────────────────────────────┐")
                for row in secrets_data[:15]:
                    print(f"\n│ 🔴 {row['detector']:15} | {row['slug']}")
                    print(f"│    Target: {row['app_name'] or 'unknown'}")
                    print(f"│    Detail: {row['detail'] or 'N/A'}")
                    print(f"│    Count: {row['count']} | Found: {row['created_at'][-10:]}")
                print("\n└────────────────────────────────────────────────────────────────────┘\n")
            return
        
        if args.targets_critical:
            print(f"\n🔴 Critical Targets Summary (Last {args.hours} hours)\n")
            targets = query.get_critical_targets(hours=args.hours)
            
            if not targets:
                print("✅ No critical targets found\n")
            else:
                print("┌─ Critical Targets ──────────────────────────────────────────────────┐")
                print("│ Target              │ App Name            │ Category │ Findings │")
                print("├─────────────────────┼─────────────────────┼──────────┼──────────┤")
                for row in targets[:20]:
                    target = str(row['slug'])[:19].ljust(19)
                    app = str(row['app_name'] or 'unknown')[:19].ljust(19)
                    cat = str(row['category'] or 'misc')[:8].ljust(8)
                    findings = str(row['critical_findings'] or 0).rjust(8)
                    print(f"│ {target} │ {app} │ {cat} │ {findings} │")
                print("└─────────────────────┴─────────────────────┴──────────┴──────────┘\n")
            return
        
        # Get findings
        findings = query.get_all_findings(
            severity_min=args.severity,
            hours=args.hours,
            finding_type=args.type
        )
        
        # Get summary
        summary = query.get_findings_summary(hours=args.hours)
        
        # Output
        if args.json:
            print_json_output(findings, summary)
        else:
            # Header
            print(f"\n📊 Findings Status Report")
            print(f"   Time Range: Last {args.hours} hours")
            if args.severity != "info":
                print(f"   Min Severity: {args.severity} and higher")
            print(f"   Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
            
            # Summary table
            print_summary_table(summary)
            
            # Findings
            print_findings_table(findings, limit=args.limit)
            
            # Footer
            total_count = len(findings)
            print(f"✅ Query complete - {total_count} findings found")
            print()
    
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
