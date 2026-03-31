# Model Routing

The dispatcher (`dispatch/mesh_dispatcher.py`) routes LLM inference tasks to the best available pool based on task type, cost tier, budget state, and circuit breaker health.

## Key Concepts

### Cost Tiers

Every model is assigned a tier:

| Tier | Examples | Use for |
|------|----------|---------|
| `free` | OpenRouter `:free` models, Ollama | Bulk classification, dedup, scoring |
| `nano` | `gpt-4.1-nano`, `llama-3.1-8b` | Simple extraction, short summaries |
| `cheap` | `gemini-2.5-flash`, `gpt-4.1-mini` | Most tasks — default tier |
| `mid` | `gpt-4o` | Complex reasoning when cheap fails |
| `premium` | `claude-sonnet-4-5`, `claude-opus-4-5` | Critical work only |

**Key policy: never auto-upgrade to premium on timeout — downgrade instead.**

### task_tier_map

`key_pools.json` maps task types to default tiers:

```json
"task_tier_map": {
  "classification": "free",
  "summarization": "cheap",
  "code_generation": "cheap",
  "architecture_review": "premium"
}
```

Call `dispatch(task="classification", prompt="...")` and the router picks the right tier automatically.

### Key Pools

Each pool in `key_pools.json` represents one LLM endpoint:

```json
{
  "id": "alpha_litellm",
  "base_url": "http://litellm-proxy:4000",
  "api_key_env": "LITELLM_API_KEY",
  "weight": 40,
  "daily_budget_usd": 30,
  "soft_limit_usd": 25,
  "hard_limit_usd": 35,
  "failover_to": ["alpha_openrouter", "beta_gpu"],
  "dispatch": "http"
}
```

**Fields:**
- `weight` — relative probability of selection in weighted random choice
- `daily_budget_usd` — informational budget target
- `soft_limit_usd` — when exceeded, pool weight is halved
- `hard_limit_usd` — when exceeded, pool is skipped entirely until next day
- `failover_to` — ordered list of pool IDs to try if this pool fails
- `dispatch` — `"http"` (OpenAI-compatible API) or `"a2a"` (remote agent)

### Pool Selection Strategy

`select_pool(task)` picks a pool in this order:

1. **GPU-first**: if task is in `gpu_first_tasks` → try `beta_gpu` first ($0 cost)
2. **Free-first**: if task is in `free_first_tasks` → try OpenRouter with `free_models_first`
3. **Weighted random**: among healthy pools, weighted by `weight` field
   - Pools over soft limit have their weight halved
   - Pools over hard limit are excluded
   - Circuit-broken pools are excluded

### Failover Chain

If the selected pool fails:
1. `record_error(pool_id)` increments the error counter
2. The dispatcher tries each pool in `failover_to` in order
3. If all pools fail, `DispatchError` is raised

### Circuit Breaker

3 errors within 60 seconds → circuit opens for 300 seconds. Pool is skipped until circuit resets.

Manual reset: `python dispatch/spend_tracker.py --reset-circuit alpha_litellm`

### Timeout Policy

| Tier | Timeout | On timeout |
|------|---------|------------|
| `free` | 45s | Downgrade to cheap |
| `nano` | 30s | Downgrade to cheap |
| `cheap` | 30s | Downgrade to mid |
| `mid` | 60s | Log + retry once |
| `premium` | 90s | Alert |

## CLI Usage

```bash
# Dispatch a task
python dispatch/mesh_dispatcher.py --task code_review --prompt "review this function..."

# Force a specific tier
python dispatch/mesh_dispatcher.py --task summarization --prompt "..." --tier free

# Check agent health + recent dispatch log
python dispatch/mesh_dispatcher.py --status

# Check pool budgets and circuit state
python dispatch/mesh_dispatcher.py --spend-status

# Test all pools with the same prompt
python dispatch/mesh_dispatcher.py --spread-test

# Reset a tripped circuit breaker
python dispatch/spend_tracker.py --reset-circuit alpha_litellm
```
