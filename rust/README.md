# mesh-harness — Rust shared library for the agent mesh

Vendored from [`instructkr/claw-code`](https://github.com/instructkr/claw-code) (`dev/rust` branch, MIT license).
Extended with mesh-specific integrations.

> **Architecture decision (2026-03-31):** LiteLLM dropped. `crates/router` is the
> canonical LLM provider abstraction. `mesh-agent` binary is the inference entrypoint
> for all agents. Direct provider APIs only.

---

## Crates

| Crate | Description |
|---|---|
| `api` | Anthropic API client — SSE streaming, retry/backoff (vendored) |
| `runtime` | Agent turn loop, session format, permissions (vendored + trimmed) |
| `a2a-client` | A2A JSON-RPC client — token+TOTP auth, `A2AToolExecutor` |
| `mesh-queue` | Async Redis queue — drop-in for `queue/worker.py`; includes `RedisDedup`, `RateLimit`, `CircuitBreaker`, `Checkpoint` |
| `router` | Multi-provider LLM router — replaces LiteLLM + mesh_dispatcher + model_selector |
| `scanner` | High-throughput pipeline scanner binary — parallel async workers |
| `mesh-agent` | Standalone agent binary — `ConversationRuntime` + `router` + A2A dispatch |

---

## Build

```bash
cd rust/
cargo build --workspace
cargo build --workspace --release
cargo test --workspace
```

---

## Crate Details

### `api` + `runtime` (vendored, read-only)
Core agent harness from claw-code. Do not modify — pull upstream updates manually.

### `a2a-client`
The bridge crate. `A2aToolExecutor` implements `runtime::ToolExecutor` by routing
tool calls to mesh agents via A2A JSON-RPC (bearer token + TOTP). Dispatch rules
mirror the mesh ownership policy (Charlie=pipeline, Oxalis=GPU, MrPink=OSINT, Rex=DB).

### `mesh-queue`
Async Rust drop-in for `queue/mesh_queue.py` + `queue/worker.py`. Includes:
- `MeshQueue` — LPUSH/BRPOP send/receive/reply/dead-letter
- `Worker` — async dispatch loop, retry, SIGTERM shutdown
- `RedisDedup` — `SMISMEMBER` batched dedup (fixes Python race + halves round trips)
- `RateLimit` — Lua atomic rate limiter (fixes over-decrement race in `scouts/rate_limiter.py`)
- `CircuitBreaker` — Lua atomic circuit breaker (replaces `spend_tracker.py` circuit logic)
- `Checkpoint` — scan progress save/resume (replaces `scouts/checkpoint.py`)
- `DistributedLock` — Lua token-release lock (replaces `scouts/distributed_lock.py`)

### `router` ← **replaces LiteLLM + mesh_dispatcher + model_selector**
Multi-provider LLM router. Direct API calls, no proxy.

```
router/
  provider.rs    — Provider trait: async fn complete(req) -> Result<Response>
  anthropic.rs   — AnthropicProvider (wraps crates/api AnthropicClient)
  openai.rs      — OpenAiProvider (Gemini, GPT-4o, GPT-4.1 — OpenAI-compat API)
  pool.rs        — ApiKeyPool: round-robin across keys (atomic Redis INCR)
  selector.rs    — tier → model → provider (nano/mini/mid/strong/premium)
  circuit.rs     — Lua circuit breaker per pool (3 errors/60s → open 300s)
  spend.rs       — Redis spend tracker (compatible with dispatch:spend:* schema)
  cost.rs        — per-model $/token table for cost estimation
  telemetry.rs   — tracing spans + Prometheus metrics (replaces pipeline/telemetry.py)
```

**Call path:**
```
Python agent → mesh-agent binary → router::select() → AnthropicProvider / OpenAiProvider
```

One fewer network hop vs LiteLLM. Circuit breaking and spend tracking in-process.

### `scanner`
Replaces Python `cli.py` Firebase batch workers.
Pulls from Redis queue, fetches + SHA256-hashes each URL, configurable concurrency.

```bash
SCANNER_QUEUE_KEY=scanner:queue:firebase SCANNER_WORKERS=32 scanner
```

### `mesh-agent`
Standalone binary for multi-turn LLM reasoning. Python servers call as subprocess.

```bash
echo "Analyze this contract for reentrancy..." | \
  mesh-agent --agent iris --model claude-sonnet-4-5 --tier strong
```

---

## Cost Tracking (no LiteLLM)

LiteLLM's spend tracking is replaced with three complementary layers:

### 1. In-process: `router::spend` (Redis)
Redis schema (compatible with existing `spend_tracker.py`):
```
dispatch:spend:{pool_id}:{YYYY-MM-DD}   float  — USD spent today
dispatch:tokens:{pool_id}:{YYYY-MM-DD}  int    — tokens today
dispatch:calls:{pool_id}:{YYYY-MM-DD}   int    — calls today
```
Every `mesh-agent` invocation writes to Redis after each turn. No separate process needed.

### 2. Metrics: Prometheus + Grafana (already running)
`router::telemetry` exposes:
- `mesh_llm_tokens_total{model, agent, direction}` — input/output tokens
- `mesh_llm_cost_usd_total{model, agent}` — estimated cost (per-model $/token table)
- `mesh_llm_latency_seconds{model, agent}` — p50/p95/p99
- `mesh_llm_errors_total{model, agent, error_type}` — error rate

Grafana dashboard at `https://openclaw.tail9c4667.ts.net:3000` already has the
Prometheus datasource configured (MrPink service account token in TOOLS.md).

### 3. Persistent: mrpink-memory PostgreSQL
For long-term cost analysis and audit trail. `router::spend` optionally writes to:
```sql
INSERT INTO memories (agent, category, content, metadata)
VALUES ($agent, 'llm_spend', $summary, $json)
```
One row per session, not per call — low write volume.

---

## Python files → Rust replacements

| Python file | Rust replacement | Status |
|---|---|---|
| `queue/worker.py` + `queue/mesh_queue.py` | `mesh-queue` crate | ✅ Built |
| `scouts/distributed_lock.py` | `mesh-queue::DistributedLock` | 🔲 TODO |
| `scouts/rate_limiter.py` | `mesh-queue::RateLimit` | 🔲 TODO |
| `scouts/dedup.py` | `mesh-queue::RedisDedup` | 🔲 TODO |
| `scouts/checkpoint.py` | `mesh-queue::Checkpoint` | 🔲 TODO |
| `pipeline/telemetry.py` | `router::telemetry` (tracing + Prometheus) | 🔲 TODO |
| `dispatch/spend_tracker.py` | `router::spend` + Prometheus | 🔲 TODO |
| `dispatch/mesh_dispatcher.py` | `router` crate + `mesh-agent` binary | 🔲 TODO |
| `tools/model_router/model_selector.py` | `router::selector` | 🔲 TODO |
| `tools/model_router/task_classifier.py` | fast-path regex kept in Python; slow-path → `mesh-agent` | 🔲 TODO |
| Firebase `cli.py` batch workers | `scanner` binary | ✅ Built |
