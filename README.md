# AI HackLab Starter

[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://python.org)

A production-ready starter kit for building multi-agent AI research labs. Provides an Agent-to-Agent (A2A) JSON-RPC mesh, cost-aware model routing, Redis-backed spend tracking, and Docker orchestration — everything you need to stand up a multi-agent lab without starting from scratch.

## Architecture

```
  ┌──────────────────────────────────────────────────────────┐
  │                     Your Research Task                    │
  └──────────────────────────┬───────────────────────────────┘
                             │
  ┌──────────────────────────▼───────────────────────────────┐
  │                  mesh_dispatcher.py                       │
  │   Pool selection · Budget limits · Circuit breaker        │
  │   GPU-first · Free-first · Weighted failover              │
  └────────┬──────────────┬──────────────┬───────────────────┘
           │              │              │
  ┌────────▼──┐   ┌───────▼──┐   ┌──────▼──────────┐
  │  Alpha 🅰️  │   │  Beta 🅱️  │   │  OpenRouter     │
  │ Coordinator│   │ GPU/Ollama│   │  (free models)  │
  └────────────┘   └──────────┘   └─────────────────┘
           │              │
  ┌────────▼──┐   ┌───────▼──┐
  │  Gamma 🔍  │   │  Delta 🔷 │
  │ OSINT/Recon│   │ Data/Batch│
  └────────────┘   └──────────┘
           │              │
  ┌────────▼──────────────▼──────┐
  │             Redis             │
  │  Spend tracking · Task queue  │
  │  Agent memory · Call logs     │
  └──────────────────────────────┘
```

Each agent runs the same `a2a/server.py` — a JSON-RPC server with Bearer + TOTP auth. The dispatcher routes inference to the best pool based on cost, availability, and task type.

## What's Included

| Component | Description |
|-----------|-------------|
| `a2a/server.py` | FastAPI JSON-RPC server — runs on every agent |
| `a2a/watchdog.py` | Process watchdog — restarts the server if it dies |
| `dispatch/mesh_dispatcher.py` | Cost-aware task router with failover |
| `dispatch/spend_tracker.py` | Redis-backed budget tracking + circuit breaker |
| `agents/alpha/` | Coordinator agent stub |
| `agents/beta/` | GPU compute agent stub (Ollama) |
| `agents/gamma/` | OSINT/recon agent with working skill handlers |
| `agents/delta/` | Batch/data ops agent stub |
| `docker/docker-compose.yml` | Full stack: agents + Redis + LiteLLM |
| `totp/generate_seed.py` | TOTP seed generator |

## Quick Start

**1. Clone and configure**
```bash
git clone https://github.com/rjmendez/ai-hacklab-starter
cd ai-hacklab-starter
cp .env.example .env
cp dispatch/key_pools.example.json dispatch/key_pools.json
cp dispatch/agent_registry.example.json dispatch/agent_registry.json
```

**2. Generate tokens**
```bash
# One token per agent
python -c "import secrets; print(secrets.token_hex(32))"

# One TOTP seed (can share across agents or use one per agent)
python totp/generate_seed.py
```
Fill these into `.env`, `key_pools.json`, and `agent_registry.json`.

**3. Configure LiteLLM**
```bash
cp docker/litellm_config.example.yaml docker/litellm_config.yaml
# Edit to add your API keys (OpenAI, Anthropic, Google, Ollama, etc.)
```

**4. Start the stack**
```bash
docker compose -f docker/docker-compose.yml up -d
```

**5. Verify**
```bash
# All agents should return {"status": "ok"}
for port in 8201 8202 8203 8204; do
  echo "Port $port:"; curl -s http://localhost:$port/health; echo
done

# Test a skill call
TOTP=$(python -c "import pyotp; print(pyotp.TOTP('YOUR_SEED').now())")
curl -X POST http://localhost:8203/a2a \
  -H "Authorization: Bearer YOUR_GAMMA_TOKEN" \
  -H "X-TOTP: $TOTP" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tasks/send","id":"1","params":{"skill_id":"ct_enum","input":{"domain":"example.com"}}}'
```

## Documentation

| Doc | Description |
|-----|-------------|
| [docs/architecture.md](docs/architecture.md) | Full architecture deep dive |
| [docs/adding-an-agent.md](docs/adding-an-agent.md) | Step-by-step: add a new agent |
| [docs/model-routing.md](docs/model-routing.md) | Dispatcher, tiers, budget limits |
| [docs/security.md](docs/security.md) | Auth, TOTP, token rotation, network isolation |
| [a2a/README.md](a2a/README.md) | A2A protocol reference |
| [docker/README.md](docker/README.md) | Docker stack setup |

## License

MIT — see [LICENSE](LICENSE).
