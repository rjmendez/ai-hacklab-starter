# Research Notes MCP Server

A generic MCP (Model Context Protocol) server that gives your agents a shared research database. Any agent can submit findings during an investigation; any can query them later.

## Setup

```json
{
  "mcpServers": {
    "research-notes": {
      "command": "python3",
      "args": ["/path/to/ai-hacklab-starter/mcp/research_notes.py"]
    }
  }
}
```

**Custom DB path:**
```bash
RESEARCH_DB_PATH=/data/research.db python3 mcp/research_notes.py
```

Default: `~/.agent-mesh/research.db`

## Tools

### `finding_submit`
Submit a new finding:
```json
{
  "value": "https://example.com/admin/config.json",
  "type": "config_leak",
  "confidence": "high",
  "source": "osint",
  "notes": "Publicly accessible config with internal service URLs",
  "target": "example.com",
  "investigation_id": "inv-001",
  "source_url": "https://example.com/admin/config.json",
  "reported_by": "gamma",
  "tags": ["unauthenticated", "config"]
}
```

Finding types: `credential` · `endpoint` · `config_leak` · `cert` · `domain` · `api_key` · `private_key` · `other`

### `finding_query`
Search and filter:
```json
{"type": "api_key", "confidence": "high"}
{"target": "example.com", "search": "stripe"}
{"tag": "unauthenticated", "limit": 50}
{"investigation_id": "inv-001"}
```

### `finding_get`
```json
{"id": "uuid-of-finding"}
```

### `finding_stats`
Returns total count + breakdown by type, confidence, and reporting agent. No arguments required.

### `finding_tag`
```json
{"id": "uuid-of-finding", "tags": ["verified", "high-impact"]}
```

## From Python

```python
import json, subprocess

def mcp_call(tool_name, args):
    proc = subprocess.Popen(
        ["python3", "mcp/research_notes.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    # Initialize
    proc.stdin.write(json.dumps({"jsonrpc":"2.0","method":"initialize","id":0,"params":{}})+"\n")
    proc.stdin.flush(); proc.stdout.readline()

    # Call tool
    proc.stdin.write(json.dumps({
        "jsonrpc":"2.0","method":"tools/call","id":1,
        "params":{"name": tool_name, "arguments": args}
    })+"\n")
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    proc.terminate()
    return json.loads(resp["result"]["content"][0]["text"])

# Example
result = mcp_call("finding_stats", {})
print(result)
```
