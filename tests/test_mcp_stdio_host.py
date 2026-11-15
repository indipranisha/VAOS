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
        if self.path == "/api/v1/admin/integrations/mcp-policy":
            self._reply({
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
            })
        elif self.path == "/api/v1/openapi.json":
            self._reply({"paths": {"/health": {"get": {"x-vaos-group": "diagnostics"}}}})
        elif self.path == "/api/v1/health":
            self._reply({"status": "ok", "service": "synthetic-vaos-fixture"})
        else:
            self._reply({"detail": "fixture route not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        type(self).seen_authorization.append(self.headers.get("Authorization", ""))
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/api/v1/admin/observability/mcp/invocations":
            item = json.loads(body.decode("utf-8"))
            type(self).seen_telemetry.append(item)
            self._reply({"ok": True})
        else:
            self._reply({"detail": "fixture route not found"}, 404)


@unittest.skipUnless(SDK_AVAILABLE, "install requirements-mcp.txt to run the real stdio host integration")
class McpStdioHostIntegrationTests(unittest.TestCase):
    def test_official_host_lists_tools_reads_resource_and_calls_health(self) -> None:
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        _FixtureApiHandler.seen_authorization = []
        _FixtureApiHandler.seen_telemetry = []
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


if __name__ == "__main__":
    unittest.main()
