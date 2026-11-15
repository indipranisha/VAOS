# Built-in API Explorer

The API Explorer is an administrator-only, same-origin page at `/api-explorer`. Open it from **Settings → API and AI agent access → Explore APIs**. It reads the current generated OpenAPI contract instead of maintaining a second endpoint list, so it reflects the routes in the build you are running.

## Find and understand an operation

1. Sign in to the VAOS dashboard.
2. Open **Explore APIs** in Settings.
3. Search by action, endpoint path, or description. Use the endpoint list to choose an operation.
4. Read the published authentication, request, and response details. Expand **Request schema and response contract** for the exact parameters and response codes.
5. For path or query fields, enter values in the request form. JSON bodies start with an example derived from the endpoint schema. Multipart and binary operations show file fields when the contract describes them.

## Try a request safely

- Requests are sent only to the current VAOS site; the page does not accept an arbitrary destination URL.
- Dashboard sign-in is used unless you enter an API token. A token is held in the current page only and is not saved to browser storage or included in copied examples.
- VAOS still enforces the token’s scope, capability group, global settings, rate limit, and MCP approval policy on the server.
- `GET`, `HEAD`, and `OPTIONS` are treated as read requests. Before a write method is sent, the page asks for confirmation. The backend remains the authority for permission checks.
- Use the copy buttons to make a curl or Python starting example. The examples omit credentials; add them through a secure local environment or secret manager.

The Swagger/OpenAPI reference remains available at `/api/docs`, and the machine-readable contract at `/api/openapi.json`. The current API and authentication details are in [API.md](API.md). No external API or cloud account is needed for the local explorer.

## Limits and release status

The explorer currently covers the OpenAPI operations published by the admin service. It does not yet provide WebSocket playback, webhook subscription management, a downloadable SDK, or full live browser usability evidence. The separate camera worker’s private internal routes are not exposed as public developer API operations. See the [traceability matrix](TRACEABILITY_MATRIX.md) and [project status](PROJECT_STATUS.md).
