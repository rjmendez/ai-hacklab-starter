#!/usr/bin/env python3
"""
iris_cantina_scout.py — Passive OSINT scout for bug bounty targets

Rewritten 2026-04-01. No stubs. Only real calls to confirmed-reachable APIs.

What Iris does:
  1. Pulls high-severity firebase/bucket targets that have H1/Bugcrowd programs
     (from disclosure_programs mapped by Atlas)
  2. For each target, runs real passive OSINT:
     - DNS records (A/AAAA/MX/TXT/NS via dnspython)
     - Subdomain brute-force (socket, 30 common subs, no external API)
     - HackerOne public program search (unauthenticated, 450 programs)
     - WHOIS (port 43 direct)
  3. Stores findings to pipeline.cantina_bounties + pipeline.cantina_osint
  4. Alerts MrPink via mesh:queue:mrpink on high-value finds (H1 bounty program, 
     interesting subdomains)

Skipped (unavailable from container):
  - crtsh: 503 unreliable
  - Shodan InternetDB: 403
  - GitHub search: 401 (needs token)

Rate limits: DNS 50/min, H1 10/min, WHOIS 5/min
"""

import json
import socket
import sys
import time
from datetime import datetime

import psycopg2

sys.path.insert(0, '/home/openclaw/.openclaw/workspace/agent-mesh/scouts')
from dedup import RedisDedup
from checkpoint import RedisCheckpoint
from rate_limiter import RedisRateLimiter, HACKERONE_LIMITER
import redis as _redis_mod

DB_DSN   = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/audit_framework"
REDIS_PASS = os.environ.get("REDIS_PASSWORD", "")

SHODAN_KEY = None  # loaded from Redis at startup

def get_shodan_key():
    global SHODAN_KEY
    if SHODAN_KEY:
        return SHODAN_KEY
    try:
        r = get_redis()
        SHODAN_KEY = r.get("secrets:shodan_api_key")
    except Exception:
        pass
    return SHODAN_KEY


def shodan_dns_resolve(hostnames):
    """Free — no credits. Resolve up to 100 hostnames at once."""
    key = get_shodan_key()
    if not key or not hostnames:
        return {}
    try:
        import urllib.request
        joined = ",".join(hostnames[:100])
        req = urllib.request.urlopen(
            f"https://api.shodan.io/dns/resolve?hostnames={joined}&key={key}",
            timeout=8
        )
        return json.loads(req.read())
    except Exception:
        return {}


def shodan_reverse_dns(ips):
    """Free — no credits. Reverse lookup up to 100 IPs."""
    key = get_shodan_key()
    if not key or not ips:
        return {}
    try:
        import urllib.request
        joined = ",".join(ips[:100])
        req = urllib.request.urlopen(
            f"https://api.shodan.io/dns/reverse?ips={joined}&key={key}",
            timeout=8
        )
        return json.loads(req.read())
    except Exception:
        return {}


def shodan_host_lookup(ip, only_if_credits_above=20):
    """Costs 1 query credit. Only use on high-value IPs."""
    key = get_shodan_key()
    if not key:
        return None
    # Credit guard — check remaining before spending
    try:
        import urllib.request
        req = urllib.request.urlopen(
            f"https://api.shodan.io/api-info?key={key}", timeout=5
        )
        info = json.loads(req.read())
        if info.get("query_credits", 0) < only_if_credits_above:
            return None  # conserve credits
        req2 = urllib.request.urlopen(
            f"https://api.shodan.io/shodan/host/{ip}?key={key}", timeout=10
        )
        return json.loads(req2.read())
    except Exception:
        return None


DDL = """
CREATE TABLE IF NOT EXISTS pipeline.cantina_bounties (
    id           SERIAL PRIMARY KEY,
    slug         TEXT NOT NULL,
    schema       TEXT NOT NULL DEFAULT 'firebase',
    domain       TEXT,
    app_name     TEXT,
    app_package  TEXT,
    severity     TEXT,
    h1_handle    TEXT,
    bc_url       TEXT,
    status       TEXT DEFAULT 'new',
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    last_scanned TIMESTAMPTZ,
    findings     JSONB,
    UNIQUE(slug, schema)
);
CREATE TABLE IF NOT EXISTS pipeline.cantina_osint (
    id           SERIAL PRIMARY KEY,
    bounty_id    INT REFERENCES pipeline.cantina_bounties(id) ON DELETE CASCADE,
    slug         TEXT NOT NULL,
    domain       TEXT,
    finding_type TEXT NOT NULL,
    finding_value TEXT NOT NULL,
    ts TIMESTAMPTZ DEFAULT NOW(),
    confidence   FLOAT DEFAULT 0.7,
    severity     TEXT DEFAULT 'info',
    source       TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
"""

COMMON_SUBS = [
    "www","api","app","mail","cdn","static","assets","dev","staging","beta",
    "admin","portal","dashboard","auth","login","account","help","support",
    "docs","status","blog","shop","media","uploads","vpn","git","gitlab",
    "jenkins","jira","confluence","s3","storage","backup","db","grafana",
    "kibana","monitor","metrics",
]

DNS_RECORD_TYPES = ["A","AAAA","MX","TXT","NS"]
INTERESTING_SUBS = {"admin","vpn","staging","git","gitlab","jenkins","jira",
                    "grafana","kibana","db","backup","s3","storage"}


def get_db():
    return psycopg2.connect(DB_DSN)


def get_redis():
    r = _redis_mod.Redis(host="audit-redis", port=6379, password=REDIS_PASS, decode_responses=True)
    r.ping()
    return r


# ── OSINT primitives ───────────────────────────────────────────────────────────

def dns_lookup(domain, rtype="A"):
    try:
        import dns.resolver
        return [str(r) for r in dns.resolver.resolve(domain, rtype, lifetime=4)]
    except Exception:
        return []


def dns_brute(domain):
    found = []
    for sub in COMMON_SUBS:
        fqdn = f"{sub}.{domain}"
        try:
            infos = socket.getaddrinfo(fqdn, None)
            ips = list({i[4][0] for i in infos})
            if ips:
                found.append({"subdomain": fqdn, "ips": ips})
        except (socket.gaierror, OSError):
            pass
    return found


def whois_lookup(domain):
    try:
        tld = domain.split(".")[-1]
        s = socket.socket(); s.settimeout(5)
        s.connect(("whois.iana.org", 43))
        s.send(f"{tld}\r\n".encode())
        resp = s.recv(4096).decode(errors="replace"); s.close()
        whois_host = next(
            (l.split(":",1)[1].strip() for l in resp.splitlines()
             if l.lower().startswith("whois:")), None
        )
        if not whois_host:
            return None
        s2 = socket.socket(); s2.settimeout(8)
        s2.connect((whois_host, 43))
        s2.send(f"{domain}\r\n".encode())
        data = b""
        while True:
            chunk = s2.recv(4096)
            if not chunk: break
            data += chunk
        s2.close()
        result = {}
        for line in data.decode(errors="replace").splitlines():
            ll = line.lower()
            if "registrar:" in ll and "registrar" not in result:
                result["registrar"] = line.split(":",1)[1].strip()
            if "creation date:" in ll and "created" not in result:
                result["created"] = line.split(":",1)[1].strip()[:20]
            if "name server:" in ll:
                result.setdefault("nameservers",[]).append(line.split(":",1)[1].strip())
        return result if result else None
    except Exception:
        return None


def h1_search(query):
    try:
        import urllib.request
        q = query.split("-")[0]
        req = urllib.request.Request(
            f"https://hackerone.com/programs/search.json?query={q}&limit=3&page=1",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=8)
        results = json.loads(resp.read()).get("results", [])
        if results:
            p = results[0]
            return {"handle": p.get("handle"), "name": p.get("name"),
                    "url": f"https://hackerone.com/{p.get('handle','')}",
                    "offers_bounties": p.get("offers_bounties", False)}
    except Exception:
        pass
    return None


def derive_domain(slug, app_package=None):
    if slug.endswith("-firebaseio-com"):
        return None
    if app_package:
        parts = app_package.split(".")
        if len(parts) >= 2 and parts[0] in ("com","org","net","io","co"):
            return f"{parts[1]}.{parts[0]}"
    for tld in ["-com","-net","-org","-io","-co","-app","-dev"]:
        if slug.endswith(tld):
            return slug[:-len(tld)].rstrip("-") + tld.replace("-",".")
    return None


# ── DB helpers ─────────────────────────────────────────────────────────────────

def ensure_schema(conn):
    cur = conn.cursor()
    cur.execute(DDL)
    conn.commit()


def get_targets(conn, limit=20):
    cur = conn.cursor()
    cur.execute("""
        SELECT t.slug, 'firebase' AS schema, t.app_name, t.app_package,
               t.severity, dp.platform, dp.program_url, cb.id
        FROM firebase.targets t
        JOIN firebase.disclosure_programs dp ON dp.target_slug = t.slug
        LEFT JOIN pipeline.cantina_bounties cb
               ON cb.slug = t.slug AND cb.schema = 'firebase'
        WHERE t.severity IN ('critical','high','medium')
        AND dp.platform IN ('hackerone','bugcrowd','intigriti')
        AND (cb.id IS NULL OR cb.status = 'new')
        ORDER BY CASE t.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    cur.execute("""
        SELECT dp.target_slug, 'bucket' AS schema,
               dp.target_slug, NULL, 'medium', dp.platform, dp.program_url, cb.id
        FROM bucket.disclosure_programs dp
        LEFT JOIN pipeline.cantina_bounties cb
               ON cb.slug = dp.target_slug AND cb.schema = 'bucket'
        WHERE dp.platform IN ('hackerone','bugcrowd','intigriti')
        AND (cb.id IS NULL OR cb.status = 'new')
        LIMIT %s
    """, (limit,))
    return rows + cur.fetchall()


def upsert_bounty(conn, slug, schema, app_name, app_package, severity, platform, program_url):
    domain = derive_domain(slug, app_package)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pipeline.cantina_bounties
            (slug, schema, domain, app_name, app_package, severity, h1_handle, bc_url, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'researching')
        ON CONFLICT (slug, schema) DO UPDATE SET
            status = EXCLUDED.status, last_scanned = NOW()
        RETURNING id
    """, (slug, schema, domain, app_name, app_package, severity,
          program_url if platform=="hackerone" else None,
          program_url if platform=="bugcrowd" else None))
    row_id = cur.fetchone()[0]
    conn.commit()
    return row_id, domain


def store_findings(conn, bounty_id, slug, domain, findings):
    try:
        conn.rollback()  # clear any aborted tx state
    except Exception:
        pass
    if not findings: return 0
    cur = conn.cursor()
    count = 0
    for f in findings:
        try:
            cur.execute("""
                INSERT INTO pipeline.cantina_osint
                    (bounty_id, slug, domain, finding_type, finding_value,
                     confidence, severity, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (bounty_id, slug, domain, f["type"], f["value"],
                  f.get("confidence",0.7), f.get("severity","info"), f.get("source","iris")))
            count += 1
        except Exception as e:
            print(f"[Iris] store error: {e}", file=sys.stderr)
            try:
                conn.rollback()
            except Exception:
                pass
    conn.commit()
    return count


def mark_done(conn, bounty_id, summary):
    cur = conn.cursor()
    cur.execute("""
        UPDATE pipeline.cantina_bounties
        SET status='done', last_scanned=NOW(), findings=%s WHERE id=%s
    """, (json.dumps(summary), bounty_id))
    conn.commit()


# ── Core research loop ────────────────────────────────────────────────────────

def github_search(domain, slug):
    """
    Search GitHub for leaked credentials referencing this target.
    Uses code search API — 30 req/min rate limit.
    Returns list of {repo, file, url} hits.
    """
    key = None
    try:
        r = get_redis()
        key = r.get("secrets:github_pat")
    except Exception:
        pass
    if not key:
        return []

    import urllib.request, urllib.parse
    headers = {
        "Authorization": f"token {key}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "SecurityResearch/1.0",
    }

    results = []
    # Search for domain in code (firebase URLs, config files, etc.)
    queries = [
        f'"{domain}" apiKey filename:.json',
        f'"{domain}" firebase filename:google-services.json',
        f'"{slug}" firebase apiKey',
    ]

    for q in queries[:2]:  # limit to 2 queries per target (rate budget)
        try:
            encoded = urllib.parse.quote(q)
            req = urllib.request.Request(
                f"https://api.github.com/search/code?q={encoded}&per_page=5",
                headers=headers
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            for item in data.get("items", [])[:3]:
                results.append({
                    "repo":     item.get("repository", {}).get("full_name", "?"),
                    "file":     item.get("path", "?"),
                    "html_url": item.get("html_url", ""),
                    "query":    q,
                })
        except Exception:
            pass

    return results


class IrisCantanaScout:
    def __init__(self):
        self.conn = get_db()
        ensure_schema(self.conn)
        try:
            self._dedup      = RedisDedup("iris:seen_slugs", ttl_days=30)
            self._checkpoint = RedisCheckpoint("iris:cantina-run", ttl_hours=24)
            self._dns_rl     = RedisRateLimiter("ratelimit:iris_dns", max_calls=50, window_seconds=60)
            self._h1_rl      = HACKERONE_LIMITER
            self._gh_rl      = RedisRateLimiter("ratelimit:iris_github", max_calls=25, window_seconds=60)
            self._redis      = get_redis()
            print("[Iris] Redis initialized (dedup + checkpoint + rate limiters)")
        except Exception as e:
            print(f"[Iris] Redis unavailable: {e}", file=sys.stderr)
            self._dedup = self._checkpoint = self._dns_rl = self._h1_rl = self._gh_rl = self._redis = None

    def _research(self, slug, schema, app_name, app_package, severity, platform, program_url):
        findings, high_value = [], []
        bounty_id, domain = upsert_bounty(
            self.conn, slug, schema, app_name, app_package, severity, platform, program_url
        )

        if not domain:
            print(f"[Iris] {slug}: no resolvable domain — logged, skipping OSINT")
            mark_done(self.conn, bounty_id, {"skipped": "no_domain"})
            return

        print(f"[Iris] {slug} → {domain}")

        # DNS records
        if self._dns_rl: self._dns_rl.acquire(block=True)
        for rtype in DNS_RECORD_TYPES:
            for rec in dns_lookup(domain, rtype):
                findings.append({"type": f"dns_{rtype.lower()}", "value": rec,
                                  "confidence": 0.95, "severity": "info", "source": "dnspython"})
                if rtype == "TXT" and any(k in rec.lower() for k in ["verify","google-site","apple-","ms="]):
                    high_value.append(f"TXT ownership record: {rec[:80]}")

        # Subdomain brute-force
        if self._dns_rl: self._dns_rl.acquire(block=True)
        subs = dns_brute(domain)
        for sub in subs:
            fqdn, ips = sub["subdomain"], ", ".join(sub["ips"])
            findings.append({"type": "subdomain", "value": f"{fqdn} → {ips}",
                              "confidence": 0.95, "severity": "low", "source": "dns_brute"})
            label = fqdn.split(".")[0]
            if label in INTERESTING_SUBS:
                high_value.append(f"interesting subdomain: {fqdn} ({ips})")

        print(f"[Iris]   DNS: {sum(1 for f in findings if 'dns_' in f['type'])} records, "
              f"{len(subs)} subdomains")


        # Shodan: free DNS resolve on discovered subdomains
        if subs:
            sub_domains = [s["subdomain"] for s in subs]
            resolved = shodan_dns_resolve(sub_domains)
            if resolved:
                for host, ip in resolved.items():
                    if ip and ip not in ("", "None"):
                        findings.append({
                            "type": "shodan_dns_resolve",
                            "value": f"{host} → {ip}",
                            "confidence": 0.95, "severity": "info", "source": "shodan_free"
                        })

            # Reverse DNS on resolved IPs
            ips = [ip for ip in resolved.values() if ip and ip not in ("", "None")]
            if ips:
                rev = shodan_reverse_dns(list(set(ips)))
                for ip, hostnames in rev.items():
                    if hostnames:
                        findings.append({
                            "type": "shodan_reverse_dns",
                            "value": f"{ip} → {', '.join(hostnames[:3])}",
                            "confidence": 0.9, "severity": "info", "source": "shodan_free"
                        })

            # Host lookup on primary IP (credit-gated: only if >20 credits remain + high severity)
            if severity in ("critical", "high") and ips:
                primary_ip = ips[0]
                host_data = shodan_host_lookup(primary_ip, only_if_credits_above=20)
                if host_data:
                    ports = host_data.get("ports", [])
                    vulns = list(host_data.get("vulns", {}).keys())
                    org   = host_data.get("org", "")
                    findings.append({
                        "type": "shodan_host",
                        "value": json.dumps({"ip": primary_ip, "org": org, "ports": ports[:10], "vulns": vulns[:5]}),
                        "confidence": 0.95, "severity": "high" if vulns else "info",
                        "source": "shodan_paid"
                    })
                    if vulns:
                        high_value.extend([f"Shodan vuln {v} on {primary_ip}" for v in vulns[:3]])
                        print(f"[Iris]   🔴 Shodan CVEs: {vulns[:3]}")
                    elif ports:
                        print(f"[Iris]   Shodan: {primary_ip} ports={ports[:5]}")

        # WHOIS
        whois = whois_lookup(domain)
        if whois:
            findings.append({"type": "whois", "value": json.dumps(whois),
                              "confidence": 0.9, "severity": "info", "source": "whois"})

        # HackerOne
        if self._h1_rl: self._h1_rl.acquire(block=True)
        h1 = h1_search(slug)
        if h1:
            findings.append({"type": "h1_program", "value": json.dumps(h1),
                              "confidence": 0.9, "severity": "info", "source": "hackerone"})
            if h1.get("offers_bounties"):
                high_value.append(f"H1 BOUNTY: {h1['url']}")
                print(f"[Iris]   🏆 Bounty program confirmed: {h1['url']}")


        # GitHub code search — find leaked credentials referencing this domain
        if domain and self._gh_rl and self._gh_rl.acquire(block=False):
            gh_hits = github_search(domain, slug)
            if gh_hits:
                for hit in gh_hits:
                    findings.append({
                        "type":      "github_exposure",
                        "value":     json.dumps(hit),
                        "confidence": 0.8,
                        "severity":  "high",
                        "source":    "github_search",
                    })
                    high_value.append(f"GitHub leak: {hit['repo']}/{hit['file']}")
                print(f"[Iris]   🐙 GitHub: {len(gh_hits)} code hits for {domain}")

        count = store_findings(self.conn, bounty_id, slug, domain, findings)
        mark_done(self.conn, bounty_id, {
            "dns_records": sum(1 for f in findings if "dns_" in f["type"]),
            "subdomains": len(subs),
            "high_value": len(high_value),
            "total": count,
        })

        if high_value and self._redis:
            self._redis.lpush("mesh:queue:mrpink", json.dumps({
                "from": "iris", "to": "mrpink", "skill_id": "osint_alert",
                "input": {"subject": f"Iris: {slug}", "domain": domain,
                          "findings": count, "highlights": high_value[:5]}
            }))
            print(f"[Iris]   → MrPink alerted ({len(high_value)} highlights)")

        print(f"[Iris]   {count} findings stored")

    def run(self):
        print(f"[Iris] Starting Cantina scout... {datetime.utcnow().isoformat()}")
        targets = get_targets(self.conn, limit=20)
        print(f"[Iris] {len(targets)} targets to research")

        if not targets:
            print("[Iris] Nothing to do. Idle.")
            self.conn.close()
            return

        processed = 0
        for row in targets:
            slug, schema, app_name, app_package, severity, platform, program_url, bounty_id = row
            key = f"{slug}:{schema}"

            if self._dedup and not self._dedup.is_new(key):
                continue

            self._research(slug, schema, app_name, app_package, severity, platform, program_url)
            processed += 1

            if self._checkpoint and processed % 5 == 0:
                self._checkpoint.save({"processed": processed, "last": slug})

            time.sleep(0.3)

        if self._checkpoint:
            self._checkpoint.clear()

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pipeline.cantina_osint WHERE created_at > NOW() - INTERVAL '1h'")
        recent = cur.fetchone()[0]
        print(f"\n[Iris] 🌸 Complete. {processed} targets researched, {recent} findings this hour.")
        self.conn.close()


if __name__ == "__main__":
    IrisCantanaScout().run()
