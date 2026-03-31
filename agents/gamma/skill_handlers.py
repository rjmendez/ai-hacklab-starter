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


def handle_wifi_scan(input_data: dict) -> dict:
    """
    Scan for nearby Wi-Fi networks.
    Uses nmcli if available, falls back to iwlist.
    Input:  {}
    Output: {"status": "ok", "networks": [{"ssid":..,"bssid":..,"signal":..,"security":..}], "count": N}
    """
    import subprocess
    networks = []

    # Try nmcli first
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split(":")
                if len(parts) >= 3:
                    networks.append({
                        "ssid":     parts[0] or "<hidden>",
                        "bssid":    ":".join(parts[1:7]) if len(parts) >= 7 else parts[1],
                        "signal":   parts[-2] if len(parts) >= 4 else "",
                        "security": parts[-1] if len(parts) >= 4 else "",
                    })
            return {"status": "ok", "networks": networks, "count": len(networks), "source": "nmcli"}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("nmcli failed: %s", exc)

    # Fallback: iwlist
    try:
        result = subprocess.run(
            ["iwlist", "scan"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            import re as _re
            cells = result.stdout.split("Cell ")
            for cell in cells[1:]:
                ssid     = _re.search(r'ESSID:"([^"]*)"', cell)
                bssid    = _re.search(r'Address: ([0-9A-F:]+)', cell)
                signal   = _re.search(r'Signal level=(-?\d+)', cell)
                security = "WPA" if "WPA" in cell else ("WEP" if "WEP" in cell else "Open")
                networks.append({
                    "ssid":     ssid.group(1) if ssid else "<hidden>",
                    "bssid":    bssid.group(1) if bssid else "",
                    "signal":   signal.group(1) if signal else "",
                    "security": security,
                })
            return {"status": "ok", "networks": networks, "count": len(networks), "source": "iwlist"}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("iwlist failed: %s", exc)

    return {"status": "error", "message": "Neither nmcli nor iwlist available"}


def handle_dns_lookup(input_data: dict) -> dict:
    """
    Resolve DNS records for a domain.
    Input:  {"domain": "example.com", "record_types": ["A", "MX", "TXT"]}
    Output: {"status": "ok", "domain": "...", "records": {"A": [...], "MX": [...]}}
    """
    domain       = input_data.get("domain", "")
    record_types = input_data.get("record_types", ["A", "MX", "TXT"])
    if not domain:
        return {"status": "error", "message": "domain required"}

    records: dict = {}

    # Try dnspython first
    try:
        import dns.resolver
        for rtype in record_types:
            try:
                answers = dns.resolver.resolve(domain, rtype)
                records[rtype] = [str(r) for r in answers]
            except Exception:
                records[rtype] = []
        return {"status": "ok", "domain": domain, "records": records, "resolver": "dnspython"}
    except ImportError:
        pass

    # Fallback: socket for A/AAAA only
    for rtype in record_types:
        if rtype in ("A", "AAAA"):
            try:
                family = socket.AF_INET6 if rtype == "AAAA" else socket.AF_INET
                addrs  = socket.getaddrinfo(domain, None, family)
                records[rtype] = list({a[4][0] for a in addrs})
            except Exception:
                records[rtype] = []
        else:
            records[rtype] = []  # socket can't resolve MX/TXT etc.

    return {"status": "ok", "domain": domain, "records": records, "resolver": "socket"}


def handle_whois(input_data: dict) -> dict:
    """
    Run whois on a domain or IP and return the raw output.
    Input:  {"target": "example.com"}
    Output: {"status": "ok", "target": "...", "raw": "...", "truncated": bool}
    """
    import subprocess
    target = input_data.get("target", "")
    if not target:
        return {"status": "error", "message": "target required"}
    try:
        result = subprocess.run(
            ["whois", target],
            capture_output=True, text=True, timeout=20,
        )
        raw       = result.stdout
        truncated = len(raw) > 10000
        return {
            "status":    "ok",
            "target":    target,
            "raw":       raw[:10000],
            "truncated": truncated,
        }
    except FileNotFoundError:
        return {"status": "error", "message": "whois not installed"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ── Skill handler registry ────────────────────────────────────────────────────
# Referenced by a2a/server.py when AGENT_NAME=gamma
GAMMA_SKILL_HANDLERS: dict = {
    "osint_research": handle_osint_research,
    "web_fetch":      handle_web_fetch,
    "ct_enum":        handle_ct_enum,
    "port_scan":      handle_port_scan,
    "wifi_scan":      handle_wifi_scan,
    "dns_lookup":     handle_dns_lookup,
    "whois":          handle_whois,
}
