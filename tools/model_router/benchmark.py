#!/usr/bin/env python3
"""
tools/model_router/benchmark.py — Benchmark model latency and availability.

Tests each tier's models with standard prompts, records latency/tokens/pass-fail,
and outputs a JSON results file.

Usage:
    python tools/model_router/benchmark.py
    python tools/model_router/benchmark.py --tier mid
    python tools/model_router/benchmark.py --all --output results.json
    python tools/model_router/benchmark.py --tier strong --prompt "Explain recursion"
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from tools.model_router.model_selector import TIER_MODELS, TIER_ORDER, _call_model

DEFAULT_PROMPTS = {
    "nano":    "What is 2 + 2? Answer in one word.",
    "mini":    "Write a Python function that returns the Fibonacci sequence up to n terms.",
    "mid":     "Explain the difference between a stack and a queue. Give a use case for each.",
    "strong":  "Describe a secure architecture for a multi-tenant API service with rate limiting.",
    "premium": "What are the key trade-offs between eventual consistency and strong consistency in distributed databases?",
}


def benchmark_tier(tier: str, prompt: str) -> dict:
    results = []
    models  = TIER_MODELS.get(tier, [])

    for model in models:
        print(f"  Testing {model}...", end=" ", flush=True)
        t0      = time.monotonic()
        content, usage = _call_model(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            timeout=30,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        passed = content is not None
        tokens = usage.get("total_tokens", 0) if usage else 0
        print(f"{'✅' if passed else '❌'} {latency_ms}ms {tokens} tokens")

        results.append({
            "model":      model,
            "passed":     passed,
            "latency_ms": latency_ms,
            "tokens":     tokens,
            "preview":    (content or "")[:100],
        })

    return {
        "tier":    tier,
        "prompt":  prompt[:80],
        "results": results,
        "passed":  sum(1 for r in results if r["passed"]),
        "total":   len(results),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark model router")
    parser.add_argument("--tier",   help="Single tier to benchmark")
    parser.add_argument("--all",    action="store_true", help="Benchmark all tiers")
    parser.add_argument("--prompt", help="Override the test prompt")
    parser.add_argument("--output", help="Write results to JSON file")
    args = parser.parse_args()

    tiers = TIER_ORDER if args.all else ([args.tier] if args.tier else ["mid"])

    all_results = []
    for tier in tiers:
        if tier not in TIER_MODELS:
            print(f"Unknown tier: {tier}. Choose from: {TIER_ORDER}")
            sys.exit(1)
        prompt = args.prompt or DEFAULT_PROMPTS.get(tier, DEFAULT_PROMPTS["mid"])
        print(f"\n🔵 Tier: {tier}")
        print(f"   Prompt: {prompt[:60]}...")
        result = benchmark_tier(tier, prompt)
        all_results.append(result)
        print(f"   {result['passed']}/{result['total']} passed")

    print()
    if args.output:
        Path(args.output).write_text(json.dumps(all_results, indent=2))
        print(f"✅ Results written to {args.output}")
    else:
        print(json.dumps(all_results, indent=2))


if __name__ == "__main__":
    main()
