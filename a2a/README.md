# Agent-to-Agent (A2A) Protocol

The A2A protocol is how agents in the mesh talk to each other. Every agent runs `a2a/server.py`, which exposes a JSON-RPC 2.0 endpoint at `POST /a2a`.

## Quick Reference

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /health` | None | Liveness check |
| `GET /.well-known/agent-card.json` | None | Agent capabilities |
| `POST /a2a` | Bearer + TOTP | Execute a skill |

## Calling an Agent

```bash
# Generate TOTP code
TOTP=$(python -c "import pyotp; print(pyotp.TOTP('YOUR_SEED').now())")

# Call the ct_enum skill on the gamma agent
curl -X POST http://gamma-agent:8200/a2a \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "X-TOTP: $TOTP" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tasks/send",
    "id": "1",
    "params": {
      "skill_id": "ct_enum",
      "input": {"domain": "example.com"}
    }
  }'
```

## JSON-RPC Envelope

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "tasks/send",
  "id": "any-string-or-int",
  "params": {
    "skill_id": "skill_name",
    "input": { ... skill-specific input ... }
  }
}
```

**Success response:**
```json
{
  "jsonrpc": "2.0",
  "id": "any-string-or-int",
  "result": {
    "output": { ... skill-specific output ... }
  }
}
```

**Error response:**
```json
{
  "jsonrpc": "2.0",
  "id": "any-string-or-int",
  "error": {
    "code": -32601,
    "message": "Unknown skill: bad_skill",
    "data": {"available": ["task_status", "ct_enum", ...]}
  }
}
```

## Authentication

### 1. Bearer Token
Every request must include `Authorization: Bearer <AGENT_TOKEN>`. Tokens are generated per-agent and stored in `.env`. See `docs/security.md` for rotation procedure.

### 2. TOTP (optional, recommended)
When `AGENT_TOTP_SEED` is set, each request must also include `X-TOTP: <6-digit-code>`. Codes are time-based (30s window, ±1 window tolerance). Generate a seed with:

```bash
python totp/generate_seed.py
```

If TOTP fails, the server returns HTTP 403 and **does not** fall back to token-only auth. It fails closed.

## Agent Card

`GET /.well-known/agent-card.json` returns the agent's self-description:

```json
{
  "name": "gamma",
  "url": "http://gamma-agent:8200",
  "skills": [
    {"id": "ct_enum", "name": "CT Enumeration", "description": "..."},
    ...
  ],
  "capabilities": {"streaming": false, "push_notifications": false},
  "protocol_version": "0.3.0"
}
```

The dispatcher reads agent cards to discover what skills are available. See [Architecture](../docs/architecture.md) for the full flow.

## Adding Skills

See [Adding an Agent](../docs/adding-an-agent.md#5-write-your-skill-handlers) for a step-by-step guide.
