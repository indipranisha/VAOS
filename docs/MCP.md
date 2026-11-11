# VAOS MCP bridge

VAOS includes an optional Model Context Protocol (MCP) bridge. Compatible agent hosts can discover the API reference and call documented VAOS operations through three guarded tools. The bridge is a separate stdio process; it does not add a permanent service, open a new port, or restart the running application.

## Current MCP tools and resources

| MCP item | Purpose |
| --- | --- |
| `vaos_api_request` | Invoke a documented versioned endpoint with JSON input. |
| `vaos_upload_file` | Send a base64 file to a documented multipart upload endpoint (up to 100 MB). |
| `vaos_send_binary` | Send a raw binary payload such as a JPEG frame with documented request headers. |
| `vaos://openapi-v1` | Read the current generated OpenAPI v1 contract. |
| `vaos://agent-policy` | Read the current MCP policy and selected capabilities. |

Each operation rereads VAOS policy. The API token's scope and feature groups, global read-only setting, enabled MCP capability groups, sensitive-action switch, and delete switch are all enforced. Effective access is the intersection of the token's groups and the MCP groups selected in Settings. MCP cannot create/revoke API tokens or change integration settings. Tool requests must match a documented OpenAPI method/path; arbitrary URLs, `..` paths, host overrides, authentication-header overrides, and cookie forwarding are rejected.

## Setup

1. Let any active training job finish before installing optional packages into the VAOS Python environment. Package installation is intentionally not part of the normal VAOS startup.
2. On the VAOS machine, from the project directory, install the optional SDK:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
   ```

   The official MCP Python SDK v2 requires Python 3.11 or newer; the supported Windows setup uses Python 3.12.
3. Open **Settings → API and AI agent access**. Enable API access. Leave MCP read-only checked initially. Select only the feature groups the agent needs, then save.
4. Create a **read** token for inspection. Copy the secret when VAOS shows it; it will not be displayed again. If it is lost, revoke it and create another.
5. Use the settings page's **Agent configuration template** as the host configuration. Replace the token placeholder with the token secret using the MCP host's secure environment/secret mechanism. Do not save the populated file in Git or share it in chat.
6. Restart/reload the MCP host so it launches `mcp_server.py`. VAOS itself does not need a restart for Settings changes, but the currently running VAOS service must already include the MCP/API code.

The template uses the interpreter running this VAOS project and the absolute path to its `mcp_server.py`. That is ready to copy when the agent host can access those files. For Docker or a remote VAOS deployment, copy the bridge to the agent host, use that host's Python/script paths, set `VAOS_BASE_URL` to the VAOS HTTPS origin, and make sure the host can reach it. The remote admin host should remain behind VPN, identity-aware access control, or equivalent protection.

Example host entry shape (the Settings page provides machine-specific paths):

```json
{
  "mcpServers": {
    "vaos": {
      "command": "C:\\path\\to\\VAOS\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\VAOS\\mcp_server.py"],
      "env": {
        "VAOS_BASE_URL": "http://127.0.0.1:8000",
        "VAOS_API_TOKEN": "<store-in-your-MCP-host-secret-manager>"
      }
    }
  }
}
```

## Granting control

Read access is the recommended default. To allow writes:

1. Create a separate `control` token (keep the read token for safe monitoring).
2. Turn off MCP **Read-only mode**.
3. Enable only the needed feature groups.
4. Leave **Allow sensitive actions** and **Allow deletion actions** off unless the agent has a specific, supervised job that needs them.
5. Revoke the control token after the task or if it is exposed.

Sensitive actions include training start/stop, dataset preparation/import, model upload/selection/download, camera-link creation, participant enrollment, service restart, video-review session operations, and tunnel controls. Deletions and revocations have their own switch. VAOS's existing API validation, conflict checks, safe-stop behavior, and enrollment consent checks continue to apply. MCP configuration cannot override the application's own safety checks.

## Limits and safety notes

- The MCP bridge supports JSON, multipart uploads, and raw binary submissions. MCP upload/request content is limited to 100 MB; large datasets/model checkpoints should be transferred through the existing VAOS UI or directly through its REST API.
- Non-JSON responses are returned as base64 only when under the MCP response limit. Use the REST API directly for large files.
- API access is off by default. MCP is off by default. MCP starts read-only, with sensitive and delete actions disabled.
- Do not place secrets in shell arguments, source files, repository settings, screenshots, or logs. Environment variables and the MCP host's secret store are preferred.
- Public enrollment and camera-frame APIs use invitation/camera tokens. Treat these links as secrets; only enrollment flows requiring consent should create a face profile.
- AI agents can make incorrect decisions. Keep a human in the loop for labels, consent, dataset purges, profile deletion, model replacement, tunnel exposure, and training operations.
- A local stdio bridge is not automatically a hosted or remote MCP service. Do not expose an MCP endpoint publicly without an explicit authentication and deployment design.

## Troubleshooting

- **Bridge exits with an install message:** install `requirements-mcp.txt` into the interpreter shown in the Settings configuration template.
- **401:** create a fresh VAOS API token and update the MCP host's secure environment. Check token expiry/revocation.
- **403:** enable API and MCP in VAOS Settings, use a control token for writes, and enable the relevant capability group/action switch.
- **Remote HTTP rejected:** use the HTTPS VAOS origin. Clear-text HTTP is allowed only for local loopback addresses.
- **Host cannot find the command:** use the absolute interpreter and script paths from Settings, then reload the MCP host.
- **Operation not in reference:** use the `/api/docs` page for the active service build. The MCP bridge deliberately refuses undocumented methods/routes.

## Official SDK reference

VAOS uses the official [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk) v2 over stdio, the recommended transport for a local agent-launched process. The optional dependency is isolated in `requirements-mcp.txt`; the VAOS runtime does not import the SDK.
