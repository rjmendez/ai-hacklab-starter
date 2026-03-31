# Delta Agent — Batch Processing & Data Ops

The **delta** archetype handles everything data: querying databases, batch processing large result sets, exporting findings, and archiving completed work.

## Responsibilities
- Run parameterized queries against research databases
- Process large datasets in chunks without overwhelming memory
- Export results to CSV/JSON/SQLite for downstream use
- Archive reports and artifacts to configured storage

## Skills
| Skill | Description |
|-------|-------------|
| `db_query` | Execute parameterized SQL queries |
| `batch_process` | Process a list of items in configurable chunks |
| `data_export` | Export query results to CSV/JSON/SQLite |
| `report_archive` | Write completed reports to disk/S3/GCS |
| `memory_write` | Store key/value to agent memory |
| `memory_read` | Retrieve from agent memory |

## Configuration
```bash
AGENT_NAME=delta
AGENT_TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")
AGENT_TOTP_SEED=$(python totp/generate_seed.py | grep 'TOTP Seed' | awk '{print $3}')
AGENT_URL=http://YOUR_HOST:8200
AUDIT_DB_DSN=postgresql://user:pass@localhost:5432/research_db
```

## Use Case Example
Alpha receives 500 URLs to analyze → delegates batch to delta → delta chunks into groups of 50, processes each, stores results → alpha aggregates and generates report.
