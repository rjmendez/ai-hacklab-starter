# Security

## Authentication Model

Every agent-to-agent call requires two independent factors:

### 1. Bearer Token
- Each agent has a unique `AGENT_TOKEN` — a random 64-character hex string
- Stored in `.env`, never committed to git
- Passed as `Authorization: Bearer <token>` on every request
- Peers that are allowed to call an agent are registered in `KNOWN_PEERS` in `a2a/server.py`

**Generate a token:**
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 2. TOTP (Time-Based One-Time Password)
- Optional but strongly recommended
- When `AGENT_TOTP_SEED` is set, every inbound request must include `X-TOTP: <code>`
- Codes rotate every 30 seconds; server allows ±1 window
- **Fails closed** — if TOTP is enabled and the header is missing or wrong, request is rejected (HTTP 403)
- Prevents replay attacks: a stolen token is useless without the current code

**Generate a seed:**
```bash
python totp/generate_seed.py
```

**Get current code at call time:**
```python
import pyotp
code = pyotp.TOTP("YOUR_SEED").now()
```

## What NOT to Commit

These files must never be in git. They're in `.gitignore` but worth calling out explicitly:

| File | Why |
|------|-----|
| `.env` | Contains all tokens and API keys |
| `dispatch/key_pools.json` | Contains API keys (filled from example) |
| `dispatch/agent_registry.json` | Contains agent tokens |
| `*.totp_state.json` | Live TOTP counter state |
| `secrets.env` | Any secondary secrets file |

Always work from the `.example` files and populate locally.

## Token Rotation

1. Generate a new token: `python -c "import secrets; print(secrets.token_hex(32))"`
2. Update `.env` on the affected agent host
3. Update `agent_registry.json` on all agents that call the rotated agent
4. Update `KNOWN_PEERS` in `a2a/server.py` on the rotated agent
5. Restart the agent container: `docker compose restart agent-<name>`
6. Verify with a health check: `curl -H "Authorization: Bearer NEW_TOKEN" http://agent:8200/health`

For TOTP seed rotation: same process, but also update `X-TOTP` in all callers.

## Network Isolation

For production deployments, agents should only be reachable from each other — not the open internet.

**Recommended options:**

- **Tailscale** (easiest): install on each host, use Tailscale IPs in `agent_registry.json`. Free for small teams.
- **WireGuard**: self-managed VPN mesh, more control, more setup.
- **Docker network**: if all agents run on one host, use an internal Docker network — no external exposure.

Bind the A2A server to `0.0.0.0` only within the private network. Never expose port 8200 to the internet without a reverse proxy and TLS.

## Principle of Least Privilege

Each agent should only have:
- The API keys it actually uses (don't give gamma the LiteLLM key if it doesn't use it)
- Tokens only for the agents it calls (alpha calls beta+gamma — doesn't need delta's token)
- Database DSNs only if it needs DB access (gamma is stateless — no DB needed)

Configure per-agent env files instead of sharing one `.env` across all agents.

## Secrets in Logs

The A2A server logs skill calls but never logs request bodies or response content. Bearer tokens and TOTP codes are read from headers and never written to any log. Redis call logs only record: timestamp, agent, skill, caller name, latency, success/fail.

Do not log `input_data` or `result` at INFO level in your skill handlers — use DEBUG if needed for development.
