# Operations Guide

Day-to-day runbook for the AI HackLab mesh.

## Daily Operations

### Check agent health
```bash
make status
# or manually:
curl http://localhost:8201/health   # alpha
curl http://localhost:8202/health   # beta
curl http://localhost:8203/health   # gamma
curl http://localhost:8204/health   # delta
```

### Monitor spend
```bash
python3 dispatch/spend_tracker.py --status
python3 dispatch/mesh_dispatcher.py --spend-status
```

### Check queue depths
```bash
redis-cli LLEN mesh:inbox:alpha
redis-cli LLEN mesh:inbox:gamma
# or via alpha's skill:
curl -X POST http://localhost:8201/a2a \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tasks/send","id":"1","params":{"skill_id":"queue_status","input":{}}}'
```

### View logs
```bash
make logs           # all agents
make logs-gamma     # single agent
docker logs --tail 100 ai-hacklab-starter-agent-gamma-1
```

---

## Teardown and Rebuild

**What gets lost on teardown:**
- Redis data: agent memories, queue state, spend tracking, circuit breaker state
- Generated config files: `.env`, `dispatch/key_pools.json`, `dispatch/agent_registry.json`

**What survives (it's all in git):**
- All Python code (a2a server, dispatcher, skill handlers, tools)
- Example configs (`.env.example`, `key_pools.example.json`, `agent_registry.example.json`)
- Docker configs, Makefile, docs

### Full rebuild procedure

```bash
# Destructive — will prompt for confirmation
make teardown

# Fresh setup
make setup
# → edit .env, key_pools.json, agent_registry.json with new tokens
make tokens   # generate new bearer tokens
make totp     # generate new TOTP seed

# Start everything
make up
make status   # verify all agents healthy
```

---

## Backup and Restore

### Backup Redis
```bash
make backup
# Saves to: backups/redis-YYYYMMDD-HHMMSS.rdb
```

### Restore Redis from backup
```bash
# Stop the stack first
make down

# Copy the RDB into the Redis volume
REDIS_CONTAINER=$(docker ps -aqf "name=redis")
docker cp backups/redis-20260101-120000.rdb $REDIS_CONTAINER:/data/dump.rdb

# Restart
make up
```

---

## Troubleshooting

### Redis connection failures
```
[mesh-queue:alpha] Could not connect to Redis
```
1. Check Redis is running: `docker ps | grep redis`
2. Check env vars: `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`
3. Check logs: `make logs` and look for the redis service

### Circuit breaker tripped
```
Pool alpha_litellm circuit OPEN
```
```bash
# Check status
python3 dispatch/spend_tracker.py --status

# Reset the circuit
python3 dispatch/spend_tracker.py --reset-circuit alpha_litellm
```
Circuit opens after 3 API errors in 60s and stays open for 300s. The underlying issue (bad API key, quota exceeded) should be resolved before resetting.

### TOTP drift
```
HTTP 403: Invalid TOTP code
```
TOTP codes are time-based. If your system clock is off:
```bash
# Check NTP sync (Linux)
timedatectl status
systemctl restart systemd-timesyncd

# Or: allow wider TOTP window in a2a/server.py
# Change: totp.verify(x_totp, valid_window=1)  →  valid_window=2
```

### Agent unreachable
1. Check it's running: `make status`
2. Check logs: `make logs-gamma`
3. Check the port: `curl http://localhost:8203/health`
4. Restart: `docker compose -f docker/docker-compose.yml restart agent-gamma`

### Adding an agent to a running mesh (no full teardown)
1. Create the agent directory: `cp -r agents/gamma agents/epsilon`
2. Write skills in `agents/epsilon/skill_handlers.py`
3. Add to `docker/docker-compose.yml`
4. Add token to `dispatch/agent_registry.json`
5. Start just the new agent: `docker compose -f docker/docker-compose.yml up -d agent-epsilon`
6. No restart of existing agents needed — they discover peers via registry
