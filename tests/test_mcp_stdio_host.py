"""Wire-level MCP host smoke test for the optional official SDK.

This launches the real VAOS MCP bridge as a stdio child process and connects
with the official SDK client. A loopback fixture supplies only a synthetic
policy, OpenAPI document, health response and telemetry acknowledgement; it
does not contact or modify a running VAOS installation.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest


SDK_AVAILABLE = importlib.util.find_spec("mcp") is not None
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SCRIPT = PROJECT_ROOT / "mcp_server.py"
TEST_TOKEN = "vaos_mcp_stdio_fixture_token"


class _FixtureApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_authorization: list[str] = []
    seen_telemetry: list[dict[str, object]] = []
    seen_tool_requests: list[dict[str, object]] = []
    policy: dict[str, object] = {}
    approval_id = "vaos-fixture-approval-id-00001"
    approval_status = "pending"
    approval_body: bytes | None = None

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _reply(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        type(self).seen_authorization.append(self.headers.get("Authorization", ""))
        path = self.path.split("?", 1)[0]
        if path == "/api/v1/admin/integrations/mcp-policy":
            self._reply(dict(type(self).policy))
        elif self.path == "/api/v1/openapi.json":
            self._reply({"paths": {
                "/health": {"get": {"x-vaos-group": "diagnostics"}},
                "/admin/events": {"get": {"x-vaos-group": "events"}},
                "/admin/tracking/rules": {"post": {"x-vaos-group": "events"}},
                "/admin/tracking/rules/{rule_id}": {"delete": {"x-vaos-group": "events"}},
                "/admin/mcp-approvals/{approval_id}": {"get": {"x-vaos-group": "settings"}},
                "/admin/api-tokens": {"post": {"x-vaos-group": "settings"}},
                "/admin/mcp-approvals/{approval_id}/decision": {"post": {"x-vaos-group": "settings"}},
            }})
        elif path == "/api/v1/health":
            self._reply({"status": "ok", "service": "synthetic-vaos-fixture"})
        elif path == "/api/v1/admin/events":
            type(self).seen_tool_requests.append({"method": "GET", "path": path})
            self._reply({"events": []})
        elif path == f"/api/v1/admin/mcp-approvals/{type(self).approval_id}":
            type(self).seen_tool_requests.append({"method": "GET", "path": path})
            self._reply({"request_id": type(self).approval_id, "status": type(self).approval_status})
        else:
            self._reply({"detail": "fixture route not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        type(self).seen_authorization.append(self.headers.get("Authorization", ""))
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/api/v1/admin/observability/mcp/invocations":
            item = json.loads(body.decode("utf-8"))
            type(self).seen_telemetry.append(item)
            self._reply({"ok": True})
        elif self.path == "/api/v1/admin/tracking/rules":
            type(self).seen_tool_requests.append({
                "method": "POST", "path": self.path,
                "approval_id": self.headers.get("X-vaos-mcp-approval-id", ""),
                "body": body,
            })
            approval_id = self.headers.get("X-vaos-mcp-approval-id", "")
            if not approval_id:
                type(self).approval_body = body
                type(self).approval_status = "pending"
                self._reply({"detail": {
                    "approval_required": True,
                    "request_id": type(self).approval_id,
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "preview": {"method": "POST", "path": "/api/admin/tracking/rules", "risk": "sensitive action"},
                }}, 428)
            elif approval_id != type(self).approval_id:
                self._reply({"detail": "Approval not found for this token."}, 404)
            elif type(self).approval_status != "approved":
                self._reply({"detail": f"Approval is {type(self).approval_status}."}, 409)
            elif body != type(self).approval_body:
                self._reply({"detail": "Approved request fingerprint did not match."}, 409)
            else:
                type(self).approval_status = "used"
                self._reply({"rule": {"name": "fixture rule"}, "message": "Rule saved."}, 201)
        else:
            self._reply({"detail": "fixture route not found"}, 404)


@unittest.skipUnless(SDK_AVAILABLE, "install requirements-mcp.txt to run the real stdio host integration")
class McpStdioHostIntegrationTests(unittest.TestCase):
    def test_official_host_lists_tools_reads_resource_and_calls_health(self) -> None:
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        _FixtureApiHandler.seen_authorization = []
        _FixtureApiHandler.seen_telemetry = []
        _FixtureApiHandler.seen_tool_requests = []
        _FixtureApiHandler.policy = {
            "api_enabled": True,
            "mcp_enabled": True,
            "token_mcp_agent": True,
            "token_groups": ["diagnostics"],
            "allowed_groups": ["diagnostics"],
            "allow_sensitive_reads": False,
            "token_sensitive_read": False,
            "read_only": True,
            "token_scope": "read",
            "allow_sensitive": False,
            "allow_deletes": False,
        }
        _FixtureApiHandler.approval_status = "pending"
        _FixtureApiHandler.approval_body = None
        api = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureApiHandler)
        api_thread = Thread(target=api.serve_forever, daemon=True)
        api_thread.start()
        self.addCleanup(api_thread.join, 2)
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)

        with tempfile.TemporaryDirectory(prefix="vaos-mcp-stdio-") as state_dir:
            # StdioServerParameters.env replaces, rather than overlays, the
            # child's environment. Pass only the runtime variables needed to
            # launch Python and locate installed packages, then add the
            # synthetic VAOS fixture values. Do not forward unrelated secrets.
            env = {
                key: os.environ[key]
                for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONPATH", "VIRTUAL_ENV")
                if os.environ.get(key)
            }
            env.update({
                "VAOS_BASE_URL": f"http://127.0.0.1:{api.server_port}",
                "VAOS_API_TOKEN": TEST_TOKEN,
                "VAOS_MCP_TELEMETRY_DB": str(Path(state_dir) / "telemetry.sqlite3"),
            })
            server = StdioServerParameters(
                command=sys.executable,
                args=[str(BRIDGE_SCRIPT)],
                env=env,
            )

            async def exercise_host() -> None:
                async with Client(server) as host:
                    listed = await host.list_tools()
                    names = {tool.name for tool in listed.tools}
                    self.assertIn("vaos_get_system_health", names)
                    self.assertIn("vaos_api_request", names)

                    response = await host.call_tool("vaos_get_system_health", {})
                    contents = [getattr(block, "text", "") for block in response.content]
                    self.assertTrue(any('"service":"synthetic-vaos-fixture"' in value for value in contents))

                    resources = await host.list_resources()
                    uris = {str(resource.uri) for resource in resources.resources}
                    self.assertIn("vaos://openapi-v1", uris)
                    policy = await host.read_resource("vaos://agent-policy")
                    policy_text = "\n".join(getattr(block, "text", "") for block in policy.contents)
                    self.assertIn('"token_mcp_agent": true', policy_text)

            asyncio.run(exercise_host())

        self.assertTrue(_FixtureApiHandler.seen_authorization)
        self.assertEqual(set(_FixtureApiHandler.seen_authorization), {f"Bearer {TEST_TOKEN}"})
        self.assertTrue(_FixtureApiHandler.seen_telemetry)
        self.assertTrue(all("duration_ms" in item and "tool" in item for item in _FixtureApiHandler.seen_telemetry))
        self.assertTrue(all("arguments" not in item and "content" not in item for item in _FixtureApiHandler.seen_telemetry))

    def test_sdk_host_enforces_policy_and_retries_only_the_approved_request(self) -> None:
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        _FixtureApiHandler.seen_authorization = []
        _FixtureApiHandler.seen_telemetry = []
        _FixtureApiHandler.seen_tool_requests = []
        _FixtureApiHandler.policy = {
            "api_enabled": True, "mcp_enabled": True, "token_mcp_agent": True,
            "token_groups": ["diagnostics"], "allowed_groups": ["diagnostics"],
            "allow_sensitive_reads": False, "token_sensitive_read": False,
            "read_only": True, "token_scope": "read",
            "allow_sensitive": False, "allow_deletes": False,
        }
        _FixtureApiHandler.approval_status = "pending"
        _FixtureApiHandler.approval_body = None
        api = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureApiHandler)
        api_thread = Thread(target=api.serve_forever, daemon=True)
        api_thread.start()
        self.addCleanup(api_thread.join, 2)
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)

        with tempfile.TemporaryDirectory(prefix="vaos-mcp-policy-stdio-") as state_dir:
            env = {
                key: os.environ[key]
                for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PYTHONPATH", "VIRTUAL_ENV")
                if os.environ.get(key)
            }
            env.update({
                "VAOS_BASE_URL": f"http://127.0.0.1:{api.server_port}",
                "VAOS_API_TOKEN": TEST_TOKEN,
                "VAOS_MCP_TELEMETRY_DB": str(Path(state_dir) / "telemetry.sqlite3"),
            })
            server = StdioServerParameters(command=sys.executable, args=[str(BRIDGE_SCRIPT)], env=env)

            async def exercise_host() -> None:
                async with Client(server) as host:
                    names = {tool.name for tool in (await host.list_tools()).tools}
                    self.assertTrue({"vaos_api_request", "vaos_check_approval", "vaos_get_system_health"}.issubset(names))
                    event_path = "/api/v1/admin/events"
                    rule_path = "/api/v1/admin/tracking/rules"
                    payload_json = '{"name":"fixture rule","enabled":true}'

                    def result_json(result) -> dict[str, object]:
                        text = " ".join(getattr(item, "text", "") for item in result.content)
                        value = json.loads(text)
                        self.assertIsInstance(value, dict)
                        return value

                    denied_group = await host.call_tool("vaos_api_request", {"method": "GET", "path": event_path})
                    denied_group_json = result_json(denied_group)
                    self.assertTrue(denied_group_json["error"])
                    self.assertIn("does not grant the 'events'", str(denied_group_json["detail"]))

                    invalid_path = await host.call_tool("vaos_api_request", {
                        "method": "GET", "path": "/api/v1/not-in-openapi",
                    })
                    invalid_path_json = result_json(invalid_path)
                    self.assertTrue(invalid_path_json["error"])
                    self.assertIn("not present in the VAOS API reference", str(invalid_path_json["detail"]))
                    self.assertFalse(_FixtureApiHandler.seen_tool_requests)

                    _FixtureApiHandler.policy.update({
                        "token_groups": ["diagnostics", "events"],
                        "allowed_groups": ["diagnostics", "events"],
                    })
                    denied_read_only = await host.call_tool("vaos_api_request", {
                        "method": "POST", "path": rule_path, "body_json": payload_json,
                    })
                    denied_read_only_json = result_json(denied_read_only)
                    self.assertTrue(denied_read_only_json["error"])
                    self.assertIn("read-only mode", str(denied_read_only_json["detail"]))
                    self.assertFalse(_FixtureApiHandler.seen_tool_requests)

                    _FixtureApiHandler.policy.update({
                        "token_groups": ["diagnostics", "events", "settings"],
                        "allowed_groups": ["diagnostics", "events", "settings"],
                        "read_only": False,
                        "token_scope": "control",
                        "allow_sensitive": True,
                    })
                    denied_private_read = await host.call_tool("vaos_api_request", {"method": "GET", "path": event_path})
                    denied_private_read_json = result_json(denied_private_read)
                    self.assertTrue(denied_private_read_json["error"])
                    self.assertIn("can reveal profiles", str(denied_private_read_json["detail"]))
                    self.assertFalse(_FixtureApiHandler.seen_tool_requests)

                    _FixtureApiHandler.policy.update({"allow_sensitive_reads": True, "token_sensitive_read": True})
                    permitted_private_read = await host.call_tool("vaos_api_request", {"method": "GET", "path": event_path})
                    self.assertEqual(result_json(permitted_private_read), {"events": []})

                    approval_result = await host.call_tool("vaos_api_request", {
                        "method": "POST", "path": rule_path, "body_json": payload_json,
                    })
                    approval = result_json(approval_result)
                    self.assertTrue(approval["error"])
                    self.assertEqual(approval["status"], 428)
                    self.assertTrue(approval["approval_required"])

                    pending = await host.call_tool("vaos_check_approval", {"approval_id": _FixtureApiHandler.approval_id})
                    self.assertEqual(result_json(pending)["status"], "pending")
                    _FixtureApiHandler.approval_status = "approved"
                    retried = await host.call_tool("vaos_api_request", {
                        "method": "POST", "path": rule_path, "body_json": payload_json,
                        "approval_id": _FixtureApiHandler.approval_id,
                    })
                    self.assertEqual(result_json(retried)["rule"]["name"], "fixture rule")
                    self.assertEqual(_FixtureApiHandler.approval_status, "used")
                    posted_requests = [item for item in _FixtureApiHandler.seen_tool_requests if item["method"] == "POST"]
                    self.assertEqual(len(posted_requests), 2)
                    self.assertEqual(posted_requests[0]["body"], posted_requests[1]["body"])
                    self.assertEqual(posted_requests[1]["approval_id"], _FixtureApiHandler.approval_id)

                    replay = await host.call_tool("vaos_api_request", {
                        "method": "POST", "path": rule_path, "body_json": payload_json,
                        "approval_id": _FixtureApiHandler.approval_id,
                    })
                    replay_json = result_json(replay)
                    self.assertTrue(replay_json["error"])
                    self.assertEqual(replay_json["status"], 409)

                    denied_secret_management = await host.call_tool("vaos_api_request", {
                        "method": "POST", "path": "/api/v1/admin/api-tokens", "body_json": "{}",
                    })
                    secret_management_json = result_json(denied_secret_management)
                    self.assertTrue(secret_management_json["error"])
                    self.assertIn("cannot manage API secrets", str(secret_management_json["detail"]))
                    self.assertFalse(any("api-tokens" in str(item.get("path", "")) for item in _FixtureApiHandler.seen_tool_requests))

                    denied_delete = await host.call_tool("vaos_api_request", {
                        "method": "DELETE", "path": "/api/v1/admin/tracking/rules/1",
                    })
                    self.assertIn("Deletion and revoke actions are disabled", str(result_json(denied_delete)["detail"]))
                    self.assertFalse(any(item.get("method") == "DELETE" for item in _FixtureApiHandler.seen_tool_requests))

                    denied_self_approval = await host.call_tool("vaos_api_request", {
                        "method": "POST", "path": f"/api/v1/admin/mcp-approvals/{_FixtureApiHandler.approval_id}/decision",
                        "body_json": '{"decision":"approve"}',
                    })
                    self.assertIn("only an administrator can approve or deny", str(result_json(denied_self_approval)["detail"]))
                    self.assertFalse(any("/decision" in str(item.get("path", "")) for item in _FixtureApiHandler.seen_tool_requests))

            asyncio.run(exercise_host())

        self.assertTrue(_FixtureApiHandler.seen_telemetry)
        self.assertTrue(all("arguments" not in item and "content" not in item for item in _FixtureApiHandler.seen_telemetry))
        self.assertTrue(any(item.get("outcome") == "error" for item in _FixtureApiHandler.seen_telemetry))
        self.assertTrue(_FixtureApiHandler.seen_authorization)
        self.assertEqual(set(_FixtureApiHandler.seen_authorization), {f"Bearer {TEST_TOKEN}"})


if __name__ == "__main__":
    unittest.main()
