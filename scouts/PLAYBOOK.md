# Scout Playbook — Shared Knowledge Base

> **All scouts read this at startup.**
> This is the mesh's shared operational knowledge. Update it when you learn something new.
> When in doubt: ask your operator. Silence under uncertainty is not a feature.

---

## Model Routing Rules

### ❌ NEVER use for subagent file-writing tasks

| Model | Reason |
|-------|--------|
| `gpt-4.1` | Instant-fail in subagent mode — exits <1s, empty output |
| `gpt-4.1-mini` | Same instant-fail bug |
| `claude-opus-4-5` | Same instant-fail bug in subagent mode |

### ✅ ALWAYS use for subagent file-writing

```
claude-sonnet-4-6   ← canonical subagent workhorse, validated
```

### Model tiers by use case

```
Cron / heartbeat / simple checks:
  gpt-4.1-nano          ← cheap, fast, reliable for structured output
  gemini-2.5-flash      ← good alternative for high-frequency jobs

Complex analysis / code / writing:
  claude-sonnet-4-6     ← default workhorse

Critical / premium tasks:
  claude-opus-4-6       ← expensive, use sparingly
```

### ⚠️ Model config rules

- **Always set model explicitly.** Never rely on proxy default — it falls through to the most expensive model.
- **Validate model alias before deploying to cron.** A bad alias silently routes to default.
  If a model alias is unrecognized, LiteLLM does NOT error — it falls to default. Test with a single manual call first.
- **LiteLLM table names are case-sensitive.** Use quoted names in SQL: `"LiteLLM_SpendLogs"` not `LiteLLM_SpendLogs`.

---

## Cron Rules

```
✅ DO:
- Set model explicitly in every cron config
- Set delivery: announce + bestEffort for failure visibility
- Set a reasonable timeout (300s for simple, 600s for DB/file ops)
- Scope each run to a single operation (read OR write OR analyze — never chain)
- Validate model aliases manually before first deploy

❌ DON'T:
- Use delivery: none — silent failures are invisible
- Chain multiple heavy operations in one cron run (read + write + analyze = timeout)
- Use isolated subagent crons for anything that needs outbound A2A calls
- Deploy a cron calling an LLM without a spend limit or frequency check
```

### Isolated cron subagents cannot make A2A calls

This is a hard constraint of the current architecture. If a cron task requires
talking to another agent, it must run in a session-bound context, not an isolated subagent.

---

## Mesh Communication Policy

```
RULE 1: A2A first, always.
  All operational agent-to-agent communication goes through A2A.
  No exceptions. If A2A is down, escalate to your operator.

RULE 2: If A2A fails or a skill is a stub, escalate.
  Do NOT route around failures. Do NOT assume silence = success.
  Surface the failure to your operator explicitly.

RULE 3: Outbound sysadmin skills are manual review queues.
  Nothing auto-executes. All tasks sent to sysadmin-type skills require human action.
  Sending a task does NOT mean it will run.
  If you need something to actually happen, flag it to your operator directly.
```

---

## Agent Capabilities Reference

### Alpha — Coordinator (port 8201)
- Role: Task routing, Docker ops, queue management
- Skills: task_status, docker_status, docker_restart, docker_logs, report_generation, queue_status

### Beta — GPU / Compute (port 8202)
- Role: Local inference, hash identification
- Skills: gpu_inference, model_list, hashcat_identify

### Gamma — Recon / OSINT (port 8203)
- Role: External intelligence gathering
- Skills: osint_research, ct_enum, web_fetch, port_scan, wifi_scan, dns_lookup, whois

### Delta — Data Ops (port 8204)
- Role: Batch processing, database, export
- Skills: db_query, batch_process, data_export, redis_sync

---

## Scout Registry

### Ratchet 🔧 — Meta-Scout (Continuous Improvement)
- Purpose: Reads failure signals, patches PLAYBOOK, append-only improvement reports
- File: `scouts/ratchet/ratchet_seed.py`
- Rule: Append-only. Never deletes content. Patches are dated and reversible.
- Run: `python3 scouts/ratchet/ratchet_seed.py --report`

### Q 🔬 — Research Specialist
- Purpose: Cross-links findings across data sources, tests hypotheses, proposes new tools
- File: `scouts/q/q_seed.py`
- Rule: Read-only from DB. Writes only to `workspace/planning/` and Redis `q:*` keys.
- Run: `python3 scouts/q/q_seed.py --report`

### Atlas 🗺️ — Disclosure Path Scout
- Purpose: Maps who to notify for any given target/finding
- File: `scouts/atlas/atlas_seed.py`
- Rule: Read-only. Never submits anything.
- Run: `python3 scouts/atlas/atlas_seed.py --target example.com`

### Hermes 🪽 — Disclosure Drafter
- Purpose: Drafts professional disclosure reports, queues them for human review
- File: `scouts/hermes/hermes_seed.py`
- Rule: NEVER submits. Every draft sits as `draft` status until operator approves.
- Run: `python3 scouts/hermes/hermes_seed.py --draft`

### Mnemosyne 🏛️ — Dashboard & Notifications
- Purpose: Grafana dashboards, mesh status summaries, notification routing
- File: `scouts/mnemosyne/mnemosyne_seed.py`
- Run: `python3 scouts/mnemosyne/mnemosyne_seed.py --daily-brief`

---

## Distributed Primitives

All available at `scouts/`:

| Module | Purpose | Usage |
|--------|---------|-------|
| `distributed_lock.py` | Redis SET NX EX lock | Prevent duplicate workers |
| `dedup.py` | Redis SET-based dedup with TTL | Skip already-processed items |
| `checkpoint.py` | Redis-backed checkpoint | Resume after timeout/restart |
| `rate_limiter.py` | Redis INCR+TTL rate limiting | Respect external API limits |

---

## Common Gotchas

```python
# Redis pipeline: always check for connection errors before assuming data
try:
    depth = r.llen("mesh:inbox:gamma")
except redis.RedisError as e:
    logger.warning("Redis error: %s", e)
    depth = 0

# subprocess output: always strip \r\n, not just \n
output = result.stdout.replace('\r\n', '\n').strip()

# Docker exec via API (if CLI not available):
# Use Docker socket via requests_unixsocket or curl --unix-socket
# Never assume docker CLI is installed inside a container

# Parameterized queries: postgres uses %s, sqlite uses ?
# psycopg2:  cursor.execute("SELECT * FROM t WHERE id = %s", (id,))
# sqlite3:   cursor.execute("SELECT * FROM t WHERE id = ?",  (id,))

# Advisory locks (postgres): use %s not ?
# cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
```

---

## Spend Safety Checklist

Before deploying any scheduled job that calls an LLM:

- [ ] Model name set explicitly (not relying on default)
- [ ] Model alias validated with a single manual test call
- [ ] Run frequency is reasonable (not sub-minute for heavy models)
- [ ] Failure delivery mode is NOT `none`
- [ ] Timeout is set (never unbounded)
- [ ] Spend limit or frequency check in place

---

## Emergency Response

| Issue | Action |
|-------|--------|
| Unexpected LLM spend spike | Kill offending cron immediately, investigate, ping operator |
| A2A down on any node | Do NOT route around it — escalate to operator |
| DB connection failure | Check container health, ping operator |
| Worker stuck / looping | Check queue depth, do not restart without operator approval |
| Dead-letter queue growing | `python3 queue/monitor.py` to assess, investigate root cause |

---

*This playbook is a living document. Ratchet appends dated patches at the bottom.*
*Operators integrate patches into the main body when appropriate.*
*Update it when you learn something new. Stale playbooks cause incidents.*
