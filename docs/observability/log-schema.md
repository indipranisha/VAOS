# Observability event schema

Operational events use a typed SQLite record with redacted JSON details. The stable API object is:

```json
{
  "event_id": "opaque-stable-id",
  "timestamp": "2026-11-12T10:15:30.123456Z",
  "event_type": "training.epoch_completed",
  "component": "training",
  "level": "INFO",
  "message": "A training epoch completed.",
  "session_id": null,
  "correlation_id": "opaque-request-correlation-id",
  "details": {"epoch": 4, "training_job_id": "run-opaque-id"},
  "expires_at": "2027-02-10T10:15:30.123456Z"
}
```

## Field definitions

| Field | Stored meaning |
| --- | --- |
| `event_id` | Unique idempotency key. A repeated ID does not add a duplicate event or counter update. |
| `timestamp` | Normalized UTC event time. |
| `event_type` | Lower-case, dot-separated event key such as `lifecycle.session_started`. |
| `component` | Bounded source label such as `admin`, `camera`, `training` or `observability`. |
| `level` | Bounded severity text. Current UI filters expose DEBUG, INFO, WARNING, ERROR and CRITICAL. |
| `message` | Short human-readable summary after text redaction and length bounds. |
| `session_id` | Optional foreign key to a recorded runtime process session. |
| `correlation_id` | Opaque bounded ID for connecting request-scoped records. VAOS accepts a valid `X-VAOS-Correlation-ID` request header or generates 128-bit lowercase hexadecimal ID; the same value is returned in the response header and attached to request-scoped audit/observability events. Invalid, overlong or missing values are replaced. Background records that have no request context may leave this field empty. It is not a distributed trace ID. |
| `details` | Optional JSON metadata. It is recursively bounded and redacted before storage. |
| `expires_at` | Operational event-text retention deadline, based on the administrator policy (90 days by default). |

These fields are backed by `observability_events` in [the database schema](database-schema.md). Metric observations and counter deltas use separate typed tables and preserve their source event ID. A runtime session stores the VAOS version; operational events do not currently attach a source-version, installation ID, actor/tenant ID or distributed trace ID as universal fields.

## Redaction and size limits

The store removes values whose keys look like credentials or secrets, masks camera/enrollment capability paths, redacts query strings and local Windows paths, omits binary media, bounds nested values and truncates text. Event detail JSON is limited to 16 KiB. A missing field is not inferred from the process, user, camera or model context.

Raw images, frames, face templates, embeddings, model weights, API request bodies and credentials are not intended event payloads. Keep event metadata minimal and use opaque IDs. Sensitive security records are written to a separate append-only hash chain; the ordinary event table is not itself immutable. The hash chain detects edits or omissions against its currently retained records, but it is not an external cryptographic anchor: a host administrator with direct database access could disable triggers, rewrite rows and recompute the chain. Treat it as tamper-evident application history, not immutable or independently verified audit storage.

## NDJSON and log file distinction

`vaos logs cat --format ndjson` serializes authorized event-store records as newline-delimited JSON for pipelines. This is an export format, not a second durable log database or a promise that stdout/stderr has been captured. The current service still emits ordinary Python logs to its process output; legacy text logs are not automatically imported or rotated by the observability event store.
