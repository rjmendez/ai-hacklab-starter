#!/usr/bin/env python3
"""
agent-factory/blueprints/analysis-worker/worker.py

Blueprint: Binary / code analysis worker (radare2 + static analysis).

Spawns as a standalone agent. Reads analysis tasks from a Redis queue,
runs radare2 or other static analysis tools, returns structured results.

Environment:
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD — queue connection
    RADARE2_BIN                            — path to radare2 (default: r2)
    AGENT_NAME                             — defaults to "analysis-worker"

Usage:
    python3 agent-factory/blueprints/analysis-worker/worker.py
    python3 agent-factory/blueprints/analysis-worker/worker.py --dry-run
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [analysis-worker] %(levelname)s %(message)s")
log = logging.getLogger("analysis-worker")

AGENT_NAME  = os.environ.get("AGENT_NAME", "analysis-worker")
RADARE2_BIN = os.environ.get("RADARE2_BIN", "r2")
REDIS_HOST  = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT  = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASS  = os.environ.get("REDIS_PASSWORD") or None
QUEUE_KEY   = f"mesh:inbox:{AGENT_NAME}"


def _get_redis():
    import redis
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS,
                       decode_responses=True, socket_connect_timeout=5)


# ── Task handlers ─────────────────────────────────────────────────────────────

def handle_r2_info(task: dict) -> dict:
    """
    Get file info and imports via radare2.
    Input: {"file_path": "/path/to/binary"}
    """
    file_path = task.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        return {"status": "error", "message": f"File not found: {file_path}"}

    try:
        # r2 -q -c 'ij; iij' /path/to/binary (info + imports, JSON output, quiet mode)
        result = subprocess.run(
            [RADARE2_BIN, "-q", "-c", "ij; iij", file_path],
            capture_output=True, text=True, timeout=60,
        )
        info_json = {}
        imports   = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if "core" in parsed:
                    info_json = parsed
                elif isinstance(parsed, list) and parsed and "name" in parsed[0]:
                    imports = [i.get("name") for i in parsed if i.get("name")]
            except json.JSONDecodeError:
                pass

        return {
            "status":   "ok",
            "file":     file_path,
            "info":     info_json,
            "imports":  imports[:50],  # cap at 50
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "message": "r2 exceeded 60s"}
    except FileNotFoundError:
        return {"status": "error", "message": f"radare2 not found at: {RADARE2_BIN}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def handle_strings_extract(task: dict) -> dict:
    """
    Extract strings from a binary.
    Input: {"file_path": "/path/to/binary", "min_len": 6, "limit": 500}
    """
    file_path = task.get("file_path", "")
    min_len   = int(task.get("min_len", 6))
    limit     = int(task.get("limit", 500))

    if not file_path or not os.path.exists(file_path):
        return {"status": "error", "message": f"File not found: {file_path}"}

    try:
        result = subprocess.run(
            ["strings", f"--bytes={min_len}", file_path],
            capture_output=True, text=True, timeout=30,
        )
        strings = [s.strip() for s in result.stdout.splitlines() if s.strip()]
        return {
            "status":    "ok",
            "file":      file_path,
            "count":     len(strings),
            "strings":   strings[:limit],
            "truncated": len(strings) > limit,
        }
    except FileNotFoundError:
        return {"status": "error", "message": "strings utility not found"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def handle_entropy_check(task: dict) -> dict:
    """
    Check file entropy (high entropy may indicate packing/encryption/secrets).
    Input: {"file_path": "/path/to/file"}
    """
    file_path = task.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        return {"status": "error", "message": f"File not found: {file_path}"}

    import math
    try:
        data     = open(file_path, "rb").read()
        size     = len(data)
        if size == 0:
            return {"status": "error", "message": "Empty file"}

        counts   = [0] * 256
        for byte in data:
            counts[byte] += 1

        entropy  = 0.0
        for c in counts:
            if c > 0:
                p = c / size
                entropy -= p * math.log2(p)

        return {
            "status":  "ok",
            "file":    file_path,
            "size":    size,
            "entropy": round(entropy, 4),
            "rating":  "high" if entropy > 7.0 else ("medium" if entropy > 5.0 else "low"),
            "note":    "Entropy > 7.0 may indicate packing, encryption, or embedded secrets",
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


TASK_HANDLERS = {
    "r2_info":         handle_r2_info,
    "strings_extract": handle_strings_extract,
    "entropy_check":   handle_entropy_check,
}


# ── Worker loop ───────────────────────────────────────────────────────────────

def run_worker(dry_run: bool = False) -> None:
    r = _get_redis()
    log.info("Analysis worker ready — listening on %s (dry_run=%s)", QUEUE_KEY, dry_run)

    while True:
        try:
            item = r.brpop(QUEUE_KEY, timeout=30)
            if not item:
                continue

            _, raw    = item
            msg       = json.loads(raw)
            task_type = msg.get("skill_id", "")
            task_data = msg.get("input", {})
            reply_key = msg.get("reply_key", "")
            msg_id    = msg.get("id", "unknown")

            log.info("Task %s: type=%s", msg_id, task_type)

            if dry_run:
                log.info("DRY RUN — would process %s", task_type)
                continue

            handler = TASK_HANDLERS.get(task_type)
            if not handler:
                result = {"status": "error", "message": f"Unknown task type: {task_type}"}
            else:
                t0     = time.monotonic()
                result = handler(task_data)
                result["latency_ms"] = int((time.monotonic() - t0) * 1000)

            if reply_key:
                r.lpush(reply_key, json.dumps({
                    "id":     msg_id,
                    "result": result,
                    "from":   AGENT_NAME,
                }))
                r.expire(reply_key, 3600)

            log.info("Task %s complete: status=%s", msg_id, result.get("status"))

        except KeyboardInterrupt:
            log.info("Worker shutting down.")
            break
        except Exception as exc:
            log.error("Worker error: %s", exc)
            time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analysis worker blueprint")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_worker(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
