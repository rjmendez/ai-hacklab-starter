#!/usr/bin/env python3
"""
Atlas 🗺️ — Disclosure Program Scout
Maps responsible disclosure channels, bug bounty programs, and reporting paths
for any target discovered by the audit mesh.

Usage:
    python3 atlas_seed.py [--target SLUG] [--batch] [--schema firebase|bucket] [--limit N] [--dry-run]

Atlas maps, never submits — submission is Hermes's job.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import aiohttp
import psycopg2
import psycopg2.extras
import requests

# ── Scout state (optional — degrades gracefully if Redis is down) ─────────────
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from scout_state import set_running, set_idle, set_error
except Exception:
    def set_running(*a, **k): pass
    def set_idle(*a, **k): pass
    def set_error(*a, **k): pass

SCOUT_NAME = "atlas"

# ─────────────────────────────────────────────
# DB Connection
# ─────────────────────────────────────────────
DSN = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/audit_framework"

HEADERS = {
    "User-Agent": "Atlas-DisclosureScout/1.0 (security-research; responsible-disclosure-mapping)"
}

REQUEST_TIMEOUT = 10  # seconds


# ─────────────────────────────────────────────
# DB Setup
# ─────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS firebase.disclosure_programs (
    id              SERIAL PRIMARY KEY,
    target_slug     TEXT NOT NULL,
    program_type    TEXT NOT NULL,
    platform        TEXT,
    program_url     TEXT,
    submission_url  TEXT,
    contact_email   TEXT,
    scope_status    TEXT DEFAULT 'unknown',
    notes           TEXT,
    discovered_at   TIMESTAMPTZ DEFAULT NOW(),
    verified_at     TIMESTAMPTZ,
    UNIQUE(target_slug, program_type, platform)
);

CREATE TABLE IF NOT EXISTS bucket.disclosure_programs (
    id              SERIAL PRIMARY KEY,
    target_slug     TEXT NOT NULL,
    program_type    TEXT NOT NULL,
    platform        TEXT,
    program_url     TEXT,
    submission_url  TEXT,
    contact_email   TEXT,
    scope_status    TEXT DEFAULT 'unknown',
    notes           TEXT,
    discovered_at   TIMESTAMPTZ DEFAULT NOW(),
    verified_at     TIMESTAMPTZ,
    UNIQUE(target_slug, program_type, platform)
);
"""


def get_db():
    return psycopg2.connect(DSN)


def ensure_tables():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()


# ─────────────────────────────────────────────
# 1. security.txt checker (RFC 9116)
# ─────────────────────────────────────────────

def check_security_txt(domain: str) -> Optional[dict]:
    """
    Fetch and parse security.txt per RFC 9116.
    Tries /.well-known/security.txt then /security.txt.
    Returns parsed dict or None.
    """
    urls = [
        f"https://{domain}/.well-known/security.txt",
        f"https://{domain}/security.txt",
        f"http://{domain}/.well-known/security.txt",   # fallback for http-only
    ]

    raw = None
    source_url = None
    for url in urls:
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS, allow_redirects=True)
            if r.status_code == 200 and "Contact:" in r.text:
                raw = r.text
                source_url = url
                break
        except Exception:
            continue

    if not raw:
        return None

    result = {"source_url": source_url, "raw": raw}

    # Parse RFC 9116 fields
    field_map = {
        "contact": "Contact",
        "expires": "Expires",
        "policy": "Policy",
        "acknowledgments": "Acknowledgments",
        "preferred_languages": "Preferred-Languages",
        "hiring": "Hiring",
        "encryption": "Encryption",
        "canonical": "Canonical",
    }

    for key, field in field_map.items():
        pattern = rf"^{re.escape(field)}:\s*(.+)$"
        matches = re.findall(pattern, raw, re.MULTILINE | re.IGNORECASE)
        if matches:
            result[key] = matches if len(matches) > 1 else matches[0]

    return result


# ─────────────────────────────────────────────
# 2. HackerOne public program check
# ─────────────────────────────────────────────

def check_hackerone(company_name: str) -> Optional[dict]:
    """
    Query HackerOne public directory for matching programs.
    No auth needed for public programs.
    """
    url = f"https://api.hackerone.com/v1/hackers/programs?query={quote(company_name)}&limit=5"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return None
        data = r.json()
        programs = data.get("data", [])
        if not programs:
            return None

        # Return the first matching public program
        for prog in programs:
            attrs = prog.get("attributes", {})
            handle = prog.get("id") or attrs.get("handle", "")
            if attrs.get("state") == "public_mode":
                return {
                    "platform": "hackerone",
                    "handle": handle,
                    "program_url": f"https://hackerone.com/{handle}",
                    "submission_url": f"https://hackerone.com/{handle}/reports/new",
                    "offers_bounties": attrs.get("offers_bounties", False),
                    "response_efficiency": attrs.get("response_efficiency_percentage"),
                    "scope_summary": _h1_scope_summary(attrs),
                }
        return None
    except Exception:
        return None


def _h1_scope_summary(attrs: dict) -> str:
    """Pull a brief scope description from H1 program attributes."""
    profile = attrs.get("profile_picture", "")
    name = attrs.get("name", "")
    return f"HackerOne public program: {name}" if name else "HackerOne public program"


# ─────────────────────────────────────────────
# 3. Bugcrowd check
# ─────────────────────────────────────────────

def check_bugcrowd(domain: str) -> Optional[dict]:
    """
    Derive a company slug from domain and probe Bugcrowd.
    Heuristic: strip TLD, split on hyphens, use first part or full slug.
    """
    # Strip common TLDs and derive slug candidates
    base = re.sub(r"\.(com|net|org|io|co|app|dev|xyz|ai|cloud|tech)$", "", domain, flags=re.I)
    # Also strip firebaseio.com pattern
    base = re.sub(r"-default-rtdb$|\.firebaseio$", "", base)
    slug_candidates = [base, base.split("-")[0], base.replace("-", "")]

    for slug in dict.fromkeys(slug_candidates):  # dedupe preserving order
        url = f"https://bugcrowd.com/{slug}"
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS, allow_redirects=True)
            if r.status_code == 200 and "bugcrowd" in r.url.lower():
                # Check if it's actually a program page (not a generic 200)
                if "program" in r.text.lower() or "submit" in r.text.lower() or "scope" in r.text.lower():
                    return {
                        "platform": "bugcrowd",
                        "program_url": url,
                        "submission_url": f"{url}/submissions/new",
                        "slug": slug,
                    }
        except Exception:
            continue

    return None


# ─────────────────────────────────────────────
# 4. Abuse contact lookup
# ─────────────────────────────────────────────

def get_abuse_contact(domain: str) -> Optional[str]:
    """
    Find abuse/security contact via RDAP, then fallback to standard emails.
    """
    # Try RDAP
    try:
        rdap_url = f"https://rdap.org/domain/{domain}"
        r = requests.get(rdap_url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if r.status_code == 200:
            data = r.json()
            email = _extract_rdap_abuse_email(data)
            if email:
                return email
    except Exception:
        pass

    # Try RDAP via IANA bootstrap for the TLD
    try:
        tld = domain.rsplit(".", 1)[-1]
        bootstrap_url = f"https://data.iana.org/rdap/dns.json"
        r = requests.get(bootstrap_url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if r.status_code == 200:
            bootstrap = r.json()
            for service in bootstrap.get("services", []):
                tlds, endpoints = service[0], service[1]
                if tld in tlds and endpoints:
                    rdap_base = endpoints[0].rstrip("/")
                    r2 = requests.get(f"{rdap_base}/domain/{domain}", timeout=REQUEST_TIMEOUT, headers=HEADERS)
                    if r2.status_code == 200:
                        email = _extract_rdap_abuse_email(r2.json())
                        if email:
                            return email
    except Exception:
        pass

    # Standard fallback contacts
    return f"abuse@{domain}"


def _extract_rdap_abuse_email(data: dict) -> Optional[str]:
    """Walk RDAP JSON looking for abuse role vCard email."""
    for entity in data.get("entities", []):
        roles = entity.get("roles", [])
        if "abuse" in roles or "registrar" in roles:
            vcardarray = entity.get("vcardArray", [])
            if len(vcardarray) > 1:
                for item in vcardarray[1]:
                    if item[0] == "email":
                        return item[3]
        # Recurse into nested entities
        nested = entity.get("entities", [])
        for sub in nested:
            if "abuse" in sub.get("roles", []):
                vcardarray = sub.get("vcardArray", [])
                if len(vcardarray) > 1:
                    for item in vcardarray[1]:
                        if item[0] == "email":
                            return item[3]
    return None


# ─────────────────────────────────────────────
# 5. Classify disclosure path
# ─────────────────────────────────────────────

# Known GDPR Data Protection Authorities by country code
GDPR_DPA = {
    "default": {
        "name": "European Data Protection Board",
        "url": "https://edpb.europa.eu/about-edpb/about-edpb/members_en",
        "email": "edpb@edpb.europa.eu",
    },
    "de": {"name": "BfDI (Germany)", "url": "https://www.bfdi.bund.de/", "email": "poststelle@bfdi.bund.de"},
    "fr": {"name": "CNIL (France)", "url": "https://www.cnil.fr/", "email": "https://www.cnil.fr/fr/plaintes"},
    "gb": {"name": "ICO (UK)", "url": "https://ico.org.uk/", "email": "casework@ico.org.uk"},
    "nl": {"name": "AP (Netherlands)", "url": "https://autoriteitpersoonsgegevens.nl/", "email": "https://autoriteitpersoonsgegevens.nl/nl/melden-datalekken"},
    "ie": {"name": "DPC (Ireland)", "url": "https://www.dataprotection.ie/", "email": "info@dataprotection.ie"},
    "us": {"name": "FTC (US - COPPA/privacy)", "url": "https://reportfraud.ftc.gov/", "email": "https://reportfraud.ftc.gov/"},
}

CRYPTO_C2_INDICATORS = [
    "usdt", "mining", "wallet", "crypto", "bitcoin", "btc", "eth", "c2",
    "botnet", "stealer", "dropper", "malware", "rat",
]

FIREBASE_ABUSE_URL = "https://firebase.google.com/support/troubleshooter/contact"
AWS_ABUSE_URL = "https://support.aws.amazon.com/s/report-abuse"
GCP_ABUSE_URL = "https://support.google.com/code/contact/cloud_platform_report"
AZURE_ABUSE_URL = "https://www.microsoft.com/en-us/trust-center/privacy/report-abuse"

CERT_CHANNELS = {
    "us-cert": {"name": "US-CERT / CISA", "url": "https://www.cisa.gov/report", "email": "report@cisa.gov"},
    "cert-eu": {"name": "CERT-EU", "url": "https://cert.europa.eu/", "email": "cert-eu@ec.europa.eu"},
    "ncsc-uk": {"name": "NCSC UK", "url": "https://report.ncsc.gov.uk/", "email": "report@ncsc.gov.uk"},
}


def classify_disclosure_path(target_info: dict) -> list[dict]:
    """
    Given a target dict, return an ordered list of disclosure paths.
    Priority: bugbounty > responsible_disclosure > abuse/platform > gdpr > cert > law_enforcement > direct
    """
    slug = target_info.get("slug", target_info.get("target_slug", ""))
    domain = target_info.get("domain", target_info.get("bucket_name", slug))
    schema = target_info.get("schema", "firebase")
    pii_count = 0  # pii_record_count not in schema
    h1_result = target_info.get("_h1", None)
    bc_result = target_info.get("_bc", None)
    sec_txt = target_info.get("_sec_txt", None)
    abuse_contact = target_info.get("_abuse_contact", None)

    paths = []

    # Bug bounty first (if found)
    if h1_result:
        paths.append({
            "program_type": "bugbounty",
            "platform": "hackerone",
            "program_url": h1_result.get("program_url"),
            "submission_url": h1_result.get("submission_url"),
            "contact_email": None,
            "scope_status": "unknown",
            "notes": f"HackerOne public program. Bounties: {h1_result.get('offers_bounties', False)}",
        })

    if bc_result:
        paths.append({
            "program_type": "bugbounty",
            "platform": "bugcrowd",
            "program_url": bc_result.get("program_url"),
            "submission_url": bc_result.get("submission_url"),
            "contact_email": None,
            "scope_status": "unknown",
            "notes": "Bugcrowd program found via slug heuristic",
        })

    # security.txt responsible disclosure
    if sec_txt:
        contacts = sec_txt.get("contact", [])
        if isinstance(contacts, str):
            contacts = [contacts]
        policy_url = sec_txt.get("policy")
        contact_email = None
        for c in contacts:
            if c.startswith("mailto:"):
                contact_email = c.replace("mailto:", "")
                break
            elif "@" in c:
                contact_email = c
                break
        paths.append({
            "program_type": "responsible_disclosure",
            "platform": "security_txt",
            "program_url": sec_txt.get("source_url"),
            "submission_url": policy_url,
            "contact_email": contact_email,
            "scope_status": "unknown",
            "notes": f"RFC 9116 security.txt found. Contacts: {', '.join(contacts[:3])}",
        })

    # Detect crypto/C2 indicators
    slug_lower = slug.lower()
    is_crypto_c2 = any(ind in slug_lower for ind in CRYPTO_C2_INDICATORS)

    # Firebase platform abuse (always for firebase schema)
    if schema == "firebase":
        paths.append({
            "program_type": "abuse",
            "platform": "google_firebase",
            "program_url": FIREBASE_ABUSE_URL,
            "submission_url": FIREBASE_ABUSE_URL,
            "contact_email": "firebase-support@google.com",
            "scope_status": "in_scope",
            "notes": "Google Firebase abuse report — always applicable for Firebase targets",
        })

    # Cloud platform abuse for bucket schema
    # Use provider from DB (target_info["provider"]) — never try to detect from slug/domain.
    # Bucket slugs are opaque identifiers, NOT hostnames. Do NOT probe DNS for them.
    if schema == "bucket":
        provider = target_info.get("provider") or target_info.get("platform", "unknown")
        provider = provider.lower()
        _DO_ABUSE_URL  = "https://www.digitalocean.com/company/contact/abuse/"
        _ALI_ABUSE_URL = "https://help.aliyun.com/knowledge_detail/37544.html"
        _PROVIDER_MAP = {
            "aws":   (AWS_ABUSE_URL,   "abuse@amazonaws.com",           "aws"),
            "gcp":   (GCP_ABUSE_URL,   "cloud-compliance@google.com",   "gcp"),
            "azure": (AZURE_ABUSE_URL, "abuse@microsoft.com",           "azure"),
            "dos":   (_DO_ABUSE_URL,   "abuse@digitalocean.com",        "digitalocean"),
            "ali":   (_ALI_ABUSE_URL,  "abuse@service.aliyun.com",      "alibaba"),
        }
        if provider in _PROVIDER_MAP:
            url, email, plat = _PROVIDER_MAP[provider]
            paths.append({
                "program_type": "abuse",
                "platform": plat,
                "program_url": url,
                "submission_url": url,
                "contact_email": email,
                "scope_status": "in_scope",
                "notes": f"{plat.upper()} cloud abuse report channel (provider from DB)",
            })
        else:
            # Unknown provider — generic cloud abuse fallback
            paths.append({
                "program_type": "abuse",
                "platform": "cloud_unknown",
                "program_url": None,
                "submission_url": None,
                "contact_email": "abuse@" + (provider or "unknown"),
                "scope_status": "unknown",
                "notes": f"Unknown cloud provider '{provider}' — manual abuse contact needed",
            })

    # GDPR / DPA if large PII exposure
    if pii_count > 100000:
        dpa = GDPR_DPA["default"]
        paths.append({
            "program_type": "gdpr",
            "platform": "edpb",
            "program_url": dpa["url"],
            "submission_url": dpa["url"],
            "contact_email": dpa["email"],
            "scope_status": "in_scope",
            "notes": f"PII count {pii_count:,} exceeds 100k threshold — GDPR DPA notification may be required",
        })

    # Crypto/C2: law enforcement + ISP abuse, NOT operator
    if is_crypto_c2:
        paths.append({
            "program_type": "law_enforcement",
            "platform": "ic3",
            "program_url": "https://www.ic3.gov/",
            "submission_url": "https://www.ic3.gov/Home/FileComplaint",
            "contact_email": None,
            "scope_status": "in_scope",
            "notes": "IC3 (FBI Internet Crime Complaint Center) — crypto/C2 indicators detected",
        })
        paths.append({
            "program_type": "cert",
            "platform": "us-cert",
            "program_url": CERT_CHANNELS["us-cert"]["url"],
            "submission_url": CERT_CHANNELS["us-cert"]["url"],
            "contact_email": CERT_CHANNELS["us-cert"]["email"],
            "scope_status": "in_scope",
            "notes": "CISA/US-CERT — C2 infrastructure reporting",
        })
    else:
        # Non-C2: include CERT as option
        paths.append({
            "program_type": "cert",
            "platform": "us-cert",
            "program_url": CERT_CHANNELS["us-cert"]["url"],
            "submission_url": CERT_CHANNELS["us-cert"]["url"],
            "contact_email": CERT_CHANNELS["us-cert"]["email"],
            "scope_status": "unknown",
            "notes": "CISA/US-CERT — optional escalation path",
        })

    # Direct contact (always as fallback)
    paths.append({
        "program_type": "direct",
        "platform": "direct",
        "program_url": None,
        "submission_url": None,
        "contact_email": abuse_contact or f"security@{domain}",
        "scope_status": "unknown",
        "notes": f"Direct contact fallback. Abuse contact: {abuse_contact or 'unknown — defaulting to security@'}",
    })

    return paths


def _detect_cloud_provider(domain: str) -> str:
    d = domain.lower()
    if "s3.amazonaws.com" in d or "s3-" in d or ".s3." in d:
        return "aws"
    if "storage.googleapis.com" in d or "appspot.com" in d:
        return "gcp"
    if "blob.core.windows.net" in d or "azurewebsites" in d:
        return "azure"
    return "unknown"


# ─────────────────────────────────────────────
# 6. Upsert to DB
# ─────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO {schema}.disclosure_programs
    (target_slug, program_type, platform, program_url, submission_url,
     contact_email, scope_status, notes, discovered_at, verified_at)
VALUES
    (%(target_slug)s, %(program_type)s, %(platform)s, %(program_url)s,
     %(submission_url)s, %(contact_email)s, %(scope_status)s, %(notes)s,
     NOW(), NOW())
ON CONFLICT (target_slug, program_type, platform)
DO UPDATE SET
    program_url    = EXCLUDED.program_url,
    submission_url = EXCLUDED.submission_url,
    contact_email  = EXCLUDED.contact_email,
    scope_status   = EXCLUDED.scope_status,
    notes          = EXCLUDED.notes,
    verified_at    = NOW()
"""


def upsert_paths(target_slug: str, paths: list[dict], schema: str, dry_run: bool = False):
    if dry_run:
        print(f"\n[DRY RUN] Would upsert {len(paths)} disclosure paths for {target_slug}:")
        for p in paths:
            print(f"  [{p['program_type']}] {p['platform']} — {p.get('contact_email') or p.get('program_url') or 'n/a'}")
        return

    with get_db() as conn:
        with conn.cursor() as cur:
            for path in paths:
                row = dict(path)
                row["target_slug"] = target_slug
                sql = UPSERT_SQL.format(schema=schema)
                cur.execute(sql, row)
        conn.commit()


# ─────────────────────────────────────────────
# 6. atlas_scan — single target
# ─────────────────────────────────────────────

def domain_from_slug(slug: str) -> str:
    """
    Derive a scannable domain from a target slug.
    Handles patterns like:
      - ad-cash-c2917-firebaseio-com  → ad-cash-c2917.firebaseio.com
      - myapp-default-rtdb            → myapp-default-rtdb.firebaseio.com
      - example-com                   → example.com
    """
    # Known firebaseio pattern: ends with -firebaseio-com
    if slug.endswith("-firebaseio-com"):
        base = slug[: -len("-firebaseio-com")]
        return f"{base}.firebaseio.com"
    # Ends with known TLD patterns
    for tld in ["-com", "-net", "-org", "-io", "-co"]:
        if slug.endswith(tld):
            base = slug[: -len(tld)]
            # Replace remaining hyphens that look like dots (last segment)
            # Heuristic: rejoin only last separator
            domain = base + tld.replace("-", ".")
            return domain
    # Default: treat hyphens as part of the name
    return slug.replace("_", "-")


def company_name_from_slug(slug: str) -> str:
    """Extract a likely company/product name from a slug for H1/BC lookup."""
    # Strip firebase suffixes
    s = re.sub(r"-firebaseio-com$|-default-rtdb$", "", slug)
    # Take first 1-2 meaningful words
    parts = re.split(r"[-_]", s)
    return " ".join(parts[:2])


def atlas_scan(target_slug: str, domain: str = None, schema: str = "firebase", dry_run: bool = False) -> list[dict]:
    """
    Run all disclosure checks for one target and upsert results.
    Returns the list of disclosure paths found.
    """
    if not domain:
        domain = domain_from_slug(target_slug)

    company = company_name_from_slug(target_slug)

    print(f"🗺️  Atlas scanning: {target_slug} ({domain}) [{schema}]")

    # Run checks (synchronously for single-target; batch uses async)
    sec_txt = check_security_txt(domain)
    h1 = check_hackerone(company)
    bc = check_bugcrowd(domain)
    abuse = get_abuse_contact(domain)

    print(f"  security.txt: {'✓' if sec_txt else '✗'}")
    print(f"  HackerOne:    {'✓ ' + h1.get('program_url','') if h1 else '✗'}")
    print(f"  Bugcrowd:     {'✓ ' + bc.get('program_url','') if bc else '✗'}")
    print(f"  Abuse:        {abuse or '✗'}")

    # Get PII count from DB if available (best-effort)
    pii_count = 0
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                if schema == "firebase":
                    cur.execute(
                        "SELECT severity FROM firebase.targets WHERE slug = %s LIMIT 1",
                        (target_slug,),
                    )
                else:
                    cur.execute(
                        "SELECT severity FROM bucket.targets WHERE slug = %s LIMIT 1",
                        (target_slug,),
                    )
                row = cur.fetchone()
                if row:
                    pii_count = row[0] or 0
    except Exception:
        pass

    target_info = {
        "slug": target_slug,
        "domain": domain,
        "schema": schema,
        "pii_record_count": pii_count,
        "_h1": h1,
        "_bc": bc,
        "_sec_txt": sec_txt,
        "_abuse_contact": abuse,
    }

    paths = classify_disclosure_path(target_info)

    upsert_paths(target_slug, paths, schema, dry_run=dry_run)

    print(f"  → {len(paths)} disclosure path(s) mapped")
    for p in paths:
        label = f"{p['program_type']}/{p['platform']}"
        dest = p.get("contact_email") or p.get("program_url") or "n/a"
        print(f"    [{label}] {dest}")

    return paths


# ─────────────────────────────────────────────
# 7. atlas_batch — async multi-target
# ─────────────────────────────────────────────

async def _async_sec_txt(session: aiohttp.ClientSession, domain: str) -> Optional[dict]:
    urls = [
        f"https://{domain}/.well-known/security.txt",
        f"https://{domain}/security.txt",
    ]
    for url in urls:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as r:
                if r.status == 200:
                    text = await r.text()
                    if "Contact:" in text:
                        result = {"source_url": url, "raw": text}
                        for key, field in {
                            "contact": "Contact", "policy": "Policy",
                            "expires": "Expires", "acknowledgments": "Acknowledgments"
                        }.items():
                            matches = re.findall(
                                rf"^{re.escape(field)}:\s*(.+)$", text,
                                re.MULTILINE | re.IGNORECASE
                            )
                            if matches:
                                result[key] = matches if len(matches) > 1 else matches[0]
                        return result
        except Exception:
            continue
    return None


async def _scan_one_async(sem: asyncio.Semaphore, session: aiohttp.ClientSession,
                          row: dict, schema: str, dry_run: bool):
    async with sem:
        slug = row.get("slug") or row.get("target_slug")

        # ── Bucket schema: NO DNS, NO HTTP probes ────────────────────────────
        # Bucket slugs are opaque identifiers, not hostnames.
        # Provider is stored in the DB (platform column). Use it directly.
        if schema == "bucket":
            provider = (row.get("platform") or "unknown").lower()
            # Org name from slug for H1/BC lookup only (no DNS)
            company = company_name_from_slug(slug)
            loop = asyncio.get_event_loop()
            h1  = await loop.run_in_executor(None, check_hackerone, company)
            bc  = await loop.run_in_executor(None, check_bugcrowd, slug.split("-")[0])
            target_info = {
                "slug": slug,
                "domain": slug,   # never used for DNS in bucket path
                "schema": schema,
                "provider": provider,
                "platform": provider,
                "pii_record_count": 0,
                "_h1": h1,
                "_bc": bc,
                "_sec_txt": None,       # no security.txt for buckets
                "_abuse_contact": None, # cloud abuse mapped from provider, not RDAP
            }
            paths = classify_disclosure_path(target_info)
            upsert_paths(slug, paths, schema, dry_run=dry_run)
            print(f"  ✓ {slug} [{provider}]: {len(paths)} paths")
            return slug, paths

        # ── Firebase / domain schema: full probe suite ───────────────────────
        domain = row.get("domain") or domain_from_slug(slug)
        company = company_name_from_slug(slug)

        loop = asyncio.get_event_loop()
        sec_txt = await _async_sec_txt(session, domain)
        h1 = await loop.run_in_executor(None, check_hackerone, company)
        bc = await loop.run_in_executor(None, check_bugcrowd, domain)
        abuse = await loop.run_in_executor(None, get_abuse_contact, domain)

        target_info = {
            "slug": slug,
            "domain": domain,
            "schema": schema,
            "pii_record_count": 0,
            "_h1": h1,
            "_bc": bc,
            "_sec_txt": sec_txt,
            "_abuse_contact": abuse,
        }

        paths = classify_disclosure_path(target_info)
        upsert_paths(slug, paths, schema, dry_run=dry_run)
        print(f"  ✓ {slug}: {len(paths)} paths")
        return slug, paths


async def atlas_batch_async(schema: str = "firebase", limit: int = 50,
                            unscanned_only: bool = True, dry_run: bool = False):
    """Async batch scan for targets not yet in disclosure_programs."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if schema == "firebase":
                base_table = "firebase.targets"
                dp_table = "firebase.disclosure_programs"
            else:
                base_table = "bucket.targets"
                dp_table = "bucket.disclosure_programs"

            if unscanned_only:
                sql = f"""
                    SELECT slug, severity, platform
                    FROM {base_table}
                    WHERE slug NOT IN (
                        SELECT DISTINCT target_slug FROM {dp_table}
                    )
                    LIMIT %s
                """
            else:
                sql = f"SELECT slug, severity, platform FROM {base_table} LIMIT %s"

            try:
                cur.execute(sql, (limit,))
                rows = [dict(r) for r in cur.fetchall()]
            except Exception as e:
                print(f"  [WARN] Could not query {base_table}: {e}")
                rows = []

    if not rows:
        print(f"No unscanned targets found in {schema} schema.")
        return

    print(f"🗺️  Atlas batch: {len(rows)} targets [{schema}] {'(dry run)' if dry_run else ''}")

    sem = asyncio.Semaphore(10)  # max 10 concurrent
    connector = aiohttp.TCPConnector(limit=20, ssl=False)
    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        tasks = [_scan_one_async(sem, session, row, schema, dry_run) for row in rows]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ok = sum(1 for r in results if not isinstance(r, Exception))
    print(f"\n✅ Batch complete: {ok}/{len(rows)} targets scanned")


def atlas_batch(schema: str = "firebase", limit: int = 50,
                unscanned_only: bool = True, dry_run: bool = False):
    asyncio.run(atlas_batch_async(schema, limit, unscanned_only, dry_run))


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Atlas 🗺️ — Disclosure Program Scout"
    )
    parser.add_argument("--target", help="Target slug to scan")
    parser.add_argument("--domain", help="Override domain for --target")
    parser.add_argument("--batch", action="store_true", help="Batch scan unscanned targets")
    parser.add_argument("--schema", choices=["firebase", "bucket"], default="firebase",
                        help="DB schema to use (default: firebase)")
    parser.add_argument("--limit", type=int, default=50, help="Max targets in batch mode")
    parser.add_argument("--all", action="store_true", help="Rescan already-scanned targets too")
    parser.add_argument("--dry-run", action="store_true", help="Print paths without writing to DB")
    args = parser.parse_args()

    if not args.dry_run:
        try:
            ensure_tables()
        except Exception as e:
            print(f"[WARN] Could not ensure tables (may already exist or schema missing): {e}")

    mode = "target" if args.target else ("batch" if args.batch else "help")
    set_running(SCOUT_NAME, {"mode": mode, "schema": args.schema})
    try:
        if args.target:
            paths = atlas_scan(args.target, domain=args.domain, schema=args.schema, dry_run=args.dry_run)
            set_idle(SCOUT_NAME, {"mode": mode, "paths_found": len(paths), "target": args.target})
        elif args.batch:
            atlas_batch(
                schema=args.schema,
                limit=args.limit,
                unscanned_only=not args.all,
                dry_run=args.dry_run,
            )
            set_idle(SCOUT_NAME, {"mode": mode, "schema": args.schema})
        else:
            set_idle(SCOUT_NAME, {"mode": "help"})
            parser.print_help()
            sys.exit(1)
    except Exception as e:
        set_error(SCOUT_NAME, str(e))
        raise


if __name__ == "__main__":
    main()
