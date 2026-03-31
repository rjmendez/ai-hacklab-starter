# Alpha Agent — Coordinator

The **alpha** archetype is the coordinator: it manages task queues, delegates to specialized agents, and aggregates results.

## Responsibilities
- Receive incoming research tasks and break them into subtasks
- Dispatch subtasks to beta (GPU), gamma (recon), delta (data) via A2A
- Track task state and expose status via `task_status` skill
- Generate final reports from aggregated agent outputs

## Skills
| Skill | Description |
|-------|-------------|
| `task_status` | Check pipeline and queued task health |
| `report_generation` | Compile agent outputs into a final report |
| `memory_write` | Store key/value pairs to agent memory |
| `memory_read` | Retrieve from agent memory |

## Configuration
```bash
AGENT_NAME=alpha
AGENT_TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")
AGENT_TOTP_SEED=$(python totp/generate_seed.py | grep 'TOTP Seed' | awk '{print $3}')
AGENT_URL=http://YOUR_HOST:8200
```

## Extending
Add new skills to `a2a/server.py` under `SKILL_HANDLERS`. See [Adding an Agent](../../docs/adding-an-agent.md) for the full guide.
