"""
tools/model_router/model_selector.py

Maps complexity tiers to model fallback chains and executes chat completion
requests via a LiteLLM proxy (OpenAI-compatible API).

Config (env vars):
  LITELLM_BASE_URL  — proxy URL (default: http://litellm-proxy:4000)
  LITELLM_API_KEY   — proxy API key

Tiers: nano → mini → mid → strong → premium
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm-proxy:4000")
LITELLM_API_KEY  = os.environ.get("LITELLM_API_KEY", "")

# ---------------------------------------------------------------------------
# Tier → model fallback chains (cheapest → best within tier)
# ---------------------------------------------------------------------------
TIER_MODELS: dict[str, list[str]] = {
    "nano":    ["gemini-2.0-flash", "gpt-4.1-nano"],
    "mini":    ["gpt-4.1-nano", "gemini-2.0-flash", "gpt-4.1-mini"],
    "mid":     ["gpt-4.1-mini", "gemini-2.5-flash"],
    "strong":  ["claude-sonnet-4-5", "gemini-2.5-flash"],
    "premium": ["claude-opus-4-5", "claude-sonnet-4-5"],
}

TIER_ORDER = ["nano", "mini", "mid", "strong", "premium"]
DEFAULT_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Low-level LiteLLM call (no external deps — uses stdlib urllib)
# ---------------------------------------------------------------------------
def _call_model(
    model: str,
    messages: list[dict],
    max_tokens: int = 2048,
    temperature: float = 0.3,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[Optional[str], Optional[dict]]:
    """
    Call LiteLLM for a chat completion.

    Returns:
        (content_str, usage_dict) on success
        (None, None) on any failure
    """
    payload = json.dumps({
        "model":       model,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
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

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data    = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            usage   = data.get("usage", {})
            latency = int((time.monotonic() - t0) * 1000)
            logger.debug("model=%s latency=%dms tokens=%s", model, latency, usage.get("total_tokens"))
            return content, usage
    except (urllib.error.HTTPError, urllib.error.URLError,
            KeyError, json.JSONDecodeError, TimeoutError) as exc:
        logger.warning("_call_model failed model=%s: %s", model, exc)
        return None, None


# ---------------------------------------------------------------------------
# Tier-based model selection with fallback
# ---------------------------------------------------------------------------
def select_model(
    tier: str,
    messages: list[dict],
    exclude: Optional[list[str]] = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> tuple[str, str, dict]:
    """
    Try each model in the tier's chain until one succeeds.

    Returns:
        (model_used, content, usage)

    Raises:
        RuntimeError if all models in the chain fail.
    """
    exclude = set(exclude or [])
    models  = [m for m in TIER_MODELS.get(tier, TIER_MODELS["mid"]) if m not in exclude]

    if not models:
        raise RuntimeError(f"No models available for tier={tier!r} after exclusions")

    for model in models:
        content, usage = _call_model(model, messages, max_tokens, temperature)
        if content is not None:
            return model, content, usage or {}

    raise RuntimeError(
        f"All models failed for tier={tier!r}: {models}"
    )


# ---------------------------------------------------------------------------
# Tier escalation
# ---------------------------------------------------------------------------
def escalate_tier(current_tier: str) -> str:
    """Return the next tier up. Never escalates past premium."""
    try:
        idx = TIER_ORDER.index(current_tier)
    except ValueError:
        idx = 2  # default to mid
    return TIER_ORDER[min(idx + 1, len(TIER_ORDER) - 1)]
