"""Beta agent card — GPU compute / local inference."""
import os


def build_agent_card() -> dict:
    return {
        "name": "Beta",
        "description": "GPU compute agent. Runs local LLM inference via Ollama and GPU-accelerated tasks.",
        "url": os.getenv("AGENT_URL", "http://localhost:8200"),
        "skills": [
            {"id": "gpu_inference", "name": "GPU Inference", "description": "Run LLM inference on local GPU via Ollama"},
            {"id": "model_list", "name": "Model List", "description": "List available local models"},
            {"id": "hashcat_identify", "name": "Hash Identify", "description": "Identify hash type for cracking"},
            {"id": "memory_write", "name": "Memory Write", "description": "Store key/value to agent memory"},
            {"id": "memory_read", "name": "Memory Read", "description": "Read from agent memory"},
        ],
        "capabilities": {"streaming": False, "push_notifications": False},
        "protocol_version": "0.3.0",
    }
