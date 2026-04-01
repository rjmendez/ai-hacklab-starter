# Scout Playbook — Shared Knowledge Base

> **All scouts read this at startup.**
> Last updated: 2026-03-28
> Maintained by: Charlie 🐀

---

## Model Routing Rules

### ❌ NEVER use for subagent file-writing tasks:

| Model | Reason |
|-------|--------|
| `gpt-4.1` | Instant-fail in subagent mode — exits <1s, empty output |
| `gpt-4.1-mini` | Same instant-fail bug |
| `claude-opus-4-5` | Same instant-fail bug in subagent mode (287ms fail observed) |

### ✅ ALWAYS use for subagent file-writing:

```
claude-sonnet-4-6   ← canonical subagent model, validated
```

### Model tiers by use case:

```
Cron / heartbeat / simple checks:
  gpt-4.1-nano          ← cheap, fast, reliable for structured output
  gemini-2.5-flash      ← good alternative for high-frequency jobs

Complex analysis / architecture / writing:
  claude-sonnet-4-6     ← default workhorse
  claude-opus-4-6       ← premium, use sparingly (expensive)

Free quota (GitHub Copilot — use when available):
  openai/github-copilot/claude-sonnet-4.6
  openai/github-copilot/claude-opus-4.6
```

### ⚠️ Model config rules:

- **Always set model explicitly.** Never rely on proxy default — it falls through to sonnet, which is expensive.
- **Validate model alias before deploying to cron.** A bad alias silently routes to default. 847 calls at sonnet rates = $413.
- If a model alias is unrecognized, LiteLLM does NOT error — it falls to default. Test with a single manual call first.

---

## Cron Rules

```
✅ DO:
- Set model explicitly in every cron config
- Set delivery: announce + bestEffort for failure visibility
- Use sessionTarget="current" if the cron needs to make A2A calls
- Set reasonable timeout (300s for simple, 600s for DB/file ops)
- Scope each run to a single operation (read OR write OR analyze)

❌ DON'T:
- Use delivery: none — silent failures are invisible
- Chain multiple heavy operations in one cron run (read + write + analyze = timeout)
- Use isolated subagent crons for anything that needs outbound A2A calls
- Deploy a cron that calls an LLM without a spend limit or frequency check
```

### Isolated cron subagents cannot make A2A calls

This is a hard constraint of the current architecture. If your cron task requires
talking to another agent (Oxalis, MrPink, etc.), it must run in a session-bound
context (`sessionTarget="current"`), not as an isolated subagent.

---

## Mesh Communication Policy

```
RULE 1: A2A first, always.
  All operational agent-to-agent communication goes through A2A.
  No exceptions. If A2A is down, escalate to RJ.

RULE 2: IRC is passive logging only.
  IRC channels (#ops, #announce) are for human-readable logs and alerts.
  Never trigger actions, send commands, or route operations via IRC.
  If something is on IRC only, it's a log — not a command bus.

RULE 3: If A2A fails or a skill is a stub, escalate.
  Do NOT route around failures. Do NOT assume silence = success.
  Surface the failure to RJ explicitly.

RULE 4: Oxalis sysadmin skill = manual review queue.
  Nothing auto-executes. All tasks sent to it require human action.
  Sending a task to Oxalis sysadmin skill does NOT mean it will run.
  If you need Oxalis to actually do something, flag it to RJ directly.
```

---

## Agent Capabilities (current state as of 2026-03-28)

### Charlie 🐀 — `100.95.177.44:8200`

- **Role:** Pipeline manager, findings DB owner
- **A2A:** UP, TOTP enabled
- **Container:** `charlie` on DESKTOP-BVRDK4J (Docker Desktop / Linux)
- **Skills:**
  - `findings_query` — query firebase.targets, bucket.scan_files, bucket.scan_secrets
  - `data_processing` — ETL, ingestion, deduplication
  - `pipeline_status` — health checks, scan worker status
  - `report_generation` — markdown reports from DB data
- **DB access:** `audit-postgres:5432`, db `audit_framework`
- **Redis:** `audit-redis:6379`

### Oxalis 🌿 — `100.73.200.19:8200`

- **Role:** Infra lead, GPU compute
- **GPU:** RTX 4070 Ti (hashcat-capable)
- **A2A:** UP
- **Skills:**
  - `hashcat_identify` — hash type identification
  - `hashcat_benchmark` — GPU benchmark
  - `hashcat_crack` — *(coming soon)*
  - `sysadmin` — **MANUAL QUEUE ONLY. Nothing auto-executes.**
  - `docker_manage` — Docker container management
  - `litellm_manage` — LiteLLM proxy config
- **BLOCKED:**
  - Hashcat binary not renamed: `C:\hashcat-7.1.2` must become `C:\hashcat` (manual Windows action required)
  - Model patch pending (in sysadmin queue — needs human review)

### MrPink — `100.115.69.88:8200`

- **Role:** Field operative, OSINT, Firebase pipeline
- **A2A:** DOWN (server not running as of 2026-03-28)
- **Gateway:** UP (IRC accessible at `#ops`)
- **Firebase pipeline:** 3,914 targets, 28 critical findings
- **Note:** Reach via IRC only until A2A server is restarted

---

## Scout Registry

### Iris 🌸 — CT Log Enumeration

- **Purpose:** Certificate Transparency log enumeration, Firebase subdomain discovery
- **File:** `agent-mesh/iris/iris_seed.py`
- **Feeds into:** `firebase.targets` (unscored rows)
- **Run:** `python3 iris_seed.py [--dry-run] [--limit N] [--exposed-only]`
- **Model:** `gpt-4.1-nano` for scheduling, `claude-sonnet-4-6` for analysis tasks

### Rate Scout — LiteLLM Spend Monitor

- **Purpose:** Monitor LiteLLM spend, alert on anomalies, recommend model routing
- **File:** `agent-mesh/scouts/rate_scout.py`
- **Watches:** `litellm_proxy.LiteLLM_SpendLogs` (note: quoted table name required)
- **Run:** `python3 rate_scout.py [--report] [--watch] [--recommend]`
- **Born after:** $413 overnight overspend incident (2026-03-28)

### Rex 🦴 — DB Audit & Findings Reporting

- **Purpose:** DB audit, ETL validation, findings reports
- **Spawn as:** subagent (`claude-sonnet-4-6`, timeout=600s)
- **Reads:** `firebase.targets`, `bucket.scan_files`, `bucket.scan_secrets`
- **Writes:** `planning/*.md` reports
- **Scope rule:** One operation per run (read OR write OR analyze — never chain)
- **Known issue:** Timed out at 5min when chaining ops; solved by 600s timeout + narrow scope

---

## Known Gotchas

```python
# psycopg2: LiteLLM tables need quoted names
cursor.execute('SELECT * FROM "LiteLLM_SpendLogs" LIMIT 10')
# NOT: SELECT * FROM LiteLLM_SpendLogs  ← fails

# Docker exec API: use Tty:true for clean output
{"Tty": true, "AttachStdout": true, "Cmd": [...]}
# Strip \r from results: output.replace('\r\n', '\n')

# Docker CLI not installed — use Docker API via Unix socket ONLY
# Wrong: docker exec charlie python3 ...
# Right: curl --unix-socket /var/run/docker.sock ...

# Git HTTPS auth broken in charlie container
# Use gh CLI or SSH for any git operations

# scan_pipeline.py does NOT exist in the running image — never deployed
# http.py was renamed to http_session.py — update any imports that reference http.py

# psycopg2 parameterization: use %s not ? (sqlite style)
cursor.execute("SELECT * FROM table WHERE id = %s", (value,))

# Advisory lock placeholders: %s not ?
cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
```

---

## Spend Safety Checklist

Before deploying any scheduled job that calls an LLM:

- [ ] Model name set explicitly (not relying on default)
- [ ] Model alias validated with a single manual test call
- [ ] Run frequency is reasonable (not sub-minute for heavy models)
- [ ] Rate Scout is watching `LiteLLM_SpendLogs`
- [ ] Failure delivery mode is NOT `none`
- [ ] Timeout is set (never unbounded)

---

## Emergency Contacts

| Issue | Action |
|-------|--------|
| Unexpected LLM spend spike | Check Rate Scout, kill offending cron, ping RJ |
| A2A down on any node | Do NOT route around it — escalate to RJ |
| DB connection failure | Check `audit-postgres` container health, ping RJ |
| Scan worker stuck | Query `pipeline_status`, do not restart without RJ approval |
| Oxalis unresponsive | IRC only until A2A confirmed; flag to RJ if MrPink A2A also down |

---

*This playbook is a living document. Update it when you learn something new.*
*When in doubt: ask RJ. Silence under uncertainty is not a feature.*


---

## Ratchet Patch — 2026-03-28 15:11 UTC

Add Redis credentials: host=audit-redis:6379, password=-pyGzOHVtcESCnHb3NkMEWwMbc5i47On

Document Meshtastic on COM20 if this is a known integration point.

Add Ratchet 🔧 to Scout Registry:
  - Purpose: meta-scout, continuous system improvement
  - File: agent-mesh/scouts/ratchet/ratchet_seed.py
  - Run: python3 ratchet_seed.py --report

Document MrPink A2A is DOWN; IRC only until server restarted.

---

## Ratchet Patch — 2026-03-29 01:15 UTC

Document MrPink A2A is DOWN; IRC only until server restarted.