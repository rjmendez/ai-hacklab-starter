"""Gamma agent card — OSINT and recon."""
import os


def build_agent_card() -> dict:
    return {
        "name": "Gamma",
        "description": "Field recon agent. Subdomain enumeration, web fetching, port scanning, OSINT research.",
        "url": os.getenv("AGENT_URL", "http://localhost:8200"),
        "skills": [
            {"id": "osint_research", "name": "OSINT Research", "description": "Org/domain OSINT: subdomains, cloud hints, tech stack"},
            {"id": "web_fetch", "name": "Web Fetch", "description": "HTTP fetch with optional headers"},
            {"id": "ct_enum", "name": "CT Enumeration", "description": "Certificate Transparency subdomain lookup via crt.sh"},
            {"id": "port_scan", "name": "Port Scan", "description": "Basic TCP port connectivity check"},
            {"id": "memory_write", "name": "Memory Write", "description": "Store key/value to agent memory"},
            {"id": "memory_read", "name": "Memory Read", "description": "Read from agent memory"},
        ],
        "capabilities": {"streaming": False, "push_notifications": False},
        "protocol_version": "0.3.0",
    }
