# VAOS MCP bridge

VAOS includes an optional Model Context Protocol (MCP) bridge. MCP hosts that support local stdio processes can discover the API reference and call documented VAOS operations through guarded tools. The bridge is a separate stdio process; it does not add a permanent service, open a new port, or restart the running application. See [MCP host setup](MCP_HOST_SETUP.md) for a non-programmer connection guide and the evidence-based host compatibility status.

New installations expose only the **Diagnostics** group by default. Administrators opt in to other groups in Settings and each token also needs the corresponding group. Existing saved group selections are preserved; this default change does not silently rewrite an installation's saved MCP policy.

## Current MCP tools and resources

| MCP item | Purpose |
| --- | --- |
| `vaos_api_request` | Invoke a documented versioned endpoint with JSON input. |
| `vaos_upload_file` | Send a base64 file to a documented multipart upload endpoint (up to 100 MB). |
| `vaos_send_binary` | Send a raw binary payload such as a JPEG frame with documented request headers. |
| `vaos_check_approval` | Check an approval request created by this token. |
| `vaos://openapi-v1` | Read the current generated OpenAPI v1 contract. |
| `vaos://agent-policy` | Read the current MCP policy and selected capabilities. |

Observability-specific wrappers are also available: `vaos_get_statistics`, `vaos_query_metric`, `vaos_list_runtime_sessions`, `vaos_search_logs`, `vaos_get_system_health`, `vaos_list_report_types`, `vaos_generate_report`, and `vaos_get_report_status`. These call the same versioned REST API and preserve its scope and sensitive-read rules. Sensitive writes still require exact-request administrator approval. See the [Observability Centre guide](observability/overview.md) and [report limits](observability/reporting.md).

The local bridge records an invocation count and elapsed duration for each outermost tool call, including its fixed tool name and success/error outcome. It does not send tool arguments or returned content. This telemetry uses the MCP-designated token and remains disabled unless both API and MCP are enabled. Reports use random invocation IDs so a retry cannot count the same completed tool call twice. Failed reports are persisted in a private local SQLite queue capped at 512 records and 2 MiB; each later tool call retries pending reports within a 1.25-second network reporting budget, plus short bounded local lock waits. A full queue drops its oldest pending record and emits a warning. The queue is stored by default under `%LOCALAPPDATA%\VAOS` on Windows or `$XDG_STATE_HOME/VAOS` on macOS/Linux (falling back to `~/.local/state/VAOS`); set `VAOS_MCP_TELEMETRY_DB` to an absolute local path to move it. On POSIX, VAOS creates new queue paths with current-user permissions and refuses existing group/world-accessible queue paths rather than changing their permissions; on Windows, the queue inherits the local application-data directory ACL. If local SQLite is unavailable, a bounded process-local retry is retained as a best effort and can be lost when the MCP host exits. A successful report is removed only after VAOS confirms acceptance; a retry after an ambiguous response is safe because VAOS deduplicates the invocation ID. Reporting never repeats the tool action or replaces its result.

| Tool | Inputs | Result / limits |
| --- | --- | --- |
| `vaos_get_statistics` | `period` (`lifetime`, `24h`, `7d`, `30d`); optional UTC `after` and exclusive `before` | Overview KPI JSON; custom range when either boundary is provided (the API requires a valid ordered pair). |
| `vaos_query_metric` | `metric_key`, UTC `after`, exclusive `before`, `limit` 1–1,000; optional exact `dimensions_json`, `group_by`, `aggregation` (`raw`, `sum`, `average`, `min`, `max`), and `interval` (`minute`, `5min`, `hour`, `day`) | Registered metric definition and bounded raw or time-bucketed series with units, normalized text dimensions, record counts and provenance. The REST service checks whether the selected aggregation is valid for that metric; unsupported combinations return a clear error. |
| `vaos_list_runtime_sessions` | `limit` 1–200, optional `after`, `before`, `cursor` | Lifecycle page plus next cursor/has-more status; unknown exits remain unknown. |
| `vaos_search_logs` | Optional `search`, `level`, `component`, `event_type`, exact `session_id`, `correlation_id`, `training_job_id`, `dataset_id`, `model_id`, `after`, `before`, `cursor`; `limit` 1–200 | Redacted operational-event page. Session and correlation IDs select exact recorded values. Job/dataset/model filters match only safe event details that actually contain those IDs; correlation IDs are not universal trace IDs. |
| `vaos_get_system_health` | None | Public service liveness/version only; does not reveal private metrics. |
| `vaos_list_report_types` | None | Supported report types, formats and current row/range caps. |
| `vaos_generate_report` | `report_type`, `output_format` (`csv`/`pdf`), `filters_json`; optional approval ID on retry | Queues a bounded job. Security-event reports accept an exact `actor_ref` filter; actor references may be labels, token prefixes or hashes rather than verified identities. Sensitive export requires a one-time approval for MCP-bound control tokens. |
| `vaos_get_report_status` | Opaque 32-character lowercase-hex `job_id` | Status and download metadata for this authenticated identity; report bytes are downloaded through REST/UI. |

Each operation rereads VAOS policy. The API token's scope and feature groups, global read-only setting, enabled MCP capability groups, sensitive-read grant, sensitive-action switch, delete switch, and MCP-agent designation are all enforced. Effective access is the intersection of the token's groups and the MCP groups selected in Settings. Sensitive reads (profiles, private images, camera/enrollment links, tunnel URLs, event identities) require both the global API sensitive-read setting and a token-specific grant. MCP cannot create/revoke API tokens, approve its own requests, or change integration settings. Tool requests must match a documented OpenAPI method/path; arbitrary URLs, `..` paths, host overrides, authentication-header overrides, and cookie forwarding are rejected.

Expected local permission denials and invalid tool inputs are returned as a JSON text result with `error: true` and a bounded `detail` field. MCP hosts should display that detail as the reason the requested operation was rejected. HTTP errors returned by VAOS retain their HTTP status and safe response detail. Unexpected bridge failures remain MCP tool errors; the bridge does not return stack traces or credentials as tool output.

## Human approval for MCP changes

An API token must be explicitly marked **Use this token for an MCP agent** when it is created. A control token with this flag cannot perform a write immediately. After the Settings policy checks pass, VAOS records an approval request and returns HTTP `428` with an opaque request ID and a safe preview. The request body, raw query, file name, image, and token secret are not saved in that preview. Numeric/boolean parameters are shown; text values are represented by field and length.

1. The agent calls `vaos_api_request`, `vaos_upload_file`, or `vaos_send_binary` as usual.
2. If the call returns `approval_required`, the agent reports the preview and request ID. It must not claim that the action ran.
3. An administrator opens **Settings → API and AI agent access → Agent action approvals**, inspects the method, route, risk, and safe parameter summary, then chooses **Approve once** or **Deny**. An unused approval can also be revoked.
4. After approval, the agent retries the *same* tool call with the returned `request_id` as `approval_id`. It can call `vaos_check_approval` to see pending/approved/denied/used/expired status.

Approvals expire after 10 minutes, are bound to the full API-token hash and a fingerprint of method, route, query, content type, body and behavior-changing headers, and are consumed atomically once. A changed payload, another token, expired approval, or replay is rejected. Multipart uploads are fingerprinted from canonical fields and file content, so a newly generated multipart boundary does not invalidate an otherwise identical retry. Revoke the token to cancel its outstanding requests. Approval actions require dashboard Basic authentication and are not MCP tools. Ordinary API control tokens that are not MCP-designated retain direct REST behavior; this approval workflow is specific to MCP-bound tokens.

## Setup

1. Let any active training job finish before installing optional packages into the VAOS Python environment. Package installation is intentionally not part of the normal VAOS startup.
2. On the VAOS machine, from the project directory, install the optional SDK (bounded to the 2.3 minor until a newer minor passes the host test matrix):

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
   ```

   MCP Python SDK v2.3 supports Python 3.10 or newer; the documented Windows setup uses Python 3.12. On macOS/Linux, use `.venv/bin/python` in place of the Windows interpreter path.
3. Open **Settings → API and AI agent access**. Enable API access. Leave MCP read-only checked initially. Select only the feature groups the agent needs; new API tokens start with Diagnostics only. Leave sensitive reads off unless the agent must see profiles, private images, camera links, tunnel URLs or identity-bearing activity.
4. Create a **read** token for inspection and mark **Use this token for an MCP agent**. Copy the secret when VAOS shows it; it will not be displayed again. If it is lost, revoke it and create another.
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

Read access is the recommended default. New tokens start with Diagnostics only. Add groups only as needed. To allow writes:

1. Create a separate `control` token (keep the read token for safe monitoring) and mark it as an MCP agent token.
2. Turn off MCP **Read-only mode**.
3. Enable only the needed feature groups.
4. Leave **Allow sensitive actions** and **Allow deletion actions** off unless the agent has a specific, supervised job that needs them. Enabling these switches still does not skip per-operation approval.
5. Review each pending action in the Settings approval inbox. Do not approve vague or unexpected requests.
6. Revoke the control token after the task or if it is exposed.

Sensitive actions include training start/stop, dataset preparation/import, model upload/selection/download, camera-link creation, participant enrollment, tracking-rule creation/changes, service restart, video-review session operations, and tunnel controls. Deletions and revocations have their own switch. VAOS's existing API validation, conflict checks, safe-stop behavior, and enrollment consent checks continue to apply. MCP configuration cannot override the application's own safety checks.

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


The HTTP bridge refuses redirects rather than forwarding the bearer token to a redirected host. File and binary tools reject an encoded payload over the 100 MB request limit before decoding, and their JSON metadata is capped at 64 KiB. JSON request bodies are size-checked before parsing. A VAOS API endpoint that requires a redirect should be addressed through its canonical API origin instead.
