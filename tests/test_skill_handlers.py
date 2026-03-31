"""
tests/test_skill_handlers.py — Unit tests for all agent skill handlers.

These tests run offline — no Redis, Docker, network, or LLM required.
Skills that make external calls are tested for error handling (no network
responses needed to verify they return a well-formed dict).

Run:
    python -m pytest tests/ -v
    python -m pytest tests/ -v -k "alpha"
"""

import importlib
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Allow imports from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Helpers ───────────────────────────────────────────────────────────────────

def assert_ok(result: dict, test: "unittest.TestCase") -> None:
    """Assert the result dict has status=ok and no unexpected keys."""
    test.assertIsInstance(result, dict, "Handler must return a dict")
    test.assertIn("status", result, "Handler must return a 'status' key")
    test.assertEqual(result["status"], "ok", f"Expected status=ok, got: {result}")


def assert_error(result: dict, test: "unittest.TestCase") -> None:
    test.assertIsInstance(result, dict)
    test.assertEqual(result.get("status"), "error")
    test.assertIn("message", result)


# ── Alpha ─────────────────────────────────────────────────────────────────────

class TestAlphaHandlers(unittest.TestCase):

    def setUp(self):
        from agents.alpha.skill_handlers import (
            handle_task_status, handle_docker_status, handle_docker_restart,
            handle_docker_logs, handle_report_generation, handle_queue_status,
        )
        self.task_status       = handle_task_status
        self.docker_status     = handle_docker_status
        self.docker_restart    = handle_docker_restart
        self.docker_logs       = handle_docker_logs
        self.report_generation = handle_report_generation
        self.queue_status      = handle_queue_status

    def test_task_status_no_redis(self):
        """task_status must return a dict even with no Redis."""
        result = self.task_status({})
        self.assertIsInstance(result, dict)
        self.assertIn("status", result)

    def test_docker_status_called(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "my-container\tUp 2 hours\tnginx:alpine\n"
        with patch("subprocess.run", return_value=mock_result):
            result = self.docker_status({})
        assert_ok(result, self)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["containers"][0]["name"], "my-container")

    def test_docker_restart_missing_container(self):
        result = self.docker_restart({})
        assert_error(result, self)

    def test_docker_logs_missing_container(self):
        result = self.docker_logs({})
        assert_error(result, self)

    def test_docker_logs_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "2026-01-01 INFO starting up\n"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = self.docker_logs({"container": "agent-gamma"})
        assert_ok(result, self)
        self.assertIn("starting up", result["logs"])

    def test_report_generation_empty(self):
        result = self.report_generation({"title": "Test Report", "sections": []})
        assert_ok(result, self)
        self.assertIn("Test Report", result["report"])

    def test_report_generation_with_sections(self):
        result = self.report_generation({
            "title": "Findings",
            "sections": [
                {"heading": "Summary", "content": "Three issues found."},
                {"heading": "Details", "content": "See below."},
            ]
        })
        assert_ok(result, self)
        self.assertIn("Summary", result["report"])
        self.assertIn("Three issues found.", result["report"])

    def test_queue_status_no_redis(self):
        result = self.queue_status({})
        self.assertIn("status", result)


# ── Beta ──────────────────────────────────────────────────────────────────────

class TestBetaHandlers(unittest.TestCase):

    def setUp(self):
        from agents.beta.skill_handlers import (
            handle_gpu_inference, handle_model_list, handle_hashcat_identify,
        )
        self.gpu_inference    = handle_gpu_inference
        self.model_list       = handle_model_list
        self.hashcat_identify = handle_hashcat_identify

    def test_gpu_inference_missing_prompt(self):
        result = self.gpu_inference({"model": "llama3.1:70b"})
        assert_error(result, self)

    def test_gpu_inference_network_error(self):
        """Should return error dict, not raise, when Ollama is unreachable."""
        result = self.gpu_inference({"model": "llama3.1:70b", "prompt": "Hello"})
        self.assertIsInstance(result, dict)
        self.assertIn("status", result)

    def test_model_list_network_error(self):
        result = self.model_list({})
        self.assertIsInstance(result, dict)
        self.assertIn("status", result)

    def test_hashcat_identify_md5(self):
        result = self.hashcat_identify({"hash": "5f4dcc3b5aa765d61d8327deb882cf99"})
        assert_ok(result, self)
        self.assertIn("MD5", result["likely_types"])
        self.assertIn(0, result["hashcat_modes"])

    def test_hashcat_identify_sha256(self):
        result = self.hashcat_identify({"hash": "a" * 64})
        assert_ok(result, self)
        self.assertIn("SHA-256", result["likely_types"])

    def test_hashcat_identify_ntlm(self):
        result = self.hashcat_identify({"hash": "A" * 32})
        assert_ok(result, self)
        self.assertIn("NTLM", result["likely_types"])

    def test_hashcat_identify_bcrypt(self):
        result = self.hashcat_identify({"hash": "$2b$12$abcdefghijklmnopqrstuuVJPLFsMTkJFfxv0kVxfCJgIFT1d3O7K"})
        assert_ok(result, self)
        self.assertIn("bcrypt", result["likely_types"])

    def test_hashcat_identify_unknown(self):
        result = self.hashcat_identify({"hash": "not-a-real-hash"})
        assert_ok(result, self)
        self.assertIn("unknown", result["likely_types"])

    def test_hashcat_identify_empty(self):
        result = self.hashcat_identify({})
        assert_error(result, self)


# ── Gamma ─────────────────────────────────────────────────────────────────────

class TestGammaHandlers(unittest.TestCase):

    def setUp(self):
        from agents.gamma.skill_handlers import (
            handle_ct_enum, handle_web_fetch, handle_osint_research,
            handle_port_scan, handle_dns_lookup, handle_whois,
        )
        self.ct_enum        = handle_ct_enum
        self.web_fetch      = handle_web_fetch
        self.osint_research = handle_osint_research
        self.port_scan      = handle_port_scan
        self.dns_lookup     = handle_dns_lookup
        self.whois          = handle_whois

    def test_ct_enum_missing_domain(self):
        result = self.ct_enum({})
        assert_error(result, self)

    def test_ct_enum_network_error(self):
        """Should return error dict, not raise, when crt.sh unreachable."""
        import requests as req_mod
        with patch.object(req_mod, "get", side_effect=Exception("timeout")):
            result = self.ct_enum({"domain": "example.com"})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["status"], "error")

    def test_web_fetch_missing_url(self):
        result = self.web_fetch({})
        assert_error(result, self)

    def test_web_fetch_success(self):
        import requests as req_mod
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>hello</html>"
        with patch.object(req_mod, "get", return_value=mock_resp):
            result = self.web_fetch({"url": "https://example.com"})
        assert_ok(result, self)
        self.assertEqual(result["status_code"], 200)
        self.assertIn("hello", result["body"])

    def test_osint_research_cloud_hints(self):
        import requests as req_mod
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(req_mod, "get", return_value=mock_resp):
            result = self.osint_research({"target": "example.com", "org_name": "Example Corp"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("cloud_hints", result)
        self.assertTrue(any("example" in h for h in result["cloud_hints"]))

    def test_port_scan_missing_host(self):
        result = self.port_scan({})
        assert_error(result, self)

    def test_port_scan_all_closed(self):
        import socket
        with patch("socket.create_connection", side_effect=OSError("refused")):
            result = self.port_scan({"host": "192.0.2.1", "ports": [80, 443]})
        assert_ok(result, self)
        self.assertEqual(result["open_ports"], [])

    def test_dns_lookup_missing_domain(self):
        result = self.dns_lookup({})
        assert_error(result, self)

    def test_whois_missing_target(self):
        result = self.whois({})
        assert_error(result, self)

    def test_whois_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Domain Name: EXAMPLE.COM\nRegistrar: IANA"
        with patch("subprocess.run", return_value=mock_result):
            result = self.whois({"target": "example.com"})
        assert_ok(result, self)
        self.assertIn("EXAMPLE.COM", result["raw"])


# ── Delta ─────────────────────────────────────────────────────────────────────

class TestDeltaHandlers(unittest.TestCase):

    def setUp(self):
        from agents.delta.skill_handlers import (
            handle_db_query, handle_batch_process,
            handle_data_export, handle_redis_sync,
        )
        self.db_query      = handle_db_query
        self.batch_process = handle_batch_process
        self.data_export   = handle_data_export
        self.redis_sync    = handle_redis_sync

    def test_db_query_missing_db_path(self):
        result = self.db_query({"query": "SELECT 1"})
        assert_error(result, self)

    def test_db_query_missing_query(self):
        result = self.db_query({"db_path": "/tmp/test.db"})
        assert_error(result, self)

    def test_db_query_rejects_writes(self):
        result = self.db_query({"db_path": "/tmp/test.db", "query": "DROP TABLE foo"})
        assert_error(result, self)
        self.assertIn("allow_writes", result["message"])

    def test_db_query_in_memory(self):
        """Can run a SELECT against an in-memory SQLite DB."""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()
        # Use file-based DB for a real test
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")
            conn.commit()
            conn.close()
            result = self.db_query({"db_path": db_path, "query": "SELECT * FROM t"})
            assert_ok(result, self)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["rows"][0]["x"], 42)
        finally:
            os.unlink(db_path)

    def test_batch_process_missing_items(self):
        result = self.batch_process({"target_agent": "gamma", "skill_id": "ct_enum"})
        assert_error(result, self)

    def test_batch_process_missing_target(self):
        result = self.batch_process({"items": ["example.com"]})
        assert_error(result, self)

    def test_data_export_missing_data(self):
        result = self.data_export({"format": "csv"})
        assert_error(result, self)

    def test_data_export_json(self):
        import tempfile, os, json
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            result = self.data_export({
                "data": [{"name": "alpha", "port": 8201}, {"name": "beta", "port": 8202}],
                "format": "json",
                "output_path": path,
            })
            assert_ok(result, self)
            self.assertEqual(result["rows"], 2)
            data = json.loads(open(path).read())
            self.assertEqual(len(data), 2)
        finally:
            os.unlink(path)

    def test_data_export_csv(self):
        import tempfile, os, csv
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            result = self.data_export({
                "data": [{"name": "gamma", "skill": "ct_enum"}],
                "format": "csv",
                "output_path": path,
            })
            assert_ok(result, self)
            rows = list(csv.DictReader(open(path)))
            self.assertEqual(rows[0]["name"], "gamma")
        finally:
            os.unlink(path)

    def test_redis_sync_missing_target(self):
        result = self.redis_sync({"event_type": "full_sync", "keys_pattern": "mesh:*"})
        assert_error(result, self)


# ── Task Classifier ───────────────────────────────────────────────────────────

class TestTaskClassifier(unittest.TestCase):

    def setUp(self):
        from tools.model_router.task_classifier import classify_fast
        self.classify_fast = classify_fast

    def test_nano_disk_space(self):
        self.assertEqual(self.classify_fast("check disk space"), "nano")

    def test_nano_status(self):
        self.assertIn(self.classify_fast("what time is it"), ["nano", None])

    def test_strong_security(self):
        result = self.classify_fast("security audit of the authentication service")
        self.assertEqual(result, "strong")

    def test_strong_architecture(self):
        result = self.classify_fast("design a distributed system architecture")
        self.assertEqual(result, "strong")

    def test_mid_debug(self):
        result = self.classify_fast("fix bug in the parser function")
        self.assertEqual(result, "mid")

    def test_mini_format(self):
        result = self.classify_fast("format this JSON file")
        self.assertEqual(result, "mini")


if __name__ == "__main__":
    unittest.main(verbosity=2)
