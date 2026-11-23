"""The MCP bridge must honor policy without requiring the optional runtime package."""
from __future__ import annotations

from contextlib import closing
import importlib.util
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch


class _FakeMCPServer:
    def __init__(self, *_args, **_kwargs):
        pass

    def tool(self):
        return lambda function: function

    def resource(self, *_args, **_kwargs):
        return lambda function: function

    def run(self):
        return None


class McpBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_mcp = types.ModuleType("mcp")
        fake_server = types.ModuleType("mcp.server")
        fake_server.MCPServer = _FakeMCPServer
        fake_mcp.server = fake_server
        cls.modules_patch = patch.dict(sys.modules, {"mcp": fake_mcp, "mcp.server": fake_server})
        cls.modules_patch.start()
        source = Path(__file__).resolve().parents[1] / "mcp_server.py"
        spec = importlib.util.spec_from_file_location("vaos_test_mcp_server", source)
        cls.bridge = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.bridge
        spec.loader.exec_module(cls.bridge)

    def setUp(self):
        self.telemetry_directory = tempfile.TemporaryDirectory(prefix="vaos-mcp-telemetry-test-")
        self.addCleanup(self.telemetry_directory.cleanup)
        self.bridge.MCP_TELEMETRY_DB_PATH = Path(self.telemetry_directory.name) / "mcp-telemetry.sqlite3"

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.bridge.__name__, None)
        cls.modules_patch.stop()

    def test_policy_failure_returns_actionable_error_without_api_call(self):
        with patch.object(self.bridge, "_authorize_operation", side_effect=RuntimeError("API access is disabled in Settings.")), patch.object(self.bridge, "_api_request") as request, patch.object(self.bridge, "_record_tool_invocation"):
            result = json.loads(self.bridge.vaos_get_system_health())
        self.assertTrue(result["error"])
        self.assertIn("API access is disabled", result["detail"])
        request.assert_not_called()

    def test_denied_operation_remains_blocked_and_redacts_token(self):
        with patch.object(self.bridge, "TOKEN", "vaos_fixture_secret"), patch.object(self.bridge, "_authorize_operation", side_effect=PermissionError("Denied vaos_fixture_secret")), patch.object(self.bridge, "_api_request") as request, patch.object(self.bridge, "_record_tool_invocation"):
            result = json.loads(self.bridge.vaos_api_request("GET", "/api/v1/admin/ml/samples"))
        self.assertEqual(result["status"], 403)
        self.assertNotIn("vaos_fixture_secret", result["detail"])
        request.assert_not_called()

    def test_sensitive_read_authorization_has_no_uninitialized_method_bug(self):
        policy = {
            "api_enabled": True, "mcp_enabled": True, "token_mcp_agent": True,
            "token_groups": ["people"], "allowed_groups": ["people"],
            "allow_sensitive_reads": True, "token_sensitive_read": True,
            "read_only": True, "token_scope": "read",
        }
        with patch.object(self.bridge, "_policy", return_value=policy), patch.object(
            self.bridge, "_operation_for", return_value={"x-vaos-group": "people"}
        ):
            self.bridge._authorize_operation("GET", "/api/v1/admin/participants")

    def test_mcp_cannot_approve_or_manage_approval_inbox(self):
        policy = {
            "api_enabled": True, "mcp_enabled": True, "token_mcp_agent": True,
            "token_groups": ["settings"], "allowed_groups": ["settings"],
            "allow_sensitive_reads": False, "token_sensitive_read": False,
            "read_only": False, "token_scope": "control",
        }
        with patch.object(self.bridge, "_policy", return_value=policy):
            with self.assertRaisesRegex(PermissionError, "only an administrator"):
                self.bridge._authorize_operation(
                    "POST", "/api/v1/admin/mcp-approvals/abc/decision"
                )

    def test_integration_destination_update_is_sensitive(self):
        self.assertTrue(self.bridge._is_sensitive_action("PUT", "/api/v1/admin/observability/integrations/destination-fixture"))

    def test_mcp_token_must_be_explicitly_agent_bound(self):
        with patch.object(self.bridge, "_api_request", return_value='{"api_enabled":true,"mcp_enabled":true,"token_mcp_agent":false}'):
            with self.assertRaisesRegex(PermissionError, "not designated for MCP"):
                self.bridge._policy()

    def test_cross_origin_redirect_never_receives_mcp_bearer_token(self):
        received_authorization = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                received_authorization.append(self.headers.get("Authorization", ""))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, _format, *_args):
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            with patch.object(self.bridge, "BASE_URL", f"http://127.0.0.1:{redirect.server_port}"), patch.object(
                self.bridge, "TOKEN", "vaos_redirect_fixture_token"
            ):
                result = self.bridge._api_request("GET", "/api/v1/health")
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=2)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)

        self.assertEqual(json.loads(result)["status"], 302)
        self.assertEqual(received_authorization, [])

    def test_upload_and_binary_base64_limits_are_checked_before_decoding_or_authorizing(self):
        with patch.object(self.bridge, "MAX_REQUEST_BYTES", 3), patch.object(
            self.bridge.base64, "b64decode", side_effect=AssertionError("must reject before decode")
        ), patch.object(self.bridge, "_authorize_operation") as authorize, patch.object(
            self.bridge, "_record_tool_invocation"
        ):
            with self.assertRaisesRegex(ValueError, "100 MB MCP request limit"):
                self.bridge.vaos_upload_file(
                    "/api/v1/admin/datasets", "file", "sample.zip", "application/zip", "AAAAA"
                )
            with self.assertRaisesRegex(ValueError, "100 MB MCP request limit"):
                self.bridge.vaos_send_binary("POST", "/api/v1/camera/frame", "image/jpeg", "AAAAA")
        authorize.assert_not_called()

    def test_upload_and_binary_metadata_json_have_a_small_predecode_bound(self):
        with patch.object(self.bridge, "MAX_METADATA_JSON_BYTES", 4), patch.object(
            self.bridge, "_authorize_operation"
        ) as authorize, patch.object(self.bridge, "_record_tool_invocation"):
            with self.assertRaisesRegex(ValueError, "fields_json exceeds"):
                self.bridge.vaos_upload_file(
                    "/api/v1/admin/datasets", "file", "sample.zip", "application/zip", "AA==", '{"x":1}'
                )
            with self.assertRaisesRegex(ValueError, "headers_json exceeds"):
                self.bridge.vaos_send_binary(
                    "POST", "/api/v1/camera/frame", "image/jpeg", "AA==", '{"x":1}'
                )
        authorize.assert_not_called()

    def test_json_body_size_is_checked_before_parsing_or_authorizing(self):
        with patch.object(self.bridge, "MAX_REQUEST_BYTES", 3), patch.object(
            self.bridge, "_authorize_operation"
        ) as authorize, patch.object(self.bridge, "_record_tool_invocation"):
            with self.assertRaisesRegex(ValueError, "body_json exceeds"):
                self.bridge.vaos_api_request("POST", "/api/v1/admin/cameras", "{\"x\":1}")
        authorize.assert_not_called()

    def test_log_search_forwards_exact_session_and_correlation_ids(self):
        with patch.object(self.bridge, "_observability_read", side_effect=lambda path: path), patch.object(
            self.bridge, "_record_tool_invocation"
        ):
            result = self.bridge.vaos_search_logs(session_id="session/one", correlation_id="request /one", training_job_id="job-42", dataset_id="idd-lite", model_id="best.pt", event_type="hardware.metric_sample_gap")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result).query)
        self.assertEqual(query["correlation_id"], ["request /one"])
        self.assertEqual(query["session_id"], ["session/one"])
        self.assertEqual(query["training_job_id"], ["job-42"])
        self.assertEqual(query["dataset_id"], ["idd-lite"])
        self.assertEqual(query["model_id"], ["best.pt"])
        self.assertEqual(query["event_type"], ["hardware.metric_sample_gap"])

    def test_metric_query_forwards_group_aggregation_bucket_and_exact_dimensions(self):
        with patch.object(self.bridge, "_observability_read", side_effect=lambda path: path), patch.object(
            self.bridge, "_record_tool_invocation"
        ):
            result = self.bridge.vaos_query_metric(
                metric_key="training.box_loss", after="2026-11-12T00:00:00Z", before="2026-11-13T00:00:00Z",
                limit=100, dimensions_json='{"dataset_id":"idd-lite"}', aggregation="average", interval="5min",
                group_by="training_job_id",
            )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result).query)
        self.assertEqual(query["metric"], ["training.box_loss"])
        self.assertEqual(query["aggregation"], ["average"])
        self.assertEqual(query["interval"], ["5min"])
        self.assertEqual(query["group_by"], ["training_job_id"])
        self.assertEqual(query["dimensions"], ['{"dataset_id":"idd-lite"}'])

    def test_tool_wrapper_records_success_and_preserves_result(self):
        with patch.object(self.bridge, "_record_tool_invocation") as record:
            wrapped = self.bridge._instrument_tool("vaos_get_system_health")(
                lambda: '{"ok":true}'
            )
            self.assertEqual(wrapped(), '{"ok":true}')
        record.assert_called_once()
        name, outcome, duration = record.call_args.args
        self.assertEqual((name, outcome), ("vaos_get_system_health", "success"))
        self.assertGreaterEqual(duration, 0)

    def test_tool_wrapper_records_error_payload_exception_and_nested_call_once(self):
        with patch.object(self.bridge, "_record_tool_invocation") as record:
            error_wrapper = self.bridge._instrument_tool("vaos_get_system_health")(
                lambda: '{"error":true,"status":403}'
            )
            self.assertEqual(error_wrapper(), '{"error":true,"status":403}')
        self.assertEqual(record.call_args.args[:2], ("vaos_get_system_health", "error"))

        record.reset_mock()
        def fail():
            raise RuntimeError("fixture error")
        with patch.object(self.bridge, "_record_tool_invocation") as record:
            wrapped = self.bridge._instrument_tool("vaos_search_logs")(fail)
            with self.assertRaisesRegex(RuntimeError, "fixture error"):
                wrapped()
        self.assertEqual(record.call_args.args[:2], ("vaos_search_logs", "error"))

        record.reset_mock()
        with patch.object(self.bridge, "_record_tool_invocation") as record:
            inner = self.bridge._instrument_tool("vaos_api_request")(lambda: '{"ok":true}')
            outer = self.bridge._instrument_tool("vaos_get_statistics")(inner)
            self.assertEqual(outer(), '{"ok":true}')
        record.assert_called_once()
        self.assertEqual(record.call_args.args[:2], ("vaos_get_statistics", "success"))

    def test_telemetry_failure_does_not_replace_tool_result(self):
        with patch.object(self.bridge, "_record_tool_invocation", side_effect=OSError("offline")), patch(
            "sys.stderr"
        ):
            wrapped = self.bridge._instrument_tool("vaos_get_system_health")(
                lambda: '{"healthy":true}'
            )
            self.assertEqual(wrapped(), '{"healthy":true}')

    def test_failed_invocation_report_retries_with_same_id_without_replaying_tool(self):
        posted = []

        def report(_method, _path, *, body, **_kwargs):
            payload = json.loads(body)
            posted.append(payload)
            if len(posted) == 1:
                return '{"error":true,"status":503}'
            return '{"ok":true,"duplicate":false}'

        tool_calls = []
        wrapped = self.bridge._instrument_tool("vaos_get_system_health")(
            lambda: tool_calls.append("called") or '{"healthy":true}'
        )
        with patch.object(self.bridge, "_api_request", side_effect=report), patch("sys.stderr"):
            self.assertEqual(wrapped(), '{"healthy":true}')
            first_id = posted[0]["invocation_id"]
            self.assertEqual(self.bridge._mcp_telemetry_queue_status()["pending_reports"], 1)
            self.assertEqual(wrapped(), '{"healthy":true}')

        self.assertEqual(tool_calls, ["called", "called"])
        self.assertEqual(posted[0]["invocation_id"], first_id)
        self.assertEqual(posted[1]["invocation_id"], first_id)
        self.assertEqual(len(posted[2]["invocation_id"]), 32)
        self.assertEqual(self.bridge._mcp_telemetry_queue_status(), {"pending_reports": 0, "dropped_reports": 0})

    def test_failed_invocation_report_survives_bridge_module_restart(self):
        posted = []

        def report(_method, _path, *, body, **_kwargs):
            posted.append(json.loads(body))
            if len(posted) == 1:
                return '{"error":true,"status":503}'
            return '{"ok":true,"duplicate":false}'

        with patch.object(self.bridge, "_api_request", side_effect=report), patch("sys.stderr"):
            self.bridge._record_tool_invocation("vaos_get_system_health", "success", 12.5)
        queued_id = posted[0]["invocation_id"]
        self.assertEqual(self.bridge._mcp_telemetry_queue_status()["pending_reports"], 1)

        source = Path(__file__).resolve().parents[1] / "mcp_server.py"
        spec = importlib.util.spec_from_file_location("vaos_test_mcp_server_restart", source)
        restarted = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = restarted
        self.addCleanup(lambda: sys.modules.pop(spec.name, None))
        spec.loader.exec_module(restarted)
        restarted.MCP_TELEMETRY_DB_PATH = self.bridge.MCP_TELEMETRY_DB_PATH
        with patch.object(restarted, "_api_request", side_effect=report), patch("sys.stderr"):
            restarted._flush_pending_invocations()

        self.assertEqual(len(posted), 2)
        self.assertEqual(posted[1]["invocation_id"], queued_id)
        self.assertEqual(restarted._mcp_telemetry_queue_status(), {"pending_reports": 0, "dropped_reports": 0})
        with closing(restarted._connect_mcp_telemetry_db()) as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(mcp_telemetry_queue)")}
        self.assertNotIn("arguments", columns)
        self.assertNotIn("result", columns)

    def test_local_queue_storage_failure_uses_bounded_retry_without_changing_tool_result(self):
        wrapped = self.bridge._instrument_tool("vaos_get_system_health")(
            lambda: '{"healthy":true}'
        )
        with patch.object(self.bridge, "_connect_mcp_telemetry_db", side_effect=OSError("local store unavailable")), patch.object(
            self.bridge, "_api_request", return_value='{"error":true,"status":503}'
        ), patch("sys.stderr"):
            self.assertEqual(wrapped(), '{"healthy":true}')
            self.assertEqual(len(self.bridge._EPHEMERAL_PENDING_INVOCATIONS), 1)

        with patch.object(self.bridge, "_connect_mcp_telemetry_db", side_effect=OSError("local store unavailable")), patch.object(
            self.bridge, "_api_request", return_value='{"ok":true,"duplicate":false}'
        ), patch("sys.stderr"):
            self.bridge._flush_pending_invocations()

        self.assertEqual(len(self.bridge._EPHEMERAL_PENDING_INVOCATIONS), 0)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not enforced by the Windows filesystem")
    def test_existing_publicly_readable_telemetry_file_is_rejected_without_chmod(self):
        self.bridge._connect_mcp_telemetry_db().close()
        path = self.bridge.MCP_TELEMETRY_DB_PATH
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "must be private"):
            self.bridge._connect_mcp_telemetry_db()
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_invocation_retry_queue_is_bounded_and_drops_oldest_on_overflow(self):
        with patch.object(self.bridge, "_MAX_PENDING_INVOCATIONS", 2), patch.object(
            self.bridge, "_api_request", return_value='{"error":true,"status":503}'
        ), patch("sys.stderr"):
            self.bridge._record_tool_invocation("vaos_get_system_health", "success", 1)
            first_id = self.bridge._oldest_pending_invocation()["invocation_id"]
            self.bridge._record_tool_invocation("vaos_get_system_health", "success", 2)
            self.bridge._record_tool_invocation("vaos_get_system_health", "success", 3)

        self.assertEqual(self.bridge._mcp_telemetry_queue_status(), {"pending_reports": 2, "dropped_reports": 1})
        self.assertNotEqual(self.bridge._oldest_pending_invocation()["invocation_id"], first_id)


if __name__ == "__main__":
    unittest.main()
