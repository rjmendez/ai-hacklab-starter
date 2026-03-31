#!/usr/bin/env python3
"""
agent-factory/blueprints/gpu-worker/worker.py

Blueprint: GPU compute worker (hashcat + local inference).

Spawns as a standalone agent. Reads tasks from a Redis queue,
routes to hashcat or Ollama based on task type, reports results back.

Environment:
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD — queue connection
    OLLAMA_HOST                            — Ollama inference endpoint
    AGENT_NAME                             — defaults to "gpu-worker"
    HASHCAT_BIN                            — path to hashcat binary

Usage:
    python3 agent-factory/blueprints/gpu-worker/worker.py
    python3 agent-factory/blueprints/gpu-worker/worker.py --dry-run
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [gpu-worker] %(levelname)s %(message)s")
log = logging.getLogger("gpu-worker")

AGENT_NAME   = os.environ.get("AGENT_NAME", "gpu-worker")
OLLAMA_HOST  = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
HASHCAT_BIN  = os.environ.get("HASHCAT_BIN", "hashcat")
REDIS_HOST   = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT   = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASS   = os.environ.get("REDIS_PASSWORD") or None
QUEUE_KEY    = f"mesh:inbox:{AGENT_NAME}"


def _get_redis():
    import redis
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS,
                       decode_responses=True, socket_connect_timeout=5)


# ── Task handlers ─────────────────────────────────────────────────────────────

def handle_hashcat(task: dict) -> dict:
    """
    Run hashcat against a hash list.
    Input: {"hashes": ["hash1", ...], "mode": 0, "wordlist": "/path/to/wordlist.txt"}
    """
    hashes   = task.get("hashes", [])
    mode     = int(task.get("mode", 0))
    wordlist = task.get("wordlist", "/usr/share/wordlists/rockyou.txt")

    if not hashes:
        return {"status": "error", "message": "hashes required"}

    # Write hashes to temp file
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("\n".join(hashes))
        hash_file = f.name

    try:
        result = subprocess.run(
            [HASHCAT_BIN, "-m", str(mode), hash_file, wordlist,
             "--quiet", "--outfile-format=2", "--status-timer=10"],
            capture_output=True, text=True, timeout=300,
        )
        cracked = [l.strip() for l in result.stdout.splitlines() if ":" in l]
        return {
            "status":        "ok",
            "mode":          mode,
            "submitted":     len(hashes),
            "cracked":       len(cracked),
            "results":       cracked,
            "returncode":    result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "message": "hashcat exceeded 300s"}
    except FileNotFoundError:
        return {"status": "error", "message": f"hashcat not found at: {HASHCAT_BIN}"}
    finally:
        os.unlink(hash_file)


def handle_inference(task: dict) -> dict:
    """
    Run inference via local Ollama.
    Input: {"model": "llama3.1:70b", "prompt": "...", "max_tokens": 1024}
    """
    import urllib.request
    model     = task.get("model", "llama3.1:70b")
    prompt    = task.get("prompt", "")
    max_tokens = task.get("max_tokens", 1024)

    if not prompt:
        return {"status": "error", "message": "prompt required"}

    payload = json.dumps({
        "model":  model, "prompt": prompt, "stream": False,
        "options": {"num_predict": max_tokens},
    }).encode("utf-8")

    req = urllib.request.Request(f"{OLLAMA_HOST}/api/generate", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
            return {
                "status":   "ok",
                "model":    data.get("model", model),
                "response": data.get("response", ""),
                "tokens":   data.get("eval_count", 0),
            }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


TASK_HANDLERS = {
    "hashcat":   handle_hashcat,
    "inference": handle_inference,
}


# ── Worker loop ───────────────────────────────────────────────────────────────

def run_worker(dry_run: bool = False) -> None:
    r = _get_redis()
    log.info("GPU worker ready — listening on %s (dry_run=%s)", QUEUE_KEY, dry_run)

    while True:
        try:
            item = r.brpop(QUEUE_KEY, timeout=30)
            if not item:
                continue

            _, raw = item
            msg = json.loads(raw)

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
    parser = argparse.ArgumentParser(description="GPU worker blueprint")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_worker(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
