# Ratchet 🔧 — Meta-Scout for Continuous System Improvement

> *Ratchet only turns one way: forward.*
> *He reads failure signals and tightens things. He never breaks.*

---

## What Ratchet Monitors

| Signal Source | What He Reads | What He Flags |
|--------------|--------------|---------------|
| `LiteLLM_SpendLogs` | Per-model spend, call counts, token averages | Spend concentration, cron tasks on expensive models, unused free quota |
| `/tmp/charlie-a2a.log` | A2A server error lines | Error spikes, missing log (server down) |
| `PLAYBOOK.md` | Known broken states, documented gotchas | Undocumented issues, stale entries |
| `playbooks/*.md` (AARs) | Subagent timeout incidents | Scope-too-broad patterns, missing timeout configs |
| `playbooks/improvements/` | His own prior reports | Recurring issues that never got patched |

---

## The One-Way Ratchet Principle

```
✅ Ratchet CAN:
  - Append patches to PLAYBOOK.md (dated, labeled, reversible)
  - Write improvement reports to playbooks/improvements/
  - Flag issues as Critical / Recommended / Nice-to-Have
  - Recommend model routing changes
  - Document known failures

❌ Ratchet CANNOT:
  - Delete or overwrite existing PLAYBOOK.md content
  - Kill crons or running jobs
  - Restart services
  - Execute pipeline operations
  - Contact external agents autonomously
  - Make decisions that require human judgment
```

Patches are append-only. Every patch is dated. Nothing is destroyed.
If a patch is wrong, remove it manually — Ratchet won't fight back.

---

## Analysis Functions

### `analyze_spend(hours=24, since_iso=None) -> list[dict]`
Queries `litellm_proxy.LiteLLM_SpendLogs`. Finds:
- Single model consuming >50% of hourly spend
- Expensive models (claude-sonnet, gpt-4o, etc.) running high-frequency low-token cron tasks
- Zero free-quota (github-copilot/*) calls while paid models ran

### `analyze_cron_failures() -> list[dict]`
Reads `/tmp/charlie-a2a.log` and known PLAYBOOK states. Finds:
- Error/exception lines in A2A log
- Missing log (A2A server not running)
- Documented broken states: MrPink A2A down, Oxalis hashcat path wrong

### `analyze_subagent_timeouts() -> list[dict]`
Scans `playbooks/*.md` for timeout patterns. Finds:
- Tasks that timed out due to scope-too-broad (chained read+write+analyze)
- Missing or too-short timeout configs

### `analyze_playbook_gaps() -> list[dict]`
Compares PLAYBOOK.md content against known required entries. Finds:
- Broken models not documented
- Redis credentials missing from playbook
- Meshtastic COM20 not documented
- Ratchet himself not in Scout Registry

### `produce_improvements(findings) -> str`
Generates a markdown improvement report structured as:
- 🔴 Critical (act now)
- 🟡 Recommended (act soon)
- 🟢 Nice to Have
- 📋 Playbook Patches (exact text to add)

### `patch_playbook(patches, dry_run=False)`
Appends dated patches to PLAYBOOK.md. Always:
- Adds a `## Ratchet Patch — YYYY-MM-DD HH:MM UTC` header
- Never overwrites existing content
- Supports `--dry-run` to preview without writing

---

## Adding New Analysis Functions

1. Write a function `analyze_<thing>() -> list[dict]`
2. Each dict must include at minimum: enough fields for `produce_improvements()` to classify it
   - For spend issues: `{type, model, cost, calls, recommendation, estimated_savings}`
   - For cron/system failures: `{job_name, failure_type, root_cause, fix}`
   - For timeout patterns: `{task, timeout_ms, root_cause, recommendation}`
   - For playbook gaps: `{gap, current_state, recommended_addition}`
3. Call your function in `run()` and extend `all_findings`
4. Add a row to the monitoring table above

---

## How Patches Get Promoted to PLAYBOOK.md

```
Ratchet detects gap
       ↓
produce_improvements() generates recommended_addition text
       ↓
--patch flag → patch_playbook() appends to PLAYBOOK.md
       ↓
Dated header: "## Ratchet Patch — 2026-04-01 12:00 UTC"
       ↓
Human reviews on next session
       ↓
(Optional) Human integrates patch into main body of PLAYBOOK
```

Ratchet's patches live at the bottom of PLAYBOOK.md, clearly dated.
They don't auto-integrate into the main body — that requires human judgment.

---

## Ratchet's Relationship to Other Scouts

| Scout | Ratchet's Relationship |
|-------|----------------------|
| **Rate Scout** | Ratchet reads Rate Scout's spend data (same DB source). If Rate Scout is healthy, Ratchet's spend analysis is redundant but validates it. |
| **Iris 🌸** | Ratchet reads Iris AARs for timeout patterns. If Iris is timing out frequently, Ratchet flags her scope or model config. |
| **Rex 🦴** | Same — Ratchet reads Rex AARs. Rex's known scope issue (chaining ops → timeout) is exactly the pattern Ratchet catches. |
| **PLAYBOOK.md** | Ratchet's primary output target. He reads it for known issues, patches it with new findings. |
| **Charlie 🐀** | Ratchet runs inside Charlie's container. He reads Charlie's A2A log. He does NOT command Charlie — he reports. |

Ratchet is **read-mostly**. His only write operation is appending to PLAYBOOK.md and saving reports.
He never commands other scouts. He surfaces findings for humans or higher-level agents to act on.

---

## CLI Reference

```bash
# Full report (default)
python3 ratchet_seed.py --report

# Report + apply playbook patches
python3 ratchet_seed.py --report --patch

# Dry run — show what would be patched
python3 ratchet_seed.py --report --patch --dry-run

# Narrow lookback window
python3 ratchet_seed.py --report --since 2026-04-01T00:00:00

# Patch only (no report output)
python3 ratchet_seed.py --patch
```

---

## Output Files

| Path | What |
|------|------|
| `playbooks/improvements/ratchet_YYYY-MM-DD_HHMM.md` | Full report from each run |
| `PLAYBOOK.md` (appended) | Dated patches from `--patch` flag |

---

## Cron Integration (when ready)

Ratchet is a good candidate for a scheduled daily run. When deploying to cron:

- Model: `gpt-4.1-nano` (analysis is structured, not creative)
- Timeout: `300s` (DB + filesystem ops, not chained)
- Delivery: `announce` (never `none`)
- Scope per run: `--report` only (separate cron for `--patch` with human gate)
- Session target: `current` (if A2A calls are ever added)

Follow the Spend Safety Checklist in PLAYBOOK.md before deploying.

---

*Ratchet was born after the $413 overnight overspend incident (2026-03-28).*
*He exists so that never happens again.*
