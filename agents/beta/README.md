# Beta Agent — GPU Compute

The **beta** archetype wraps a GPU machine running [Ollama](https://ollama.com). It exposes local model inference over A2A so the rest of the mesh can use it without touching cloud APIs — $0 cost for compute-heavy tasks.

## Responsibilities
- Serve local LLM inference (code review, analysis, summarization)
- Run GPU-accelerated tasks (hashcat, embeddings, etc.)
- Act as the free-tier fallback when cloud API budgets are tight

## Skills
| Skill | Description |
|-------|-------------|
| `gpu_inference` | Run a prompt against a local Ollama model |
| `model_list` | Return list of pulled Ollama models |
| `hashcat_identify` | Identify hash type for offline cracking |
| `memory_write` | Store key/value to agent memory |
| `memory_read` | Retrieve from agent memory |

## Prerequisites
- GPU host with [Ollama](https://ollama.com) installed and running
- Pull models: `ollama pull llama3.1:70b`, `ollama pull deepseek-coder-v2`

## Configuration
```bash
AGENT_NAME=beta
AGENT_TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")
AGENT_TOTP_SEED=$(python totp/generate_seed.py | grep 'TOTP Seed' | awk '{print $3}')
AGENT_URL=http://YOUR_GPU_HOST:8200
OLLAMA_HOST=http://localhost:11434
```

## Routing
Set `beta_gpu` as the pool for `gpu_first_tasks` in `key_pools.json` to automatically route code and inference tasks here first.
