"""
queue/mesh_queue.py — Redis-backed async message queue for the agent mesh.

Each agent has an inbox at Redis key: mesh:inbox:<agent_name>
Messages are JSON-serialized envelopes with full metadata.

Usage:
    from queue.mesh_queue import MeshQueue

    q = MeshQueue("alpha")
    msg_id = q.send("gamma", "ct_enum", {"domain": "example.com"})
    msg = q.receive(timeout=10)
    q.reply(msg, {"subdomains": [...]})
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import redis
from redis.exceptions import ConnectionError as RedisConnectionError

logger = logging.getLogger(__name__)

_INBOX_KEY  = "mesh:inbox:{agent}"
_DL_KEY     = "mesh:dead_letter:{agent}"


class MeshQueue:
    """Redis-backed message queue for one agent in the mesh."""

    def __init__(
        self,
        agent_name: str,
        redis_host: Optional[str] = None,
        redis_port: Optional[int] = None,
        redis_password: Optional[str] = None,
    ):
        self.agent_name    = agent_name
        self._redis_host   = redis_host   or os.environ.get("REDIS_HOST", "redis")
        self._redis_port   = redis_port   or int(os.environ.get("REDIS_PORT", "6379"))
        self._redis_pw     = redis_password or os.environ.get("REDIS_PASSWORD", "") or None
        self._client: Optional[redis.Redis] = None
        self._connect()

    # ── Connection ────────────────────────────────────────────────────────

    def _connect(self) -> None:
        pool = redis.ConnectionPool(
            host=self._redis_host,
            port=self._redis_port,
            password=self._redis_pw,
            db=0,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=30,
        )
        self._client = redis.Redis(connection_pool=pool)
        self._ping_with_retry()

    def _ping_with_retry(self, max_wait: int = 60) -> None:
        delay = 1
        waited = 0
        while True:
            try:
                self._client.ping()
                logger.info("[mesh-queue:%s] Connected to Redis at %s:%d",
                            self.agent_name, self._redis_host, self._redis_port)
                return
            except RedisConnectionError as exc:
                if waited >= max_wait:
                    raise RuntimeError(
                        f"[mesh-queue:{self.agent_name}] Could not connect to Redis "
                        f"at {self._redis_host}:{self._redis_port} after {waited}s"
                    ) from exc
                logger.warning("[mesh-queue:%s] Redis unavailable, retrying in %ds: %s",
                               self.agent_name, delay, exc)
                time.sleep(delay)
                waited += delay
                delay = min(delay * 2, 10)

    @property
    def r(self) -> redis.Redis:
        return self._client  # type: ignore

    # ── Messaging ─────────────────────────────────────────────────────────

    def send(
        self,
        to_agent: str,
        skill_id: str,
        input_data: dict[str, Any],
        reply_to: Optional[str] = None,
    ) -> str:
        """
        Send a task to another agent's inbox.
        Returns the message ID.
        """
        msg_id = str(uuid.uuid4())
        envelope = {
            "id":         msg_id,
            "from":       self.agent_name,
            "to":         to_agent,
            "skill_id":   skill_id,
            "input":      input_data,
            "reply_to":   reply_to or _INBOX_KEY.format(agent=self.agent_name),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "attempts":   0,
        }
        inbox = _INBOX_KEY.format(agent=to_agent)
        self.r.lpush(inbox, json.dumps(envelope))
        logger.debug("[mesh-queue:%s] → %s skill=%s id=%s",
                     self.agent_name, to_agent, skill_id, msg_id)
        return msg_id

    def receive(self, timeout: int = 10) -> Optional[dict]:
        """
        Block until a message arrives on this agent's inbox.
        Returns None on timeout.
        """
        inbox = _INBOX_KEY.format(agent=self.agent_name)
        result = self.r.brpop(inbox, timeout=timeout)
        if result is None:
            return None
        _, raw = result
        try:
            msg = json.loads(raw)
            logger.debug("[mesh-queue:%s] ← from=%s skill=%s id=%s",
                         self.agent_name, msg.get("from"), msg.get("skill_id"), msg.get("id"))
            return msg
        except json.JSONDecodeError as exc:
            logger.warning("[mesh-queue:%s] Failed to parse message: %s", self.agent_name, exc)
            return None

    def reply(self, original_msg: dict, result: Any) -> str:
        """Send a result back to the original sender's reply_to queue."""
        reply_to = original_msg.get("reply_to") or _INBOX_KEY.format(agent=original_msg.get("from", "alpha"))
        msg_id = str(uuid.uuid4())
        envelope = {
            "id":           msg_id,
            "from":         self.agent_name,
            "to":           original_msg.get("from", "unknown"),
            "skill_id":     f"{original_msg.get('skill_id', 'unknown')}:reply",
            "input":        {},
            "result":       result,
            "reply_to_id":  original_msg.get("id"),
            "created_at":   datetime.now(timezone.utc).isoformat(),
            "attempts":     0,
        }
        self.r.lpush(reply_to, json.dumps(envelope))
        return msg_id

    def dead_letter(self, msg: dict, reason: str) -> None:
        """Move a failed message to the dead letter queue."""
        msg["attempts"] = msg.get("attempts", 0) + 1
        msg["dead_letter_reason"] = reason
        msg["dead_letter_at"] = datetime.now(timezone.utc).isoformat()
        dl_key = _DL_KEY.format(agent=self.agent_name)
        self.r.lpush(dl_key, json.dumps(msg))
        self.r.ltrim(dl_key, 0, 499)  # keep last 500
        logger.warning("[mesh-queue:%s] Dead-lettered msg id=%s reason=%s",
                       self.agent_name, msg.get("id"), reason)

    # ── Inspection ────────────────────────────────────────────────────────

    def queue_depth(self, agent: Optional[str] = None) -> int:
        """Return the number of messages waiting in an inbox."""
        target = agent or self.agent_name
        return self.r.llen(_INBOX_KEY.format(agent=target))

    def dead_letter_depth(self, agent: Optional[str] = None) -> int:
        target = agent or self.agent_name
        return self.r.llen(_DL_KEY.format(agent=target))
