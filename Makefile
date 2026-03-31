# AI HackLab Starter — Operations Makefile
#
# Quick reference:
#   make setup       — first-time setup (copy examples, generate tokens)
#   make up          — start the full stack
#   make down        — stop the stack (preserve data volumes)
#   make teardown    — DESTRUCTIVE: stop + remove containers, volumes, configs
#   make rebuild     — teardown + setup + up (full fresh start, memories lost)
#   make status      — show agent health + queue depths
#   make logs        — tail all agent logs
#   make scan        — TruffleHog secrets scan
#   make totp        — generate a TOTP seed
#   make tokens      — generate 4 agent tokens
#   make backup      — backup Redis RDB to ./backups/

.PHONY: help setup up down teardown rebuild status logs \
        logs-alpha logs-beta logs-gamma logs-delta \
        scan totp tokens backup

COMPOSE = docker compose -f docker/docker-compose.yml

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## First-time setup: copy example configs, remind to fill in tokens
	@[ -f .env ] || (cp .env.example .env && echo "✅ Created .env — fill in your tokens and API keys")
	@[ -f dispatch/key_pools.json ] || \
		(cp dispatch/key_pools.example.json dispatch/key_pools.json && \
		echo "✅ Created dispatch/key_pools.json — fill in your API keys")
	@[ -f dispatch/agent_registry.json ] || \
		(cp dispatch/agent_registry.example.json dispatch/agent_registry.json && \
		echo "✅ Created dispatch/agent_registry.json — fill in your tokens")
	@echo ""
	@echo "🔑 Next steps:"
	@echo "   1. Edit .env with your tokens and API keys"
	@echo "   2. Run 'make tokens' to generate agent bearer tokens"
	@echo "   3. Run 'make totp' to generate a TOTP seed"
	@echo "   4. Fill in dispatch/key_pools.json and agent_registry.json"
	@echo "   5. Run 'make up' when ready"

up: ## Start the full agent mesh stack
	$(COMPOSE) up -d
	@echo "✅ Stack started. Run 'make status' to check health."

down: ## Stop the stack (preserves data volumes)
	$(COMPOSE) down

teardown: ## DESTRUCTIVE: remove containers, volumes, and generated configs
	@echo "⚠️  WARNING: This will destroy all containers, volumes, and generated config files."
	@echo "   Redis data (agent memories, queue state, spend tracking) will be LOST."
	@echo ""
	@read -p "Type 'yes' to confirm teardown: " confirm && [ "$$confirm" = "yes" ] || \
		(echo "Aborted."; exit 1)
	$(COMPOSE) down -v --remove-orphans
	@rm -f .env dispatch/key_pools.json dispatch/agent_registry.json
	@echo "✅ Teardown complete."
	@echo "   Skills and code are intact. Run 'make setup' to start fresh."

rebuild: teardown setup up ## Full teardown + fresh setup + start (agent memories will be lost)

status: ## Show agent health and queue depths
	@echo "🔌 Agent Health"
	@echo "─────────────────────────────────"
	@for port in 8201 8202 8203 8204; do \
		printf "  Port %-6s " ":$$port —"; \
		curl -sf --max-time 3 http://localhost:$$port/health 2>/dev/null \
			| python3 -c "import sys,json; d=json.load(sys.stdin); print('✅', d.get('agent','?'))" \
			2>/dev/null || echo "❌ unreachable"; \
	done
	@echo ""
	@python3 dispatch/mesh_dispatcher.py --status 2>/dev/null \
		|| echo "(dispatcher: run 'make setup' to configure dispatch/key_pools.json)"

logs: ## Tail logs from all agents
	$(COMPOSE) logs -f

logs-alpha: ## Tail alpha agent logs
	$(COMPOSE) logs -f agent-alpha

logs-beta: ## Tail beta agent logs
	$(COMPOSE) logs -f agent-beta

logs-gamma: ## Tail gamma agent logs
	$(COMPOSE) logs -f agent-gamma

logs-delta: ## Tail delta agent logs
	$(COMPOSE) logs -f agent-delta

scan: ## Run TruffleHog secrets scan on local repo
	@echo "🔍 Scanning for secrets..."
	@command -v trufflehog >/dev/null 2>&1 \
		&& (trufflehog filesystem . --only-verified && echo "✅ No verified secrets found") \
		|| echo "⚠️  trufflehog not found. Install: https://github.com/trufflesecurity/trufflehog#installation"

totp: ## Generate a new TOTP seed
	@python3 totp/generate_seed.py

tokens: ## Generate 4 agent bearer tokens (alpha/beta/gamma/delta)
	@echo "Agent tokens — add to .env and dispatch/agent_registry.json:"
	@echo ""
	@printf "  alpha  %s\n" "$$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
	@printf "  beta   %s\n" "$$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
	@printf "  gamma  %s\n" "$$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
	@printf "  delta  %s\n" "$$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
	@echo ""

backup: ## Backup Redis RDB to ./backups/redis-<timestamp>.rdb
	@mkdir -p backups
	@REDIS_ID=$$($(COMPOSE) ps -q redis 2>/dev/null) && \
		[ -n "$$REDIS_ID" ] || (echo "❌ Redis container not running"; exit 1)
	@$(COMPOSE) exec -T redis redis-cli BGSAVE
	@sleep 2
	@REDIS_ID=$$($(COMPOSE) ps -q redis) && \
		STAMP=$$(date +%Y%m%d-%H%M%S) && \
		docker cp $$REDIS_ID:/data/dump.rdb backups/redis-$$STAMP.rdb && \
		echo "✅ Backup saved: backups/redis-$$STAMP.rdb"
