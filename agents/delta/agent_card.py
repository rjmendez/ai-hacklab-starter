"""Delta agent card — batch processing and data ops."""
import os


def build_agent_card() -> dict:
    return {
        "name": "Delta",
        "description": "Batch processing and data ops agent. DB queries, data export, archival, reporting.",
        "url": os.getenv("AGENT_URL", "http://localhost:8200"),
        "skills": [
            {"id": "db_query", "name": "DB Query", "description": "Run parameterized queries against configured databases"},
            {"id": "batch_process", "name": "Batch Process", "description": "Process large datasets in chunks"},
            {"id": "data_export", "name": "Data Export", "description": "Export data to CSV, JSON, or SQLite"},
            {"id": "report_archive", "name": "Report Archive", "description": "Archive completed reports to storage"},
            {"id": "memory_write", "name": "Memory Write", "description": "Store key/value to agent memory"},
            {"id": "memory_read", "name": "Memory Read", "description": "Read from agent memory"},
        ],
        "capabilities": {"streaming": False, "push_notifications": False},
        "protocol_version": "0.3.0",
    }
