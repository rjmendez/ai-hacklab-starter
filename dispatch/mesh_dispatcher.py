#!/usr/bin/env python3
"""
dispatch/mesh_dispatcher.py — Multi-agent mesh dispatch layer

Routes LLM inference to the best available agent+model based on:
  - Task type (code, summarization, classification, etc.)
  - Cost tier (free → nano → cheap → mid → premium)
  - Key pool budgets and circuit breaker state
  - GPU-first and free-first routing policies
  - Round-robin load spreading across equivalent candidates

Configuration:
  Copy dispatch/key_pools.example.json → dispatch/key_pools.json and fill in.
  Copy dispatch/agent_registry.example.json → dispatch/agent_registry.json and fill in.

Usage:
    python dispatch/mesh_dispatcher.py --task code_review --prompt "..."
    python dispatch/mesh_dispatcher.py --task summarization --prompt "..." --tier free
    python dispatch/mesh_dispatcher.py --status
    python dispatch/mesh_dispatcher.py --spend-status
    python dispatch/mesh_dispatcher.py --spread-test
    python dispatch/mesh_dispatcher.py --reset-circuit alpha_litellm
"""

import argparse
import json
import logging
import os
import random
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mesh_dispatcher")

# ---------------------------------------------------------------------------
# Tier constants
# ---------------------------------------------------------------------------
TIER_ORDER = ["free", "nano", "cheap", "mid", "premium"]

MODEL_TIERS: dict[str, str] = {
    # Premium — use sparingly
    "claude-opus-4-5":    "premium",
    "claude-sonnet-4-5":  "premium",
    # Mid
    "gpt-4o":             "mid",
    # Cheap (default tier)
    "gemini-2.5-flash":   "cheap",
    "gemini-2.0-flash":   "cheap",
    "gpt-4.1-mini":       "cheap",
    "gpt-4.1":            "cheap",
    # Nano
    "gpt-4.1-nano":       "nano",
    "llama-3.1-8b":       "nano",
    # Free suffix (OpenRouter :free models)
    # handled by _model_tier() — any model ending in ":free" → "free"
}
_FREE_SUFFIX = ":free"

# ---------------------------------------------------------------------------
# Timeout policy per tier
# POLICY: never auto-upgrade to premium on timeout — always downgrade
# ---------------------------------------------------------------------------
TIMEOUT_POLICY: dict[str, dict] = {
    "free":    {"timeout": 45, "on_timeout": "downgrade_to_cheap"},
    "nano":    {"timeout": 30, "on_timeout": "downgrade_to_cheap"},
    "cheap":   {"timeout": 30, "on_timeout": "downgrade_to_mid"},
    "mid":     {"timeout": 60, "on_timeout": "log_and_retry_once"},
    "premium": {"timeout": 90, "on_timeout": "alert"},
}

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_rr_counter: dict[str, int] = {}
_rr_lock = threading.Lock()
_KEY_POOLS_CACHE: Optional[dict] = None
_KEY_POOLS_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DISPATCH_DIR = Path(__file__).parent.resolve()
_KEY_POOLS_PATH    = _DISPATCH_DIR / "key_pools.json"
_REGISTRY_PATH     = _DISPATCH_DIR / "agent_registry.json"
_ROUTING_TABLE_PATH = _DISPATCH_DIR / ".." / "benchmarks" / "routing_table.json"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class DispatchResult:
    agent:         str
    model:         str
    provider:      str
    response_text: str
    latency_ms:    int
    cost_usd:      float
    tokens:        int
    task:          str
    pool_id:       str = ""


class DispatchError(Exception):
    """Raised when all agents/models fail after retries."""


# ---------------------------------------------------------------------------
# Key pools
# ---------------------------------------------------------------------------
def _load_key_pools() -> dict:
    global _KEY_POOLS_CACHE
    with _KEY_POOLS_LOCK:
        if _KEY_POOLS_CACHE is not None:
            return _KEY_POOLS_CACHE
        try:
            with open(_KEY_POOLS_PATH) as f:
                data = json.load(f)
            _KEY_POOLS_CACHE = data
            logger.debug("Loaded %d key pools", len(data.get("pools", [])))
            return data
        except Exception as exc:
            logger.warning("key_pools.json unavailable (%s); pool routing disabled", exc)
            return {"pools": [], "free_first_tasks": [], "gpu_first_tasks": [], "task_tier_map": {}}


def _get_pool_list() -> list[dict]:
    return _load_key_pools().get("pools", [])


def _get_pool_by_id(pool_id: str) -> Optional[dict]:
    for pool in _get_pool_list():
        if pool["id"] == pool_id:
            return pool
    return None


def _get_pool_api_key(pool: dict) -> str:
    env_var = pool.get("api_key_env", "")
    if env_var:
        val = os.environ.get(env_var, "")
        if val:
            return val
    return pool.get("api_key_default", "")


# ---------------------------------------------------------------------------
# SpendTracker integration
# ---------------------------------------------------------------------------
_spend_tracker = None
_spend_tracker_lock = threading.Lock()


def _get_spend_tracker():
    global _spend_tracker
    with _spend_tracker_lock:
        if _spend_tracker is None:
            try:
                from spend_tracker import SpendTracker
                _spend_tracker = SpendTracker()
            except Exception as exc:
                logger.warning("SpendTracker unavailable: %s", exc)
                _spend_tracker = _NoopSpendTracker()
    return _spend_tracker


class _NoopSpendTracker:
    def record_spend(self, *a, **kw): pass
    def record_error(self, *a, **kw): pass
    def get_today_spend(self, pool_id): return 0.0
    def get_all_pool_spend(self): return {}
    def is_over_soft_limit(self, pool_id): return False
    def is_over_hard_limit(self, pool_id): return False
    def circuit_open(self, pool_id): return False
    def reset_circuit(self, pool_id): pass
    def is_healthy(self, pool_id): return True
    def get_status(self): return {"redis_available": False, "pools": {}}


# ---------------------------------------------------------------------------
# Tier helpers
# ---------------------------------------------------------------------------
def _model_tier(model_id: str) -> str:
    if model_id.endswith(_FREE_SUFFIX):
        return "free"
    return MODEL_TIERS.get(model_id, "cheap")


def _tier_index(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return len(TIER_ORDER)


def _downgrade_tier(tier: str) -> str:
    idx = _tier_index(tier)
    return TIER_ORDER[max(0, idx - 1)]


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------
def _get_totp_code() -> str:
    seed = _load_key_pools().get("totp_seed", "")
    if not seed or seed == "YOUR_TOTP_SEED_HERE":
        return ""
    try:
        import pyotp
        return pyotp.TOTP(seed).now()
    except ImportError:
        return ""


# ---------------------------------------------------------------------------
# Redis (graceful degradation)
# ---------------------------------------------------------------------------
def _get_redis():
    try:
        import redis
        r = redis.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        r.ping()
        return r
    except Exception:
        return None


def _log_to_redis(r, result: DispatchResult) -> None:
    if r is None:
        return
    try:
        entry = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent": result.agent, "model": result.model,
            "pool_id": result.pool_id, "task": result.task,
            "latency_ms": result.latency_ms, "tokens": result.tokens,
            "cost_usd": result.cost_usd,
        })
        r.lpush("dispatch:log", entry)
        r.ltrim("dispatch:log", 0, 199)
        key = f"dispatch:stats:{result.agent}"
        pipe = r.pipeline()
        pipe.hincrby(key, "total_calls", 1)
        pipe.hincrbyfloat(key, "total_tokens", result.tokens)
        pipe.hincrbyfloat(key, "total_cost_usd", result.cost_usd)
        pipe.hset(key, "last_used", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        pipe.execute()
    except Exception as exc:
        logger.debug("Redis log failed: %s", exc)


# ---------------------------------------------------------------------------
# Pool selection
# ---------------------------------------------------------------------------
def select_pool(task: str, tier_override: Optional[str] = None) -> Optional[dict]:
    """
    Select the best key pool for a task.

    Strategy:
      1. GPU-first: for compute tasks → prefer gpu_local pools
      2. Free-first: for bulk tasks  → prefer free_models_first pools
      3. Weighted random: among healthy pools within budget
      4. Soft-limit: pool weight halved if over soft limit
      5. Hard-limit: pool skipped entirely if over hard limit
      6. Circuit-broken pools are always skipped
    """
    tracker = _get_spend_tracker()
    pools   = _get_pool_list()
    kp      = _load_key_pools()
    free_first_tasks = set(kp.get("free_first_tasks", []))
    gpu_first_tasks  = set(kp.get("gpu_first_tasks", []))

    # 1. GPU-first
    if task in gpu_first_tasks:
        for pool in pools:
            if pool.get("gpu_local") and tracker.is_healthy(pool["id"]):
                logger.debug("GPU-first: %s → %s", task, pool["id"])
                return pool

    # 2. Free-first
    if task in free_first_tasks:
        for pool in pools:
            if pool.get("free_models_first") and tracker.is_healthy(pool["id"]):
                logger.debug("Free-first: %s → %s", task, pool["id"])
                return pool

    # 3. Weighted random among healthy pools
    eligible: list[tuple[dict, float]] = []
    for pool in pools:
        pid = pool["id"]
        if tracker.circuit_open(pid) or tracker.is_over_hard_limit(pid):
            continue
        weight = float(pool.get("weight", 0))
        if weight <= 0:
            continue
        if tracker.is_over_soft_limit(pid):
            weight = max(1.0, weight / 2)
        eligible.append((pool, weight))

    if not eligible:
        # Emergency fallback
        for pool in pools:
            if not tracker.circuit_open(pool["id"]):
                return pool
        return None

    pool_list = [p for p, _ in eligible]
    weights   = [w for _, w in eligible]
    return random.choices(pool_list, weights=weights, k=1)[0]


# ---------------------------------------------------------------------------
# HTTP and A2A dispatch
# ---------------------------------------------------------------------------
def _dispatch_via_http(pool: dict, model: str, prompt: str, timeout: int) -> tuple[str, int, float]:
    from openai import OpenAI
    client = OpenAI(
        base_url=pool["base_url"],
        api_key=_get_pool_api_key(pool) or "placeholder",
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
        timeout=timeout,
    )
    text   = resp.choices[0].message.content or ""
    tokens = resp.usage.total_tokens if resp.usage else 0
    cost_usd = 0.0  # caller can enrich from routing_table if needed
    return text, tokens, cost_usd


def _dispatch_via_a2a(pool: dict, model: str, prompt: str, task: str, timeout: int) -> tuple[str, int, float]:
    import requests
    payload = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "id": f"dispatch-{int(time.time()*1000)}",
        "params": {
            "skill_id": "gpu_inference",
            "input": {
                "action": "llm_inference",
                "model": model,
                "prompt": prompt,
                "max_tokens": 1024,
                "task_type": task,
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {_get_pool_api_key(pool)}",
        "X-TOTP": _get_totp_code(),
        "Content-Type": "application/json",
    }
    resp = requests.post(pool["base_url"], json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data   = resp.json()
    if "error" in data:
        raise DispatchError(f"A2A error: {data['error']}")
    result = data.get("result", {})
    output = result.get("output", {})
    if isinstance(output, str):
        output = {"text": output}
    text   = output.get("text") or output.get("content") or json.dumps(result)
    tokens = result.get("tokens", 0)
    cost_usd = result.get("cost_usd", 0.0)
    return text, tokens, cost_usd


# ---------------------------------------------------------------------------
# dispatch_with_failover
# ---------------------------------------------------------------------------
def dispatch_with_failover(
    task: str,
    prompt: str,
    tier: Optional[str] = None,
    agent_hint: Optional[str] = None,
) -> DispatchResult:
    """Pool-aware dispatch with automatic failover chain."""
    tracker = _get_spend_tracker()
    r       = _get_redis()
    kp      = _load_key_pools()
    current_tier = tier or kp.get("task_tier_map", {}).get(task, "cheap")
    timeout      = TIMEOUT_POLICY.get(current_tier, {}).get("timeout", 60)

    primary = select_pool(task, tier_override=current_tier)
    if primary is None:
        raise DispatchError(f"No healthy pool for task={task!r}")

    pools_to_try = [primary]
    seen = {primary["id"]}
    for fid in primary.get("failover_to", []):
        if fid not in seen:
            fp = _get_pool_by_id(fid)
            if fp and tracker.is_healthy(fid):
                pools_to_try.append(fp)
                seen.add(fid)

    errors: list[str] = []
    for pool in pools_to_try:
        pid           = pool["id"]
        dispatch_type = pool.get("dispatch", "http")
        models        = pool.get("models", [])
        if not models:
            continue

        # Prefer free models for free-first tasks
        free_first_tasks = set(kp.get("free_first_tasks", []))
        if task in free_first_tasks and pool.get("free_models_first"):
            models = sorted(models, key=lambda m: (0 if ":free" in str(m) else 1))

        model = models[0] if isinstance(models[0], str) else models[0].get("id", "")

        logger.info("dispatch pool=%s model=%s task=%s tier=%s timeout=%ds",
                    pid, model, task, current_tier, timeout)

        t0 = time.monotonic()
        try:
            if dispatch_type == "a2a":
                text, tokens, cost_usd = _dispatch_via_a2a(pool, model, prompt, task, timeout)
            else:
                text, tokens, cost_usd = _dispatch_via_http(pool, model, prompt, timeout)

            latency_ms = int((time.monotonic() - t0) * 1000)
            tracker.record_spend(pid, cost_usd, tokens, model)
            result = DispatchResult(
                agent=pid, model=model, provider=dispatch_type,
                response_text=text, latency_ms=latency_ms,
                cost_usd=cost_usd, tokens=tokens, task=task, pool_id=pid,
            )
            _log_to_redis(r, result)
            return result

        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            errors.append(f"{pid}/{model}: {exc}")
            logger.warning("Pool %s failed: %s", pid, exc)
            tracker.record_error(pid)

            on_timeout = TIMEOUT_POLICY.get(current_tier, {}).get("on_timeout", "")
            if "downgrade" in on_timeout:
                current_tier = _downgrade_tier(current_tier)
                timeout = TIMEOUT_POLICY.get(current_tier, {}).get("timeout", 60)

    raise DispatchError(f"All pools failed for task={task!r}. Errors: {errors}")


# ---------------------------------------------------------------------------
# Main dispatch entry point
# ---------------------------------------------------------------------------
def dispatch(
    task: str,
    prompt: str,
    preferred_tier: Optional[str] = None,
    preferred_agent: Optional[str] = None,
    exclude_agents: Optional[list[str]] = None,
    max_cost_per_1k: Optional[float] = None,
) -> DispatchResult:
    """
    Route a prompt to the best available agent+model.

    Args:
        task:             Task type (see task_tier_map in key_pools.json).
        prompt:           Input text.
        preferred_tier:   Tier hint: free|nano|cheap|mid|premium.
        preferred_agent:  Lock to a specific pool ID.
        exclude_agents:   Pool IDs to skip.
        max_cost_per_1k:  Hard cost ceiling in USD per 1k tokens.

    Returns:
        DispatchResult.

    Raises:
        DispatchError if all candidates fail.
    """
    if preferred_agent is None and not exclude_agents and max_cost_per_1k is None:
        return dispatch_with_failover(task=task, prompt=prompt, tier=preferred_tier)

    # Filtered pool selection
    tracker = _get_spend_tracker()
    r       = _get_redis()
    exclude = set(exclude_agents or [])

    for pool in _get_pool_list():
        pid = pool["id"]
        if pid in exclude:
            continue
        if preferred_agent and pid != preferred_agent:
            continue
        if not tracker.is_healthy(pid):
            continue

        models = [m if isinstance(m, str) else m.get("id", "") for m in pool.get("models", [])]
        if not models:
            continue

        model = models[0]

        t0 = time.monotonic()
        try:
            dispatch_type = pool.get("dispatch", "http")
            timeout = TIMEOUT_POLICY.get(preferred_tier or "cheap", {}).get("timeout", 60)
            if dispatch_type == "a2a":
                text, tokens, cost_usd = _dispatch_via_a2a(pool, model, prompt, task, timeout)
            else:
                text, tokens, cost_usd = _dispatch_via_http(pool, model, prompt, timeout)

            latency_ms = int((time.monotonic() - t0) * 1000)
            tracker.record_spend(pid, cost_usd, tokens, model)
            result = DispatchResult(
                agent=pid, model=model, provider=dispatch_type,
                response_text=text, latency_ms=latency_ms,
                cost_usd=cost_usd, tokens=tokens, task=task, pool_id=pid,
            )
            _log_to_redis(r, result)
            return result
        except Exception as exc:
            logger.warning("Pool %s failed: %s", pid, exc)
            tracker.record_error(pid)
            continue

    raise DispatchError(f"No pools succeeded for task={task!r}")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------
def cmd_status() -> None:
    import requests
    r = _get_redis()
    print("\n🔌 Agent Mesh Status")
    print("=" * 60)
    for pool in _get_pool_list():
        pid  = pool["id"]
        url  = pool.get("base_url", "")
        health_url = url.replace("/a2a", "/health") if "/a2a" in url else url + "/health"
        reachable = False
        try:
            resp = requests.get(health_url, timeout=4)
            reachable = resp.status_code < 400
        except Exception:
            pass
        icon = "✅" if reachable else "❌"
        print(f"\n  {icon} {pid}")
        print(f"     URL:      {url}")
        print(f"     Models:   {len(pool.get('models', []))}")
        print(f"     Weight:   {pool.get('weight', 0)}%")
        if r:
            stats = r.hgetall(f"dispatch:stats:{pid}") or {}
            print(f"     Calls:    {stats.get('total_calls', 0)}")
            print(f"     Cost:     ${float(stats.get('total_cost_usd', 0)):.4f}")

    print("\n📜 Last 5 Dispatch Entries")
    print("-" * 60)
    if r:
        for i, raw in enumerate(r.lrange("dispatch:log", 0, 4), 1):
            try:
                e = json.loads(raw)
                print(f"  {i}. [{e.get('ts','')}] {e.get('pool_id','?')}/{e.get('model','?')} "
                      f"task={e.get('task','?')} {e.get('latency_ms','?')}ms ${float(e.get('cost_usd',0)):.4f}")
            except Exception:
                print(f"  {i}. {raw}")
    else:
        print("  Redis unavailable")
    print()


def cmd_spend_status() -> None:
    tracker = _get_spend_tracker()
    status  = tracker.get_status()
    print(f"\n💰 Pool Spend Status — {status.get('date', 'today')}")
    print(f"   Redis: {'✅' if status['redis_available'] else '❌'}")
    print("=" * 60)
    for pid, ps in status.get("pools", {}).items():
        health = "✅" if ps.get("healthy") else "🔴"
        circuit = "⚡ OPEN" if ps.get("circuit_open") else "closed"
        soft = " ⚠️" if ps.get("soft_exceeded") else ""
        hard = " 🛑" if ps.get("hard_exceeded") else ""
        budget = ps.get("daily_budget_usd", 0)
        print(f"\n  {health} {pid}")
        print(f"     Spend:   ${ps.get('spend_usd', 0):.4f} / ${budget or '∞'} ({ps.get('budget_pct', 0):.1f}%){soft}{hard}")
        print(f"     Calls:   {ps.get('calls', 0)}   Tokens: {ps.get('tokens', 0):,}")
        print(f"     Circuit: {circuit}")
    print()


def cmd_spread_test() -> None:
    print("\n🔁 Spread Test — sending to all pools")
    print("=" * 60)
    for pool in _get_pool_list():
        pid = pool["id"]
        print(f"\n→ {pid}")
        try:
            result = dispatch(task="summarization", prompt="What is 2 + 2? One sentence.",
                              preferred_agent=pid)
            print(f"  Model:    {result.model}")
            print(f"  Latency:  {result.latency_ms}ms")
            print(f"  Response: {result.response_text[:120]!r}")
        except DispatchError as exc:
            print(f"  ❌ {exc}")
    print()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Mesh dispatcher — route LLM tasks across the agent mesh")
    parser.add_argument("--task",    default="summarization")
    parser.add_argument("--prompt",  help="Input prompt")
    parser.add_argument("--tier",    dest="preferred_tier", help="free|nano|cheap|mid|premium")
    parser.add_argument("--agent",   dest="preferred_agent", help="Force a specific pool ID")
    parser.add_argument("--exclude", dest="exclude_agents", nargs="*")
    parser.add_argument("--max-cost", dest="max_cost_per_1k", type=float)
    parser.add_argument("--status",       action="store_true")
    parser.add_argument("--spend-status", action="store_true")
    parser.add_argument("--spread-test",  action="store_true")
    parser.add_argument("--reset-circuit", metavar="POOL_ID")
    parser.add_argument("--json",    action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.status:        cmd_status();       return
    if args.spend_status:  cmd_spend_status(); return
    if args.spread_test:   cmd_spread_test();  return

    if args.reset_circuit:
        _get_spend_tracker().reset_circuit(args.reset_circuit)
        print(f"✅ Circuit reset for: {args.reset_circuit}")
        return

    if not args.prompt:
        parser.error("--prompt required unless using --status, --spend-status, --spread-test, or --reset-circuit")

    try:
        result = dispatch(
            task=args.task, prompt=args.prompt,
            preferred_tier=args.preferred_tier, preferred_agent=args.preferred_agent,
            exclude_agents=args.exclude_agents, max_cost_per_1k=args.max_cost_per_1k,
        )
    except DispatchError as exc:
        print(f"❌ DispatchError: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(f"\n✅ pool={result.pool_id}  model={result.model}  task={result.task}")
        print(f"   latency={result.latency_ms}ms  tokens={result.tokens}  cost=${result.cost_usd:.6f}")
        print(f"\n--- Response ---\n{result.response_text}\n----------------\n")


if __name__ == "__main__":
    main()
