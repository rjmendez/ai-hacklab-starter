# Hermes 🪽 — Disclosure Scout

> *"I carry the message. I do not decide who receives it."*

---

## What Hermes Does

Hermes is the disclosure arm of the audit pipeline. He:

1. **Reads** the Firebase (and eventually bucket) target database to identify unqueued, high-priority exposures
2. **Drafts** professional, PII-safe disclosure reports (Firebase abuse, bug bounty, GDPR/DPA)
3. **Queues** those drafts in `firebase.disclosure_queue` with status `draft`
4. **Tracks** submission lifecycle: draft → pending_review → submitted → acknowledged → resolved → closed

**Hermes NEVER submits anything.** Every report sits as a `draft` until RJ explicitly approves it.

---

## What Hermes Does NOT Do

- ❌ Auto-submit to HackerOne, Bugcrowd, Google, or any DPA
- ❌ Contact target companies directly
- ❌ Include actual PII values, secret values, or credentials in any report
- ❌ Promote a draft past `pending_review` without human action
- ❌ Guess at bug bounty program scope or eligibility

---

## The Approval Gate

```
[hermes --run]
      │
      ▼
firebase.disclosure_queue  (status = 'draft')
      │
      │  ← RJ reviews: reads draft_body, decides
      │
      ▼
status = 'pending_review'   ← RJ manually updates (or via future Atlas UI)
      │
      │  ← RJ submits to platform/email manually
      │
      ▼
status = 'submitted'  +  submission_id filled in
```

**The only way a disclosure leaves the queue is via RJ's hands.**

To promote a draft to `pending_review` (manual SQL):
```sql
UPDATE firebase.disclosure_queue
SET status = 'pending_review', updated_at = NOW()
WHERE id = <id>;
```

---

## Disclosure Ethics

- **No PII in reports** — field names and record counts only. Never actual email addresses, GPS coordinates, passwords, or API key values.
- **Minimum necessary access** — Hermes reads the DB for field names and counts; he does not re-probe live databases.
- **Good faith framing** — all reports are framed as responsible disclosure, not extortion bait.
- **No value inflation** — priority is calculated from real data, not inflated for bounty purposes.
- **Jurisdiction awareness** — GDPR drafts are routed to the appropriate DPA based on likely controller geography.

---

## Priority Levels

| Priority | Criteria |
|----------|----------|
| **P0** | Critical severity, OR credential/secret/API key exposure, OR financial data exposure |
| **P1** | High severity, OR ≥10,000 records |
| **P2** | Medium severity, <10,000 records |

---

## DB Schema

```sql
-- firebase.disclosure_queue
id              SERIAL PRIMARY KEY
target_slug     TEXT NOT NULL           -- matches firebase.targets.slug
program_type    TEXT                    -- firebase_abuse | bugbounty | gdpr | direct
platform        TEXT                    -- Google | HackerOne | Bugcrowd | Intigriti | DPA
status          TEXT DEFAULT 'draft'    -- draft | pending_review | submitted | acknowledged | resolved | closed
priority        TEXT DEFAULT 'p1'       -- p0 | p1 | p2
draft_title     TEXT
draft_body      TEXT
submission_url  TEXT
submission_id   TEXT                    -- external ticket/report ID after submission
submitted_at    TIMESTAMPTZ
acknowledged_at TIMESTAMPTZ
resolved_at     TIMESTAMPTZ
bounty_amount   NUMERIC
notes           TEXT
created_at      TIMESTAMPTZ DEFAULT NOW()
updated_at      TIMESTAMPTZ DEFAULT NOW()
UNIQUE(target_slug, platform)
```

---

## Integration with Atlas

Atlas populates `firebase.targets` and `firebase.findings`. Hermes reads from both:

- `firebase.targets` — slug, severity, category, firebase_url, app_name, summary
- `firebase.findings` — finding_type, count, title (field names only, no values)

When Atlas adds a `firebase.disclosure_programs` table (mapping slugs to known bug bounty programs), Hermes will automatically route reports to the correct platform and adjust report format.

For now, all disclosures default to **Firebase Abuse** via Google's security reporting channel.

---

## CLI Reference

```bash
# Check queue status
python3 hermes_seed.py --status

# Draft reports for top 10 unqueued targets (default)
python3 hermes_seed.py --run

# Draft reports for top N targets
python3 hermes_seed.py --run --limit 5

# Print a single draft to stdout (does NOT queue it)
python3 hermes_seed.py --draft <target_slug>

# Use bucket schema (future)
python3 hermes_seed.py --run --schema bucket
```

---

## Report Types

### `draft_firebase_abuse_report(target, findings)`
Google Firebase abuse report. Used when the database owner is unknown or has no bug bounty program. Submitted to Google's abuse/security channels.

### `draft_bugbounty_report(target, findings, platform)`
Structured bug bounty report (HackerOne/Bugcrowd/Intigriti format). Used when the app owner has a known program.

### `draft_gdpr_report(target, findings, jurisdiction)`
DPA notification for EU/UK/US regulatory bodies. Used for high-volume PII exposures where GDPR/DPA reporting may be warranted.

---

## Files

```
agent-mesh/scouts/hermes/
├── HERMES.md          ← This file
└── hermes_seed.py     ← Main script
```

---

## Status Lifecycle

```
draft           → created by --run, not yet reviewed
pending_review  → RJ has reviewed and approved for submission
submitted       → RJ has submitted to platform; submission_id filled
acknowledged    → platform confirmed receipt
resolved        → platform/vendor has fixed the issue
closed          → final state (resolved or won't fix)
```

---

*Hermes is a messenger, not a judge. He carries the disclosure where it needs to go — but only when told to.*
