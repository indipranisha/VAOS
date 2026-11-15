# VAOS testing and validation

## Current automated checks

The test suite uses Python's standard `unittest` runner and Starlette's in-process test client. Tests set `VAOS_DATA_DIR` and `MODEL_DIR` to a temporary directory **before importing `main`**. They never open the user's `data/app.db`, touch model weights, start a camera, launch training, or call an external service.

From the repository root, with the development test dependency installed:

```powershell
python -m pip install -r requirements-dev.txt
python -m py_compile main.py camera_service.py mcp_server.py
python -m unittest discover -s tests -v
```

GitHub Actions runs these checks on clean Windows, macOS and Ubuntu Python 3.12 workers for pushes and pull requests. The latest completed result at source commit `d8ec25c` passed all eight jobs in [run 43765194760](https://github.com/indipranisha/VAOS/actions/runs/43765194760): three OS unit-contract jobs, three MCP stdio jobs, a real Chromium UI smoke, and the production CPU-container smoke. The container job built both images, started isolated admin and camera services, verified their health endpoints, checked CPU detector imports, and exercised authenticated internal service status. CI does not deploy the application.

## Latest local verification (15 November 2026)

The latest isolated Windows suite ran **300 tests: 298 passed and 2 were skipped in 62.728 seconds**. The skips were the POSIX file-permission assertion (not enforceable by the Windows filesystem) and the optional MCP SDK host test not installed in the existing environment; the SDK stdio-host integration separately passed in CI on Windows, macOS and Ubuntu. After a Windows CI run exposed a timing-sensitive SQLite counter write, the focused four-process regression passed **10 consecutive repetitions** locally with a bounded five-second busy timeout; that fix also passed in the full eight-job CI run above. Python compilation, five JavaScript syntax checks, and a Markdown link scan (259 relative links across 58 files; none broken) passed. The production CPU-container build/start smoke passed in CI; Docker is not installed locally. No live service, user database, dataset, model, camera or training job was used.

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

The browser job `tools/ci_ui_smoke.py` starts temporary CPU-only admin and camera services, checks 12 page routes plus navigation, creator links, keyboard focus and 390 px horizontal overflow on three pages, and uploads desktop/mobile screenshots as a GitHub Actions artifact. This complements the unit/API contracts; it is not a full persona-based usability or production acceptance test and does not test a live GPU.

Historical checkpoint (13 November 2026, superseded): the repository-wide run passed **198 tests in 38.750 seconds**. That checkpoint covered a no-network CLI guide parser check, authenticated report API matrices, correlation and identity filters, MCP telemetry/privacy, dataset/training propagation, multi-process logs/counters, failure recovery, SIEM queue safety and the observability frontend contracts. External SIEM compatibility remains untested. All automated app tests use temporary data, and none launches training or opens a camera. The run emitted Starlette TestClient and Pydantic deprecation warnings, plus Python's expected duplicate-archive-member warning from a hostile-bundle rejection test. Do not infer browser, performance or production readiness from these test results; see [Feature 8 validation](observability/validation.md) and the Phase 1/2 review records.

**Historical focused UI checkpoint (13 November 2026):** the standard `unittest` suite passed 248 tests in 50.443 seconds at that earlier checkpoint. A separate isolated browser review checked the Statistics & Observability page at 320×568, 320×720 and 390×844. This historical page check does not establish application-wide responsive or accessibility readiness.

## Creator attribution browser smoke and remaining manual checks

The CI browser smoke checked the creator statement, metadata-backed website/LinkedIn `href` values and safe new-tab attributes on desktop, and confirmed that About renders without horizontal overflow at 390×844. It did not open the external sites or test every creator link at mobile size. Before release, complete these manual checks:

1. Sign in and open **About VAOS** from the application navigation. Confirm Prateek Dubey and the **Founder and Original Creator** role are visible.
2. Open the website and LinkedIn links. Confirm the browser destination matches VAOS_PROJECT_METADATA.json; the links open separately and do not replace the VAOS page.
3. At both widths, check that the profile links wrap without horizontal overflow or overlap. Use Tab and Enter to verify keyboard focus is visible and both links activate.
4. Inspect the dashboard, camera, enrollment, ML training, review, dataset, hardware, tracking, API and project pages. The compact footer attribution must not cover controls or require an acknowledgement.
5. Confirm the README, documentation overview, credits, citation metadata and release notes retain the same attribution and contributor/licensing language.

Record the browser version, viewport, external destination results and screenshots. The run's desktop and mobile screenshots are attached to [CI run 43765194760](https://github.com/indipranisha/VAOS/actions/runs/43765194760). The broader camera, enrollment and end-to-end user journeys remain open.
## Scope exclusions

AI Model Arena and Ready-to-Run Vision Applications were deferred beyond v1.1.0 by product-owner direction. Their feature-specific suites are not v1.1.0 release gates.

## Further release evidence still to capture

- The automated database migration, MCP transport/approval, tracking-rule, hardware-report and project-bundle suites are present; they do not replace target-host restore, real webhook/receiver, physical camera or GPU checks.
- Complete the isolated browser persona journeys, keyboard/accessibility review, screenshots at supported sizes, and representative performance measurements.
- Validate Apple MPS and NVIDIA CUDA inference/training on actual devices; mocked CI cases are contract checks only. Record low-memory behavior and tested minimum profiles.
- Rehearse a representative deployment backup, paired data-directory restore, migration and rollback on the target staging environment.
- The production CPU image build and two-service startup/health check passed in CI; validate the GPU image on a compatible NVIDIA host if GPU deployment is intended.
- Review exact platform-resolved dependencies and scan the release images, including the PyTorch/torchvision source that is outside the prior PyPI advisory scan.

Do not mark staging ready until those applicable suites have recorded results and high-severity defects are resolved. See [the traceability matrix](TRACEABILITY_MATRIX.md) and [baseline audit](reviews/VAOS-1.1.0-baseline-audit.md).

The isolated API suite verifies coalesced successful and failed Basic-auth events and generic 401/403 authorization-denial events, including that submitted credentials and client addresses are absent from audit details. See [Feature 8 validation](observability/validation.md).

**Central report routing follow-up (13 November 2026):** the Live System page now exposes the same central Cameras report as the Control Room. The frontend contract checks that page links use a report type registered by the service. All 21 Jinja templates parsed; the focused frontend suite passed 17 tests and the complete isolated suite passed 248 tests in 50.443 seconds. No browser export journey was added by this change.
