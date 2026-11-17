# Log Explorer

The Log Explorer searches persistent, redacted operational events stored in VAOS's existing SQLite observability store. It is an event explorer, not a viewer for every Python console line or operating-system journal record. Standard process-console logs and older plain-text logs are not automatically imported. The event store should not be described as a complete copy of all process output.

## In the application

Open **Statistics & observability → Log explorer**. The current source page supports:

- UTC date-range filtering, severity, component, event type, exact runtime session ID and correlation ID, and exact training-job, dataset and model identifiers when recorded in safe event details. Literal text search covers messages and safe event details. Those identifiers are optional in source events; no match means no matching recorded identifier, not proof that the source activity never occurred.
- Each HTTP response returns `X-VAOS-Correlation-ID`. Clients may send a bounded opaque value in the same request header to connect related requests; missing or invalid values are replaced with a generated ID. Request-scoped audit events are searchable by that ID. Do not place names, email addresses, secrets or other personal information in the header. Background events without a request context may remain uncorrelated.
- Cursor-based paging, 50 events per browser page (API pages allow 1–200), with expandable redacted details, the recorded correlation ID, a per-event **Copy JSON** action and an authenticated **Open record** permalink. Direct links return 404 once the event expires under the configured event-details retention policy (90 days by default).
- Up to 20 named saved filters per authenticated account. A filter stores only the supported time period/range, severity, component, event type, runtime session ID, correlation ID, training-job, dataset and model identifiers, and literal search values. Account identities are hashed for storage, so filters are not shared between Basic-auth users or API-token principals.
- A live-tail toggle that polls the newest matching events every three seconds; pausing cancels and invalidates any in-flight poll so it cannot repaint after the paused state appears, and opening an older page stops the refresh.
- CSV/PDF buttons that create jobs through the central audited report service, plus direct NDJSON export capped at 1,000 matching rows. With **Lifetime** selected, report exports use the retained 90-day event window; narrower time and field filters are applied consistently.
- Empty, loading and request-error messages.

The adjacent central **Reports & exports** service offers CSV and PDF exports for operational events. Choose the `events` report type and provide the desired time, severity, component, event-type, runtime session ID, correlation-ID, training-job, dataset, model and search filters. The report service applies the same bounded event query, records the request/download and limits generated files to 5,000 rows and 12 MiB. Event exports include recorded session and correlation identifiers.

## API

`GET /api/admin/observability/events` (also `/api/v1/admin/observability/events`) accepts:

| Parameter | Meaning |
| --- | --- |
| `after`, `before` | UTC time range `[after, before)`; maximum range is constrained by event retention. |
| `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `component` | Exact component value, up to 80 characters. |
| `event_type` | Exact lower-case event key. |
| `event_id` | Read one event at `/api/admin/observability/events/{event_id}`; the detail endpoint returns 404 when the event is absent or expired. |
| `session_id` | Exact runtime session identifier, up to 120 characters; matches only events recorded with that session. |
| `correlation_id` | Exact optional request/operation correlation value, up to 120 characters. This is not a universal trace ID; only events that were recorded with the same correlation value match. |
| `training_job_id` | Exact training run identifier in safe event details (`training_job_id` or the recorded `run_id`), up to 120 characters. Only events that recorded this value match. |
| `dataset_id` | Exact dataset identifier in safe event details (`dataset_id` or the recorded `source`), up to 120 characters. Only events that recorded this value match. |
| `model_id` | Exact model identifier in safe event details (`model_id` or the recorded `initial_model`), up to 120 characters. Only events that recorded this value match. |
| `search` | Literal case-insensitive text in message, event type or serialized safe details; maximum 200 characters. It is not a regular expression. |
| `cursor`, `limit` | Opaque next-page cursor and page size from 1–200. The API returns `has_more` and `next_cursor`. |

The browser requires administrator Basic authentication. API and MCP event reads require a Diagnostics token plus both the installation sensitive-read setting and that token's sensitive-read grant. Security audit records are a separate restricted endpoint and are not mixed into the mutable operational event list.

The separate **Security events** table supports an exact `actor_ref` filter, alongside time, event type, outcome, component and literal text. The actor reference is the value VAOS recorded for that audit row; depending on the event, it may be a local service label, an API-token prefix or a hashed reference. It is not guaranteed to identify a verified individual or tenant. Both the page and API limit it to 120 characters. Audit search remains behind administrator authentication and the API sensitive-read policy.

## Linux terminal examples

The CLI calls the same authenticated API and honors its token scopes. It can format matching records for shell pipelines:

```sh
vaos logs list --component training --level ERROR
vaos logs search --component training --level ERROR --format ndjson
vaos logs search --session-id 2bb84936-bc96-40a3-9cca-c32ec886a523 --format ndjson
vaos logs search --correlation-id run-abc123 --format ndjson
vaos logs search --training-job-id vaos_objects_20261111_120000_a1b2c3 --dataset-id uvh26 --model-id best.pt --format ndjson
vaos logs cat --format text | grep -Ei 'cuda|error|failed'
vaos logs cat --format ndjson | grep 'training'
vaos logs tail --follow --component training --format ndjson
```

`logs cat` follows cursor pages up to the 5,000-record CLI safety limit. `logs list` and `logs search` return a bounded page; `logs tail` polls until Ctrl+C. Session and correlation filters match exact recorded values. Job, dataset and model filters also use exact recorded values, and only match events where safe details contain that identifier. Text can be appended to a named file, and JSON/NDJSON can be redirected from stdout. These commands query the event store; they do not read arbitrary local log paths or bypass authorization. See [the CLI reference](cli-reference.md) for credential setup, formats, timezones and exit codes.

## Current boundaries

The operational Log Explorer does not yet provide regular expressions, virtualized rows, or universal filters for actor, camera, request ID and trace ID. Its exact correlation and job/dataset/model filters only match optional recorded fields. Security audit has a separate exact actor-reference filter, but that value does not establish a universal actor or tenant identity. Per-event JSON can be copied from the expanded record. Legacy plain-text records and uninstrumented Python logging calls are not searchable here. Operational event text follows the administrator-configured event-details policy (90 days by default); the append-only security audit has its separate retention and access policy.
