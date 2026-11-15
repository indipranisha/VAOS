# VAOS REST API

VAOS exposes its application operations as a versioned REST API. The existing browser routes remain available for the dashboard; the API is an additional interface for scripts, integrations, and agent tools.

## Start here

- In-app explorer: sign in to the dashboard, then open `/api-explorer` or choose **Settings → API and AI agent access → Explore APIs**.
- Interactive OpenAPI reference: sign in to the dashboard, then open `https://<your-vaos-host>/api/docs` (or `http://127.0.0.1:8000/api/docs` locally).
- Machine-readable OpenAPI 3 document: `/api/openapi.json`.
- Stable versioned API root: `/api/v1`.
- Health probe: `GET /api/v1/health` (no credentials; returns service name, version, and instance id).
- The camera station is a separate service, normally port `8001`; its public URL is the configured HTTPS camera-site origin.

The OpenAPI document is generated from the handlers in the running VAOS build, including query/path parameters, request body schemas, response schemas, and auth. It lists `/admin/...` paths under the `/api/v1` server base, so the full URL is the server base plus the listed path. The interactive reference requires dashboard Basic authentication; the JSON schema accepts dashboard Basic authentication or an enabled API token.

The current working source exposes 72 OpenAPI paths containing 88 HTTP operations, including Observability, Hardware Intelligence, Tracking & Event Rules, Project Bundles, and the scoped MCP approval-status lookup. These counts are source-tree counts and do not imply that the older live service has activated them. The catalogue below groups operations by purpose; use the generated OpenAPI document for the exact field-level contract.

Every admin and camera-service HTTP response includes `X-VAOS-Correlation-ID`. Send a valid opaque value in this request header to correlate related requests; if it is missing or invalid, VAOS generates a 128-bit hexadecimal value. The same ID is echoed in the response and attached to request-scoped security/audit events when available. Do not put credentials or personal data in this header. It is a request correlation aid, not distributed tracing or an authorization mechanism.

## Authentication and access

### Dashboard sign-in

The dashboard username is `admin`. VAOS accepts HTTP Basic authentication for protected admin endpoints. This is useful for Swagger UI and local scripts. Do not expose the admin host publicly without HTTPS and an additional network or identity access policy.

### API token

Create tokens in **Settings → API and AI agent access**. Tokens have one of two scopes:

| Scope | What it permits |
| --- | --- |
| `read` | Safe `GET`, `HEAD`, and `OPTIONS` requests. |
| `control` | Read plus write requests. MCP settings still apply to MCP calls. |

Each token can also be limited to one or more feature groups: diagnostics, cameras, people, events/activity, review samples, training, datasets, models, engine settings, tunnels, video review, and projects. Group limits apply to direct REST calls and MCP calls, so a limited token cannot read or control resources in groups it was not granted.

Tokens expire from 1 to 365 days. VAOS shows each token only at creation and stores its SHA-256 hash, not the secret. The API can be disabled independently of dashboard sign-in. Requests are rate limited per token (30–600 requests per minute), and a bounded audit log retains up to 5,000 recent requests (90-day retention) with timestamp, token prefix, method, and path. Request bodies and token secrets are not logged. Revoking a token takes effect immediately.

New tokens default to the **Diagnostics** capability only. Reading profiles, participant photos, camera/enrollment QR links, tunnel URLs, event identities, or review-sample images requires both the global **Allow sensitive data reads** setting and a separate sensitive-read grant on that token. This is independent of read/control write access. Existing tokens retain their chosen groups, but do not inherit the new sensitive-read grant.

Tokens marked **Use this token for an MCP agent** are subject to MCP Settings and require a one-time administrator approval for every write. The approval is bound to the full token hash and exact request fingerprint, expires after 10 minutes, and cannot be replayed or used with changed parameters. Non-MCP API control tokens keep their separate direct REST behavior. The MCP bridge surfaces the request ID and safe preview on HTTP `428`; an administrator approves or denies it in Settings.

Example (replace the host and paste the token into a secure local environment variable):

```powershell
$env:VAOS_API_TOKEN = 'vaos_...'
$headers = @{ Authorization = "Bearer $env:VAOS_API_TOKEN" }
Invoke-RestMethod -Headers $headers -Uri 'http://127.0.0.1:8000/api/v1/admin/status'
```

For remote connections, use HTTPS. VAOS accepts forwarded HTTPS indicators only from the loopback launcher/tunnel or an explicitly trusted proxy peer. If a reverse proxy is not passing the original HTTPS scheme through to the app, set `VAOS_TRUSTED_PROXY_CIDRS` to the narrow proxy address/range; do not trust an entire network without need. Never commit API tokens, admin passwords, private dataset paths, or local configuration files to Git.

## Endpoint catalogue

The interactive OpenAPI reference is the source of truth for exact JSON fields and validation constraints. This catalogue groups every current API operation by purpose. All `/admin/...` operations require dashboard Basic authentication or an enabled API token with the appropriate scope, except API-key and integration-setting management, which deliberately requires dashboard Basic authentication only.

### Observability and analytics

These endpoints are also served at `/api/admin/observability/...` for the browser UI. API-token paths are versioned under `/api/v1/admin/observability/...`; statistics, lifecycle and event routes use the diagnostics permission group, while `/observability/integrations` uses the Settings group because it configures external destinations. For API tokens, operational-event, lifecycle, security-audit, report, integration and counter-queue access is additionally gated by the global sensitive-read setting and a token explicitly granted sensitive-read access. Dashboard Basic-authenticated administrators remain able to use the browser interface. Retention policy changes require Basic-authenticated admin access; SIEM destination changes require control access and the Settings group. MCP-sensitive writes also require exact-request administrator approval.

| Method | Versioned path | Purpose |
| --- | --- | --- |
| `GET` | `/admin/observability/overview` | Installation counts and time-period summary. |
| `GET` | `/admin/observability/metrics` | Metric definitions, units, sources, availability, supported dimensions and supported series aggregations. |
| `GET` | `/admin/observability/series` | Bounded time series for one metric. Accepts `metric`, `after`, `before`, `limit` (1–1,000), exact `dimensions` JSON, optional valid `group_by`, `aggregation` (`raw` or one of the metric's advertised choices), and `interval` (`minute`, `5min`, `hour`, `day`). End time is exclusive and the range is capped at 366 days. Counter deltas can only be summed; gauges expose only supported bucket aggregations. Grouped dimension values are returned as text so grouping, exact filters and group counts use the same normalization. |
| `GET` | `/admin/observability/events` | Filter and cursor-page operational events by time, severity, component, event type, exact `session_id` and optional `correlation_id`, plus exact recorded `training_job_id`, `dataset_id` and `model_id` values (each max 120 characters). Literal search is supported. Correlation values are not universal trace IDs; job/dataset/model IDs match only when present in safe event details. |
| `GET` | `/admin/observability/events/{event_id}` | Read one redacted operational event for an authenticated administrator or a diagnostics token with both required sensitive-read grants. Records return 404 after retention expires. |
| `GET`, `POST` | `/admin/observability/log-filters` | List or create up to 20 saved Log Explorer filters, scoped to the authenticated Basic-auth account or API-token principal. |
| `DELETE` | `/admin/observability/log-filters/{filter_id}` | Delete a saved filter owned by the authenticated principal. Other principals receive 404. |
| `GET` | `/admin/observability/lifecycle` | Filter and cursor-page runtime sessions. |
| `GET` | `/admin/observability/security-audit/integrity` | Verify the local append-only audit hash chain. |
| `GET` | `/admin/observability/security-audit/events` | Search restricted security audit records, including an exact optional `actor_ref` filter. |
| `GET` | `/admin/observability/reports/types` | List supported report types and formats. |
| `GET`, `POST` | `/admin/observability/reports` and `/reports/jobs` | List owned jobs and queue CSV/PDF reports. |
| `GET`, `POST` | `/admin/observability/reports/jobs/{id}` and `/{id}/cancel` | Read job state or cancel queued work. |
| `GET` | `/admin/observability/reports/jobs/{id}/download` | Download an unexpired integrity-checked report. |
| `GET`, `POST` | `/admin/observability/integrations` | Read status or create disabled-by-default signed HTTPS JSON, Syslog TLS or OTLP/HTTP JSON destinations. Creation accepts `transport`, `event_format`, `endpoint`, and an optional environment-variable `secret_ref` where applicable. |
| `PUT`, `DELETE` | `/admin/observability/integrations/{id}` | Update or remove a destination. PUT preserves an omitted credential reference only when the transport remains unchanged; changing transport clears the prior reference unless a new one is supplied. DELETE archives it, clears endpoint/credential references and disables pending deliveries while preserving local event/delivery history. |
| `POST` | `/admin/observability/integrations/{id}/enabled` and `/{id}/test` | Enable/disable a destination or send an explicit test. Removed destinations cannot be re-enabled. |
| `GET`, `PUT`, `POST` | `/admin/observability/retention` | Read policy, save event/metric/delivery retention (30, 90, 180 or 365 days), or explicitly apply cleanup. Shorter periods require `confirm_shorter: true`; cleanup requires `confirm_expired_data_redaction: true`. Lifetime counters and append-only security audit records are preserved. Generated report files are separate copies and remain downloadable until their own 24-hour expiry; retention cleanup does not invalidate them. |

### Internal observability hooks (not public or versioned API)

These two local integration hooks are deliberately excluded from the generated OpenAPI schema. They have no `/api/v1` alias and are documented here for maintainers; they are not general-purpose telemetry ingestion endpoints.

| Method and path | Inputs and result | Access and errors |
| --- | --- | --- |
| `GET /api/admin/observability/counter-queue` | No caller-supplied fields. Returns a process-scoped snapshot with `state`, `scope`, `writes_verified`, primary/fallback counter queue summaries, authentication security-event fallback status, in-memory counter totals and `updated_at`. It omits local paths, event details and dimensions. | Requires dashboard Basic authentication, or an enabled diagnostics API token explicitly granted sensitive-read when the global sensitive-read setting is on. Reports the current process snapshot only; `writes_verified` is always false and an empty queue does not prove every process has no pending data. |
| `POST /api/admin/observability/mcp/invocations` | JSON body contains `tool` (registered MCP bridge tool name, 3–64 characters), `outcome` (`success` or `error`), finite `duration_ms` (0–300,000), and an optional 32-character lowercase hexadecimal `invocation_id`. Extra fields are rejected. Only tool name, outcome and duration are recorded; arguments and results are never accepted or stored. | Requires an enabled MCP-designated API token; Basic authentication or a regular API token receives 403. Invalid/unknown tools or non-finite duration receive 400; unavailable telemetry storage receives 503. Repeated requests with the same invocation ID do not double-count. Legacy reports without an ID remain accepted but are not retry-deduplicated. |

Metric series may be grouped only by a dimension listed in the selected metric's catalogue entry. `aggregation=raw` preserves observations/counter events. Aggregated requests use an end-exclusive UTC range and a bucket size of `minute`, `5min`, `hour`, or `day`. The example uses a gauge aggregation supported by the metric catalogue:

```sh
curl -G "http://127.0.0.1:8000/api/v1/admin/observability/series" \
  -H "Authorization: Bearer $VAOS_TOKEN" \
  --data-urlencode "metric=hardware.gpu_utilization_percent" \
  --data-urlencode "after=2026-11-12T00:00:00Z" \
  --data-urlencode "before=2026-11-13T00:00:00Z" \
  --data-urlencode "aggregation=average" \
  --data-urlencode "interval=5min" \
  --data-urlencode "group_by=device_id"
```

Pages are capped at 200 events/sessions, 500 catalogue entries and 1,000 metric points. Metric/report time ranges are limited to 366 days. See the [Observability Centre guide](observability/overview.md) and generated OpenAPI schema for request/response details.

`GET /api/admin/observability/security-audit/events` accepts `after`, `before`, `event_type`, `outcome`, `component`, literal `search`, exact `actor_ref` (maximum 120 characters), `cursor` and `limit`. `actor_ref` is matched exactly against the audit row; it may be a service label, API-token prefix or hash rather than a verified person or tenant. This restricted route retains the sensitive-read policy for API tokens.

### Service and diagnostics

| Method | Versioned path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Public liveness and build information. |
| `GET` | `/admin/status` | Cameras, engine state, training state, model/services, and host telemetry. |
| `GET` | `/admin/hardware` | Redacted host hardware facts, per-GPU telemetry when available, CUDA inference status, training state, and non-applied estimates. Uses the existing `diagnostics` capability. |
| `GET` | `/admin/engine-settings` | Read detection thresholds, frame size/rate, and engine switches. |
| `POST` | `/admin/engine-settings` | Save bounded engine settings. |
| `GET` | `/admin/model-selection` | Read the selected live detector. |
| `POST` | `/admin/model-selection` | Select the built-in or trained detector. |
| `POST` | `/admin/services/restart` | Request a service restart (local deployment only). |

### Camera stations and tunnels

| Method | Versioned path | Purpose |
| --- | --- | --- |
| `GET`, `POST` | `/admin/cameras` | List or create camera links. |
| `PATCH` | `/admin/cameras/{token}/map` | Change a camera pin and coverage direction. |
| `GET` | `/admin/cameras/{token}/qr` | Return the camera QR image. |
| `POST` | `/admin/cameras/{token}/revoke` | Revoke a camera link. |
| `GET` | `/admin/tunnel` | Read camera tunnel status and URL. |
| `POST` | `/admin/tunnel/{action}` | Pause, start, stop, or restart the camera tunnel. |
| `GET` | `/admin/admin-tunnel` | Read the private dashboard tunnel status and URL. |
| `POST` | `/admin/admin-tunnel/{action}` | Control the separate dashboard tunnel. |
| `POST` | `/camera/{token}/frame` | Accept a JPEG frame using an active camera capability token. |

Camera and enrollment tokens are capability secrets; do not expose them in logs or shared links. The separate camera service also provides `GET /c/{token}`, `GET /enroll/{token}`, `POST /api/camera/{token}/frame`, and `POST /api/enroll/{token}` outside the admin API. Public enrollment requires a valid one-time invitation, explicit consent, and one or more photos.

### People and activity

| Method | Versioned path | Purpose |
| --- | --- | --- |
| `GET`, `POST` | `/admin/participants` | List profiles or create an enrolled profile from an uploaded photo. |
| `GET` | `/admin/participants/{person_id}/photo` | Return the profile photo. |
| `GET` | `/admin/participants/{person_id}/photos/{photo_id}` | Return an additional profile photo. |
| `DELETE` | `/admin/participants/{person_id}` | Delete a profile and related records. |
| `GET` | `/admin/events` | List recent sightings/activity. |
| `DELETE` | `/admin/events` | Clear activity history. |
| `GET`, `POST` | `/admin/tracking/rules` | List or create camera-scoped object count/dwell rules. Reads that disclose camera capability tokens require both sensitive-read controls. |
| `PATCH`, `DELETE` | `/admin/tracking/rules/{rule_id}` | Edit, pause/enable, or delete a rule; event history is retained when a rule is deleted. MCP changes require sensitive-action settings and one-time approval; deletion also uses the deletion control. |
| `GET` | `/admin/tracking/events` | Page through recent rule alerts (maximum 500 per request), optionally filtered by camera or rule. Events contain metadata, not images. |
| `POST` | `/admin/enrollment-links` | Create a time-limited phone enrollment invitation. |
| `GET` | `/admin/enrollment-links/{token}/qr` | Return an invitation QR code. |
| `DELETE` | `/admin/enrollment-links/{token}` | Revoke an invitation. |
| `POST` | `/enroll/{token}` | Create a profile from a one-time invitation, consent, and uploaded photos. |

### Review samples and datasets

| Method | Versioned path | Purpose |
| --- | --- | --- |
| `GET` | `/admin/ml/samples` | List, filter, and paginate collected samples and review state. |
| `GET` | `/admin/ml/samples/{sample_id}/image` | Return a stored review sample image. |
| `POST` | `/admin/ml/samples/{sample_id}/review` | Confirm, reject, or relabel one sample. |
| `POST` | `/admin/ml/samples/batch-review` | Apply review decisions to selected samples. |
| `POST` | `/admin/ml/capture` | Enable or disable live sample collection. |
| `DELETE` | `/admin/ml/samples` | Purge collected review samples. |
| `GET` | `/admin/ml/datasets/managed` | List prepared/managed dataset status. |
| `POST` | `/admin/ml/datasets/prepare` | Validate and convert an available local dataset. |
| `DELETE` | `/admin/ml/datasets/managed/{dataset_id}` | Purge a managed prepared dataset. |
| `POST` | `/admin/ml/datasets/import` | Start or resume a supported dataset import/preparation. |

Dataset preparation/import can consume substantial disk, CPU, and time. Training, review, and preparation endpoints can reject conflicting operations with `409` rather than starting a competing job.

### Project bundles

| Method | Versioned path | Purpose |
| --- | --- | --- |
| `GET` | `/admin/projects` | List saved configuration-only project bundles. |
| `GET` | `/admin/projects/export?name=...` | Download allowlisted engine settings and local dataset/model names as `.vaosproject`. |
| `GET` | `/admin/projects/{bundle_id}` | Review a saved bundle and check which referenced dataset/model IDs are available locally. |
| `POST` | `/admin/projects/import?dry_run=true` | Validate and preview a multipart `.vaosproject` upload without writing it. |
| `POST` | `/admin/projects/import?dry_run=false` | Save the validated configuration archive locally without applying its settings. |
| `POST` | `/admin/projects/{bundle_id}/apply` | Apply only reviewed engine settings after `{ "confirm": true }`. |
| `DELETE` | `/admin/projects/{bundle_id}` | Remove the saved archive; it does not delete referenced datasets or model weights. |

Project operations require the `projects` capability. MCP imports and apply actions are sensitive writes and use the existing exact-request approval; deletion remains subject to the delete switch. See [Project Bundle guide](PROJECT_BUNDLES.md) for archive limits and excluded data.

Tracking-rule regions are normalized rectangles with `x`, `y`, `width`, and `height` in the `0`–`1` range. Supported rule types are `count_in_region` and `dwell_in_region`; rules also specify a detector label, minimum confidence, consecutive-frame confirmation, camera scope, and cooldown. Dwell rules require object tracking to be enabled. Track IDs are approximate and process-local; they reset on a restart and must not be treated as identity.

### Models and training

| Method | Versioned path | Purpose |
| --- | --- | --- |
| `GET` | `/admin/ml/logs` | Read recent training activity log records. |
| `GET` | `/admin/ml/history` | Read training runs and per-epoch metrics. |
| `POST` | `/admin/ml/train` | Start a configured custom-detector run. |
| `POST` | `/admin/ml/train/stop` | Request a safe stop at the checkpoint boundary. |
| `GET`, `POST` | `/admin/ml/model-sources` | List or upload a starting model checkpoint. |
| `GET`, `POST` | `/admin/ml/huggingface-models` | List or download supported official model checkpoints. |
| `POST` | `/admin/ml/enrichment` | Start a best.pt fine-tuning/enrichment run. |

### Video analysis

| Method | Versioned path | Purpose |
| --- | --- | --- |
| `POST` | `/admin/video-review/sessions` | Create a local-video review session. |
| `POST` | `/admin/video-review/sessions/{session_id}/frame` | Submit a JPEG frame with timestamp headers for analysis/sampling. |

### Integrations and token administration

Token creation/revocation and integration-setting routes are intentionally absent from the OpenAPI schema and require dashboard Basic authentication, so an automation token cannot create or escalate credentials. The read-only MCP policy handshake is an exception: the local bridge can read it using its API token, but cannot use it to change settings.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`, `POST` | `/api/admin/integrations` | Read or save API/MCP controls in Settings. |
| `POST` | `/api/admin/api-tokens` | Create and show a token once. |
| `DELETE` | `/api/admin/api-tokens/{prefix}` | Revoke a token. |
| `GET` | `/api/admin/integrations/mcp-policy` | Read-only policy handshake used by the local MCP bridge. |
| `GET` | `/admin/mcp-approvals/{approval_id}` | Read approval status for the calling token only; requires the token's Settings capability. |

The human approval inbox list and approve/deny/revoke actions are deliberately Basic-auth-only, omitted from the bearer API schema, and cannot be invoked by MCP. The status lookup is read-only and scoped to the token that submitted that request.

## Responses and errors

Success responses are JSON unless an endpoint returns an image/file. Error responses use FastAPI's JSON format, commonly `{"detail":"..."}`. Common statuses include `400` invalid input, `401` missing/invalid credentials, `403` disabled API or insufficient scope, `404` unknown resource, `409` conflicting/running operation or an invalid/replayed approval, `413` oversized request, `415` unsupported content type, `422` schema/validation error, `428` MCP action awaiting approval, `429` rate limited, and `503` unavailable model/service. Follow `Retry-After` when returned.

Image uploads use `multipart/form-data`; raw frame endpoints require an image content type and may accept documented headers such as `x-video-time` and `x-save-samples`. Do not send full camera frames to review/sample APIs unless the route explicitly expects them. The OpenAPI document describes route request bodies; use the dedicated upload flow for binary media.

## Versioning

`/api/v1` is the stable URL prefix. Existing unversioned `/api/...` paths remain for the VAOS browser and compatibility. A future breaking API change should use `/api/v2`; additive optional fields can be introduced in v1. The generated API reference and this guide must be updated together when a route is added, removed, or changes behavior.
