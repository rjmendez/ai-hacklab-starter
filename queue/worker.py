"""
queue/worker.py — Queue worker for the agent mesh.

Runs a BRPOP loop on the agent's inbox, routes to registered skill handlers,
replies with results, and dead-letters messages after MAX_RETRIES failures.

Usage:
    python3 queue/worker.py --agent alpha
    python3 queue/worker.py --agent gamma --handlers-module agents.gamma.skill_handlers
    SKILL_HANDLERS_MODULE=agents.alpha.skill_handlers python3 queue/worker.py --agent alpha
"""

import argparse
import importlib
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from queue.mesh_queue import MeshQueue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("queue-worker")

MAX_RETRIES = 3


def load_skill_handlers(
    module_path: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> dict[str, Callable]:
    """
    Load skill handlers from a dotted module path.

    Looks for, in order:
      1. <AGENT_NAME_UPPER>_SKILL_HANDLERS in the module
      2. SKILL_HANDLERS in the module
    """
    if not module_path:
        return {}
    try:
        mod = importlib.import_module(module_path)
        # Try agent-specific dict first
        if agent_name:
            attr = f"{agent_name.upper()}_SKILL_HANDLERS"
            handlers = getattr(mod, attr, None)
            if isinstance(handlers, dict):
                logger.info("Loaded %d handlers from %s.%s", len(handlers), module_path, attr)
                return handlers
        # Generic fallback
        handlers = getattr(mod, "SKILL_HANDLERS", None)
        if isinstance(handlers, dict):
            logger.info("Loaded %d handlers from %s.SKILL_HANDLERS", len(handlers), module_path)
            return handlers
        logger.warning("No SKILL_HANDLERS dict found in %s", module_path)
        return {}
    except ImportError as exc:
        logger.error("Could not import %s: %s", module_path, exc)
        return {}


class Worker:
    def __init__(
        self,
        agent_name: str,
        redis_host: Optional[str] = None,
        redis_port: Optional[int] = None,
        redis_password: Optional[str] = None,
        skill_handlers: Optional[dict[str, Callable]] = None,
    ):
        self.agent_name = agent_name
        self.skill_handlers: dict[str, Callable] = skill_handlers or {}
        self.queue = MeshQueue(agent_name, redis_host, redis_port, redis_password)
        self._running = True
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

    def _shutdown(self, signum, frame):
        logger.info("[worker:%s] Shutdown signal received", self.agent_name)
        self._running = False

    def run(self) -> None:
        logger.info("[worker:%s] Starting — %d skill handlers: %s",
                    self.agent_name, len(self.skill_handlers), list(self.skill_handlers.keys()))
        while self._running:
            try:
                msg = self.queue.receive(timeout=5)
                if msg is None:
                    continue
                self._handle_message(msg)
            except Exception as exc:
                logger.exception("[worker:%s] Unexpected error in loop: %s", self.agent_name, exc)
                time.sleep(1)
        logger.info("[worker:%s] Stopped", self.agent_name)

    def _handle_message(self, msg: dict) -> None:
        skill_id = msg.get("skill_id", "")
        msg_id   = msg.get("id", "?")
        attempts = msg.get("attempts", 0)

        handler = self.skill_handlers.get(skill_id)
        if handler is None:
            logger.warning("[worker:%s] Unknown skill=%s id=%s", self.agent_name, skill_id, msg_id)
            self.queue.dead_letter(msg, f"unknown skill: {skill_id}")
            return

        t0 = time.monotonic()
        try:
            result = self._process_message(msg, handler)
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.info("[worker:%s] skill=%s id=%s latency=%dms ok",
                        self.agent_name, skill_id, msg_id, latency_ms)
            self.queue.reply(msg, result)
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("[worker:%s] skill=%s id=%s attempt=%d failed: %s",
                           self.agent_name, skill_id, msg_id, attempts + 1, exc)
            if attempts + 1 >= MAX_RETRIES:
                logger.error("[worker:%s] skill=%s id=%s dead-lettering after %d attempts",
                             self.agent_name, skill_id, msg_id, MAX_RETRIES)
                self.queue.dead_letter(msg, str(exc))
            else:
                # Re-queue with incremented attempt counter
                msg["attempts"] = attempts + 1
                self.queue.r.lpush(
                    f"mesh:inbox:{self.agent_name}",
                    json.dumps(msg),
                )

    def _process_message(self, msg: dict, handler: Callable) -> Any:
        input_data = msg.get("input", {})
        return handler(input_data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mesh queue worker")
    parser.add_argument("--agent",           required=True, help="Agent name (e.g. alpha)")
    parser.add_argument("--redis-host",      default=None)
    parser.add_argument("--redis-port",      default=None, type=int)
    parser.add_argument("--handlers-module", default=os.environ.get("SKILL_HANDLERS_MODULE"),
                        help="Dotted module path to skill handlers")
    parser.add_argument("--verbose",         action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    handlers = load_skill_handlers(args.handlers_module, args.agent)

    # Also try auto-loading from agents/<agent>/skill_handlers.py
    if not handlers:
        auto_module = f"agents.{args.agent}.skill_handlers"
        handlers = load_skill_handlers(auto_module, args.agent)

    worker = Worker(
        agent_name=args.agent,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        skill_handlers=handlers,
    )
    worker.run()


if __name__ == "__main__":
    main()
