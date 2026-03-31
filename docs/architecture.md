# Architecture

This document describes how the AI HackLab mesh works: how agents communicate, how tasks get routed, and how costs are controlled.

## High-Level Overview

```
                        ┌─────────────────────────────────────────┐
                        │              Your Research Task          │
                        └──────────────────┬──────────────────────┘
                                           │
                        ┌──────────────────▼──────────────────────┐
                        │           mesh_dispatcher.py            │
                        │  • select_pool()  — budget-aware        │
                        │  • dispatch_with_failover()             │
                        │  • SpendTracker + circuit breaker       │
                        └─────┬──────────┬──────────┬────────────┘
                              │          │          │
              ┌───────────────▼──┐  ┌────▼───┐  ┌──▼────────────┐
              │  LiteLLM Proxy   │  │ Agent  │  │  OpenRouter   │
              │  (alpha_litellm) │  │  Beta  │  │ (free models) │
              │  Gemini/GPT/etc  │  │  GPU   │  │               │
              └──────────────────┘  └────────┘  └───────────────┘
                                        ▲
                              ┌─────────┴─────────┐
                              │     Redis          │
                              │  Spend tracking    │
                              │  Task queue        │
                              │  Agent memory      │
                              └────────────────────┘
```

## Core Components

### A2A Protocol (`a2a/server.py`)

Each agent runs an identical A2A JSON-RPC server. It:
- Accepts `POST /a2a` with `{"method": "tasks/send", "params": {"skill_id": "...", "input": {...}}}`
- Authenticates via Bearer token + optional TOTP 2FA
- Routes calls to registered skill handlers
- Logs call metadata to Redis (latency, cost, tokens)
- Exposes `GET /health` and `GET /.well-known/agent-card.json` unauthenticated

### TOTP 2FA

All agents share a TOTP seed (or use per-agent seeds). Every inbound request must include an `X-TOTP` header with the current 6-digit code. The server verifies with ±1 window tolerance (30s drift).

This prevents replay attacks: a stolen bearer token is useless without the current TOTP code.

### Mesh Dispatcher (`dispatch/mesh_dispatcher.py`)

Sits between your code and the agents. Handles:

1. **Task classification** — maps task type to cost tier via `task_tier_map`
2. **Pool selection** — GPU-first → free-first → weighted random
3. **Budget enforcement** — soft limit halves weight; hard limit blocks pool
4. **Failover** — tries `failover_to` pools in order on failure
5. **Spend recording** — writes to Redis after each call
6. **Circuit breaker** — 3 failures in 60s → pool disabled for 300s

### Spend Tracker (`dispatch/spend_tracker.py`)

Redis-backed per-pool accounting:
- Daily spend, token count, call count (keyed by date, auto-expire 48h)
- Rolling error window (sorted set by timestamp)
- Circuit breaker state (Redis key with TTL)
- `get_status()` returns full health snapshot for all pools

### Key Pools (`dispatch/key_pools.json`)

Defines every LLM endpoint the dispatcher can use:
- `dispatch: "http"` — OpenAI-compatible HTTP endpoint (LiteLLM, OpenRouter)
- `dispatch: "a2a"` — Remote agent via A2A JSON-RPC
- Budget limits, model lists, failover chains, routing flags

See [Model Routing](model-routing.md) for the full explanation.

## Agent Archetypes

| Agent | Role | Default Skills |
|-------|------|---------------|
| **Alpha** | Coordinator | `task_status`, `report_generation`, `memory_*` |
| **Beta**  | GPU compute  | `gpu_inference`, `model_list`, `hashcat_identify` |
| **Gamma** | Recon/OSINT  | `ct_enum`, `web_fetch`, `osint_research`, `port_scan` |
| **Delta** | Data/batch   | `db_query`, `batch_process`, `data_export` |

All agents share the same A2A server. Skills are loaded dynamically from `agents/<name>/skill_handlers.py`.

## Request Flow

1. Caller invokes `dispatch(task="code_review", prompt="...")`
2. Dispatcher maps `code_review → "cheap"` tier via `task_tier_map`
3. `select_pool()` checks GPU-first → free-first → weighted random
4. Request sent to chosen pool (HTTP or A2A)
5. On success: `record_spend()` updates Redis counters
6. On failure: `record_error()` ticks circuit breaker, tries next pool in `failover_to`
7. Result returned as `DispatchResult` dataclass

## Deployment Options

- **Single machine**: all agents in Docker containers on one host
- **Multi-host**: agents on separate machines, communicate via Tailscale / WireGuard
- **Hybrid**: alpha + redis locally; beta on GPU host; gamma as lightweight recon container

See [docker/README.md](../docker/README.md) for the full stack setup.
