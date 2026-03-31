# Queue System

The mesh queue provides Redis-backed async messaging between agents. Rather than every agent knowing how to call every other agent directly, tasks are sent to inboxes and workers process them asynchronously.

## How It Works

```
  Alpha                    Redis                    Gamma
    │                        │                        │
    │── send("gamma", ──────►│                        │
    │    "ct_enum",          │── lpush ──────────────►│
    │    {"domain":...}) ──► │  mesh:inbox:gamma      │
    │                        │                        │
    │                        │◄── brpop ──────────────│
    │                        │    (worker.py)         │
    │                        │                        │
    │                        │◄── reply ──────────────│
    │◄── mesh:inbox:alpha ───│                        │
```

Each agent has one Redis key: `mesh:inbox:<agent_name>`. Messages are JSON envelopes pushed/popped via LPUSH/BRPOP.

## Sending a Task

```python
from queue.mesh_queue import MeshQueue

q = MeshQueue("alpha")

# Send ct_enum to gamma, reply will come back to alpha's inbox
msg_id = q.send("gamma", "ct_enum", {"domain": "example.com"})

# Wait for the reply
reply = q.receive(timeout=30)
if reply and reply.get("reply_to_id") == msg_id:
    print(reply["result"])
```

## Running a Worker

```bash
# Auto-loads agents/gamma/skill_handlers.py
python3 queue/worker.py --agent gamma

# Explicit module
python3 queue/worker.py --agent gamma --handlers-module agents.gamma.skill_handlers

# Verbose
python3 queue/worker.py --agent alpha --verbose
```

## Message Envelope

```json
{
  "id": "uuid4",
  "from": "alpha",
  "to": "gamma",
  "skill_id": "ct_enum",
  "input": {"domain": "example.com"},
  "reply_to": "mesh:inbox:alpha",
  "created_at": "2026-01-01T00:00:00Z",
  "attempts": 0
}
```

## Dead Letters

Messages that fail `MAX_RETRIES` (3) times are moved to `mesh:dead_letter:<agent_name>`. Check dead letters:

```bash
redis-cli LRANGE mesh:dead_letter:gamma 0 -1 | python3 -m json.tool
```

## Queue Depth

```python
q = MeshQueue("alpha")
print(q.queue_depth("gamma"))   # how many tasks waiting for gamma
print(q.dead_letter_depth())    # dead letters for current agent
```
