#!/usr/bin/env python3
"""
scouts/atlas/atlas_seed.py — Atlas: disclosure path scout.

Atlas maps who to notify for any given finding. It takes a target domain
or finding and returns an ordered list of disclosure channels.

Atlas is READ-ONLY. It never submits anything. That's Hermes's job.

Disclosure path priority order:
  1. Bug bounty program (HackerOne, Bugcrowd, etc.) — fastest remediation
  2. Responsible disclosure (security.txt / RFC 9116)
  3. Platform abuse reporting (cloud provider channels)
  4. Direct contact (security@, abuse@ via RDAP/WHOIS)
  5. CERT / national agencies (for critical infrastructure or when others fail)
  6. Regulatory (GDPR/DPA) — when PII is involved at scale

Usage:
    python3 scouts/atlas/atlas_seed.py --target example.com
    python3 scouts/atlas/atlas_seed.py --target 1.2.3.4
    python3 scouts/atlas/atlas_seed.py --finding-type pii --count 50000
"""

import argparse
import json
import logging
import sys

try:
    import requests
except ImportError:
    requests = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [atlas] %(levelname)s %(message)s")
log = logging.getLogger("atlas")


# ── Bug bounty program detection ──────────────────────────────────────────────

# Known bug bounty program domains (add your own)
KNOWN_BB_DOMAINS: dict[str, dict] = {
    # Format: "domain_fragment": {"platform": "...", "url": "...", "program": "..."}
    # This is intentionally sparse — expand based on your research targets.
}


def check_bug_bounty(domain: str) -> list[dict]:
    """Check if the domain has a known bug bounty program."""
    paths = []
    domain_lower = domain.lower()

    # Check known programs
    for fragment, info in KNOWN_BB_DOMAINS.items():
        if fragment in domain_lower:
            paths.append({
                "channel":  "bug_bounty",
                "platform": info["platform"],
                "url":      info["url"],
                "program":  info.get("program", domain),
                "priority": 1,
                "notes":    "Known bug bounty program",
            })

    return paths


def check_security_txt(domain: str) -> list[dict]:
    """Check for security.txt (RFC 9116) at standard locations."""
    if requests is None:
        log.warning("requests not installed — skipping security.txt check")
        return []

    paths = []
    for url in [
        f"https://{domain}/.well-known/security.txt",
        f"https://{domain}/security.txt",
    ]:
        try:
            resp = requests.get(url, timeout=5,
                                headers={"User-Agent": "ai-hacklab-starter-atlas/1.0"},
                                allow_redirects=True)
            if resp.status_code == 200 and "contact:" in resp.text.lower():
                # Extract contact lines
                contacts = [
                    line.strip() for line in resp.text.splitlines()
                    if line.lower().startswith("contact:")
                ]
                paths.append({
                    "channel":  "security_txt",
                    "url":      url,
                    "contacts": contacts[:3],
                    "priority": 2,
                    "notes":    "RFC 9116 security.txt found",
                })
                break
        except Exception:
            pass

    return paths


def check_platform_abuse(domain: str, cloud_hints: list[str] = None) -> list[dict]:
    """
    Return platform abuse reporting channels based on domain or cloud hints.
    These are always included for cloud-hosted assets.
    """
    paths = []
    domain_lower  = domain.lower()
    hints         = [h.lower() for h in (cloud_hints or [])]
    all_indicators = [domain_lower] + hints

    platform_channels = [
        {
            "indicator": ["firebase", "firebaseio", "google", "gcp", "appspot"],
            "channel":   "platform_abuse",
            "platform":  "Google / Firebase",
            "url":       "https://support.google.com/code/contact/cloud_vulnerability_form",
            "priority":  3,
        },
        {
            "indicator": ["amazonaws", "s3.", "aws", "cloudfront"],
            "channel":   "platform_abuse",
            "platform":  "Amazon Web Services",
            "url":       "https://aws.amazon.com/security/vulnerability-reporting/",
            "priority":  3,
        },
        {
            "indicator": ["azure", "microsoft", "blob.core.windows"],
            "channel":   "platform_abuse",
            "platform":  "Microsoft Azure",
            "url":       "https://msrc.microsoft.com/report/vulnerability",
            "priority":  3,
        },
        {
            "indicator": ["digitalocean", "spaces.digitaloceanspaces"],
            "channel":   "platform_abuse",
            "platform":  "DigitalOcean",
            "url":       "https://www.digitalocean.com/security/",
            "priority":  3,
        },
    ]

    for pc in platform_channels:
        if any(ind in item for ind in pc["indicator"] for item in all_indicators):
            paths.append({k: v for k, v in pc.items() if k != "indicator"})

    return paths


def check_direct_contact(domain: str) -> list[dict]:
    """Generate likely direct contact addresses from the domain."""
    # Strip subdomains to get root domain
    parts = domain.split(".")
    root  = ".".join(parts[-2:]) if len(parts) >= 2 else domain

    return [
        {
            "channel":  "direct_email",
            "platform": "Direct",
            "email":    f"security@{root}",
            "url":      f"https://rdap.org/domain/{root}",
            "priority": 4,
            "notes":    "Verify via RDAP/WHOIS before contacting",
        },
        {
            "channel":  "direct_email",
            "platform": "Direct (abuse)",
            "email":    f"abuse@{root}",
            "priority": 4,
            "notes":    "Fallback if security@ doesn't exist",
        },
    ]


def check_regulatory(finding_type: str, pii_count: int = 0) -> list[dict]:
    """Return regulatory channels if PII is involved at scale."""
    paths = []
    if finding_type in ("pii", "credential", "config_leak") and pii_count > 10000:
        paths.append({
            "channel":   "regulatory",
            "platform":  "CISA (US)",
            "url":       "https://www.cisa.gov/report",
            "priority":  5,
            "notes":     f"PII count {pii_count:,} may trigger mandatory notification",
        })
        paths.append({
            "channel":   "regulatory",
            "platform":  "GDPR / Your Local DPA",
            "url":       "https://edpb.europa.eu/about-edpb/about-edpb/members_en",
            "priority":  5,
            "notes":     "Required within 72h if EU residents affected",
        })
    return paths


# ── Main mapping ──────────────────────────────────────────────────────────────

def map_disclosure_paths(
    domain:       str,
    finding_type: str = "credential",
    pii_count:    int = 0,
    cloud_hints:  list[str] = None,
) -> dict:
    """
    Return ordered disclosure paths for a target.

    Args:
        domain:       Target domain or IP
        finding_type: Finding type from mcp/research_notes.py types
        pii_count:    Estimated PII record count (triggers regulatory channels)
        cloud_hints:  Known cloud platform indicators (e.g. ["firebase", "s3"])
    """
    all_paths = []
    all_paths += check_bug_bounty(domain)
    all_paths += check_security_txt(domain)
    all_paths += check_platform_abuse(domain, cloud_hints)
    all_paths += check_direct_contact(domain)
    all_paths += check_regulatory(finding_type, pii_count)

    # Sort by priority, then deduplicate channels
    all_paths.sort(key=lambda p: p.get("priority", 99))

    return {
        "target":      domain,
        "finding_type": finding_type,
        "pii_count":   pii_count,
        "paths":       all_paths,
        "count":       len(all_paths),
        "top_channel": all_paths[0]["channel"] if all_paths else "none",
        "note":        "Atlas maps paths only. Hermes drafts reports. Operator submits.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas — disclosure path scout")
    parser.add_argument("--target",       required=True, help="Target domain or IP")
    parser.add_argument("--finding-type", default="credential",
                        choices=["credential", "endpoint", "config_leak", "cert",
                                 "domain", "api_key", "private_key", "pii", "other"],
                        help="Type of finding")
    parser.add_argument("--pii-count",    type=int, default=0,
                        help="Estimated PII record count")
    parser.add_argument("--cloud-hints",  nargs="*", default=[],
                        help="Known cloud platform hints (e.g. firebase s3)")
    args = parser.parse_args()

    result = map_disclosure_paths(
        domain=args.target,
        finding_type=args.finding_type,
        pii_count=args.pii_count,
        cloud_hints=args.cloud_hints,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
