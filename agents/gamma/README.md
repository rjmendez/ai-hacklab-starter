# Gamma Agent — OSINT & Recon

The **gamma** archetype is the field operative: it handles all outward-facing recon tasks — subdomain enumeration, web fetching, port scanning, and OSINT aggregation.

## Responsibilities
- Enumerate attack surface for a target (subdomains, open ports, tech stack)
- Fetch and parse web content on demand
- Feed recon data back to alpha for analysis and reporting

## Skills
| Skill | Description |
|-------|-------------|
| `osint_research` | Full OSINT sweep: CT enum + cloud asset hints |
| `ct_enum` | Subdomain enumeration via crt.sh CT logs |
| `web_fetch` | HTTP fetch with custom headers, returns truncated body |
| `port_scan` | TCP connectivity check on specified ports |
| `memory_write` | Store key/value to agent memory |
| `memory_read` | Retrieve from agent memory |

## Adding Skills
1. Write a handler in `skill_handlers.py`: `def handle_my_skill(input_data: dict) -> dict`
2. Add to `GAMMA_SKILL_HANDLERS` at the bottom of `skill_handlers.py`
3. Add the skill entry to `agent_card.py`

## Configuration
```bash
AGENT_NAME=gamma
AGENT_TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")
AGENT_TOTP_SEED=$(python totp/generate_seed.py | grep 'TOTP Seed' | awk '{print $3}')
AGENT_URL=http://YOUR_HOST:8200
```
