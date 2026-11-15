# Connect an MCP host to VAOS

This guide is for an administrator connecting an AI agent that supports local MCP servers. The MCP bridge is an optional child process. It talks to VAOS through the same authenticated, versioned REST API used by the API Explorer; it does not open a new network port.

## Compatibility status

| Host / environment | Status |
| --- | --- |
| Official MCP Python SDK v2.3 `Client` using stdio | Passed locally on Windows/Python 3.12 against a synthetic loopback API fixture: the test launches `mcp_server.py` as a real subprocess, discovers tools/resources, reads policy, calls health, and checks token forwarding and argument-free telemetry. The separate GitHub Actions matrix is configured for Windows, macOS and Ubuntu but has not run; a green CI result is required before marking those runner combinations verified. |
| Branded desktop/editor agent hosts | Not independently certified in this repository. Use the host's local stdio-server configuration and secret-storage guidance; a generic example appears below. Host-specific UI, config location and secret handling vary by product and version. |
| Remote/public MCP endpoint | Not provided. The VAOS bridge is local stdio only. Do not expose the process or its token through a public tunnel. |

The local SDK subprocess test verifies protocol transport and the bridge/API boundary. It does not authenticate to a production VAOS installation, validate an agent's UI, prove branded host compatibility, or verify GPU/camera behavior.

The bridge does not follow HTTP redirects while carrying its API bearer token. Configure `VAOS_BASE_URL` to the canonical VAOS origin; correct any reverse-proxy redirect at the configured origin instead of relying on the client to follow it.

## Before connecting

1. Use Python 3.10 or newer for MCP Python SDK v2.3. The documented VAOS Windows setup uses Python 3.12, and the dependency is bounded to the 2.3 SDK minor (`requirements-mcp.txt`).
2. Install the optional dependency into the interpreter the MCP host will launch. The main VAOS service does not require this package:

   **Windows PowerShell**

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
   ```

   **macOS / Linux**

   ```sh
   .venv/bin/python -m pip install -r requirements-mcp.txt
   ```

3. In **Settings → API and AI agent access**, enable API access and MCP. New installations default to **Diagnostics** only; keep **Read-only mode** enabled for the first connection. Add other capability groups only when the agent needs them.
4. Create an expiring **read** API token and mark **Use this token for an MCP agent**. Copy its secret once. A regular API token that is not marked for MCP cannot use the bridge.
5. In Settings, use **Agent configuration template** to copy the correct Python interpreter and absolute `mcp_server.py` path for this installation.

## Configure the host

Add the Settings-generated VAOS entry to the MCP host's local server configuration. This example shows the expected shape, not the config file location for a particular host:

```json
{
  "mcpServers": {
    "vaos": {
      "command": "/absolute/path/to/VAOS/.venv/bin/python",
      "args": ["/absolute/path/to/VAOS/mcp_server.py"],
      "env": {
        "VAOS_BASE_URL": "http://127.0.0.1:8000",
        "VAOS_API_TOKEN": "<read-token-from-host-secret-storage>"
      }
    }
  }
}
```

On Windows, the generated command usually looks like `C:\\path\\to\\VAOS\\.venv\\Scripts\\python.exe`. For a local instance, `VAOS_BASE_URL` is the loopback origin with no path, query or trailing API prefix. A remote instance must use HTTPS and be reachable from the machine running the host. Do not put the token in a repository, shared screenshot, synced public file or chat. If the host has no secret manager, restrict its config file to your OS account and rotate the token if the file may have been copied.

Save the host settings and reload/restart that host. VAOS does not need a service restart for a Settings policy change, but a running host process may need to be reloaded to read changed configuration.

## Safe first use

Start by asking the agent only to inspect system health, list report types or read a permitted diagnostic. The available tools and resources are listed in [the MCP bridge reference](MCP.md). A tool being visible does not grant access: every call rechecks the token groups, MCP groups, read-only flag and sensitive-data policy.

To permit a write later:

1. Create a separate control token marked for MCP; keep the read token for monitoring.
2. Disable MCP read-only only for the time and task required. Select the smallest necessary groups.
3. Keep sensitive reads, sensitive actions and deletion disabled unless that specific job needs them.
4. Review each exact-request approval in **Settings → API and AI agent access → Agent action approvals**. Approve once only when the method, target and safe preview match your intended task.
5. The agent must retry the identical request with the approval ID. A changed request, expired approval or replay is rejected.
6. Revoke the control token when the job is finished.

Never ask an agent to enroll a person or access biometric information without consent and authorization. MCP cannot approve its own requests or change VAOS integration settings.

## Disable access

Turn MCP off in Settings to reject bridge calls, or revoke the API token to immediately invalidate that connection. Removing the host entry prevents future launches. The bridge's small local telemetry queue contains fixed tool names, outcome and duration only; it never stores tool arguments or results. Its path and bounds are described in [the MCP reference](MCP.md).

## Troubleshooting

- **The process exits immediately:** install `requirements-mcp.txt` in the exact interpreter used by the host and check the host's MCP server log. Keep standard output reserved for the MCP protocol; diagnostics go to standard error.
- **401 Unauthorized:** confirm the token is not expired or revoked and belongs in `VAOS_API_TOKEN`.
- **403 Forbidden:** enable API and MCP, mark the token as MCP-bound, and grant the needed group in both token and Settings policy. New configurations start with Diagnostics only.
- **Plain HTTP rejected:** local loopback HTTP is permitted; any non-loopback VAOS origin must use HTTPS.
- **The host cannot find the bridge:** use absolute command/script paths from the Settings template and verify the host account can read the project and its virtual environment.
- **A write returns approval-required:** the action did not run. Review it in VAOS Settings and only then retry the identical call with its approval ID.

## Developer validation

Install the optional SDK and run the isolated stdio host test:

```sh
python -m pip install -r requirements-mcp.txt
python -m unittest discover -s tests -p "test_mcp_stdio_host.py" -v
```

The test uses a randomly assigned loopback port, a temporary telemetry database and synthetic API responses. It never reads the user's `data` folder or authenticates to a live VAOS service. The main dependency-free test suite skips this optional host integration unless the SDK is installed. CI runs the test on Windows, macOS and Ubuntu; actual host runners and their logs determine the final verified matrix.
