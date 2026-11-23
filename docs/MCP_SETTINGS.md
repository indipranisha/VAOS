# MCP controls in VAOS Settings

This manual explains the controls at **Settings → API and AI agent access**. MCP is optional: VAOS does not start an MCP listener or expose another port. A compatible agent host starts the local `mcp_server.py` stdio bridge when it needs to connect.

## Recommended first connection

1. Enable **API access** and **MCP bridge** only when you are ready to connect an agent.
2. Keep **MCP read-only mode** on. New installations begin with only the **Diagnostics** group selected.
3. Create a separate, expiring **Read only** token and check **Use this token for an MCP agent**. Select **Diagnostics** only for the first connection.
4. Copy the token once into your agent host's secure secret store. VAOS does not show the token again.
5. Copy the generated server configuration from Settings and reload the host. Start by asking the agent to inspect VAOS health or list report types.

Do not paste the token into Git, a shared document, a screenshot or chat. Revoke and replace it if you think it was exposed.

## What each control does

| Control | What it changes | Safe starting value |
|---|---|---|
| API access | Enables authenticated API access for configured clients. | Off until needed |
| MCP bridge | Allows an MCP-designated API token to use the local bridge. | Off until needed |
| Read-only mode | Blocks MCP tool calls that change VAOS. It does not override each token's scope. | On |
| MCP capability groups | Allows selected tool families for MCP tokens. A token must also include the same group. Access is the intersection of the two selections. | Diagnostics only |
| Allow sensitive reads | Allows identity-bearing/private reads only when the API-wide switch and the individual token grant are both on. | Off |
| Allow sensitive actions | Permits eligible high-impact MCP writes to request human approval; it does not auto-approve them. | Off |
| Allow deletion actions | Permits eligible delete/revoke operations to request human approval. | Off |
| API request limit | Caps the request rate applied by the API policy. | Keep the default unless you have a measured reason to change it |
| Default token lifetime | Sets the preselected expiry for new tokens; issued tokens retain their own expiry. | Use the shortest practical period |
| Agent action approvals | Shows pending exact-request previews. Approve once, deny, or revoke unused approval. | Review every request before approval |

The available capability groups are **Diagnostics, Cameras, People, Activity, Review samples, Training, Datasets, Models, Engine settings, Tunnel controls, Video review,** and **Project bundles**. Grant only the smallest set needed. People, review samples, camera links and tunnel details can contain sensitive information. Deletion and model/training operations can change or remove data.

## Separate read and control tokens

Use a read token for regular monitoring. Create a second **Read and control** token only for a task that needs changes; set its lifetime, MCP designation and groups narrowly. Keep read-only mode on unless the supervised task requires a write. Even when writes are allowed, the token cannot change MCP policy, create or revoke tokens, or approve its own requests.

An MCP write from an MCP-designated token pauses and returns an approval request. The request preview omits secrets, images, raw bodies and free-form values. In the approval inbox, check the method, route, risk and safe parameter summary. If approved, the agent must retry the identical request with the one-time approval ID. The approval expires after ten minutes, is bound to that token and request, and works once. A modified request is not covered.

## Turn off or revoke access

- Turn off the MCP bridge to block bridge calls while preserving the token for other API use.
- Turn off API access to block the API integration as a whole.
- Revoke the token to invalidate that credential immediately and cancel its outstanding approvals.
- Remove the VAOS server entry from the agent host to prevent that host from launching the bridge.

Settings changes are checked on each API/MCP request. Reload the agent host after changing its local command, script path or secret. VAOS does not need to be restarted for a saved API/MCP policy change.

## Activity, privacy and troubleshooting

The MCP bridge reports only a fixed tool name, success/error outcome, elapsed duration and random invocation ID. It does not send tool arguments or results. If reporting is temporarily unavailable, a bounded local queue retries later; a process-local fallback can still be lost if the host exits before it is saved. Review **Statistics & observability → MCP activity** for the recorded events.

- **401:** token expired, revoked, or not supplied to the bridge.
- **403:** API/MCP is disabled, the token is not MCP-designated, read-only blocks the write, or a required group/grant is missing.
- **Approval required:** the write has not run; inspect the pending preview and approve once only if it matches your intent.
- **Agent cannot launch VAOS:** use the absolute interpreter/script paths copied from the Settings configuration template and install `requirements-mcp.txt` into that interpreter.
- **Remote connection rejected:** use the canonical HTTPS VAOS origin. Clear-text HTTP is allowed only for loopback.

The MCP bridge is local stdio, not a public MCP server. Do not route it through a camera tunnel or expose its token to the internet. For host configuration and the SDK test, see [MCP host setup](MCP_HOST_SETUP.md). For tool/resource definitions, see the [MCP reference](MCP.md).
