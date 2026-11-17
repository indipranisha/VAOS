# VAOS 1.1.0 baseline audit

**Audit date:** 2026-11-12

**Repository:** `indipranisha/VAOS`, branch `staging`

**Source commit inspected:** `dc98711632a6102b2d31e7f5e89fcefbe13c56f7`

**Source version:** `1.1.0` (`main.py`, `README.md`)

**Live services observed earlier in this session:** admin and camera health endpoints both reported `1.0.3`. No service was restarted.
**Requested baseline in the supplied prompt:** `1.0.4`; this does not match the inspected source or the live services. Treat the prompt's baseline label as stale and use repository evidence.

**Scope update (2026-11-12):** the product owner later deferred AI Model Arena and Ready-to-Run Vision Applications beyond v1.1.0. The recommendations below are historical baseline findings, not current v1.1.0 implementation requirements or release blockers.

## Scope and method

This is a source/configuration audit of the repository, not a claim of end-to-end release testing. It inspected the FastAPI services and routes, HTML/Jinja templates and JavaScript, SQLite initialization, startup and container configuration, model and inference paths, REST/OpenAPI/MCP code, and checked for automated tests, CI, and license files. The active training process, live camera streams, real GPU throughput, and browser journeys were not exercised or changed.

Three genuinely available in-session independent reviewers completed read-only passes:

| Review pass | Areas inspected | Result |
| --- | --- | --- |
| Software Architect + Backend Engineer | Service boundaries, routes, SQLite, jobs, deployment, overlap with seven features | Reuse current services; use additive schemas; durable jobs and five feature areas remain gaps. |
| Frontend Engineer + UX Specialist | Pages/navigation, forms, states, responsive CSS, overlap and missing journeys | Keep the current application; navigation is repeated across templates and should be unified before adding many destinations. No browser usability test or screenshots were performed. |
| Security Engineer + QA Engineer | Auth, tokens, MCP permissions, file imports, DB migration behavior, tests/deployment | MCP per-action approvals and sensitive-read controls are release blockers; no automated test or CI suite exists. |
| Computer Vision + Hardware + Documentation | Tracking, detector, hardware checks, docs/license, sample assets | Existing tracking and telemetry are partial; Arena, Vision Apps and bundles are missing. Hardware benchmarks were not run. |

These were local read-only code reviews, not external organizations or hosted AI systems. Their detailed findings and implementation decisions are recorded below and in `docs/FEATURE_CATALOG.md`.

## Existing functionality inventory

### Application pages and entry points

| Route/page | Existing purpose | State |
| --- | --- | --- |
| `/` Control Room | Camera links/status, activity, enrolled people, resource telemetry and dashboard actions | Existing |
| `/settings` | Engine/model settings, tunnels, integrations/API tokens and MCP policy | Existing; API/MCP controls were just added in source 1.1.0 |
| `/training` | Review samples and configure/run custom model training | Existing |
| `/training/activity` | Training activity and history | Existing |
| `/datasets` | Validate/prepare/import/purge supported datasets | Existing |
| `/video-review` | Browser-decoded local video frame analysis; optional crop sampling | Existing utility; not a reusable application-template system |
| `/live-system` | Legacy alias to the unified operations view | Existing compatibility route |
| `/c/{token}`, `/enroll/{token}` | Public camera and consent-based enrollment flows | Existing capability-token flows |
| `/api/docs`, `/api/openapi.json` | Protected Swagger reference and generated schema | Existing in source 1.1.0; runtime 1.0.3 had not activated it |

The administrator application is FastAPI + Jinja templates + inline browser JavaScript/CSS in `main.py`, `templates/`, and `static/`. Camera intake and internal model status are separated into `camera_service.py` on port 8001; the admin service is normally port 8000. The Windows launcher is `start_windows.bat`/`start_windows.ps1`. Production configuration uses Docker Compose and Caddy (`Dockerfile`, `compose.production.yaml`, `compose.production.gpu.yaml`, `Caddyfile`).

### Backend, data, models, and operations

- FastAPI routes are declared in `main.py` and `camera_service.py`. The generated versioned REST contract currently has 41 paths and 49 HTTP methods. API tokens, capability groups, rate limits, a bounded access log and optional MCP controls were added in source 1.1.0. API/MCP access defaults off.
- SQLite schema initialization is in `main.py:init_db` (`CREATE TABLE IF NOT EXISTS` plus conditional `ALTER TABLE` additions). Existing tables include cameras/status, participants/photos, enrollment links, sightings/object sightings, app settings, API tokens/access log, and review samples. There is no migration ledger or migration CLI.
- Model use is local: OpenCV YuNet/SFace face detection/matching and an Ultralytics YOLO object runtime. Face matching uses the existing CPU path; object inference tries the configured/available CUDA path and has CPU fallback behavior. Training, dataset import/preparation, and some model operations run in process-local daemon threads guarded by locks, not a durable job queue.
- Dataset preparation, review-sample storage, training history, GPU/CPU/RAM diagnostics, tunnel controls, and a local-video frame endpoint already exist. Their persistence and lifetime are local to the configured data directory and current service process.
- Existing production files describe a one-instance deployment. README explicitly says HA, multi-user roles, and external identity management are not supplied. GPU Compose requires a user-selected CUDA PyTorch index; no live production deployment was inspected.

## Original seven-feature overlap assessment (pre-scope-change)

| Requested feature | Existing evidence and status | Decision | Blocking gaps before release |
| --- | --- | --- | --- |
| AI Model Arena | No Arena page, comparison job, immutable dataset fingerprint, or comparison report found. Existing model sources, training history, and detector runtime can be reused. | Originally proposed for implementation; superseded by the product-owner decision to defer beyond v1.1.0 on 2026-11-12. | Not a current v1.1.0 acceptance gate. Historical review remains in `VAOS-1.1.0-model-arena-review.md`. |
| Object Tracking & Event Rules | **Partial:** the shared frame pipeline tracks same-class detections by bounding-box overlap and expires temporary IDs; sightings/count summaries are persisted. | **Enhance/consolidate** the existing detector pipeline; do not create another detector service. | Persistent rules/sessions, user-editable zones/counting lines, dwell/threshold/cooldown events, alert delivery, retention, and accuracy/occlusion tests. IDs are approximate, not verified identity. |
| Built-in API Explorer | **Partial:** versioned API, protected Swagger/OpenAPI, API tokens, feature groups, rate limit and audit exist. The Settings link opens Swagger in another tab. | **Enhance** the existing OpenAPI/Swagger implementation, not a parallel API. | In-app searchable catalog and safer Try-it workflow, generated code examples, route-level docs links, and documented/tested API contract. |
| MCP Agent Control Centre | **Partial:** optional local stdio bridge, three guarded tools, two resources, API token/groups and Settings policy controls exist. | **Enhance** the existing bridge and authorization. | No agent identity/connection center or approval inbox; current sensitive-action toggles execute immediately once enabled. Add exact-parameter, expiring, single-use approval before control operations. |
| Hardware Intelligence Engine | **Partial:** CPU/RAM/GPU telemetry, NVIDIA enumeration and a device selection inventory exist. | **Enhance** telemetry and training/device recommendations. | Runtime backend/provider checks, measured compatibility, explainable memory/batch/image-size advice, OOM recovery guidance, and tests on real hardware. Estimates must be labelled. |
| Ready-to-Run Vision Applications | No reusable template catalog or application instantiation workflow found. Local Video Review is a separate utility and must remain. | Originally proposed for implementation; superseded by the product-owner decision to defer beyond v1.1.0 on 2026-11-12. | Not a current v1.1.0 acceptance gate. Existing Video Review remains a separate retained feature. |
| Shareable VAOS Project Bundles | No `.vaosproject` manifest, safe export/import, or archive validation found. | **Implement** an allowlisted, versioned bundle format. | Exclude private data/secrets by default; validate paths, size, hashes, compatibility and conflicts; dry-run and round-trip tests. Never execute bundle content. |

## Findings, risks, and resolutions

| Severity | Finding | Resolution/decision |
| --- | --- | --- |
| Release blocker | Source is 1.1.0 but live services are 1.0.3; the prompt says 1.0.4. | Keep training/live services untouched. Treat repository source as baseline and do not claim the new source is live. A safe post-training activation still needs a separately scheduled restart. |
| High | API read tokens can receive broad default groups, including profile photos, camera/enrollment QR or tunnel URLs, event/person data and review images. | Add a default-deny sensitive-read permission and safer token group defaults before release. Test every sensitive route. |
| High | MCP blocks sensitive writes by default but has no per-operation pending approval bound to exact parameters, identity, expiry and one-time execution. | Keep the feature fail-closed. Implement an approval inbox before any release claim of supervised agent control. |
| High | Uploaded `.pt` files are not validated in a quarantined process and provenance is incomplete. | Add explicit trust/provenance safeguards and safe validation before recommending arbitrary checkpoints; preserve existing model flow. |
| High | No automated test suite, CI workflow, virtual install, browser usability evidence, screenshots, or performance benchmark exists. | Add isolated tests and a repeatable local/container QA harness; report unavailable hardware measurements as skipped, never as passes. |
| High | No `LICENSE` file is present even though the product request specifies Apache-2.0. | Do not claim a verified Apache license until the license file and contributor/copyright metadata are confirmed. This is a legal release gate, not a code inference. |
| Medium | Tracking state is process-local/approximate; background jobs are daemon threads and are lost on process exit. | Add durable, additive jobs/events carefully; test cancellation and crash recovery before claiming durability. |
| Medium | UI navigation markup is repeated across templates. | Consolidate shared navigation before adding several destination pages; retain existing routes and visual behavior. |
| Medium | Project has no contribution, code-of-conduct, user manual, architecture, feature/function catalog, migration, security/privacy, or release-test documentation. | Build docs from actual code and mark unfinished sections explicitly; do not invent features or donation links. |

## Baseline checks

- Source `APP_VERSION` and README: **1.1.0**.
- Live admin and camera `/health`: **1.0.3** at the time observed; both returned `status: ok`. No service was restarted.
- Git: `staging` matched `origin/staging` at `dc98711` before this audit. Pre-existing untracked `*- Copy*` files and `VIBE_CODING_GUIDE.md` are outside the audited release and must not be swept into a commit.
- Tests/CI: no `tests/`, pytest configuration, `pyproject.toml`, or `.github` workflow was found.
- License: no `LICENSE*` or `COPYING*` file was found.
- UI and hardware: no browser journey, screenshots, actual training metrics, GPU benchmark, RAM/performance measurement, or camera test was run during this source-only audit.

## Release decision

**Current recommendation: NO-GO for staging deployment.** The requested features are not all implemented, the MCP approval and sensitive-read controls need work, model upload validation needs review, there is no verified Apache license file, and the required virtual/browser, regression, security, migration, and performance evidence does not exist. Code work may proceed locally in controlled phases. Staging must remain blocked until the release gates are evidenced and any access/credential/deployment approval required by the owner is obtained.

### Licensing follow-up — 17 November 2026

The preceding findings are the baseline audit as recorded at that time. The product owner subsequently selected GNU AGPL-3.0-only for the VAOS source and bundled Ultralytics-trained checkpoint. The root `LICENSE`, repository metadata and current licensing documentation were updated to reflect that decision. Third-party dataset and dependency terms remain separate and still require review; this addendum does not change the audit's staging recommendation.

## Independent review record

| Role | Findings recorded | Status |
| --- | --- | --- |
| Software Architect | Current two-service architecture, ad-hoc additive SQLite migrations, in-process jobs, feature reuse and durable-job gaps | Completed read-only in-session review |
| Backend Engineer | 41 versioned paths/49 methods; model/dataset/training and API reuse; missing Arena/rules/templates/bundles | Completed read-only in-session review |
| Frontend Engineer | Existing routes and repeated top-navigation markup; no missing-feature page journeys | Completed read-only in-session review |
| UX Specialist | Existing loading/error states but no usability test/screenshots; use shared navigation and accessible states | Completed read-only in-session review |
| Computer Vision Specialist | Existing approximate IoU tracking; YOLO26 inference path; no event-rule engine or benchmark evidence | Completed read-only in-session review |
| Security Engineer | Sensitive reads, unvalidated arbitrary `.pt`, missing MCP approval flow | Completed read-only in-session review |
| QA Engineer | No test suite/CI; release tests and virtual environment missing | Completed read-only in-session review |
| Documentation Specialist | Missing license and comprehensive user/developer/feature docs | Completed read-only in-session review |

These reviewers were available local agents in this Codex session. No external AI service, production user, or third-party reviewer was used. Findings were resolved into the classifications and release blockers above; no disagreements were reported.
