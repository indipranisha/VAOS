# Extending VAOS

VAOS currently supports extensions through its documented REST API, optional local MCP bridge, constrained project bundles and source contributions. It does **not** currently define a stable Python plugin ABI, dynamically load third-party packages, or provide a template-gallery extension mechanism. Do not install arbitrary code into the running VAOS process and call it a supported plugin.

## Preferred integration paths

### REST API client

Use the versioned `/api/v1` API and generated OpenAPI contract. Start with a read-only, expiring token and only the required capability groups. Use HTTPS for remote instances and keep tokens in a secret store. Do not call undocumented internal routes as a compatibility contract. See [API documentation](API.md) and [API Explorer](API_EXPLORER.md).

### MCP agent

Use the optional stdio bridge on the machine that runs the agent host. The bridge applies the same VAOS authorization policy and does not expose shell or direct database access. New installs start with Diagnostics only. Read [MCP host setup](MCP_HOST_SETUP.md) and [tool reference](MCP.md); use separate read and control tokens, least privilege and human approval for sensitive writes.

### Source contribution

For behavior that is not exposed by the stable API, propose a source change. Follow [CONTRIBUTING.md](../CONTRIBUTING.md), add isolated tests, update the API/schema/docs when public behavior changes, and keep existing Apache 2.0 and third-party notices. Tests must use temporary data paths; do not include participant, camera, production database or model data in a patch.

## Adding an API capability safely

1. Reuse an existing handler and business rule; avoid a parallel implementation.
2. Define request/response schemas, validation, errors, permissions and rate/size limits.
3. Add the stable route mapping and generated OpenAPI metadata where appropriate.
4. Add tests for allowed, denied, expired/revoked token, malformed input and sensitive-data cases.
5. If adding MCP exposure, ensure the tool uses the documented route and rechecks policy for every call. Do not grant broad access by default.
6. Update `docs/API.md`, `docs/MCP.md`, the function catalogue, changelog and tests.
7. Record any required migrations and test upgrade/rollback on a disposable database copy.

## Current extension limits

The application is single-instance oriented: in-process training/coordination and local SQLite/file state are not ready for horizontal scaling. There is no public extension registry, arbitrary plugin loader, external identity-provider integration or third-party MCP-host certification. See [architecture](ARCHITECTURE.md) and [project status](PROJECT_STATUS.md) before proposing deployments that require those features.
