# Model Router

Auto-selects the cheapest model that can handle a task, with fallback chains and optional LLM-based task classification.

## Tiers

| Tier | Models | Use For |
|------|--------|---------|
| `nano` | gemini-2.0-flash, gpt-4.1-nano | Status checks, trivial ops |
| `mini` | gpt-4.1-nano, gpt-4.1-mini | Boilerplate, simple scripts |
| `mid` *(default)* | gpt-4.1-mini, gemini-2.5-flash | Most tasks: debug, refactor, API |
| `strong` | claude-sonnet-4-5, gemini-2.5-flash | Security analysis, complex impl |
| `premium` | claude-opus-4-5, claude-sonnet-4-5 | Critical decisions only |

**Policy:** never auto-escalate to premium. Downgrade on timeout instead.

## Quick Usage

```python
from tools.model_router.model_selector import select_model
from tools.model_router.task_classifier import classify

# Auto-classify then route
task = "Refactor this function to use async/await"
tier = classify(task)                          # → "mid"
model, response, usage = select_model(
    tier=tier,
    messages=[{"role": "user", "content": task}]
)
print(f"Used: {model} ({usage.get('total_tokens')} tokens)")
print(response)
```

## Task Classifier

```python
from tools.model_router.task_classifier import classify, classify_fast

# Fast heuristic only (no LLM cost)
tier = classify_fast("check disk space")  # → "nano"
tier = classify_fast("design microservices architecture")  # → "strong"

# Full: fast first, LLM fallback for ambiguous tasks
tier = classify("implement oauth2 with refresh tokens")  # → "strong" or "mid"
```

## Benchmark

Test model availability and latency:

```bash
# Benchmark the mid tier
python tools/model_router/benchmark.py --tier mid

# Benchmark all tiers, save results
python tools/model_router/benchmark.py --all --output benchmarks/results.json

# Custom prompt
python tools/model_router/benchmark.py --tier strong --prompt "Explain TLS handshake"
```

## Configuration

```bash
LITELLM_BASE_URL=http://litellm-proxy:4000   # LiteLLM proxy
LITELLM_API_KEY=your-master-key              # LiteLLM API key
```

## Integration with Dispatcher

The dispatch layer (`dispatch/mesh_dispatcher.py`) already uses equivalent tier/pool logic. Use the model router directly when you need fine-grained control from within an agent's skill handler.
