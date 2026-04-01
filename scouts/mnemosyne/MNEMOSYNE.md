# MNEMOSYNE.md 🏛️

> *"Memory is the mother of all wisdom." — Aeschylus*

Mnemosyne is the mesh's memory keeper, dashboard manager, and human interface specialist.
She turns machine churn into something a human can read, act on, and trust.

---

## What She Manages

| Domain | Responsibility |
|---|---|
| **Grafana dashboards** | Build, maintain, and provision mesh-overview panels |
| **Notifications** | Route alerts to the right channel at the right severity |
| **Project state** | Human-readable summary of what every scout is doing |
| **Meshtastic** | Out-of-band LoRa alerts when the network can't be trusted |
| **Memory / search** | Indexes all mesh output, playbooks, and scout docs into Redis |

---

## Architecture

```
           ┌──────────────────────────────────────────────┐
           │              mnemosyne_seed.py                │
           │                                               │
           │  mesh_status()  ─── Redis *:status keys       │
           │  daily_brief()  ─── DB + Redis + mesh_status │
           │  notify()       ─── severity-routed dispatch  │
           │  index/search() ─── Redis mnemosyne:index:*   │
           │  send_meshtastic_alert()  ─── TCP bridge      │
           └──────────────────────────────────────────────┘
                   │                  │
          audit-redis:6379    openclaw-grafana:3000
                   │
          meshtastic TCP bridge (oxalis 100.73.200.19:4403)
```

---

## Meshtastic Setup

### Hardware
- **Device:** COM20 on Oxalis's Windows machine (100.73.200.19)
- **Bridge type:** Meshtastic TCP interface (not serial)
- **Port:** 4403 (default meshtastic TCP)

### How to enable the TCP bridge on Oxalis

Run this on Oxalis's machine **once** (keep it running as a service or background process):

```bash
# If meshtastic CLI is installed on Oxalis:
meshtastic --host --port 4403
# This opens a TCP bridge to COM20 on port 4403
```

Or, if running as a Windows service, configure it via the Meshtastic app:
`Settings → Device → TCP Server → Enable on port 4403`

### Python usage

```python
import meshtastic
import meshtastic.tcp_interface

iface = meshtastic.tcp_interface.TCPInterface(
    hostname="100.73.200.19", portNumber=4403
)
iface.sendText("🚨 CRITICAL: pipeline down")
iface.close()
```

Install: `pip install meshtastic`

### Meshtastic OpenClaw Plugin (First Task)

Mnemosyne's first hardware project is an **OpenClaw gateway plugin** for Oxalis that:

1. Listens on the Meshtastic channel for incoming messages
2. Routes them into the mesh via `mnemosyne:alerts` Redis pub/sub
3. Allows scouts to send out-of-band alerts when internet is unavailable

**Plugin location (to build):** `workspace/agent-mesh/plugins/meshtastic_gateway/`

The plugin should:
- Subscribe to `mnemosyne:alerts` Redis channel
- Forward `critical` and `warning` messages to Meshtastic
- Decode incoming Meshtastic messages and publish to `mesh:inbound`

---

## Notification Severity Routing

| Severity | Channels | When to use |
|---|---|---|
| `critical` | Meshtastic + Redis pub/sub + log | Pipeline down, data breach, auth failure |
| `warning` | Redis pub/sub + log | Scout error, queue backup, A2A timeout |
| `info` | log only | Status updates, completions, low-signal events |

### Extending with new channels

Add a new channel handler inside `notify()`:

```python
elif ch == "pagerduty":
    # POST to PagerDuty events API
    ...

elif ch == "telegram":
    # Use message tool or Telegram Bot API
    ...

elif ch == "irc":
    # Publish to IRC #ops via Charlie's IRC bridge
    ...
```

Then update the severity→channels routing dict at the top of `notify()`.

---

## Memory / Index Schema

All indexed documents live in Redis as hashes:

```
mnemosyne:index:<slug>
  path        → /home/openclaw/.openclaw/workspace/playbooks/foo.md
  title       → "Foo Playbook"
  tags        → '["playbook", "recon"]'   (JSON array)
  summary     → first 200 chars of non-heading content
  indexed_at  → 2025-01-01T00:00:00+00:00
```

### What gets indexed

| Source | Tags |
|---|---|
| `workspace/playbooks/**/*.md` | `["playbook"]` |
| `workspace/planning/**/*.md` | `["planning"]` |
| `workspace/agent-mesh/scouts/*/SCOUT.md` | `["scout", "<name>"]` |

### Re-indexing

```bash
python3 mnemosyne_seed.py --index
```

Safe to re-run; keys are overwritten in place.

### Searching

```bash
python3 mnemosyne_seed.py --search "bucket enumeration"
```

Currently: simple case-insensitive keyword match across title + summary + tags + path.
Future: upgrade to Redis Search (RediSearch module) for full-text.

---

## CLI Reference

```
python3 mnemosyne_seed.py [options]

  --status              Print JSON mesh status (agent A2A + scout states)
  --brief               Print human-readable daily brief
  --project             Print current project status + blockers + next actions
  --notify "MSG"        Send a notification (default severity: info)
  --severity LEVEL      Severity for --notify: info | warning | critical
  --index               Index all playbooks, planning docs, and SCOUT.md files
  --search "QUERY"      Search the indexed document store
  --meshtastic "MSG"    Send a direct Meshtastic alert
  --grafana             Check Grafana health + ensure mesh-overview dashboard
```

---

## Her Role: Human Interface

Mnemosyne formats output for humans, not machines.

- **daily_brief()** → readable morning summary (pipeline stats, agent health, blockers)
- **project_status()** → current state of all work in progress
- **mesh_status()** → JSON for machines, but readable enough for spot-checking

She is the **single source of truth** for "what is the mesh doing right now?"

When a human asks "what's happening?", Mnemosyne answers.
When a critical event fires at 3am, Mnemosyne reaches through LoRa to wake someone up.

---

## Integration Points

| System | How |
|---|---|
| Charlie | Imports `mesh_status()`, calls `notify()` for pipeline events |
| Iris/Rex | Write `iris:status` / `rex:status` Redis hashes → Mnemosyne reads them |
| Oxalis | Hosts Meshtastic device; runs TCP bridge on :4403 |
| Grafana | `grafana_ensure_mesh_dashboard()` idempotently provisions dashboards |
| Redis | Primary state store — all scouts should write `<name>:status` hashes |

---

## Runbook: First-Time Setup

```bash
# 1. Install dependencies
pip install redis requests psycopg2-binary meshtastic

# 2. Verify connectivity
python3 mnemosyne_seed.py --status
python3 mnemosyne_seed.py --grafana

# 3. Index all docs
python3 mnemosyne_seed.py --index

# 4. Morning brief
python3 mnemosyne_seed.py --brief

# 5. Test Meshtastic (requires TCP bridge active on Oxalis)
python3 mnemosyne_seed.py --meshtastic "Mnemosyne online 🏛️"
```

---

*Mnemosyne remembers so the mesh doesn't forget.*
