# Atlas 🗺️ — Disclosure Program Scout

> *"Atlas maps, never submits — submission is Hermes's job."*

Atlas answers one question for any target the mesh discovers:

**"Who do we tell, and how?"**

---

## What Atlas Does

Atlas is a read-only intelligence scout. It takes a target slug or domain and returns an ordered list of disclosure paths — the channels through which a finding should be reported. It does **not** submit reports; that responsibility belongs to Hermes.

Atlas covers:

| Channel Type | Examples |
|---|---|
| Bug bounty programs | HackerOne, Bugcrowd, Intigriti, YesWeHack |
| Responsible disclosure | `security.txt` / `.well-known/security.txt` (RFC 9116) |
| Platform abuse reporting | Firebase/Google, AWS, GCP, Azure |
| GDPR / DPA notification | EDPB, ICO, CNIL, BfDI, FTC (for 100k+ PII records) |
| CERT referrals | US-CERT/CISA, CERT-EU, NCSC-UK |
| Direct vendor contact | `abuse@`, `security@`, RDAP/WHOIS lookup |
| Law enforcement | IC3 (FBI), national agencies — for fraud/C2 infrastructure |

---

## Disclosure Path Priority Order

Paths are returned in priority order, with the most actionable first:

1. **Bug Bounty** (HackerOne → Bugcrowd → others)  
   Fastest path to acknowledgment and remediation. Bounty platforms have triage SLAs.

2. **Responsible Disclosure** (`security.txt`)  
   If the operator published a disclosure policy, follow it.

3. **Platform Abuse** (Firebase, AWS, GCP, Azure)  
   Always included for cloud-hosted targets. Infrastructure providers can take down assets.

4. **GDPR / DPA Notification**  
   Triggered automatically when `pii_record_count > 100,000`. Regulators can compel remediation.

5. **CERT Referral** (US-CERT, NCSC, etc.)  
   For findings with national significance or when operator is unresponsive.

6. **Law Enforcement** (IC3, FBI, national police)  
   Triggered for crypto/C2/malware indicators. Do **not** contact operator directly for active threat infrastructure.

7. **Direct Contact** (fallback)  
   `abuse@<registrar>` or `security@<domain>`. Always included as a last resort.

---

## Special Rules

### Crypto / C2 Targets
If a target slug contains indicators like `usdt`, `mining`, `wallet`, `c2`, `botnet`, `stealer`, `dropper`, or `malware`:
- **Skip direct operator contact** — the operator may be malicious
- **Prioritize**: law enforcement (IC3) + CERT (US-CERT) + ISP abuse
- Note this in disclosure records

### Firebase Targets
Always include the Google Firebase abuse channel regardless of other findings:
`https://firebase.google.com/support/troubleshooter/contact`

### Large PII Exposures (>100k records)
Automatically adds GDPR/DPA path. Hermes (or a human) must decide which national DPA applies based on the data subjects' country.

---

## DB Schema

Atlas writes to:
- `firebase.disclosure_programs` — for Firebase targets
- `bucket.disclosure_programs` — for S3/GCS/Azure bucket targets

```sql
CREATE TABLE IF NOT EXISTS firebase.disclosure_programs (
    id              SERIAL PRIMARY KEY,
    target_slug     TEXT NOT NULL,
    program_type    TEXT NOT NULL,  -- 'bugbounty','responsible_disclosure','gdpr','abuse','cert','law_enforcement','direct'
    platform        TEXT,           -- 'hackerone','bugcrowd','security_txt','google_firebase', etc.
    program_url     TEXT,
    submission_url  TEXT,
    contact_email   TEXT,
    scope_status    TEXT,           -- 'in_scope','out_of_scope','unknown'
    notes           TEXT,
    discovered_at   TIMESTAMPTZ DEFAULT NOW(),
    verified_at     TIMESTAMPTZ,
    UNIQUE(target_slug, program_type, platform)
);
```

`scope_status` is currently set to `'unknown'` for most paths — it's Atlas's job to map channels, not to verify scope. Scope verification is a future enhancement.

---

## CLI Usage

```bash
# Scan one target (dry run — no DB write)
python3 atlas_seed.py --target ad-cash-c2917-firebaseio-com --dry-run

# Scan one target (write to DB)
python3 atlas_seed.py --target my-app-12345-firebaseio-com --schema firebase

# Scan with explicit domain override
python3 atlas_seed.py --target myapp --domain myapp.example.com

# Batch scan all unscanned Firebase targets (up to 50)
python3 atlas_seed.py --batch --schema firebase --limit 50

# Batch scan S3 buckets, rescan already-scanned
python3 atlas_seed.py --batch --schema bucket --all --limit 100

# Dry run batch
python3 atlas_seed.py --batch --dry-run
```

---

## How to Add New Program Types

1. **Add detection logic** in the relevant check function (`check_hackerone`, `check_bugcrowd`, or write a new `check_intigriti()`, etc.)

2. **Add a new disclosure path dict** in `classify_disclosure_path()`:
   ```python
   paths.append({
       "program_type": "bugbounty",
       "platform": "intigriti",
       "program_url": ...,
       "submission_url": ...,
       "contact_email": None,
       "scope_status": "unknown",
       "notes": "...",
   })
   ```

3. **Order matters** — insert at the correct priority position in `classify_disclosure_path()`.

4. **Add to the table reference** above in this doc.

Intigriti API reference: `https://app.intigriti.com/api/core/public/programs`  
YesWeHack API: `https://api.yeswehack.com/programs`

---

## Integration with the Mesh

| Agent | Relationship |
|---|---|
| **Iris** | Discovers Firebase targets → Atlas maps disclosure paths |
| **Rate Scout** | Discovers bucket targets → Atlas maps disclosure paths |
| **Hermes** | Reads Atlas's `disclosure_programs` table → submits reports |
| **Charlie** | Orchestrates; can trigger `atlas_scan()` or `atlas_batch()` directly |

Atlas should be triggered after any new target is confirmed as vulnerable/exposed. The natural trigger points are:
- After Iris logs a finding to `firebase.targets`
- After Rate Scout logs a finding to `bucket.targets`

---

## Legal Note

⚠️ **Atlas maps, never submits.**

Atlas is a passive reconnaissance tool that queries public APIs and open standards (security.txt, RDAP, HackerOne public directory). It does not:
- Submit vulnerability reports
- Contact operators
- Access non-public data

All disclosure channel data Atlas maps is publicly available. Submission decisions and communications are the sole responsibility of **Hermes** (the submission agent) and ultimately the human operator (RJ).

Responsible disclosure timelines, embargo periods, and legal jurisdiction questions are outside Atlas's scope.

---

*Last updated: auto-generated by atlas_seed.py bootstrap*
