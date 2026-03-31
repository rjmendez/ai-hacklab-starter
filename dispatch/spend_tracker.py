#!/usr/bin/env python3
"""
dispatch/spend_tracker.py — Redis-backed daily spend tracker + circuit breaker

Tracks per-pool API spend, enforces soft/hard budget limits, and implements
a rolling-window circuit breaker to automatically disable failing pools.

Redis key schema:
  dispatch:spend:{pool_id}:{YYYY-MM-DD}   float  — total USD spent today
  dispatch:tokens:{pool_id}:{YYYY-MM-DD}  int    — total tokens today
  dispatch:calls:{pool_id}:{YYYY-MM-DD}   int    — total calls today
  dispatch:errors:{pool_id}               zset   — timestamps of recent errors
  dispatch:circuit:{pool_id}              string — "1" if circuit open (with TTL)

Circuit breaker: 3 errors in 60s → circuit open for 300s.

Usage:
  python dispatch/spend_tracker.py --status
  python dispatch/spend_tracker.py --reset-circuit alpha_litellm
  python dispatch/spend_tracker.py --record alpha_litellm 0.005 500 gemini-2.5-flash
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("spend_tracker")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REDIS_HOST     = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT     = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

CIRCUIT_BREAK_ERRORS   = 3    # errors before circuit opens
CIRCUIT_BREAK_WINDOW   = 60   # seconds — rolling error window
CIRCUIT_BREAK_OPEN_TTL = 300  # seconds — how long circuit stays open

_KEY_POOLS_PATH = Path(__file__).parent / "key_pools.json"

# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _spend_key(pool_id: str, date: Optional[str] = None) -> str:
    return f"dispatch:spend:{pool_id}:{date or _today()}"

def _tokens_key(pool_id: str, date: Optional[str] = None) -> str:
    return f"dispatch:tokens:{pool_id}:{date or _today()}"

def _calls_key(pool_id: str, date: Optional[str] = None) -> str:
    return f"dispatch:calls:{pool_id}:{date or _today()}"

def _errors_key(pool_id: str) -> str:
    return f"dispatch:errors:{pool_id}"

def _circuit_key(pool_id: str) -> str:
    return f"dispatch:circuit:{pool_id}"


# ---------------------------------------------------------------------------
# SpendTracker
# ---------------------------------------------------------------------------
class SpendTracker:
    """Redis-backed daily spend tracker with circuit breaker per pool."""

    def __init__(self):
        self._redis = None
        self._pools: dict[str, dict] = {}
        self._load_pools()
        self._connect_redis()

    def _load_pools(self) -> None:
        try:
            with open(_KEY_POOLS_PATH) as f:
                data = json.load(f)
            for pool in data.get("pools", []):
                self._pools[pool["id"]] = pool
            logger.debug("Loaded %d pools from key_pools.json", len(self._pools))
        except Exception as exc:
            logger.warning("key_pools.json unavailable: %s", exc)

    def _get_pool(self, pool_id: str) -> dict:
        return self._pools.get(pool_id, {})

    def _connect_redis(self) -> None:
        try:
            import redis
            r = redis.Redis(
                host=REDIS_HOST, port=REDIS_PORT,
                password=REDIS_PASSWORD or None,
                decode_responses=True,
                socket_connect_timeout=3, socket_timeout=3,
            )
            r.ping()
            self._redis = r
            logger.debug("Redis connected at %s:%d", REDIS_HOST, REDIS_PORT)
        except Exception as exc:
            logger.warning("Redis unavailable — spend tracking disabled: %s", exc)
            self._redis = None

    @property
    def r(self):
        if self._redis is None:
            self._connect_redis()
        return self._redis

    # ── Spend recording ────────────────────────────────────────────────────

    def record_spend(self, pool_id: str, cost_usd: float, tokens: int, model: str) -> None:
        """Increment today's spend counters for a pool."""
        if not self.r:
            return
        try:
            date = _today()
            pipe = self.r.pipeline()
            pipe.incrbyfloat(_spend_key(pool_id, date), cost_usd)
            pipe.incrby(_tokens_key(pool_id, date), tokens)
            pipe.incrby(_calls_key(pool_id, date), 1)
            pipe.expire(_spend_key(pool_id, date), 172800)
            pipe.expire(_tokens_key(pool_id, date), 172800)
            pipe.expire(_calls_key(pool_id, date), 172800)
            model_key = f"dispatch:model_spend:{pool_id}:{model}"
            pipe.incrbyfloat(model_key, cost_usd)
            pipe.expire(model_key, 172800)
            pipe.execute()
        except Exception as exc:
            logger.warning("record_spend failed for %s: %s", pool_id, exc)

    # ── Spend queries ──────────────────────────────────────────────────────

    def get_today_spend(self, pool_id: str) -> float:
        if not self.r:
            return 0.0
        try:
            val = self.r.get(_spend_key(pool_id))
            return float(val) if val else 0.0
        except Exception:
            return 0.0

    def get_today_tokens(self, pool_id: str) -> int:
        if not self.r:
            return 0
        try:
            val = self.r.get(_tokens_key(pool_id))
            return int(val) if val else 0
        except Exception:
            return 0

    def get_today_calls(self, pool_id: str) -> int:
        if not self.r:
            return 0
        try:
            val = self.r.get(_calls_key(pool_id))
            return int(val) if val else 0
        except Exception:
            return 0

    def get_all_pool_spend(self) -> dict:
        return {pid: self.get_today_spend(pid) for pid in self._pools}

    # ── Limit checks ──────────────────────────────────────────────────────

    def is_over_soft_limit(self, pool_id: str) -> bool:
        soft = self._get_pool(pool_id).get("soft_limit_usd", 0)
        return soft > 0 and self.get_today_spend(pool_id) >= soft

    def is_over_hard_limit(self, pool_id: str) -> bool:
        hard = self._get_pool(pool_id).get("hard_limit_usd", 0)
        return hard > 0 and self.get_today_spend(pool_id) >= hard

    def budget_pct(self, pool_id: str) -> float:
        budget = self._get_pool(pool_id).get("daily_budget_usd", 0)
        if budget <= 0:
            return 0.0
        return (self.get_today_spend(pool_id) / budget) * 100.0

    # ── Circuit breaker ────────────────────────────────────────────────────

    def record_error(self, pool_id: str) -> None:
        """Increment error counter. Opens circuit if threshold exceeded."""
        if not self.r:
            return
        try:
            errors_key  = _errors_key(pool_id)
            circuit_key = _circuit_key(pool_id)
            now         = time.time()
            window_start = now - CIRCUIT_BREAK_WINDOW

            pipe = self.r.pipeline()
            pipe.zadd(errors_key, {str(now): now})
            pipe.zremrangebyscore(errors_key, "-inf", window_start)
            pipe.expire(errors_key, CIRCUIT_BREAK_WINDOW * 2)
            pipe.execute()

            error_count = self.r.zcard(errors_key)
            if error_count >= CIRCUIT_BREAK_ERRORS:
                self.r.setex(circuit_key, CIRCUIT_BREAK_OPEN_TTL, "1")
                logger.warning(
                    "Circuit OPENED for pool %s (%d errors in %ds — open %ds)",
                    pool_id, error_count, CIRCUIT_BREAK_WINDOW, CIRCUIT_BREAK_OPEN_TTL,
                )
        except Exception as exc:
            logger.warning("record_error failed for %s: %s", pool_id, exc)

    def circuit_open(self, pool_id: str) -> bool:
        if not self.r:
            return False
        try:
            return bool(self.r.exists(_circuit_key(pool_id)))
        except Exception:
            return False

    def reset_circuit(self, pool_id: str) -> None:
        if not self.r:
            return
        try:
            self.r.delete(_circuit_key(pool_id))
            self.r.delete(_errors_key(pool_id))
            logger.info("Circuit RESET for pool %s", pool_id)
        except Exception as exc:
            logger.warning("reset_circuit failed for %s: %s", pool_id, exc)

    def is_healthy(self, pool_id: str) -> bool:
        return not self.circuit_open(pool_id) and not self.is_over_hard_limit(pool_id)

    # ── Status summary ─────────────────────────────────────────────────────

    def get_status(self) -> dict:
        status: dict = {
            "redis_available": self.r is not None,
            "date": _today(),
            "pools": {},
        }
        for pool_id, pool_cfg in self._pools.items():
            spend   = self.get_today_spend(pool_id)
            tokens  = self.get_today_tokens(pool_id)
            calls   = self.get_today_calls(pool_id)
            circuit = self.circuit_open(pool_id)
            soft    = self.is_over_soft_limit(pool_id)
            hard    = self.is_over_hard_limit(pool_id)
            pct     = self.budget_pct(pool_id)
            status["pools"][pool_id] = {
                "spend_usd":       round(spend, 6),
                "tokens":          tokens,
                "calls":           calls,
                "daily_budget_usd": pool_cfg.get("daily_budget_usd", 0),
                "soft_limit_usd":  pool_cfg.get("soft_limit_usd", 0),
                "hard_limit_usd":  pool_cfg.get("hard_limit_usd", 0),
                "budget_pct":      round(pct, 1),
                "soft_exceeded":   soft,
                "hard_exceeded":   hard,
                "circuit_open":    circuit,
                "healthy":         not circuit and not hard,
                "weight":          pool_cfg.get("weight", 0),
            }
        return status


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_tracker_instance: Optional[SpendTracker] = None

def get_tracker() -> SpendTracker:
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = SpendTracker()
    return _tracker_instance


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="SpendTracker CLI")
    parser.add_argument("--status",        action="store_true")
    parser.add_argument("--reset-circuit", metavar="POOL_ID")
    parser.add_argument("--record",        nargs=4, metavar=("POOL", "COST", "TOKENS", "MODEL"))
    args = parser.parse_args()

    tracker = SpendTracker()

    if args.reset_circuit:
        tracker.reset_circuit(args.reset_circuit)
        print(f"✅ Circuit reset: {args.reset_circuit}")
    elif args.record:
        pool_id, cost_str, tokens_str, model = args.record
        tracker.record_spend(pool_id, float(cost_str), int(tokens_str), model)
        print(f"✅ Recorded spend: pool={pool_id} cost=${cost_str} tokens={tokens_str} model={model}")
    else:
        status = tracker.get_status()
        print(f"\n💰 Spend Tracker — {status['date']}")
        print(f"   Redis: {'✅' if status['redis_available'] else '❌ unavailable'}")
        print()
        for pid, ps in status["pools"].items():
            h  = "✅" if ps["healthy"] else "🔴"
            cb = "⚡ OPEN" if ps["circuit_open"] else "closed"
            sf = " ⚠️ SOFT" if ps["soft_exceeded"] else ""
            hf = " 🛑 HARD" if ps["hard_exceeded"] else ""
            print(f"  {h} {pid} (weight={ps['weight']}%)")
            print(f"     Spend:   ${ps['spend_usd']:.4f} / ${ps['daily_budget_usd']} ({ps['budget_pct']:.1f}%){sf}{hf}")
            print(f"     Tokens:  {ps['tokens']:,}   Calls: {ps['calls']:,}")
            print(f"     Circuit: {cb}")
            print()
