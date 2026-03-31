"""
tools/model_router/task_classifier.py

Classify a task description into a complexity tier.

Fast-path: regex keyword heuristics — handles obvious cases with no LLM call.
Slow-path: LiteLLM (gemini-2.0-flash) for ambiguous descriptions.

Tiers:
  nano    — trivial ops, status checks, simple lookups
  mini    — simple scripts, boilerplate, single-step transforms
  mid     — debugging, refactoring, API tasks, multi-step ops (DEFAULT)
  strong  — architecture, security analysis, complex implementation
  premium — critical decisions (leave to LLM classification)
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm-proxy:4000")
LITELLM_API_KEY  = os.environ.get("LITELLM_API_KEY", "")
CLASSIFIER_MODEL = "gemini-2.0-flash"

TIERS = ["nano", "mini", "mid", "strong", "premium"]

# ---------------------------------------------------------------------------
# Keyword heuristics — fastest path, zero LLM cost
# ---------------------------------------------------------------------------

_NANO_PATTERNS = [
    r"\b(run|launch|open|start|kill|stop|restart)\b.{0,40}\b(app|process|service|cmd|terminal)\b",
    r"\b(check|show|display|print|list|what is|what'?s)\b.{0,60}\b(disk\s*space|memory|cpu|uptime|time|date|ip\s*address|hostname|pid|status)\b",
    r"\bwhat (time|day|date) is it\b",
    r"\becho\b",
    r"\bping\b.{0,30}\b\d{1,3}\.\d{1,3}\.",
    r"\b(ls|dir|pwd|whoami|uname|ps|top|df|du|free|ifconfig|ipconfig)\b",
    r"\bcheck if .{0,30} (is running|exists|is up)\b",
]

_MINI_PATTERNS = [
    r"\b(write|create|generate)\b.{0,40}\b(\.gitignore|\.env\.example|dockerfile|makefile|requirements\.txt)\b",
    r"\b(format|pretty.?print|lint|sort|deduplicate)\b.{0,60}\b(json|yaml|csv|xml|toml)\b",
    r"\b(convert|transform|translate)\b.{0,40}\b(format|encoding|case|string)\b",
    r"\badd\b.{0,40}\b(docstring|comment|type hint|type annotation)\b",
    r"\b(rename|move|copy)\b.{0,40}\b(file|folder|directory)\b",
    r"\bboilerplate\b",
    r"\bsimple\b.{0,30}\bscript\b",
]

_MID_PATTERNS = [
    r"\b(fix|debug|resolve)\b.{0,60}\b(bug|error|exception|issue|crash|failure)\b",
    r"\b(write|create|generate)\b.{0,40}\b(test|unit test|test suite)\b",
    r"\b(refactor|restructure|reorganize|clean up|optimize)\b.{0,60}\b(code|function|class|module)\b",
    r"\b(implement|build|create)\b.{0,40}\b(api|endpoint|pipeline|workflow|script|function|class)\b",
    r"\b(multi.?step|several steps|multiple)\b",
    r"\b(parse|extract|scrape)\b.{0,40}\b(html|xml|json|log|csv)\b",
    r"\b(integrate|connect|wire up)\b",
]

_STRONG_PATTERNS = [
    r"\b(design|architect|plan)\b.{0,60}\b(system|service|platform|infrastructure|database)\b",
    r"\b(security|vulnerability|threat|attack|exploit|pentest)\b.{0,60}\b(review|analysis|audit|assessment)\b",
    r"\b(complex|non.?trivial|sophisticated|advanced)\b.{0,60}\b(implementation|algorithm|logic)\b",
    r"\b(scale|distributed|concurrent|async|multi.?threaded)\b",
    r"\b(migrate|upgrade)\b.{0,60}\b(database|schema|api|major version)\b",
    r"\b(code review|pull request review)\b",
    r"\bfull.?stack\b",
]

_COMPILED: list[tuple[str, list]] = [
    ("nano",   [re.compile(p, re.IGNORECASE) for p in _NANO_PATTERNS]),
    ("mini",   [re.compile(p, re.IGNORECASE) for p in _MINI_PATTERNS]),
    ("mid",    [re.compile(p, re.IGNORECASE) for p in _MID_PATTERNS]),
    ("strong", [re.compile(p, re.IGNORECASE) for p in _STRONG_PATTERNS]),
]


def classify_fast(text: str) -> Optional[str]:
    """
    Classify via keyword heuristics. Returns tier string or None if ambiguous.
    Checks tiers in order — first match wins.
    """
    for tier, patterns in _COMPILED:
        for pat in patterns:
            if pat.search(text):
                logger.debug("classify_fast: tier=%s matched pattern=%s", tier, pat.pattern[:40])
                return tier
    return None


def classify_with_llm(text: str) -> str:
    """
    Ask the LLM to classify. Returns one of: nano, mini, mid, strong, premium.
    Falls back to "mid" on any failure.
    """
    prompt = (
        "Classify the following task into exactly one complexity tier.\n\n"
        "Tiers:\n"
        "  nano    — trivial: status checks, simple commands, time/date queries\n"
        "  mini    — simple: boilerplate, formatting, single-step scripts\n"
        "  mid     — standard: debugging, refactoring, API tasks, multi-step ops\n"
        "  strong  — complex: architecture, security analysis, complex implementation\n"
        "  premium — critical: major architectural decisions, high-stakes security analysis\n\n"
        f"Task: {text}\n\n"
        "Reply with ONLY one word: nano, mini, mid, strong, or premium."
    )

    payload = json.dumps({
        "model":    CLASSIFIER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 10,
        "temperature": 0.0,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{LITELLM_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {LITELLM_API_KEY}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data    = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip().lower()
            tier    = content.split()[0] if content else "mid"
            if tier in TIERS:
                logger.debug("classify_with_llm: tier=%s", tier)
                return tier
    except Exception as exc:
        logger.warning("classify_with_llm failed: %s", exc)

    return "mid"


def classify(text: str, use_llm: bool = True) -> str:
    """
    Classify a task description into a complexity tier.

    1. Try fast keyword heuristics first (no LLM cost)
    2. If ambiguous and use_llm=True, ask the LLM
    3. Default to "mid" if everything fails
    """
    tier = classify_fast(text)
    if tier is not None:
        return tier
    if use_llm:
        return classify_with_llm(text)
    return "mid"
