"""
Delta agent skill handlers — batch processing and data operations.

Handles database queries, batch task dispatch, data export, and Redis sync.
"""

import csv
import io
import json
import logging
import os
import sqlite3
import sys

logger = logging.getLogger(__name__)


def handle_db_query(input_data: dict) -> dict:
    """
    Run a parameterized SQLite query.
    Restricted to SELECT by default — set allow_writes=true for mutations.

    Input:  {"db_path": "/path/to.db", "query": "SELECT ...", "params": [], "allow_writes": false}
    Output: {"status": "ok", "rows": [...], "count": N}
    """
    db_path      = input_data.get("db_path", "")
    query        = input_data.get("query", "").strip()
    params       = input_data.get("params", [])
    allow_writes = input_data.get("allow_writes", False)

    if not db_path:
        return {"status": "error", "message": "db_path required"}
    if not query:
        return {"status": "error", "message": "query required"}

    # Safety gate: reject write ops unless explicitly allowed
    first_word = query.split()[0].upper() if query.split() else ""
    if first_word not in ("SELECT", "PRAGMA", "EXPLAIN", "WITH") and not allow_writes:
        return {
            "status": "error",
            "message": f"Write operation ({first_word}) rejected. Set allow_writes=true to permit.",
        }

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        rows   = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return {"status": "ok", "rows": rows, "count": len(rows)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def handle_batch_process(input_data: dict) -> dict:
    """
    Send a list of items in chunks to another agent via MeshQueue.

    Input: {
        "items":        ["item1", "item2", ...],
        "chunk_size":   50,
        "target_agent": "gamma",
        "skill_id":     "ct_enum",
        "input_key":    "domain"   -- key to map each item into the skill input dict
    }
    Output: {"status": "ok", "total": N, "chunks": N, "results": [...], "errors": [...]}
    """
    items        = input_data.get("items", [])
    chunk_size   = max(1, int(input_data.get("chunk_size", 50)))
    target_agent = input_data.get("target_agent", "")
    skill_id     = input_data.get("skill_id", "")
    input_key    = input_data.get("input_key", "value")

    if not items:
        return {"status": "error", "message": "items required"}
    if not target_agent or not skill_id:
        return {"status": "error", "message": "target_agent and skill_id required"}

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    try:
        from queue.mesh_queue import MeshQueue
    except ImportError:
        return {"status": "error", "message": "MeshQueue not available — check queue/mesh_queue.py"}

    agent_name = os.environ.get("AGENT_NAME", "delta")
    q          = MeshQueue(agent_name)

    chunks   = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]
    msg_ids  = []
    errors   = []
    results  = []

    for chunk in chunks:
        for item in chunk:
            try:
                msg_id = q.send(target_agent, skill_id, {input_key: item})
                msg_ids.append(msg_id)
            except Exception as exc:
                errors.append({"item": str(item)[:100], "error": str(exc)})

    return {
        "status":  "ok",
        "total":   len(items),
        "chunks":  len(chunks),
        "sent":    len(msg_ids),
        "errors":  errors,
        "note":    f"Replies will arrive at mesh:inbox:{agent_name}",
    }


def handle_data_export(input_data: dict) -> dict:
    """
    Export a list of dicts to a CSV or JSON file.
    Input:  {"data": [...], "format": "csv|json", "output_path": "/tmp/export.csv"}
    Output: {"status": "ok", "output_path": "...", "rows": N, "bytes": N}
    """
    data        = input_data.get("data", [])
    fmt         = input_data.get("format", "json").lower()
    output_path = input_data.get("output_path", "/tmp/export." + fmt)

    if not data:
        return {"status": "error", "message": "data required"}
    if not isinstance(data, list) or not isinstance(data[0], dict):
        return {"status": "error", "message": "data must be a list of dicts"}

    try:
        if fmt == "csv":
            fieldnames = list(data[0].keys())
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data)
            content = buf.getvalue().encode("utf-8")
        else:
            content = json.dumps(data, indent=2, default=str).encode("utf-8")

        with open(output_path, "wb") as f:
            f.write(content)

        return {
            "status":      "ok",
            "output_path": output_path,
            "format":      fmt,
            "rows":        len(data),
            "bytes":       len(content),
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def handle_redis_sync(input_data: dict) -> dict:
    """
    Sync Redis keys matching a pattern between two Redis instances.
    Input: {
        "event_type":      "full_sync|push|pull",
        "keys_pattern":    "mesh:*",
        "target_host":     "100.x.x.x",
        "target_port":     6379,
        "target_password": ""
    }
    Output: {"status": "ok", "synced": N, "errors": [...]}
    """
    import redis

    event_type      = input_data.get("event_type", "full_sync")
    keys_pattern    = input_data.get("keys_pattern", "mesh:*")
    target_host     = input_data.get("target_host", "")
    target_port     = int(input_data.get("target_port", 6379))
    target_password = input_data.get("target_password", "") or None

    if not target_host:
        return {"status": "error", "message": "target_host required"}

    try:
        src = redis.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=False,
            socket_connect_timeout=5,
        )
        dst = redis.Redis(
            host=target_host, port=target_port,
            password=target_password,
            decode_responses=False,
            socket_connect_timeout=5,
        )
        src.ping(); dst.ping()
    except Exception as exc:
        return {"status": "error", "message": f"Redis connect failed: {exc}"}

    synced = 0
    errors = []

    for key in src.scan_iter(keys_pattern, count=100):
        try:
            ttl   = src.ttl(key)
            value = src.dump(key)
            if value is not None:
                dst.restore(key, max(0, ttl * 1000) if ttl > 0 else 0, value, replace=True)
                synced += 1
        except Exception as exc:
            errors.append({"key": key.decode() if isinstance(key, bytes) else key, "error": str(exc)})

    return {
        "status":      "ok",
        "event_type":  event_type,
        "pattern":     keys_pattern,
        "synced":      synced,
        "errors":      errors[:20],  # cap error list
    }


DELTA_SKILL_HANDLERS = {
    "db_query":      handle_db_query,
    "batch_process": handle_batch_process,
    "data_export":   handle_data_export,
    "redis_sync":    handle_redis_sync,
}
