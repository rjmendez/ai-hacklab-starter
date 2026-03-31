# Docker Stack

The `docker-compose.yml` brings up the full agent mesh locally: all four agents, Redis, and a LiteLLM proxy.

## Prerequisites
- Docker + Docker Compose v2
- A filled-in `.env` file (copy `.env.example` and set your values)
- A `dispatch/key_pools.json` (copy `dispatch/key_pools.example.json` and fill in)

## Setup

```bash
# 1. Configure environment
cp .env.example .env
cp dispatch/key_pools.example.json dispatch/key_pools.json
cp dispatch/agent_registry.example.json dispatch/agent_registry.json
# Edit all three files — fill in tokens, API keys, IPs

# 2. Configure LiteLLM
cp docker/litellm_config.example.yaml docker/litellm_config.yaml
# Edit to add your API keys and models

# 3. Generate TOTP seeds (one per agent, or share one across all)
python totp/generate_seed.py

# 4. Bring up the stack
docker compose -f docker/docker-compose.yml up -d

# 5. Verify all agents are healthy
for port in 8201 8202 8203 8204; do
  curl -s http://localhost:$port/health | python3 -m json.tool
done
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| `agent-alpha` | 8201 | Coordinator agent |
| `agent-beta` | 8202 | GPU compute agent |
| `agent-gamma` | 8203 | OSINT/recon agent |
| `agent-delta` | 8204 | Batch/data ops agent |
| `redis` | 6379 | Shared Redis (spend tracking, queue) |
| `litellm-proxy` | 4000 | LiteLLM proxy (OpenAI-compatible API) |

## Logs

```bash
docker compose -f docker/docker-compose.yml logs -f agent-alpha
docker compose -f docker/docker-compose.yml logs -f litellm-proxy
```

## Scaling

To run multiple instances of an agent (e.g. more recon workers):
```bash
docker compose -f docker/docker-compose.yml up -d --scale agent-gamma=3
```
