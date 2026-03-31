# Scouts

Specialized agents that extend the core mesh with higher-level capabilities.
Each scout is a focused single-purpose tool. They compose: Q feeds Ratchet,
Ratchet patches the PLAYBOOK, Atlas feeds Hermes, Mnemosyne watches all of them.

## PLAYBOOK.md

**Start here.** All scouts read `PLAYBOOK.md` at startup. It contains:
- Model routing rules and known failure modes
- Cron constraints
- Mesh communication policy
- Agent capability reference
- Common gotchas (Redis, subprocess, parameterized queries)
- Emergency response runbook

Update it when you learn something new. Stale playbooks cause incidents.

## Scout Inventory

| Scout | File | Purpose | Writes To |
|-------|------|---------|-----------|
| Ratchet 🔧 | `ratchet/ratchet_seed.py` | Reads failures, patches PLAYBOOK | PLAYBOOK.md (append-only) |
| Q 🔬 | `q/q_seed.py` | Cross-links findings, tests hypotheses, proposes tools | `workspace/planning/` |
| Atlas 🗺️ | `atlas/atlas_seed.py` | Maps disclosure paths for any target | stdout / JSON |
| Hermes 🪽 | `hermes/hermes_seed.py` | Drafts disclosure reports for human review | `workspace/disclosures/`, hermes.db |
| Mnemosyne 🏛️ | `mnemosyne/mnemosyne_seed.py` | Mesh dashboard, notifications, memory index | stdout, webhook, file |

## Shared Utilities

| Module | Purpose |
|--------|---------|
| `distributed_lock.py` | Redis SET NX EX lock — prevents duplicate workers |
| `dedup.py` | Redis SET dedup with TTL — skip already-processed items |
| `checkpoint.py` | Redis checkpoint — resume after timeout/restart |
| `rate_limiter.py` | Redis INCR+TTL rate limiter — mesh-wide API rate limiting |

## Quick Reference

```bash
# Ratchet: check for issues, patch playbook
python3 scouts/ratchet/ratchet_seed.py --report --patch

# Q: cross-link findings, save report
python3 scouts/q/q_seed.py --report --save

# Atlas: map disclosure path for a target
python3 scouts/atlas/atlas_seed.py --target example.com --finding-type api_key

# Hermes: draft a disclosure report
python3 scouts/hermes/hermes_seed.py --draft example.com --finding-type credential
python3 scouts/hermes/hermes_seed.py --list
python3 scouts/hermes/hermes_seed.py --approve <draft-id>   # operator action
# (Hermes never submits — that's always a human step)

# Mnemosyne: daily brief + mesh status
python3 scouts/mnemosyne/mnemosyne_seed.py --daily-brief --save
python3 scouts/mnemosyne/mnemosyne_seed.py --mesh-status
python3 scouts/mnemosyne/mnemosyne_seed.py --memory-index
```

## Key Constraints

- **Hermes never submits.** Every disclosure draft requires explicit operator approval before any submission. This is non-negotiable.
- **Atlas maps, Hermes drafts, operator submits.** The human is always the last step.
- **Q is read-only.** Writes only to `workspace/planning/` and Redis `q:*` keys.
- **Ratchet is append-only.** Never deletes PLAYBOOK content. Patches are dated and reversible.
- **All scouts fail gracefully.** No scout should crash the pipeline if it errors.

## Environment Variables

All scouts share:
```
REDIS_HOST        — Redis hostname (default: redis)
REDIS_PORT        — Redis port (default: 6379)
REDIS_PASSWORD    — Redis password
RESEARCH_DB_PATH  — Path to research_notes SQLite DB (default: ~/.agent-mesh/research.db)
HERMES_DB_PATH    — Path to Hermes disclosure DB (default: ~/.agent-mesh/hermes.db)
MNEMOSYNE_WEBHOOK_URL  — Webhook for notifications (optional)
MNEMOSYNE_NOTIFY_FILE  — File path for notification log (optional)
```
