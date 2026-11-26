# VAOS release notes

This file records work in the current source tree. It does not imply a tagged release or that the running services have loaded these changes.

## v1.3.3 — stable · 2026-11-26

- Renamed the full-width detector training card to **ML TRAINING**.
- Released Daksh-AI engine controls, supervision, calibration/history learning and API/MCP contracts, plus the dedicated Detection workflow.
- Synchronized stable version metadata and refreshed guides, FRS/SRS, release notes and wiki.
- See [release notes](docs/RELEASE_NOTES_1.3.3.md) and [validation](docs/VALIDATION_1.3.3.md) for upgrade instructions and evidence limits.

## v1.2.6 — 2026-11-24

- Reviewed-sample class distribution now opens in a searchable modal with CSV/JSON exports, separate model counts, explicit refresh and unavailable states.
- About distinguishes installed and running versions and links to GitHub issues and releases.
- Background stable-release checks add a dismissible update banner, offline handling and an opt-out. Installation remains manual.
- Dataset preparation lists completed public imports; public dataset purge validates managed roots and overlapping registrations.
- Added release documentation, private security-reporting guidance, contribution templates and dependency-update configuration.
- Retains the 1.2.0 launcher, telemetry, API/MCP and UI improvements. Source uses AGPL-3.0-only; see LICENSE and model notices.

Upgrade after active jobs finish. Back up configuration and data and preserve local changes. No automatic installation or rollback is included. Native accelerator certification and production deployment validation remain operator responsibilities.

## v1.2.0 — frozen source baseline · 2026-11-24

- Baseline includes cross-platform telemetry, randomized local dashboard ports,
  staging startup credentials/version output, API/MCP connection fixes, and UI
  layout, scrolling and navigation updates.
- Subsequent completed changes increment 1.2.x using `bump_version.py` and the
  repository's `AGENTS.md` instructions.
- This records a source baseline; it does not create a Git tag, deployment, or
  imply that existing services have reloaded the source.

## v1.1.0 — in progress

### Added in this working tree

- Switched VAOS source distribution from Apache-2.0 to GNU AGPL-3.0-only at the product owner's direction so the release license matches the bundled Ultralytics-trained custom checkpoint. Updated the root license, About page, package/citation metadata, README, contribution guide and licensing documentation. Added the active checkpoint and its training provenance metadata; raw datasets and historical checkpoints remain excluded.

- Cross-platform compatibility follow-up adds native macOS/Linux manual startup instructions, CPU/CUDA/MPS selection and resource-profile contracts, and explicit limits for untested OS/GPU/RAM combinations. Settings now hides Windows-only restart/tunnel controls on macOS/Linux, shows the manual restart path for model changes, and rejects unsupported restart calls rather than leaving an unprocessed command. CI source-contract matrices cover Windows, macOS and Ubuntu; no matrix result or Mac/Linux hardware certification is implied until it is recorded.
- Fixed the production Docker image manifest to include the local modules imported by the admin and camera applications, plus project metadata, license/contribution notices and documentation. A regression test checks the local import closure; an actual image build remains pending because Docker is unavailable in this environment.
- Filled in Ultralytics 8.4.183's non-OpenCV runtime dependencies (`nvidia-ml-py`, `ultralytics-platform`, and its current `ultralytics-thop` floor) while retaining VAOS's contrib-headless OpenCV package for `cv2.face` and avoiding a conflicting OpenCV wheel.
- The optional MCP bridge is now bounded to the MCP Python SDK 2.3 minor and has a dedicated stdio-host integration test that launches the real bridge and exercises tools/resources against a synthetic loopback API. A separate CI matrix is configured for Windows/macOS/Ubuntu. New installations start with Diagnostics-only MCP capability groups; explicit previously saved Settings selections are preserved.
- MCP API requests no longer follow redirects with the bearer token. Upload/binary base64 and metadata sizes are checked before decoding, and JSON bodies are bounded before parsing. Regression tests cover cross-origin redirects and oversized agent inputs.
- Added a beginner-oriented MCP host setup guide covering secure token placement, capability groups, approvals, shutdown/revocation, OS-specific interpreter paths and test boundaries. See [`docs/MCP_HOST_SETUP.md`](docs/MCP_HOST_SETUP.md) and [`docs/validation/platform-mcp.md`](docs/validation/platform-mcp.md).

- MCP invocation telemetry now retries from a bounded process-local queue with stable invocation IDs, preventing duplicate counts when the server committed a report but its response was lost. The bridge never repeats the tool action for telemetry. Queued reports may still be lost if the MCP host exits before retry.

- Security auditing records accepted and denied dashboard Basic-auth requests and otherwise-unhandled HTTP 401/403 authentication or authorization denials. Repeated events are coalesced in bounded process-local maps; event details omit client addresses, submitted usernames, credentials and raw API tokens. Valid API-token denials may include the stored token prefix as an actor reference.
- If the primary audit/event/outbox transaction fails for a standalone login or authorization event, VAOS now attempts durable replay through a private bounded SQLite sidecar (10,000 rows / 32 MiB). Stable IDs prevent duplicate audit events after ambiguous commits; the admin queue-status snapshot exposes only availability, pending/drop counts and size. If both stores fail, successful login remains fail-closed and the audit cannot be retained. MCP invocation telemetry remains in process memory until its retry succeeds.

- Added sampled NVIDIA board-power telemetry and estimated GPU-board energy where `power.draw` is supported. Energy uses trapezoidal integration across 20–90 second readings, excludes longer gaps, hashes device identifiers, and is explicitly not wall energy or training-job attribution. Unsupported power queries retain the existing utilization telemetry. The additive schema v9 migration preserves existing records.
- Added the source-only Observability & Analytics Centre: additive SQLite lifecycle/metric/event/security-audit storage, paginated APIs, a live-tail log explorer with exact runtime-session and optional correlation-ID filtering, per-account saved filters, authenticated per-event detail links and filtered CSV/PDF/NDJSON exports, 14 bounded CSV/PDF report types, sensitive-read and MCP approval checks, a disabled-by-default security-event delivery queue supporting signed HTTPS JSON, RFC 5424 Syslog TLS, and OTLP/HTTP JSON, plus JSON/NDJSON, CEF, and ECS-shaped mappings for supported transports. CSV/PDF use the central audited report service; direct NDJSON is capped at 1,000 events. The SIEM outbox is capped at 50,000 rows overall and 5,000 pending per destination; dropped sends are counted, and destinations can be archived with endpoint/credential references cleared. Dispatch uses a conditional 30-second lease with consistent microsecond timestamps for due checks and retries. A resolver stall beyond the lease can still cause duplicate external delivery, but a superseded worker cannot overwrite the newer queue result. OCSF is explicitly deferred because VAOS does not yet capture the required actor/source fields and must not invent them. No SIEM vendor receiver was tested. The standard-library `vaos` CLI is included. Training telemetry can reconcile retained current/rotated training logs on startup using idempotent event IDs. Admin and camera middleware now generate or validate a bounded request-correlation ID, return it in the response, and attach it to request-scoped observability/security events. Request-launched dataset/import and model-training workers preserve the caller context; unrelated background events may remain uncorrelated. This is not distributed tracing. Focused checks and release limitations are recorded in [`docs/observability`](docs/observability/overview.md).
- Fixed the CLI parser so the documented default `vaos stats --period 7d` command selects the overview report; added parser and request-routing regression coverage.
- Fixed CLI live log tailing to follow cursor pages during event bursts, de-duplicate across an inclusive timestamp watermark, bound retained IDs, and report catch-up overflow instead of silently skipping records. Live output is text or NDJSON; JSON/CSV are supported as one-shot snapshots.
- CLI severity filters now accept lowercase or uppercase names, matching the documented `--level error` example. A parser regression checks every command in the CLI guide, which now also explains how to inspect the separately managed systemd/journal output without implying VAOS installs a unit or imports those logs.
- A centrally configured About VAOS page, subtle dashboard-footer attribution, and creator/project metadata. VAOS was originally created by Prateek Dubey and is developed with contributions from the open-source community. See [creator and community credits](docs/CREDITS.md).
- Observability now records used bytes and percent for the VAOS data volume, and separate bounded p50/p95 server-side latency samples for successful local-video frames. The full data path, video name, frame, and local-video session token are not stored in these metric dimensions.
- The MCP activity report now includes outermost bridge tool calls with a fixed tool name, outcome and elapsed duration. Arguments and returned content are not collected; a read-only MCP token may submit only this internal telemetry record while API and MCP remain enabled.
- A schema-driven, administrator-only API Explorer backed by VAOS's generated OpenAPI contract. It supports same-origin requests, write confirmation, in-memory token entry, and credential-free curl/Python examples.
- A Hardware Intelligence page and diagnostics endpoint that reuse current VAOS telemetry, enumerate measured NVIDIA devices, distinguish GPU detection from CUDA runtime/inference state, and present recommendations as un-applied estimates.
- A Tracking & Event Rules page and versioned API for camera-scoped count-in-region and dwell-in-region rules, consecutive-frame confirmation, cooldowns, and metadata-only alert history capped at 5,000 rows. It reuses the current detector and approximate short-term tracker.
- Capped the API-request and inference-frame in-memory fallback maps at 500 keys each during temporary storage failures. Reserved mixed overflow buckets retain exact aggregate totals while reducing route/camera detail; the behavior has a focused recovery test.
- Added server-side API response-latency p50/p95 gauges, measured with a monotonic clock and sampled from a bounded 4,096-response buffer every 30 seconds. Each sample records the number of measurements and any dropped old samples; it does not claim client/network latency.
- Added per-run training observability: image and class counts when a run initializes, annotation counts only after the label scan succeeds, and terminal duration for completed, safe-stopped, or failed runs. Failed or incomplete scans do not report an annotation count.
- Added successful local-video review frame counts to the shared inference metric. The metric stores only the source type; it does not store video names, session tokens, or frames.
- Added successful camera frame-processing latency p50/p95 gauges from a separate bounded 4,096-sample window, flushed every 30 seconds and at graceful shutdown. The duration includes thread-pool scheduling, image decoding, face/object processing and result preparation; it excludes upload/network time and is not pure model/GPU latency.
- Isolated API/MCP security, hardware-report, tracking-rule, project-bundle, and observability tests, review records, updated API/MCP guides, architecture/traceability documents, and a community Apache 2.0 license file.
- Configuration-only Project Bundles with an explicit preview/import/apply flow and allowlisted archive handling. They intentionally omit private data, images, model weights, and datasets.
- Fixed the shared VAOS sidebar wordmark being clipped at a 1280×720 desktop viewport by preventing the brand row from shrinking and compacting sidebar navigation. Active templates now request the refreshed shared stylesheet.
- Fixed camera-service shutdown counter flushing: it referenced a removed pending-buffer variable and could abort before recording a clean camera stop. It now retries the shared request/frame counter flush and reports when an in-memory fallback remains; an isolated camera-service lifecycle regression test covers the shared installation ID and clean shutdown.
- Improved Metric History chart accessibility and state handling: its accessible title and description now identify the selected metric, plotted range, date window, and exact dimension filter; invalid points are excluded from the plotted-value summary, and clearing/unavailable metrics clears stale chart/table results. Added a frontend regression contract for these states.

### Existing behavior retained

- Existing person sightings, object-sighting summaries, camera/frame processing, sample review, model training, datasets, face enrollment, and the REST/MCP permission path remain in place. Tracking rule alerts use separate storage and appear in the existing activity clear action without deleting rule definitions.

### Validation and known limits

- Historical checkpoint (13 November 2026): the repository suite passed 186 tests in 38.664 seconds. Latest isolated Windows verification (15 November 2026, project Python 3.12.10) passed **296 tests in 65.020 seconds with one POSIX-permission skip**. The official MCP SDK v2.3 stdio host integration passed locally against a synthetic API fixture. No Mac/Linux CI result, named desktop MCP host, physical GPU/MPS run, production-data recovery or external SIEM receiver result is available; see [`docs/TESTING.md`](docs/TESTING.md), [`docs/validation/platform-mcp.md`](docs/validation/platform-mcp.md), and [`docs/observability/validation.md`](docs/observability/validation.md).
- The earlier 50-epoch model training attempt failed at epoch 6/50 due to a DataLoader NumPy memory-allocation error; the source/live services were not restarted for this work.
- The API Explorer, About, Project Bundles, and Tracking pages have not been exercised in a browser against the source build. Observability received a focused authenticated browser smoke against an isolated source service, but not a complete usability or responsive test. AI Model Arena and Ready-to-Run Vision Applications are deferred beyond v1.1.0 by product-owner scope decision and are not release blockers for this version. Project Bundles only share safe configuration and local references; they are not full backups. Tracking also lacks directional line crossing and alert delivery.
- The source and live service versions differ. Staging remains **NO-GO** until all feature, regression, security, usability, migration, documentation, and performance gates have evidence.
