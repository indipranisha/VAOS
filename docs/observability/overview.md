# Observability & Analytics Centre

Feature 8 adds a local, administrator-only view of VAOS lifecycle, telemetry, operational events, reports and optional external security-event delivery. It extends the existing admin service and cached hardware collector. It does not create a cloud account, a second sampler, or a second authentication service.

## Where it lives

- In the source build, open **Statistics & observability** from the admin navigation. The page is served at `/statistics`.
- The REST routes are under `/api/admin/observability/`; the versioned API exposes the same capability under `/api/v1/admin/observability/`.
- Optional terminal client: [`vaos`](cli-reference.md).
- The feature writes into the normal VAOS SQLite database under `VAOS_DATA_DIR`; generated report files are under `data/observability/reports` (or the configured data directory).

The existing admin health endpoint later reported 1.1.0, but the process source path could not be established and the camera listener was not rechecked. A separate source process passed an authenticated browser smoke on port 8765 with an isolated temporary data directory; this does not establish that the running installation has loaded these changes. See [validation and release state](validation.md).

## What is recorded

- Installation identity and admin runtime-session start, heartbeat and clean-stop records.
- CPU, RAM and per-GPU utilization samples drawn from the existing VAOS telemetry collector about every 30 seconds; measured NVIDIA board power and estimated GPU-board energy are added only where the sensor supports them.
- Redacted, bounded operational events, including training callback records and aggregated API/frame activity where those paths are instrumented. API requests and successful camera/local-video frames first update a durable 10-second aggregate in the same SQLite database; the background worker normally moves those rows to metric history within 30 seconds. Local-video frame counters retain only `source_type=local_video`, never the filename, session token, or frame content.
- MCP bridge tool calls record the fixed tool name, success/error outcome, and monotonic elapsed duration. The local bridge omits nested helper calls; arguments, returned content, file names, and token values are never sent as invocation telemetry. Reporting is best-effort and bounded to a two-second telemetry request so an unavailable telemetry endpoint does not replace the MCP tool's result.
- Successful camera frame-processing p50/p95 timings are sampled from a bounded in-memory window every 30 seconds and on graceful shutdown. Timing includes thread-pool scheduling, image decoding, face/object work and result preparation; it excludes client upload/network time and must not be interpreted as pure model/GPU inference latency.
- Successful local-video frame-processing p50/p95 timings use a separate bounded window on the same 30-second sampler. Timing includes server-side queueing and processing, excludes browser upload/network time, and is not pure model/GPU inference latency. The local video name, frame, and session token are not stored with these metrics.
- On admin-service startup, the current training JSONL file and up to 20 rotated segments (roughly 1 MB each) are replayed oldest-first with deterministic event IDs, recovering unflushed retained training records without double-counting. Older records can age out after the retention limit.
- Training telemetry records the resolved image count when a run initializes, the annotation total only after the existing train/validation label scan succeeds, and monotonic worker duration on terminal complete, safe-stop or failure events. Each run has an identifier from worker start, so preparation failures can be separated; counts describe source files and do not prove every image/box was consumed by the trainer.
- Dataset preparation records forward success/failure counts, monotonic duration and available image/annotation totals for managed local, UVH-26, Kaggle Auto and COCO preparation. The event identifies only the fixed dataset category and numeric counts; folder paths, user dataset names and image bytes are omitted. Earlier preparations are not reconstructed, and annotation totals remain unavailable when a worker did not verify them.
- The API/frame counter buffer is flushed transactionally with the corresponding event and counter rows; startup and graceful shutdown retry retained rows. SQLite write contention uses a bounded, process-memory fallback and remains a loss window if the process then stops before retry.
- Typed metric observations with unit, dimensions, source/provenance, scope, estimate marker and retention policy.
- Storage history for used bytes and percent on the volume containing VAOS `DATA_DIR`; it reuses the existing telemetry cycle and records only a fixed `vaos-data` volume label, not the full path.
- A separate append-only security audit chain with integrity verification.
- Local report job metadata and report-download audit records.
- Optional, disabled-by-default delivery queue for signed HTTPS JSON webhooks, Syslog TLS and OTLP/HTTP JSON logs, with selectable JSON/NDJSON, CEF and ECS-shaped mappings. The outbox is capped at 50,000 rows overall and 5,000 pending per destination; dropped sends are counted. Retired destinations are archived and cleared of endpoint/credential references while local history remains. OCSF is deferred until required actor/source fields can be provided without inference.

No camera image, raw frame, face template, embedding, model weight or dataset is copied into observability. Some legacy or not-yet-instrumented metrics are explicitly unavailable. A missing observation means “not recorded”, not zero.

## Main workflows

1. **Overview:** review lifetime counts and the selected time range. Uptime for an unknown session is a lower bound, not a confirmed crash duration.
2. **Metric Explorer:** choose a registered metric and bounded UTC time range; inspect raw points or a supported time-bucket aggregation. Group lines by one of that metric's registered dimensions, optionally filter one dimension to an exact value, and switch between chart and table. If more than eight groups occur in the returned points, the chart shows the eight groups with the most returned points and names the omitted count; use an exact dimension filter or the table/export to inspect the rest. CSV/PDF exports use the central audited report service and carry the selected metric, grouping, aggregation, time bucket, filters and range. Counter deltas can only be summed; gauge aggregation choices are returned by the metric catalogue. Aggregated SQL queries operate on all matching retained records in the selected range before the 1,000-row response cap, not just the points currently visible in the browser.
3. **Events:** filter by time, severity, component, event type, exact runtime session ID, exact correlation ID and text; use cursor pagination for stable navigation. Export filtered events as a central CSV/PDF report or up to 1,000 NDJSON rows. Every HTTP response includes `X-VAOS-Correlation-ID`; request-scoped security/audit events inherit that value. Background events without a request context may remain uncorrelated, and request IDs are not distributed trace IDs.
4. **Lifecycle:** inspect component sessions and whether they ended cleanly or have an unknown exit state.
5. **Security audit:** verify the chain and search restricted audit events by time, type, outcome, component, text or an exact recorded actor reference. Actor references can be service labels, token prefixes or hashes; they do not prove a person or tenant identity. The API token needs the sensitive-read grant in addition to diagnostics access.
6. **Reports:** request one of the supported bounded CSV/PDF reports, poll its job, and download it while its 24-hour expiry remains valid.
7. **Integrations:** configure a disabled-by-default signed HTTPS, Syslog TLS or OTLP/HTTP destination. Keep any credentials in the service environment, enable only when ready, and explicitly confirm test sends.

## Data behavior and limits

- Store schema is additive and versioned (`observability_store.SCHEMA_VERSION`). When upgrading a non-empty existing database, startup makes and verifies a SQLite online backup before applying the new schema. No live database was migrated during this implementation task.
- Retention is administrator-configurable in **Data retention**. Defaults remain 90 days for event details, up to 365 days for metric observations (each metric's catalogue limit can be shorter), and 90 days for completed delivery history. Supported choices are 30, 90, 180 or 365 days. Shortening a period requires confirmation and hides out-of-policy event details immediately; use **Apply cleanup now** to redact/remove expired stored detail. Increasing a period does not restore already expired or deleted data. Lifetime counters and the append-only security audit are preserved. Generated report files are separate copies, remain downloadable until their own 24-hour expiry, and are not removed by this cleanup.
- Event and session pages are limited to 200 rows; metric catalog pages to 500; metric series to 1,000 per request and 366 days per query.
- Report jobs allow at most 5 active jobs per requester; generated outputs are bounded to 5,000 rows and 12 MiB, and expire 24 hours after request. Retention cleanup for source events, metric samples and delivery history does not invalidate existing report files. Metric-series reports are limited to 1,000 rows.
- External delivery sends at most two queue items per 30-second worker cycle and retries with bounded backoff before dead-lettering. HTTPS/OTLP accept public TLS endpoints on port 443; Syslog TLS uses public TLS port 6514. Outbound delivery is off by default. Protocol-level mocked tests do not prove vendor interoperability; live receiver checks remain outstanding.
- GPU energy is estimated only when a supported NVIDIA sensor supplies consecutive measured board-power samples. Integration excludes long gaps and reports coverage and skipped intervals. It is GPU-board-only, not wall energy, and is not attributed to a training job. Utilization is never converted into watts.

## Related references

- [Metric dictionary](metrics-dictionary.md)
- [Energy measurement limits](energy-measurement.md)
- [Source and function catalogue](source-function-catalogue.md)
- [Lifecycle and uptime](lifecycle.md)
- [Log Explorer](log-explorer.md)
- [Event schema and redaction](log-schema.md)
- [Legacy lifecycle/log index](lifecycle-and-logs.md)
- [Report jobs](reporting.md)
- [Security event delivery and SIEM/SOAR integration](siem-soar.md)
- [Storage schema and migration](database-schema.md)
- [Security and privacy](security.md)
- [CLI reference](cli-reference.md)
- [Validation record](validation.md)
- [Feature 8 release-readiness report](release-readiness.md)
- [Rollback notes](deployment-and-rollback.md)
