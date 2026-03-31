# Agent Archetypes

Four pre-built archetypes cover the most common roles in a security research / data pipeline mesh. Each runs the same A2A server (`a2a/server.py`) — only the `agent_card.py` and `skill_handlers.py` differ.

## Skills Matrix

| Skill ID | Agent | Description |
|----------|-------|-------------|
| `task_status` | alpha | Recent dispatch activity + queue depths |
| `docker_status` | alpha | List all Docker containers |
| `docker_restart` | alpha | Restart a container by name |
| `docker_logs` | alpha | Fetch recent container logs |
| `report_generation` | alpha | Compile sections into a markdown report |
| `queue_status` | alpha | Queue depths + dead-letter counts for all agents |
| `gpu_inference` | beta | Run a prompt through local Ollama |
| `model_list` | beta | List available Ollama models |
| `hashcat_identify` | beta | Identify hash type and hashcat mode |
| `osint_research` | gamma | Chain CT enum + cloud asset hints for a target |
| `ct_enum` | gamma | Certificate Transparency subdomain enumeration |
| `web_fetch` | gamma | Fetch a URL, return body (10 KB cap) |
| `port_scan` | gamma | TCP connectivity check on specified ports |
| `wifi_scan` | gamma | Scan nearby Wi-Fi networks via nmcli/iwlist |
| `dns_lookup` | gamma | Resolve DNS records (A, MX, TXT, etc.) |
| `whois` | gamma | Raw whois output for a domain or IP |
| `db_query` | delta | Parameterized SQLite query (SELECT-safe by default) |
| `batch_process` | delta | Fan out a list of items to another agent via queue |
| `data_export` | delta | Export a list of dicts to CSV or JSON |
| `redis_sync` | delta | Sync Redis keys matching a pattern to a remote node |

## Ports

| Agent | Port |
|-------|------|
| alpha | 8201 |
| beta  | 8202 |
| gamma | 8203 |
| delta | 8204 |

## Adding a New Agent

See [`docs/adding-an-agent.md`](../docs/adding-an-agent.md) for the full walkthrough. The short version:

1. Copy an existing agent directory and rename it
2. Edit `agent_card.py` — update `AGENT_NAME`, port, and skills list
3. Write handlers in `skill_handlers.py` — each is `def handle_X(input: dict) -> dict`
4. Add a `Dockerfile` (copy an existing one, change the agent name and port)
5. Add the service to `docker/docker-compose.yml`
6. Add the agent's token to `.env` and `dispatch/agent_registry.json`
7. `make up` — no restart of existing agents needed

## Agent Card Structure

```python
AGENT_CARD = {
    "name":     "my-agent",
    "version":  "1.0.0",
    "skills": [
        {
            "id":          "my_skill",
            "name":        "My Skill",
            "description": "Does something useful",
            "input_schema":  {"type": "object", "properties": {"target": {"type": "string"}}},
            "output_schema": {"type": "object", "properties": {"result": {"type": "string"}}},
        },
    ],
}
```
