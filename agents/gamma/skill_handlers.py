"""
Gamma agent skill handlers — OSINT and recon capabilities.

To add a new skill:
  1. Write a handler function: def handle_my_skill(input_data: dict) -> dict
  2. Add it to GAMMA_SKILL_HANDLERS at the bottom
  3. Register the skill_id in agent_card.py

The A2A server (a2a/server.py) loads GAMMA_SKILL_HANDLERS via importlib
when AGENT_NAME=gamma.
"""
import socket
import logging
import requests

logger = logging.getLogger(__name__)


def handle_ct_enum(input_data: dict) -> dict:
    """
    Certificate Transparency subdomain enumeration via crt.sh.

    Input:  {"domain": "example.com"}
    Output: {"status": "ok", "domain": "...", "subdomains": [...], "count": N}
    """
    domain = input_data.get("domain", "")
    if not domain:
        return {"status": "error", "message": "domain required"}
    try:
        resp = requests.get(
            f"https://crt.sh/?q=%.{domain}&output=json",
            timeout=15,
            headers={"User-Agent": "ai-hacklab-starter/1.0"},
        )
        resp.raise_for_status()
        subdomains: set[str] = set()
        for entry in resp.json():
            for name in entry.get("name_value", "").split("\n"):
                name = name.strip().lstrip("*.")
                if name and name.endswith(domain):
                    subdomains.add(name)
        return {
            "status": "ok",
            "domain": domain,
            "subdomains": sorted(subdomains),
            "count": len(subdomains),
        }
    except Exception as e:
        logger.warning("ct_enum failed for %s: %s", domain, e)
        return {"status": "error", "message": str(e)}


def handle_web_fetch(input_data: dict) -> dict:
    """
    Fetch a URL and return response body (truncated to 10 KB).

    Input:  {"url": "https://...", "timeout": 10, "headers": {...}}
    Output: {"status": "ok", "url": "...", "status_code": 200, "body": "...", "truncated": false}
    """
    url = input_data.get("url", "")
    if not url:
        return {"status": "error", "message": "url required"}
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        headers.update(input_data.get("headers", {}))
        resp = requests.get(url, timeout=input_data.get("timeout", 10), headers=headers)
        body = resp.text
        truncated = len(body) > 10000
        return {
            "status": "ok",
            "url": url,
            "status_code": resp.status_code,
            "body": body[:10000],
            "truncated": truncated,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def handle_osint_research(input_data: dict) -> dict:
    """
    Basic OSINT on a target domain or organization.
    Chains CT enumeration and generates cloud asset name hints.

    Input:  {"target": "example.com", "org_name": "Example Corp"}
    Output: {"status": "ok", "target": "...", "subdomains": [...], "cloud_hints": [...]}
    """
    target = input_data.get("target", input_data.get("domain", ""))
    org = input_data.get("org_name", "")
    results: dict = {"status": "ok", "target": target}

    if target:
        ct = handle_ct_enum({"domain": target})
        results["subdomains"] = ct.get("subdomains", [])
        results["subdomain_count"] = ct.get("count", 0)

    if org:
        base = org.lower().replace(" ", "-").replace("_", "-")
        results["cloud_hints"] = [
            base,
            base.replace("-", ""),
            base + "-prod",
            base + "-dev",
            base + "-staging",
            base + "-backup",
            base + "-data",
            base + "-assets",
        ]

    return results


def handle_port_scan(input_data: dict) -> dict:
    """
    Basic TCP port connectivity check (not a full nmap-style scanner).
    Useful for quickly checking if common ports are open.

    Input:  {"host": "example.com", "ports": [80, 443, 8080, 8443]}
    Output: {"status": "ok", "host": "...", "open_ports": [...], "checked": [...]}
    """
    host = input_data.get("host", "")
    ports = input_data.get("ports", [80, 443, 8080, 8443, 22, 21, 3306, 5432])
    if not host:
        return {"status": "error", "message": "host required"}
    open_ports = []
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=2):
                open_ports.append(port)
        except Exception:
            pass
    return {
        "status": "ok",
        "host": host,
        "open_ports": open_ports,
        "checked": ports,
    }


# ── Skill handler registry ────────────────────────────────────────────────────
# Referenced by a2a/server.py when AGENT_NAME=gamma
GAMMA_SKILL_HANDLERS: dict = {
    "osint_research": handle_osint_research,
    "web_fetch":      handle_web_fetch,
    "ct_enum":        handle_ct_enum,
    "port_scan":      handle_port_scan,
}
