# Agent Factory

Blueprints for spawning new specialized worker agents into the mesh.

Each blueprint is a standalone Python worker that:
1. Reads tasks from `mesh:inbox:<agent-name>` (Redis BRPOP)
2. Routes to the appropriate handler
3. Writes results back to the caller's reply key

## Available Blueprints

### `gpu-worker/`
GPU compute tasks: hashcat cracking and local Ollama inference.

```bash
# Add to docker-compose.yml (copy agent-delta service as a template)
# Set: AGENT_NAME=gpu-worker, queue key auto-follows
python3 agent-factory/blueprints/gpu-worker/worker.py

# Send a task
python3 -c "
import json, redis
r = redis.Redis(host='localhost', password='...')
r.lpush('mesh:inbox:gpu-worker', json.dumps({
    'id': 'test-1',
    'skill_id': 'inference',
    'input': {'model': 'llama3.1:70b', 'prompt': 'What is 2+2?'},
    'reply_key': 'mesh:inbox:alpha',
}))
"
```

**Skills:** `hashcat` · `inference`

### `analysis-worker/`
Static binary analysis: radare2 inspection, string extraction, entropy checks.

```bash
python3 agent-factory/blueprints/analysis-worker/worker.py
```

**Skills:** `r2_info` · `strings_extract` · `entropy_check`

## Creating a New Blueprint

1. Copy an existing blueprint directory
2. Edit `worker.py` — implement `handle_*` functions for your task types
3. Register handlers in `TASK_HANDLERS` dict
4. Add to `docker-compose.yml` with a unique `AGENT_NAME`
5. Add agent token to `.env` and `dispatch/agent_registry.json`
6. Update `scouts/PLAYBOOK.md` — Agent Capabilities Reference section
7. Update `agents/README.md` — Skills Matrix

### Blueprint requirements

Every blueprint must:
- Read from `mesh:inbox:<AGENT_NAME>` via BRPOP
- Handle `KeyboardInterrupt` gracefully (SIGTERM → clean shutdown)
- Write results to `reply_key` when provided
- Return a dict with a `status` key from every handler
- Never fail silently — log errors, never suppress exceptions completely

### Message envelope

```json
{
  "id":        "uuid-of-task",
  "skill_id":  "handler-name",
  "input":     {"field": "value"},
  "reply_key": "mesh:inbox:alpha",
  "from":      "alpha"
}
```

Result written to reply_key:
```json
{
  "id":         "uuid-of-task",
  "result":     {"status": "ok", ...},
  "from":       "gpu-worker",
  "latency_ms": 1234
}
```
