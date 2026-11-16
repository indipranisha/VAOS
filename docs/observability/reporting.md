# Observability reports

Reports are generated from VAOS's local observability store as asynchronous jobs. The UI and CLI submit a request, poll the job, then download a generated CSV or PDF. A job can be cancelled while it is still queued. A running job cannot be cancelled in the current implementation.

## Available report types

| Report | Required filters | Contents / maximum |
| --- | --- | --- |
| Installation lifetime statistics | None | Current KPI catalogue and availability; up to 100 rows. |
| Operational events and logs | `after`, `before` | Redacted event rows with optional severity, component, event type, exact runtime session ID, exact correlation ID and text; up to 5,000 rows, at most 366 days. Correlation IDs are optional and are not universal trace IDs. |
| Append-only security audit events | `after`, `before` | Audit rows with optional component, event type, outcome, exact actor reference (up to 120 characters) and text; up to 5,000 rows, at most 366 days. Actor references may be service labels, token prefixes or hashes, not verified people. |
| Runtime lifecycle sessions | `after`, `before` | Session start/heartbeat/stop state; up to 5,000 rows, at most 366 days. |
| One metric series | `metric`, `after`, `before`, optional exact `dimensions`, `group_by`, `aggregation`, and `interval` | Raw points or supported bucket aggregates with timestamps, dimensions, source-record count, aggregation, provenance, measurement scope and estimate marker; up to 1,000 rows, at most 366 days. The Metric Explorer export uses the same central report job and copies its current controls. |
| Training activity and epoch metrics | `after`, `before` | Recorded training events and `training.*` measurements. Legacy log files are not reconstructed. |
| Dataset preparation and reviewed sample inventory | `after`, `before` | Forward validation/import summaries plus counts grouped by confirmed/suggested label and review state. Paths, dataset names and image bytes are excluded. Annotation totals appear only when a worker verified them. Imports before this telemetry was added are not reconstructed. |
| Model activity | `after`, `before` | Recorded model events only. Model files and weights are excluded. |
| Inference activity | `after`, `before` | Aggregated successful camera and local-video frame responses. A successful response does not prove an object was detected; raw frames and local-video identity are excluded. |
| Camera and stream inventory | `after`, `before` | Current camera names, source type, active state and latest summary. Camera tokens and stream URLs are excluded. |
| Object tracking and rule events | `after`, `before` | Stored object-rule events; camera tokens and stream URLs are excluded. |
| Hardware and energy observations | `after`, `before` | Recorded `hardware.*` observations only. GPU energy is estimated from supported measured board-power samples; gaps and device-only scope are disclosed. |
| API activity | `after`, `before` | Aggregated request counts with stored route/method/status dimensions; request bodies and credentials are excluded. |
| MCP activity and approvals | `after`, `before` | MCP security audit events plus local bridge tool name, success/error outcome and elapsed duration. Tool arguments and returned content are not collected or exported. |

Times must include a UTC offset; normalized report values are UTC. Date ranges are ordered, end-exclusive internally, and cannot exceed 366 days. The report API rejects unsupported filters instead of silently ignoring them.

## Shortcuts from feature pages

The Control Room, Model and Services settings, API and MCP settings, API Explorer, ML Training, Training Activity, Dataset Preparation, Hardware, Tracking & Event Rules, and Video Review pages link to their matching report type here. A shortcut selects the report type and opens this central form; it does not create or download a report automatically. Review the date range and output format, then select **Generate report**. The same authentication, sensitive-read checks, provenance, expiry, and download audit apply as when starting from this page.

## Limits and safety

- Only CSV and PDF are supported. Report creation, listing, status and downloads use existing administrator authentication; API-token report data is restricted by the caller's identity. Report generation/download requires the sensitive-read permission.
- One requester may have up to five queued/running report jobs; the installation-wide active queue is capped at 100 jobs and the report list at 100 records.
- Output is capped at 5,000 rows and 12 MiB (1,000 rows for metric-series). A report that exceeds a row cap fails with `report_limit_exceeded`; the current bounded implementation does not silently present a partial result as complete. Reports expire 24 hours after request. Applying the event/metric/delivery source-data cleanup does not invalidate already-generated report files; a report may therefore remain downloadable until its separate expiry even when its source rows have been redacted or removed. This distinction is shown in the retention controls and cleanup confirmation.
- CSV files include a record-kind column and report ID, generation time, applied filters and source coverage note. PDF reports show the same provenance and coverage note. CSV remains the preferred format for full detail; PDF long text is clipped to fit.
- Report files are written with owner-only POSIX permissions or a restricted Windows ACL for the current account, SYSTEM and local Administrators. On startup VAOS expires old jobs and removes only orphan files whose names match the generated 32-character job-ID pattern. User-named files are not touched.
- Text fields beginning with spreadsheet formula characters are escaped to prevent spreadsheet formula injection; numeric values, including negatives, remain numeric. PDF page text is clipped to fit; it is not a substitute for the CSV when full long text is required.
- Each output has a SHA-256 integrity value. The server verifies file size and hash before allowing download. Downloads are recorded in `observability_export_audit` and the security audit.
- A service restart marks interrupted running jobs failed with `process_interrupted`; queued jobs are resubmitted at startup. A failed job must be requested again after the cause is resolved.

These are bounded reports, not a full VAOS backup. They do not export camera images, face images/embeddings or model weights. Several report families intentionally export only the data VAOS currently records; a no-records result means unavailable history, not a measured zero. Dataset reports combine reviewed-sample aggregates with forward validation/import summaries for the fixed supported dataset kinds. They do not reconstruct preparations made before this instrumentation was enabled, and dataset paths, user-provided names and image bytes are excluded. Model event reports remain empty until that component emits observability events. Training activity queued in memory may lose its most recent updates if the process terminates abruptly; the separate legacy training log remains the existing source for that history.
