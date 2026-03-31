# Adding a New Agent

This guide walks through adding a new agent to the mesh from scratch.

## 1. Choose an Archetype

Pick the closest existing agent as your base:

| Archetype | Use when your agent... |
|-----------|------------------------|
| `alpha` | Coordinates tasks, manages queues, delegates |
| `beta` | Does heavy compute (GPU, cracking, local inference) |
| `gamma` | Does outward-facing recon (web, DNS, scanning) |
| `delta` | Processes data in bulk (DB queries, exports, archival) |

## 2. Create the Agent Directory

```bash
cp -r agents/gamma agents/epsilon   # or whichever archetype fits
cd agents/epsilon
```

## 3. Generate Credentials

```bash
# Unique bearer token for this agent
python -c "import secrets; print(secrets.token_hex(32))"

# TOTP seed (can share one across all agents or use per-agent)
python totp/generate_seed.py
```

## 4. Set Environment Variables

Add to your `.env`:
```
AGENT_NAME=epsilon
AGENT_TOKEN=<token from step 3>
AGENT_TOTP_SEED=<seed from step 3>
AGENT_URL=http://YOUR_HOST:8200
```

## 5. Write Your Skill Handlers

Create `agents/epsilon/skill_handlers.py`:

```python
"""Epsilon agent skill handlers."""
import logging

logger = logging.getLogger(__name__)

def handle_my_skill(input_data: dict) -> dict:
    """
    Description of what this skill does.
    Input:  {"param": "value"}
    Output: {"status": "ok", "result": ...}
    """
    param = input_data.get("param", "")
    # ... your logic here ...
    return {"status": "ok", "result": param}

EPSILON_SKILL_HANDLERS = {
    "my_skill": handle_my_skill,
}
```

## 6. Update the Agent Card

Edit `agents/epsilon/agent_card.py` to list your skills:

```python
def build_agent_card() -> dict:
    return {
        "name": "Epsilon",
        "description": "My new agent — does X, Y, Z.",
        "url": os.getenv("AGENT_URL", "http://localhost:8200"),
        "skills": [
            {"id": "my_skill", "name": "My Skill", "description": "Does the thing"},
        ],
        ...
    }
```

## 7. Wire Skills into the A2A Server

In `a2a/server.py`, add your handlers to `SKILL_HANDLERS`:

```python
# Dynamic load for epsilon (same importlib pattern as gamma)
def _epsilon_handler(skill_name: str):
    async def _wrapper(params: dict) -> dict:
        # load agents/epsilon/skill_handlers.py via importlib
        ...
    return _wrapper

SKILL_HANDLERS["my_skill"] = _epsilon_handler("my_skill")
```

Or run a dedicated server instance with `AGENT_NAME=epsilon` and add your handlers directly.

## 8. Register in agent_registry.json

```json
"epsilon": {
  "name": "Epsilon",
  "emoji": "🔺",
  "a2a_url": "http://YOUR_HOST:8200/a2a",
  "token": "YOUR_TOKEN_HERE",
  "totp_seed": "YOUR_TOTP_SEED_HERE",
  "models": {},
  "capabilities": ["my_capability"]
}
```

## 9. Add to docker-compose.yml

```yaml
agent-epsilon:
  build:
    context: ..
    dockerfile: agents/epsilon/Dockerfile
  env_file: ../.env
  environment:
    AGENT_NAME: epsilon
  ports:
    - "8205:8200"
  depends_on:
    - redis
```

## 10. Test It

```bash
# Health check
curl http://localhost:8205/health

# Agent card
curl http://localhost:8205/.well-known/agent-card.json

# Call a skill (replace TOKEN and TOTP)
curl -X POST http://localhost:8205/a2a \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "X-TOTP: $(python -c "import pyotp; print(pyotp.TOTP('YOUR_SEED').now())")" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tasks/send","params":{"skill_id":"my_skill","input":{"param":"hello"}},"id":1}'
```
