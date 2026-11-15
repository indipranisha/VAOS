# Platform and MCP validation record

**Updated:** 2026-11-15  
**Scope retained by product owner:** cross-platform operation and the MCP bridge/manuals.  
**Scope deferred:** external SIEM/restore/performance certification, broad persona/accessibility validation, advanced tracking work, AI Model Arena and Ready-to-Run Vision Applications.

## Evidence available in this work session

- Windows / Python 3.12.10 local source tree. The latest full isolated suite passed **296 tests in 65.020 seconds**, with one skip: a POSIX-permission test that Windows cannot exercise. The official MCP SDK v2.3 stdio integration and the non-Windows service-control regressions also passed locally. Earlier focused and full-suite checkpoints are historical only.
- `runtime_compat.py` has unit coverage for Windows/POSIX dataset-root selection, process-visible CPU counting, mocked CUDA/MPS/CPU resolution and conservative memory profiles. These checks do not load drivers or run a model.
- `.github/workflows/tests.yml` has Windows, macOS and Ubuntu source-contract matrices, plus a separate optional MCP SDK stdio-host integration matrix. The workflow was inspected locally but was not dispatched, and no GitHub Actions result is available in this working session.
- The new `tests/test_mcp_stdio_host.py` launches the actual bridge process with the official SDK `Client` over stdio and talks only to a synthetic loopback REST fixture with temporary telemetry storage. It covers tool/resource discovery, a health tool call, policy resource access, explicit token forwarding and argument-free telemetry.
- The configured PyPI simple-index route returned malformed chunked-compression responses. To keep the application environment unchanged, the official SDK and dependencies were downloaded from official PyPI file artifacts into a temporary target; every artifact SHA256 matched its release metadata. The MCP test then launched the bridge as a real stdio subprocess, connected using the official SDK client, enumerated tools/resources, read policy, called health, verified token forwarding and checked argument-free invocation telemetry against synthetic loopback API/temp storage. The temporary SDK target was outside the repository and VAOS virtual environment.
- Added operator manuals for installation, beginner quick start, administration, camera/streaming, model training, security/privacy, database structure, frontend pages and extension boundaries. They are linked from the documentation index. Relative Markdown links in the documentation tree were checked locally; this is a link/path check, not an editorial or browser review of every page.
- No Mac, Apple Silicon/MPS device, or usable local Linux environment is available here. No CUDA, MPS, camera, training, Docker image, low-memory or performance benchmark was run. The GitHub Actions matrix is configured but was not dispatched; no CI result or named desktop/editor MCP host result is available.
- No live VAOS services, app database, model files or user datasets were changed for these checks.
- A simulated non-Windows Settings/API check confirmed that model choice can be saved with clear manual restart guidance, the Windows-only restart/tunnel controls are hidden, and an unsupported restart request is rejected without leaving a launcher command behind. This simulates the platform condition on Windows; it is not a native macOS/Linux browser run.

## Current release boundary

The code and documentation now describe Windows, macOS and Linux source paths and CPU/CUDA/MPS selection with explicit evidence limits. They do not certify the native application on Mac/Linux, actual MPS/CUDA training, arbitrary memory sizes, AMD/Intel accelerators, or feature parity for the Windows-only service restart/public-tunnel controls.

The MCP bridge remains optional and local-stdio only. The passing SDK test validates a real MCP process/client boundary against a synthetic local API, not a branded host UI or production VAOS. A passing Actions run on each OS and, separately, at least one named desktop/editor host exercise are still needed before advertising those integrations as tested.

**Release recommendation:** no staging recommendation from this record. First record a green Windows/macOS/Ubuntu CI run, a green MCP stdio matrix, and available target-device results. Real Apple MPS verification requires access to an Apple Silicon Mac. Keep user data and admin services local until those gates are addressed.

## Independent review record

| Review role / method | Findings and recommendation | Resolution |
| --- | --- | --- |
| Software architecture and platform compatibility — read-only review by an available Codex review agent, 2026-11-15 | No source-level blocker found in the CPU/CUDA/MPS selectors, process-visible CPU detection, conservative memory profiles, or the non-Windows service-control guard. Native Mac/Linux CI, Apple Silicon/MPS, Linux/Docker, real GPU training/camera and low-memory workload evidence remain unavailable. The reviewer also noted that local terminal restarts and production process-manager restarts should be distinguished in the platform guide. | Clarified the guide: local macOS/Linux restarts happen in the two Uvicorn terminals; production restarts use the container/host process manager. Keep the release NO-GO pending CI and target-device evidence. No disagreement remains. |
| MCP security review — read-only review by an available Codex review agent, 2026-11-15 | A bearer token could be forwarded by the default HTTP redirect handler; base64 size checks occurred after decoding, and MCP JSON metadata lacked a small bound. | Replaced redirect-following with a no-redirect opener; encoded file/binary lengths and JSON body sizes are checked before decode/parse, metadata JSON is capped at 64 KiB, and content type length is capped. Added local regressions for cross-origin redirects and pre-decode/pre-authorization size rejection. The reviewer’s findings are resolved in source; no disagreement remains. Named-host UI and OS CI are still unverified. |
