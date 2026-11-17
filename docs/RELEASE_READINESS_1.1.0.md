# VAOS v1.1.0 staging readiness report

**Assessment date:** 2026-11-15

**Candidate:** current working tree on branch `staging`

**Decision:** **NO-GO — do not deploy to staging from this checkout yet.**
**Deployment performed:** none.

## Release scope assessed

This report assesses the currently retained work selected by the product owner: **cross-platform operation** and **MCP host integration/documentation**. AI Model Arena and Ready-to-Run Vision Applications are deferred. The wider application feature inventory is context; this assessment does not imply that every unrelated v1.1.0 feature was newly validated.

## Evidence that passed locally

| Check | Result | Boundary |
| --- | --- | --- |
| Isolated Windows repository suite | **298 tests run in 61.594 seconds; 296 passed, 2 skipped** | One skip is POSIX file-permission enforcement unavailable on Windows; the other is the optional MCP SDK host test because `requirements-mcp.txt` is not installed in this project environment. The separate SDK v2.3 stdio integration passed on Windows/Python 3.12 using a temporary, hash-verified package target. No native Mac/Linux result. |
| Docker source and dependency manifest regressions | Passed, including in the full suite | Static tests verify that each local Python module imported by the production app entry points is copied into the image and that the pinned Ultralytics runtime dependencies are declared while retaining VAOS contrib-headless OpenCV. They catch manifest omissions but do not replace a clean install, image build, or container startup check. |
| Python compilation | Passed for the application, portability/MCP modules, and affected tests | Syntax check only. |
| Official MCP SDK v2.3 stdio integration | Passed against a synthetic loopback API and temporary telemetry storage | Exercises the bridge process/client transport, discovery, policy read, health request and minimal telemetry; not a named desktop-host UI or production instance. |
| MCP security regressions | Passed in the full suite | Includes cross-origin redirect rejection and input-size checks before base64 decoding/JSON parsing. |
| Platform/restart contracts | Passed in the full suite | Non-Windows behavior is simulated on Windows; not a native Mac/Linux browser run. |
| Production Compose YAML | Parsed and checked statically | Docker CLI is unavailable, so Compose interpolation/configuration, image build and startup were not run. |
| PyPI dependency audit | No known vulnerabilities found for the base/MCP requirements, detector requirements, and pinned `ultralytics==8.4.183` (scanned in separate resolving sets) | PyTorch/torchvision wheels are fetched from the official PyTorch index, which the PyPI advisory scanner cannot audit; no platform-resolved lock or built image was available. |
| Documentation links | **258 inline relative links checked across 55 documentation and root-guide Markdown files; 0 broken** | Local path check only; external URLs were not requested or verified here. |
| `git diff --check` | Passed | Git emitted line-ending conversion warnings for existing working files; no whitespace errors. |

## Release blockers

1. **No OS-matrix result:** the configured Windows/macOS/Ubuntu GitHub Actions workflow has not run for this candidate. Native macOS/Apple Silicon and Linux installs, camera paths, CPU fallback, MPS/CUDA behavior and resource limits are unverified.
2. **No target hardware evidence:** no actual CUDA or MPS inference/training, low-memory run, camera capture, or performance/stability measurement was performed. AMD/Intel acceleration and arbitrary RAM sizes are not supported claims.
3. **MCP host validation incomplete:** the SDK-level stdio boundary passes, but there is no named desktop/editor host result and no isolated browser walkthrough of token creation, permissions, approval, retry, revocation and failure recovery.
4. **No runnable container validation:** Compose topology was parsed, but no `docker compose config`, container build, health check, HTTPS proxy exercise, database migration, persistent-volume restore or rollback was run because Docker is unavailable in this environment.
5. **No clean release artifact:** the `staging` branch working tree contains extensive modified and untracked work. No reviewed commit/tag, reproducible artifact, or release rollback checkpoint was created. Do not treat this checkout as a versioned release candidate.
6. **Dependency security is only partially cleared:** PyPI-resolvable base, MCP, and detector requirement sets (including `nvidia-ml-py`, `ultralytics-platform`, and the updated THOP floor) plus pinned Ultralytics returned no known vulnerabilities. The separate audit of the existing developer `.venv` reported 72 advisory entries across filelock 3.32.3, opentelemetry-api 1.45.1, Pillow 12.3.0, pip 25.0.1 and setuptools 78.1.0; a read-only `pip check` also found that this old environment lacks the two newly declared detector dependencies. It is not a release artifact and must not be used to build staging. `pip check` also reports missing `opencv-python` metadata because VAOS intentionally supplies `opencv-contrib-python-headless` for `cv2.face`; the actual image/runtime behavior still needs container validation. The PyPI scanner could not audit the PyTorch/torchvision wheels from the separate PyTorch index. The requirements are ranges, not platform-resolved lock files, and no clean container image was scanned.
7. **Live-process identity is unknown:** the previously observed admin service reported 1.1.0, but its source path was unavailable; the paired camera service was not verified. Existing services were left untouched.
8. **Staging environment is not configured:** no staging host, domains, TLS/certificate evidence, or staging credentials were supplied or exercised.

## Reliability note

The first full run after the Docker fix had one loopback SIEM dispatch assertion failure. The test passed alone, in **10 consecutive focused repetitions**, and in both subsequent full suites. The initial cause was not established; it is tracked as a non-reproducible observation, not a confirmed defect or an independent staging blocker.

## Known product boundaries

- The source has CPU fallback and conditional NVIDIA CUDA / Apple MPS selection. This is not a promise that every model or workload fits available memory.
- NVIDIA telemetry remains vendor-specific. AMD/Intel acceleration and telemetry are not release-certified.
- Local macOS/Linux service restart instructions are manual; the Windows restart and temporary tunnel controls are not portable.
- MCP is an optional local-stdio bridge. Do not expose its token or process through a public tunnel.

## Go/No-Go recommendation

**NO-GO for staging deployment now.** The repository-level tests are green, but the release gates that establish the supported OS/hardware matrix, named MCP host behavior, reproducible deployment, and rollback have not been evidenced. Keep current services and user data unchanged.

## Conditions to move to GO

1. Produce a clean, reviewed release candidate from a known commit without discarding the existing working-tree changes.
2. Run and record green Windows, macOS and Ubuntu CI jobs, including the MCP stdio matrix.
3. Exercise an actual named MCP host through read-only setup and the human approval workflow.
4. Build and start both production containers behind the configured HTTPS proxy on an isolated staging host; verify health, migration, persistent storage, backup/restore, and rollback.
5. Resolve the exact platform-specific runtime and PyTorch/torchvision versions, run a certificate-validated dependency audit for those artifacts, and scan the built staging images; do not use the current developer `.venv` as the release environment.
6. On available representative devices, verify CPU fallback, NVIDIA CUDA and Apple MPS as applicable, plus camera startup and a bounded-resource inference/training smoke test. Mark unavailable vendors or hardware as unsupported rather than inferring success.
7. Review security findings, release notes, installation steps and the final user-facing limitation statement; then reissue this report with dated evidence and a GO/NO-GO decision.

No staging deployment should occur until the applicable gates pass and the release candidate is approved.
