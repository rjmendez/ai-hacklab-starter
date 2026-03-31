"""
Beta agent skill handlers — GPU compute and local inference via Ollama.

Requires:
  - Ollama running at OLLAMA_HOST (default: http://localhost:11434)
  - Models pulled: ollama pull llama3.1:70b, etc.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def _ollama_post(path: str, payload: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        f"{OLLAMA_HOST}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ollama_get(path: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(f"{OLLAMA_HOST}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def handle_gpu_inference(input_data: dict) -> dict:
    """
    Run a prompt through a local Ollama model.
    Input:  {"model": "llama3.1:70b", "prompt": "...", "max_tokens": 1024}
    Output: {"status": "ok", "model": "...", "response": "...", "tokens": N}
    """
    model     = input_data.get("model", "llama3.1:70b")
    prompt    = input_data.get("prompt", "")
    max_tokens = input_data.get("max_tokens", 1024)

    if not prompt:
        return {"status": "error", "message": "prompt required"}

    try:
        result = _ollama_post("/api/generate", {
            "model":  model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        })
        return {
            "status":   "ok",
            "model":    result.get("model", model),
            "response": result.get("response", ""),
            "tokens":   result.get("eval_count", 0),
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def handle_model_list(input_data: dict) -> dict:
    """
    List available local Ollama models.
    Input:  {}
    Output: {"status": "ok", "models": ["llama3.1:70b", ...], "count": N}
    """
    try:
        result = _ollama_get("/api/tags")
        models = [m["name"] for m in result.get("models", [])]
        return {"status": "ok", "models": models, "count": len(models)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# Hash type identification patterns
_HASH_PATTERNS = [
    # (regex pattern, name, hashcat mode)
    (r"^\$2[aby]\$\d{2}\$",          "bcrypt",       3200),
    (r"^\$krb5tgs\$",                 "Kerberos TGS", 13100),
    (r"^\$krb5asrep\$",               "Kerberos AS",  18200),
    (r"^\$NETNTLMv2\$|^[^:]+::[^:]+:[0-9a-fA-F]{16}:[0-9a-fA-F]{32}:", "NetNTLMv2", 5600),
    (r"^[0-9a-fA-F]{128}$",           "SHA-512",      1700),
    (r"^[0-9a-fA-F]{64}$",            "SHA-256",      1400),
    (r"^[0-9a-fA-F]{40}$",            "SHA-1",        100),
    (r"^[0-9A-F]{32}$",               "NTLM",         1000),
    (r"^[0-9a-f]{32}$",               "MD5",          0),
    (r"^[0-9a-fA-F]{32}:[0-9a-fA-F]{32}$", "LM:NTLM", 3000),
]


def handle_hashcat_identify(input_data: dict) -> dict:
    """
    Identify the likely type(s) of a hash value.
    Input:  {"hash": "5f4dcc3b5aa765d61d8327deb882cf99"}
    Output: {"hash": "...", "likely_types": ["MD5"], "hashcat_modes": [0]}
    """
    hash_val = input_data.get("hash", "").strip()
    if not hash_val:
        return {"status": "error", "message": "hash required"}

    matches = []
    modes   = []
    for pattern, name, mode in _HASH_PATTERNS:
        if re.match(pattern, hash_val):
            matches.append(name)
            modes.append(mode)

    if not matches:
        matches = ["unknown"]
        modes   = []

    return {
        "status":        "ok",
        "hash":          hash_val[:64] + ("..." if len(hash_val) > 64 else ""),
        "length":        len(hash_val),
        "likely_types":  matches,
        "hashcat_modes": modes,
    }


BETA_SKILL_HANDLERS = {
    "gpu_inference":    handle_gpu_inference,
    "model_list":       handle_model_list,
    "hashcat_identify": handle_hashcat_identify,
}
