#!/usr/bin/env python3
"""
rate_scout.py — LiteLLM Spend Monitor & Routing Efficiency Scout
Charlie 🐀 | agent-mesh/scouts

Usage:
  python3 rate_scout.py [--report] [--watch] [--since ISO] [--recommend] [--json]
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not available. Install with: pip install psycopg2-binary")
    sys.exit(1)

# ── Scout state (optional — degrades gracefully if Redis is down) ─────────────
try:
    import os as _os
    sys.path.insert(0, _os.path.dirname(__file__))
    from scout_state import set_running, set_idle, set_error
except Exception:
    def set_running(*a, **k): pass
    def set_idle(*a, **k): pass
    def set_error(*a, **k): pass

SCOUT_NAME = "rate_scout"

# ─── Cost Configuration ───────────────────────────────────────────────────────

EXPENSIVE_MODELS = {
    'anthropic/claude-sonnet-4-6': 0.003,   # per 1k tokens (output)
    'anthropic/claude-opus-4-6':   0.015,
    'openai/o3':                   0.060,
    'openai/gpt-4o':               0.010,
    'openai/gpt-4.1':              0.008,
}

FREE_MODELS = [
    'openai/github-copilot/claude-sonnet-4.6',
    'openai/github-copilot/claude-opus-4.6',
]

CHEAP_MODELS = [
    'openrouter/openai/gpt-4.1-nano',
    'openai/gpt-4.1-nano',
    'gemini/gemini-2.5-flash',
]

# Alert thresholds
HOURLY_SPEND_ALERT   = 10.0   # USD — alert if hourly spend exceeds this
MODEL_DOMINANCE_ALERT = 0.80  # 80% — alert if one model dominates hourly calls

# Routing tier labels
def routing_tier(model: str) -> str:
    if model in FREE_MODELS:
        return 'free'
    if model in CHEAP_MODELS:
        return 'cheap'
    if model in EXPENSIVE_MODELS:
        cost = EXPENSIVE_MODELS[model]
        if cost >= 0.010:
            return 'premium'
        return 'mid'
    return 'unknown'

# ─── Database ─────────────────────────────────────────────────────────────────

DB_DSN = "postgresql://audit:3XhFFuOgww1CxgY6c-ydkj7Tu9Tr_O7E@audit-postgres:5432/litellm_proxy"

def get_conn():
    return psycopg2.connect(DB_DSN)

def query(sql, params=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

# ─── Data Fetchers ────────────────────────────────────────────────────────────

def fetch_spend_since(since: datetime):
    """Return all spend rows since the given UTC datetime."""
    return query(
        '''SELECT model, spend, total_tokens, "startTime", call_type, metadata
           FROM "LiteLLM_SpendLogs"
           WHERE "startTime" >= %s
           ORDER BY "startTime" DESC''',
        (since,)
    )

def fetch_top_calls(since: datetime, limit: int = 5):
    return query(
        '''SELECT model, spend, total_tokens, "startTime", call_type
           FROM "LiteLLM_SpendLogs"
           WHERE "startTime" >= %s
           ORDER BY spend DESC
           LIMIT %s''',
        (since, limit)
    )

# ─── Analysis Helpers ─────────────────────────────────────────────────────────

def aggregate_by_model(rows):
    totals = {}
    for r in rows:
        m = r['model'] or 'unknown'
        if m not in totals:
            totals[m] = {'spend': 0.0, 'tokens': 0, 'calls': 0}
        totals[m]['spend']  += float(r['spend'] or 0)
        totals[m]['tokens'] += int(r['total_tokens'] or 0)
        totals[m]['calls']  += 1
    return totals

def routing_efficiency(rows):
    """Return (free_tokens, cheap_tokens, total_tokens, score_pct)."""
    free_tok  = 0
    cheap_tok = 0
    total_tok = 0
    for r in rows:
        t = int(r['total_tokens'] or 0)
        total_tok += t
        m = r['model'] or ''
        tier = routing_tier(m)
        if tier == 'free':
            free_tok += t
        elif tier == 'cheap':
            cheap_tok += t
    score = ((free_tok + cheap_tok) / total_tok * 100) if total_tok > 0 else 0
    return free_tok, cheap_tok, total_tok, score

def now_utc():
    return datetime.now(timezone.utc)

# ─── Mode: --report ───────────────────────────────────────────────────────────

def cmd_report(since_override: datetime = None, as_json: bool = False):
    now = now_utc()
    windows = {
        '1h':  now - timedelta(hours=1),
        '6h':  now - timedelta(hours=6),
        '24h': now - timedelta(hours=24),
    }
    if since_override:
        windows = {'custom': since_override}

    results = {}
    for label, since in windows.items():
        rows = fetch_spend_since(since)
        total_spend = sum(float(r['spend'] or 0) for r in rows)
        by_model    = aggregate_by_model(rows)
        free_tok, cheap_tok, total_tok, eff = routing_efficiency(rows)
        results[label] = {
            'since':       since.isoformat(),
            'total_spend': total_spend,
            'by_model':    by_model,
            'free_tokens':  free_tok,
            'cheap_tokens': cheap_tok,
            'total_tokens': total_tok,
            'efficiency':   eff,
            'row_count':    len(rows),
        }

    top_calls_1h = fetch_top_calls(windows.get('1h', windows.get('custom')), limit=5)

    if as_json:
        print(json.dumps({'report': results, 'top_calls_1h': [dict(r) for r in top_calls_1h]}, default=str, indent=2))
        return

    print("=" * 60)
    print("  RATE SCOUT — Spend Report")
    print(f"  Generated: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    for label, data in results.items():
        print(f"\n📊 Spend ({label}): ${data['total_spend']:.4f} USD  |  {data['row_count']} calls  |  {data['total_tokens']:,} tokens")

    print("\n─── 24h Model Breakdown ──────────────────────────────────")
    if '24h' in results:
        by_model = results['24h']['by_model']
        total_24h = results['24h']['total_spend']
        sorted_models = sorted(by_model.items(), key=lambda x: x[1]['spend'], reverse=True)
        for model, stats in sorted_models:
            tier  = routing_tier(model)
            pct   = (stats['spend'] / total_24h * 100) if total_24h > 0 else 0
            tier_icon = {'free': '🟢', 'cheap': '🟡', 'mid': '🟠', 'premium': '🔴'}.get(tier, '⚪')
            print(f"  {tier_icon} {model:<50}  ${stats['spend']:.4f}  ({pct:.1f}%)  {stats['calls']} calls  {tier}")

    print("\n─── Top 5 Most Expensive Calls (last 1h) ─────────────────")
    if top_calls_1h:
        for r in top_calls_1h:
            ts = r['startTime'].strftime('%H:%M:%S') if r['startTime'] else '?'
            print(f"  ${float(r['spend'] or 0):.5f}  {r['model']:<45}  {r['total_tokens']:>6} tok  {ts}")
    else:
        print("  (no calls in last 1h)")

    # Free model capacity check
    print("\n─── Free Model Capacity Check ────────────────────────────")
    if '1h' in results:
        by_model_1h = results['1h']['by_model']
        expensive_used = any(routing_tier(m) in ('mid', 'premium') for m in by_model_1h)
        free_used      = [m for m in by_model_1h if routing_tier(m) == 'free']
        if expensive_used:
            if not free_used:
                print("  ⚠️  UNDERUSED: Expensive models were used but NO free models called in last 1h")
                for fm in FREE_MODELS:
                    print(f"       → {fm} (0 calls)")
            else:
                print(f"  ✅  Free models in use: {', '.join(free_used)}")
        else:
            print("  ✅  No expensive model calls in last 1h")

    print("\n─── Routing Efficiency ───────────────────────────────────")
    for label, data in results.items():
        eff = data['efficiency']
        bar_len = int(eff / 5)
        bar = '█' * bar_len + '░' * (20 - bar_len)
        emoji = '✅' if eff >= 60 else ('⚠️ ' if eff >= 30 else '🔴')
        print(f"  {label:>8}  {emoji} [{bar}] {eff:.1f}%  (free+cheap tokens / total)")

    print()

# ─── Mode: --watch ────────────────────────────────────────────────────────────

def cmd_watch(as_json: bool = False):
    print(f"🐀 rate_scout --watch  |  checking every 5 min  |  Ctrl+C to stop")
    print(f"   HOURLY_SPEND_ALERT=${HOURLY_SPEND_ALERT}  MODEL_DOMINANCE={MODEL_DOMINANCE_ALERT*100:.0f}%\n")

    while True:
        now  = now_utc()
        since = now - timedelta(hours=1)
        rows  = fetch_spend_since(since)
        hourly_spend = sum(float(r['spend'] or 0) for r in rows)
        by_model     = aggregate_by_model(rows)
        total_calls  = sum(v['calls'] for v in by_model.values())
        free_tok, cheap_tok, total_tok, eff = routing_efficiency(rows)

        ts = now.strftime('%H:%M:%S')
        alerts = []

        if hourly_spend > HOURLY_SPEND_ALERT:
            alerts.append(f"🚨 ALERT: Hourly spend ${hourly_spend:.4f} exceeds threshold ${HOURLY_SPEND_ALERT}")

        if total_calls > 0:
            dominant = max(by_model.items(), key=lambda x: x[1]['calls'], default=None)
            if dominant:
                dom_model, dom_stats = dominant
                dom_ratio = dom_stats['calls'] / total_calls
                if dom_ratio >= MODEL_DOMINANCE_ALERT and routing_tier(dom_model) in ('mid', 'premium'):
                    alerts.append(
                        f"⚡ ROUTING_ALERT: {dom_model} = {dom_ratio*100:.0f}% of 1h calls ({dom_stats['calls']}/{total_calls})"
                    )

        expensive_used = any(routing_tier(m) in ('mid', 'premium') for m in by_model)
        free_used      = any(routing_tier(m) == 'free' for m in by_model)
        if expensive_used and not free_used and total_calls > 0:
            alerts.append(f"💸 EFFICIENCY_ALERT: Expensive models used but zero free model calls in 1h")

        if as_json:
            payload = {
                'ts': ts,
                'hourly_spend': hourly_spend,
                'total_calls':  total_calls,
                'efficiency':   eff,
                'alerts':       alerts,
            }
            print(json.dumps(payload))
        else:
            if alerts:
                for a in alerts:
                    print(f"[{ts}] {a}")
            else:
                print(f"[{ts}] ✅ RATE_OK  |  1h spend ${hourly_spend:.4f}  |  {total_calls} calls  |  efficiency {eff:.1f}%")

        sys.stdout.flush()
        time.sleep(300)  # 5 minutes

# ─── Mode: --recommend ────────────────────────────────────────────────────────

def cmd_recommend(as_json: bool = False):
    now   = now_utc()
    since = now - timedelta(hours=24)
    rows  = fetch_spend_since(since)

    if not rows:
        print("No spend data in last 24h.")
        return

    by_model   = aggregate_by_model(rows)
    total_spend = sum(v['spend'] for v in by_model.values())
    total_tok   = sum(v['tokens'] for v in by_model.values())

    suggestions = []

    for model, stats in by_model.items():
        tier = routing_tier(model)
        if tier not in ('mid', 'premium'):
            continue
        calls   = stats['calls']
        spend   = stats['spend']
        tokens  = stats['tokens']
        avg_tok = tokens / calls if calls > 0 else 0

        # Could these calls go to nano/flash?
        if avg_tok < 2000 and calls >= 5:
            cheap_cost_per_1k = 0.0001  # rough estimate for nano/flash
            estimated_cheap_spend = (tokens / 1000) * cheap_cost_per_1k
            savings = spend - estimated_cheap_spend
            if savings > 0.01:
                suggestions.append({
                    'model':    model,
                    'calls':    calls,
                    'spend':    spend,
                    'avg_tok':  int(avg_tok),
                    'action':   f"Route to gpt-4.1-nano or gemini-2.5-flash",
                    'savings':  savings,
                    'reason':   f"avg {avg_tok:.0f} tok/call — likely simple tasks",
                })

        # Could these go to free copilot models?
        for fm in FREE_MODELS:
            if model.replace('anthropic/', 'openai/github-copilot/').replace('-4-6', '.6') in fm or \
               model.replace('anthropic/', '').replace('-4-6', '.6') in fm:
                suggestions.append({
                    'model':   model,
                    'calls':   calls,
                    'spend':   spend,
                    'avg_tok': int(avg_tok),
                    'action':  f"Switch to free: {fm}",
                    'savings': spend,
                    'reason':  f"Free equivalent available ({fm})",
                })
                break

    # Opus small-call check
    for model, stats in by_model.items():
        if 'opus' in model.lower() and stats['calls'] > 0:
            avg_tok = stats['tokens'] / stats['calls']
            if avg_tok < 500:
                suggestions.append({
                    'model':   model,
                    'calls':   stats['calls'],
                    'spend':   stats['spend'],
                    'avg_tok': int(avg_tok),
                    'action':  f"Route short calls to claude-sonnet instead",
                    'savings': stats['spend'] * 0.8,
                    'reason':  f"{stats['calls']} calls with avg {avg_tok:.0f} tok — too small for opus",
                })

    if as_json:
        print(json.dumps({'suggestions': suggestions, 'total_24h_spend': total_spend}, default=str, indent=2))
        return

    print("=" * 60)
    print("  RATE SCOUT — Routing Recommendations (24h)")
    print("=" * 60)
    print(f"\n  Total 24h spend: ${total_spend:.4f}  |  {total_tok:,} tokens\n")

    if not suggestions:
        print("  ✅ No obvious routing improvements found.\n")
        print("  Routing hierarchy: free > cheap > mid > premium")
        return

    total_savings = sum(s['savings'] for s in suggestions)
    print(f"  Potential savings: ~${total_savings:.4f}/day\n")

    for s in sorted(suggestions, key=lambda x: x['savings'], reverse=True):
        print(f"  💡 {s['model']}")
        print(f"     {s['calls']} calls, ${s['spend']:.4f} spent, avg {s['avg_tok']} tok/call")
        print(f"     → {s['action']}")
        print(f"        Reason: {s['reason']}")
        print(f"        Est. savings: ~${s['savings']:.4f}/day")
        print()

    print("  Routing hierarchy: free > cheap > mid > premium")
    print()

# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='rate_scout — LiteLLM spend monitor and routing efficiency analyzer'
    )
    parser.add_argument('--report',    action='store_true', help='Print spend report (default)')
    parser.add_argument('--watch',     action='store_true', help='Continuous watch mode (every 5 min)')
    parser.add_argument('--since',     metavar='ISO',       help='Show spend since ISO timestamp')
    parser.add_argument('--recommend', action='store_true', help='Routing improvement recommendations')
    parser.add_argument('--json',      action='store_true', help='Output as JSON')
    args = parser.parse_args()

    try:
        mode = "watch" if args.watch else ("recommend" if args.recommend else "report")
        set_running(SCOUT_NAME, {"mode": mode})
        if args.watch:
            cmd_watch(as_json=args.json)
        elif args.recommend:
            cmd_recommend(as_json=args.json)
        elif args.since:
            dt = datetime.fromisoformat(args.since)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            cmd_report(since_override=dt, as_json=args.json)
        else:
            # Default: --report
            cmd_report(as_json=args.json)
        set_idle(SCOUT_NAME, {"mode": mode, "status": "ok"})
    except psycopg2.OperationalError as e:
        set_error(SCOUT_NAME, str(e))
        print(f"❌ DB connection failed: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        set_idle(SCOUT_NAME, {"mode": mode, "status": "interrupted"})
        print("\n👋 rate_scout stopped.")

if __name__ == '__main__':
    main()
