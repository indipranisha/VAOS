# VAOS REST API

VAOS exposes its application operations as a versioned REST API. The existing browser routes remain available for the dashboard; the API is an additional interface for scripts, integrations, and agent tools.

## Start here

- Interactive reference: sign in to the dashboard, then open `https://<your-vaos-host>/api/docs` (or `http://127.0.0.1:8000/api/docs` locally).
- Machine-readable OpenAPI 3 document: `/api/openapi.json`.
- Stable versioned API root: `/api/v1`.
- Health probe: `GET /api/v1/health` (no credentials; returns service name, version, and instance id).
- The camera station is a separate service, normally port `8001`; its public URL is the configured HTTPS camera-site origin.

The OpenAPI document is generated from the handlers in the running VAOS build, including query/path parameters, request body schemas, response schemas, and auth. It lists `/admin/...` paths under the `/api/v1` server base, so the full URL is the server base plus the listed path. The interactive reference requires dashboard Basic authentication; the JSON schema accepts dashboard Basic authentication or an enabled API token.

The current build exposes 41 versioned paths containing 49 HTTP operations. The catalogue below groups those operations by purpose; use the generated OpenAPI document for the exact field-level contract.

## Authentication and access

### Dashboard sign-in

The dashboard username is `admin`. VAOS accepts HTTP Basic authentication for protected admin endpoints. This is useful for Swagger UI and local scripts. Do not expose the admin host publicly without HTTPS and an additional network or identity access policy.

### API token

Create tokens in **Settings → API and AI agent access**. Tokens have one of two scopes:

| Scope | What it permits |
| --- | --- |
| `read` | Safe `GET`, `HEAD`, and `OPTIONS` requests. |
| `control` | Read plus write requests. MCP settings still apply to MCP calls. |

Each token can also be limited to one or more feature groups: diagnostics, cameras, people, activity, review samples, training, datasets, models, engine settings, tunnels, and video review. Group limits apply to direct REST calls and MCP calls, so a limited token cannot read or control resources in groups it was not granted.

Tokens expire from 1 to 365 days. VAOS shows each token only at creation and stores its SHA-256 hash, not the secret. The API can be disabled independently of dashboard sign-in. Requests are rate limited per token (30–600 requests per minute), and a bounded audit log retains up to 5,000 recent requests (90-day retention) with timestamp, token prefix, method, and path. Request bodies and token secrets are not logged. Revoking a token takes effect immediately.

Example (replace the host and paste the token into a secure local environment variable):

```powershell
$env:VAOS_API_TOKEN = 'vaos_...'
$headers = @{ Authorization = "Bearer $env:VAOS_API_TOKEN" }
Invoke-RestMethod -Headers $headers -Uri 'http://127.0.0.1:8000/api/v1/admin/status'
```

For remote connections, use HTTPS. VAOS accepts forwarded HTTPS indicators only from the loopback launcher/tunnel or an explicitly trusted proxy peer. If a reverse proxy is not passing the original HTTPS scheme through to the app, set `VAOS_TRUSTED_PROXY_CIDRS` to the narrow proxy address/range; do not trust an entire network without need. Never commit API tokens, admin passwords, private dataset paths, or local configuration files to Git.

## Endpoint catalogue

The interactive OpenAPI reference is the source of truth for exact JSON fields and validation constraints. This catalogue groups every current API operation by purpose. All `/admin/...` operations require dashboard Basic authentication or an enabled API token with the appropriate scope, except API-key and integration-setting management, which deliberately requires dashboard Basic authentication only.

### Service and diagnostics

| Method | Versioned path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Public liveness and build information. |
| `GET` | `/admin/status` | Cameras, engine state, training state, model/services, and host telemetry. |
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

## Responses and errors

Success responses are JSON unless an endpoint returns an image/file. Error responses use FastAPI's JSON format, commonly `{"detail":"..."}`. Common statuses include `400` invalid input, `401` missing/invalid credentials, `403` disabled API or insufficient scope, `404` unknown resource, `409` conflicting/running operation, `413` oversized request, `415` unsupported content type, `422` schema/validation error, `429` rate limited, and `503` unavailable model/service. Follow `Retry-After` when returned.

Image uploads use `multipart/form-data`; raw frame endpoints require an image content type and may accept documented headers such as `x-video-time` and `x-save-samples`. Do not send full camera frames to review/sample APIs unless the route explicitly expects them. The OpenAPI document describes route request bodies; use the dedicated upload flow for binary media.

## Versioning

`/api/v1` is the stable URL prefix. Existing unversioned `/api/...` paths remain for the VAOS browser and compatibility. A future breaking API change should use `/api/v2`; additive optional fields can be introduced in v1. The generated API reference and this guide must be updated together when a route is added, removed, or changes behavior.
