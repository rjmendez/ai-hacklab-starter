# mesh-harness — Rust shared library for the agent mesh

Vendored from [`instructkr/claw-code`](https://github.com/instructkr/claw-code) (`dev/rust` branch, MIT license).
Extended with mesh-specific integrations.

## Crates

| Crate | Description |
|---|---|
| `api` | Anthropic API client — SSE streaming, retry/backoff (vendored) |
| `runtime` | Agent turn loop, session format, permissions (vendored + trimmed) |
| `a2a-client` | A2A JSON-RPC client — token+TOTP auth, `A2AToolExecutor` |
| `mesh-queue` | Async Redis queue — drop-in replacement for `queue/worker.py` |
| `scanner` | High-throughput pipeline scanner binary — parallel async workers |
| `mesh-agent` | Standalone agent binary — `ConversationRuntime` + A2A dispatch |

## Build

```bash
cd rust/
cargo build --workspace          # debug
cargo build --workspace --release
cargo test --workspace
```

## Where each crate fits

### `api` + `runtime` (vendored, read-only)
Core agent harness from claw-code. Do not modify — pull upstream updates manually.

### `a2a-client`
The bridge crate. `A2aToolExecutor` implements `runtime::ToolExecutor` by routing
tool calls to mesh agents via A2A JSON-RPC (bearer token + TOTP). Dispatch rules
mirror the mesh ownership policy (Charlie=pipeline, Oxalis=GPU, MrPink=OSINT, Rex=DB).

**Using the CLI:**
```bash
mesh-harness a2a-call --agent charlie --skill findings_query '{"query": "severity=critical"}'
```

### `mesh-queue`
Async Rust drop-in for `queue/mesh_queue.py` + `queue/worker.py`. Wire skill handlers
as async closures:

```rust
let worker = Worker::new(WorkerConfig::from_env("gamma"))
    .register("ct_enum", |input| async move {
        // enumerate certs for input["domain"]
        Ok(json!({"subdomains": ["a.example.com"]}))
    });
worker.run().await?;
```

### `scanner`
Replace Python `cli.py` batch workers with this for the Firebase pipeline.
Pulls from a Redis queue, fetches + SHA256-hashes each URL, pushes results back.

```bash
SCANNER_QUEUE_KEY=scanner:queue:firebase \
SCANNER_WORKERS=32 \
scanner
```

### `mesh-agent`
Standalone binary for agents that need multi-turn LLM reasoning.
Python servers call this via subprocess for complex analysis tasks.

```bash
echo "Analyze this contract for reentrancy: ..." | \
  mesh-agent --agent iris --registry /path/to/agent_registry.json
```

## Rust opportunities in the Python codebase

| Python file | Rust replacement | Value |
|---|---|---|
| `queue/worker.py` + `queue/mesh_queue.py` | `mesh-queue` crate | Lower memory, zero-copy JSON, proper async |
| `scouts/distributed_lock.py` | inline in `mesh-queue` via `redis::Script` | Atomic Lua already used |
| `scouts/rate_limiter.py` | `tower::RateLimit` or custom Redis INCR | Composable with async tower middleware |
| `scouts/dedup.py` | `RedisDedup` in `mesh-queue` crate | Batched pipeline SMISMEMBER |
| `pipeline/telemetry.py` | `tracing` + `tracing-subscriber` + Postgres appender | Structured, zero-overhead |
| `dispatch/spend_tracker.py` | Prometheus counter in `mesh-agent` | Grafana-native, no DB needed |
| `queue/worker.py` scan loops | `scanner` binary | Tokio concurrency >> ProcessPoolExecutor |
