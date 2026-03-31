#!/usr/bin/env python3
"""
a2a/server.py — A2A JSON-RPC server for the agent mesh.

Each agent in the mesh runs this server. It:
  - Accepts JSON-RPC 2.0 requests at POST /a2a
  - Authenticates via Bearer token + optional TOTP 2FA
  - Routes `tasks/send` calls to registered skill handlers
  - Exposes GET /health and GET /.well-known/agent-card.json unauthenticated

Configuration (all via env vars):
  AGENT_NAME        Agent identifier (default: "agent")
  AGENT_TOKEN       Bearer token for inbound auth (required)
  AGENT_TOTP_SEED   TOTP seed for 2FA (optional — set to enable)
  AGENT_URL         Public URL of this agent
  A2A_HOST          Bind host (default: 0.0.0.0)
  A2A_PORT          Bind port (default: 8200)
  REDIS_HOST        Redis host for logging (default: redis)
  REDIS_PORT        Redis port (default: 6379)
  REDIS_PASSWORD    Redis password (default: empty)

Usage:
  python a2a/server.py
  A2A_PORT=8201 AGENT_NAME=alpha python a2a/server.py
"""

import importlib
import json
import logging
import os
import time
from typing import Any, Optional

import pyotp
import requests as _requests
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("a2a.server")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AGENT_NAME   = os.environ.get("AGENT_NAME", "agent")
AGENT_TOKEN  = os.environ.get("AGENT_TOKEN", "")
TOTP_SEED    = os.environ.get("AGENT_TOTP_SEED", "")
AGENT_URL    = os.environ.get("AGENT_URL", "http://localhost:8200")
A2A_HOST     = os.environ.get("A2A_HOST", "0.0.0.0")
A2A_PORT     = int(os.environ.get("A2A_PORT", "8200"))

REDIS_HOST     = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT     = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

# DB DSNs — empty by default, set via env
MEMORY_DB_DSN = os.environ.get("MEMORY_DB_DSN", "")
AUDIT_DB_DSN  = os.environ.get("AUDIT_DB_DSN", "")

# ---------------------------------------------------------------------------
# Known peers
# Map inbound bearer token → agent name for logging.
# Populate with tokens of agents that are allowed to call this one.
# Example:
#   KNOWN_PEERS = {
#       "token-of-alpha-agent": "alpha",
#       "token-of-gamma-agent": "gamma",
#   }
# ---------------------------------------------------------------------------
KNOWN_PEERS: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Redis (graceful degradation)
# ---------------------------------------------------------------------------
_redis_client = None

def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        r = redis.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            password=REDIS_PASSWORD or None,
            decode_responses=True,
            socket_connect_timeout=2, socket_timeout=2,
        )
        r.ping()
        _redis_client = r
        logger.info("Redis connected at %s:%d", REDIS_HOST, REDIS_PORT)
    except Exception as exc:
        logger.warning("Redis unavailable — logging disabled: %s", exc)
        _redis_client = None
    return _redis_client


def _log_call(skill_id: str, caller: str, success: bool, latency_ms: int) -> None:
    r = _get_redis()
    if not r:
        return
    try:
        entry = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent": AGENT_NAME,
            "skill": skill_id,
            "caller": caller,
            "success": success,
            "latency_ms": latency_ms,
        })
        r.lpush(f"a2a:log:{AGENT_NAME}", entry)
        r.ltrim(f"a2a:log:{AGENT_NAME}", 0, 499)
    except Exception as exc:
        logger.debug("Redis log failed: %s", exc)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _verify_token(authorization: Optional[str]) -> str:
    """Verify Bearer token. Returns caller name or raises HTTP 401."""
    if not AGENT_TOKEN:
        logger.warning("AGENT_TOKEN not set — all requests will be rejected")
        raise HTTPException(status_code=401, detail="Server not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization[len("Bearer "):]
    if token != AGENT_TOKEN:
        # Check if it's a known peer token
        if token not in KNOWN_PEERS:
            raise HTTPException(status_code=401, detail="Invalid token")
    return KNOWN_PEERS.get(token, "unknown")


def _verify_totp(x_totp: Optional[str]) -> None:
    """Verify TOTP code if seed is configured. Fails closed."""
    if not TOTP_SEED:
        return  # TOTP disabled
    if not x_totp:
        raise HTTPException(status_code=403, detail="TOTP code required")
    totp = pyotp.TOTP(TOTP_SEED)
    # Allow ±1 window (30s drift tolerance)
    if not totp.verify(x_totp, valid_window=1):
        raise HTTPException(status_code=403, detail="Invalid TOTP code")

# ---------------------------------------------------------------------------
# Built-in skill handlers
# ---------------------------------------------------------------------------
def _handle_task_status(input_data: dict) -> dict:
    """Return basic server health and stats."""
    r = _get_redis()
    recent: list = []
    if r:
        try:
            entries = r.lrange(f"a2a:log:{AGENT_NAME}", 0, 9)
            recent = [json.loads(e) for e in entries]
        except Exception:
            pass
    return {
        "status": "ok",
        "agent": AGENT_NAME,
        "uptime": "running",
        "redis": r is not None,
        "recent_calls": recent,
    }


def _handle_memory_write(input_data: dict) -> dict:
    """Write a key/value pair to agent memory (Redis)."""
    key   = input_data.get("key", "")
    value = input_data.get("value", "")
    ttl   = input_data.get("ttl_seconds", 0)
    if not key:
        return {"status": "error", "message": "key required"}
    r = _get_redis()
    if not r:
        return {"status": "error", "message": "Redis unavailable"}
    try:
        mem_key = f"memory:{AGENT_NAME}:{key}"
        if ttl > 0:
            r.setex(mem_key, ttl, json.dumps(value))
        else:
            r.set(mem_key, json.dumps(value))
        return {"status": "ok", "key": key}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _handle_memory_read(input_data: dict) -> dict:
    """Read a key from agent memory (Redis)."""
    key = input_data.get("key", "")
    if not key:
        return {"status": "error", "message": "key required"}
    r = _get_redis()
    if not r:
        return {"status": "error", "message": "Redis unavailable"}
    try:
        mem_key = f"memory:{AGENT_NAME}:{key}"
        raw = r.get(mem_key)
        if raw is None:
            return {"status": "not_found", "key": key}
        return {"status": "ok", "key": key, "value": json.loads(raw)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _handle_report_generation(input_data: dict) -> dict:
    """
    Generate a simple text report from structured input.
    Input: {"title": "...", "sections": [{"heading": "...", "content": "..."}]}
    """
    title    = input_data.get("title", "Report")
    sections = input_data.get("sections", [])
    lines    = [f"# {title}", ""]
    for sec in sections:
        heading = sec.get("heading", "")
        content = sec.get("content", "")
        if heading:
            lines.append(f"## {heading}")
        lines.append(content)
        lines.append("")
    report = "\n".join(lines)
    return {"status": "ok", "report": report, "length": len(report)}


def _handle_osint_research(input_data: dict) -> dict:
    """
    Basic OSINT stub. Override in agents/gamma/skill_handlers.py for full implementation.
    Input: {"target": "example.com", "org_name": "Example Corp"}
    """
    target = input_data.get("target", "")
    return {
        "status": "ok",
        "target": target,
        "message": "Override this handler in your agent's skill_handlers.py",
    }


def _handle_web_fetch(input_data: dict) -> dict:
    """
    Fetch a URL and return response body.
    Input: {"url": "https://...", "timeout": 10}
    """
    url = input_data.get("url", "")
    if not url:
        return {"status": "error", "message": "url required"}
    try:
        resp = _requests.get(
            url,
            timeout=input_data.get("timeout", 10),
            headers={"User-Agent": "ai-hacklab-starter/1.0"},
        )
        body = resp.text
        return {
            "status": "ok",
            "url": url,
            "status_code": resp.status_code,
            "body": body[:10000],
            "truncated": len(body) > 10000,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _handle_ct_enum(input_data: dict) -> dict:
    """
    Certificate Transparency subdomain enumeration via crt.sh.
    Input: {"domain": "example.com"}
    """
    domain = input_data.get("domain", "")
    if not domain:
        return {"status": "error", "message": "domain required"}
    try:
        resp = _requests.get(
            f"https://crt.sh/?q=%.{domain}&output=json",
            timeout=15,
            headers={"User-Agent": "ai-hacklab-starter/1.0"},
        )
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
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# Skill handler registry
# ---------------------------------------------------------------------------
SKILL_HANDLERS: dict[str, Any] = {
    "task_status":       _handle_task_status,
    "memory_write":      _handle_memory_write,
    "memory_read":       _handle_memory_read,
    "report_generation": _handle_report_generation,
    "osint_research":    _handle_osint_research,
    "web_fetch":         _handle_web_fetch,
    "ct_enum":           _handle_ct_enum,
}

# Attempt to load agent-specific skill handlers
# Looks for agents/<AGENT_NAME>/skill_handlers.py and merges its *_SKILL_HANDLERS dict
def _load_agent_skill_handlers() -> None:
    module_path = f"agents.{AGENT_NAME}.skill_handlers"
    try:
        mod = importlib.import_module(module_path)
        # Find the handler dict: <AGENT_NAME_UPPER>_SKILL_HANDLERS
        handler_dict_name = f"{AGENT_NAME.upper()}_SKILL_HANDLERS"
        handlers = getattr(mod, handler_dict_name, None)
        if isinstance(handlers, dict):
            SKILL_HANDLERS.update(handlers)
            logger.info(
                "Loaded %d skill handlers from %s", len(handlers), module_path
            )
        else:
            logger.debug("No %s dict found in %s", handler_dict_name, module_path)
    except ModuleNotFoundError:
        logger.debug("No agent-specific handlers at %s", module_path)
    except Exception as exc:
        logger.warning("Failed to load agent handlers from %s: %s", module_path, exc)

_load_agent_skill_handlers()

# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------
def _error_response(id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": err}


def _success_response(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title=f"A2A Server — {AGENT_NAME}", version="1.0.0")


@app.get("/health")
async def health():
    return {"status": "ok", "agent": AGENT_NAME}


@app.get("/.well-known/agent-card.json")
async def agent_card():
    # Try to load agent-specific card
    try:
        mod = importlib.import_module(f"agents.{AGENT_NAME}.agent_card")
        card_fn = getattr(mod, "build_agent_card", None)
        if card_fn:
            return card_fn()
    except Exception:
        pass
    # Default minimal card
    return {
        "name": AGENT_NAME,
        "url": AGENT_URL,
        "skills": list(SKILL_HANDLERS.keys()),
        "capabilities": {"streaming": False, "push_notifications": False},
        "protocol_version": "0.3.0",
    }


@app.post("/a2a")
async def a2a_endpoint(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_totp: Optional[str] = Header(None, alias="X-TOTP"),
):
    t0 = time.monotonic()
    body = await request.json()
    rpc_id = body.get("id")

    # --- Auth ---
    try:
        caller = _verify_token(authorization)
        _verify_totp(x_totp)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_response(rpc_id, -32001, exc.detail),
        )

    # --- JSON-RPC validation ---
    if body.get("jsonrpc") != "2.0":
        return JSONResponse(
            content=_error_response(rpc_id, -32600, "Invalid JSON-RPC version")
        )

    method = body.get("method", "")
    if method != "tasks/send":
        return JSONResponse(
            content=_error_response(rpc_id, -32601, f"Method not found: {method}")
        )

    params = body.get("params", {})
    skill_id   = params.get("skill_id", "")
    input_data = params.get("input", {})

    if not skill_id:
        return JSONResponse(
            content=_error_response(rpc_id, -32602, "skill_id required in params")
        )

    handler = SKILL_HANDLERS.get(skill_id)
    if handler is None:
        return JSONResponse(
            content=_error_response(
                rpc_id, -32601,
                f"Unknown skill: {skill_id}",
                {"available": list(SKILL_HANDLERS.keys())},
            )
        )

    # --- Execute ---
    try:
        result = handler(input_data)
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "skill=%s caller=%s latency=%dms success=True",
            skill_id, caller, latency_ms,
        )
        _log_call(skill_id, caller, True, latency_ms)
        return JSONResponse(content=_success_response(rpc_id, {"output": result}))
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.exception("skill=%s raised: %s", skill_id, exc)
        _log_call(skill_id, caller, False, latency_ms)
        return JSONResponse(
            content=_error_response(rpc_id, -32603, "Internal error", str(exc))
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting A2A server: agent=%s port=%d totp=%s",
                AGENT_NAME, A2A_PORT, "enabled" if TOTP_SEED else "disabled")
    uvicorn.run(app, host=A2A_HOST, port=A2A_PORT, log_level="info")
