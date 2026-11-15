# VAOS testing and validation

## Current automated checks

The test suite uses Python's standard `unittest` runner and Starlette's in-process test client. Tests set `VAOS_DATA_DIR` and `MODEL_DIR` to a temporary directory **before importing `main`**. They never open the user's `data/app.db`, touch model weights, start a camera, launch training, or call an external service.

From the repository root, with the development test dependency installed:

```powershell
python -m pip install -r requirements-dev.txt
python -m py_compile main.py camera_service.py mcp_server.py
python -m unittest discover -s tests -v
```

GitHub Actions is configured to run these checks on clean Windows, macOS and Ubuntu Python 3.12 workers for pushes and pull requests. A separate optional job installs the pinned MCP SDK and launches the bridge through the SDK's stdio client. These are configured CI jobs; until their run results are recorded, this local work session does not count them as passing. CI does not deploy the application.

## Latest local verification (15 November 2026)

The latest isolated Windows suite ran **298 tests in 61.594 seconds: 296 passed and 2 were skipped**. The skips were the POSIX file-permission assertion (not enforceable by the Windows filesystem) and the optional real MCP SDK host integration (the pinned SDK is not installed in the project environment). The SDK v2.3 stdio-host integration separately passed against a synthetic loopback API fixture using hash-verified official PyPI artifacts in a temporary target. New packaging regressions verify that local Python imports required by app entry points are copied into the Docker image and that Ultralytics runtime dependencies are declared without replacing VAOS's contrib-headless OpenCV package. One earlier full run had a transient failure in the loopback SIEM dispatch assertion; the test passed alone, in 10 consecutive repetitions, and in both subsequent full suites. The initial cause was not established. The suite also exercises the non-Windows restart guard and Settings capability messaging, Diagnostics-only MCP defaults, and CPU/CUDA/MPS training-target resolver. Python compilation passed at this checkpoint. No live service, user database, dataset, model, camera or training job was used. This is Windows-only evidence; the configured macOS/Ubuntu CI results are not yet available.

## Covered now

- Stable `/api/v1` health alias and repeatable generated OpenAPI output.
- Admin authentication on the API reference.
- API token hash-at-rest, read-only enforcement, feature-group isolation, sensitive-read opt-ins, and immediate revocation.
- Sensitive path inventory for personal data and capability links.
- Hardware report logic using injected facts only: multiple GPU rows, missing telemetry, CUDA unknown/verified states, active-job warnings, and host-path redaction. These tests do not run `nvidia-smi`, load PyTorch, or touch a GPU.
- Tracking rule validation/evaluation using synthetic detections only: count thresholds, consecutive frames, cooldown, region bounds, camera scope, dwell continuity, disabled rules, and tracking-disabled behavior. These tests do not open a camera or load a model.
- Tracking REST contract/auth coverage: event-group scope, sensitive-read opt-in, MCP sensitive-action gate/approval, create/update/delete and OpenAPI routes, using the isolated temporary database.
- Observability storage/report/SIEM/CLI: verified backup and migration preservation through schema v8, including the exact actor-reference/time index, plus an isolated synthetic restore round-trip that checks the backup manifest size/hash, copies the pre-migration database to a separate recovery path, runs `PRAGMA integrity_check`, and verifies the fixture row remains present; account-scoped saved Log Explorer filters; destination-mapping migration, destination archival, idempotent counters, unknown lifecycle states, redaction/retention, audit-chain tamper detection, CSV formula safety, PDF integrity, report queue/cancel/recovery/expiry, HTTPS/Syslog TLS/OTLP endpoint rules, HMAC/bearer credential handling, RFC 5424 framing, OTLP Logs JSON shape, CEF timestamp mapping, NDJSON/ECS mapping shapes, rejection of OCSF while required source identities are absent, bounded queue/history and leased dispatch, retry/dead-letter, CLI date/timezone/security rules and text output. These use temporary data and mocked connections; no live receiver is contacted.
- Selected Feature 8 API TestClient coverage: OpenAPI paths, diagnostics scopes, report ownership/sensitive-read checks, download auditing, audit cursor protection, opt-in SIEM config and MCP exact-request approval. The tests inject temporary VAOS data paths before importing the app.
- Optional real MCP process/transport coverage: `tests/test_mcp_stdio_host.py` launches `mcp_server.py` as a child process and connects with the official MCP SDK client. Against a temporary synthetic loopback API, it checks tool/resource discovery, policy-resource reading, a permitted health call, token forwarding and argument-free invocation telemetry. It does not use a live application, branded agent-host UI or production data. Install `requirements-mcp.txt` to run this check locally; the dependency-free suite skips it when the SDK is absent.
- Portability contract coverage: `runtime_compat.py` is exercised with mocked CPU/CUDA/MPS inventory and platform path behavior. The OS matrix does not substitute for native Mac/Linux installation, camera access, driver, MPS/CUDA, low-memory, inference or training validation. The tested and untested targets are listed in [Platform support](PLATFORM_SUPPORT.md).
- macOS/Linux local service-control contract: with the Windows launcher marked unavailable, the restart API fails clearly without writing a stale launcher command, Settings hides Windows-only restart/tunnel buttons, and the saved model choice explains the required manual service restart.

These are contract/unit checks, not browser-based usability or production acceptance tests. A passing result does not mean a feature was tested on live GPU hardware.

Historical checkpoint (13 November 2026, superseded): the repository-wide run passed **198 tests in 38.750 seconds**. That checkpoint covered a no-network CLI guide parser check, authenticated report API matrices, correlation and identity filters, MCP telemetry/privacy, dataset/training propagation, multi-process logs/counters, failure recovery, SIEM queue safety and the observability frontend contracts. External SIEM compatibility remains untested. All automated app tests use temporary data, and none launches training or opens a camera. The run emitted Starlette TestClient and Pydantic deprecation warnings, plus Python's expected duplicate-archive-member warning from a hostile-bundle rejection test. Do not infer browser, performance or production readiness from these test results; see [Feature 8 validation](observability/validation.md) and the Phase 1/2 review records.

**Current isolated repository run (13 November 2026):** the standard `unittest` suite passes **248 tests in 50.443 seconds**. The latest added regression checks the Statistics & Observability mobile CSS constraints. A separate isolated browser review visually checked that page at 320×568, 320×720 and 390×844; it is not automated browser coverage and does not establish application-wide responsive or accessibility readiness.

## Creator attribution manual checks

Before release, test the actual source build in a browser at a desktop viewport (for example, 1440×900) and a phone viewport (for example, 390×844):

1. Sign in and open **About VAOS** from the application navigation. Confirm Prateek Dubey and the **Founder and Original Creator** role are visible.
2. Open the website and LinkedIn links. Confirm the browser destination matches VAOS_PROJECT_METADATA.json; the links open separately and do not replace the VAOS page.
3. At both widths, check that the profile links wrap without horizontal overflow or overlap. Use Tab and Enter to verify keyboard focus is visible and both links activate.
4. Inspect the dashboard, camera, enrollment, ML training, review, dataset, hardware, tracking, API and project pages. The compact footer attribution must not cover controls or require an acknowledgement.
5. Confirm the README, documentation overview, credits, citation metadata and release notes retain the same attribution and contributor/licensing language.

Record the browser version, viewport, link results and screenshots. These checks have not yet been run against the current source build; source inspection alone is not a pass.
## Scope exclusions

AI Model Arena and Ready-to-Run Vision Applications were deferred beyond v1.1.0 by product-owner direction. Their feature-specific suites are not v1.1.0 release gates.

## Required release suites still to add

- Database migration and restore tests using a copy of every supported prior schema.
- MCP SDK runtime/transport, approval lifecycle, tool scope and denial tests.
- Tracking direction, jitter, occlusion, reconnect, retention, and webhook signature/retry tests.
- Hardware-provider checks on available CPU/GPU environments; report unavailable hardware as skipped. The current active training run prevents a real-device benchmark in this work session.
- Project bundle round-trip, path traversal, archive bomb, hash, privacy, and conflict tests.
- Existing camera, consent, face, dataset import, training safe-stop, model selection, and launcher regression tests.
- Isolated browser persona journeys, accessibility/keyboard checks, screenshots at supported viewport sizes, and performance measurements.

Do not mark staging ready until those applicable suites have recorded results and high-severity defects are resolved. See [the traceability matrix](TRACEABILITY_MATRIX.md) and [baseline audit](reviews/VAOS-1.1.0-baseline-audit.md).

The isolated API suite verifies coalesced successful and failed Basic-auth events and generic 401/403 authorization-denial events, including that submitted credentials and client addresses are absent from audit details. See [Feature 8 validation](observability/validation.md).

**Central report routing follow-up (13 November 2026):** the Live System page now exposes the same central Cameras report as the Control Room. The frontend contract checks that page links use a report type registered by the service. All 21 Jinja templates parsed; the focused frontend suite passed 17 tests and the complete isolated suite passed 248 tests in 50.443 seconds. No browser export journey was added by this change.
