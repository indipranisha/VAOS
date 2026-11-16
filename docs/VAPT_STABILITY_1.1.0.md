# VAOS 1.1.0 VAPT and stability assessment

**Assessment date:** 16 November 2026
**Target:** isolated `codex/vaos-v1.1.0-candidate` source checkout
**Method:** focused source review, dependency advisory query, isolated regression and boundary tests, and local launcher syntax validation.
**Status:** candidate assessment; this is not a third-party penetration-test certificate or production staging approval.

## Executive summary

The assessment found and corrected three release-relevant issues in the candidate: unbounded request-body parsing ahead of upload validation, mutable Cloudflare tunnel-binary downloads without a publisher-hash check, and a Syslog-TLS shutdown path that waited for a peer close-notify contrary to the sender behavior allowed by RFC 5425. A newly created Windows Python environment also retained its bundled older `pip`; the launcher now upgrades it before installing requirements. Regression tests were added for these cases.

The upload guard counts streamed bytes independently of `Content-Length`, rejects oversized requests with HTTP 413, and keeps the existing image, face-enrollment, project-bundle and 2 GiB model-source workflows within explicit route-specific ceilings. MCP approval fingerprinting still hashes the exact uploaded file for an authenticated control request, but the request is bounded before multipart parsing and hashing.

The isolated repository suite result and current CI result are recorded below after completion. A green code suite does not certify physical cameras, a real CUDA/MPS device, production proxy rules, a live SIEM receiver, or the target server's backup/restore process. See [release readiness](RELEASE_READINESS_1.1.0.md) for the remaining staging gate. **This report does not authorize or recommend a staging deployment.**

## Scope and boundaries

Included:

- Authentication, API/MCP permission and approval paths, multipart uploads, project-bundle import boundaries, path use, TLS/export behavior and security-sensitive settings reviewed in source and existing tests.
- Request-size limits, launcher download integrity, TLS close behavior and failure-path coverage added in this candidate.
- Installed-package advisory query against OSV for the isolated Python 3.12 environment, including core runtime packages and the optional PyTorch/Ultralytics packages present there.
- Isolated unit/contract suite, Python compilation, PowerShell parser check, documentation relative-link check, and Git whitespace validation.

Not performed:

- No external penetration-testing firm, internet-facing VAOS instance, production credentials, live camera, real user data, or production deployment was used.
- No authenticated vulnerability scan against a running staging host, fuzz campaign, formal threat-model workshop, or full load/soak/chaos test was performed.
- CUDA, Apple MPS, GPU memory, real-camera throughput, Mac/Linux native installation, low-RAM exhaustion, Windows ARM and third-party SIEM receiver interoperability were not measured on physical target hardware in this run.
- The local Docker CLI is unavailable. The CI production-container job is isolated and is not a target-host install or rollback rehearsal.

## Findings and disposition

| ID | Severity | Finding | Resolution / remaining action |
|---|---|---|---|
| VAOS-SEC-01 | Medium, resolved | Multipart file bodies could be spooled and then read for MCP approval fingerprints before endpoint file-size validation. Missing or false `Content-Length` could permit excess parser disk/CPU use. | Added streaming ASGI request-body ceilings before route parsing on both admin and camera services. Default writes are limited to 16 MiB; known image/enrollment/bundle/model paths have explicit limits. Tests cover early declared-size rejection, chunked oversize rejection without draining the rest, versioned paths and permitted requests. Existing endpoint-level validation remains active. |
| VAOS-SEC-02 | Medium, resolved | Windows setup fetched a mutable `latest` tunnel binary and checked only that it ran and had a plausible size. | The launcher pins Cloudflare `2026.11.0`, verifies the official SHA-256 before install/execution, and tests that a bad download is not installed. A system-managed `cloudflared` already on `PATH` remains an administrator-managed dependency and is not silently replaced. The pinned file and checksum can be checked on the [official release page](https://github.com/cloudflare/cloudflared/releases/tag/2026.11.0). |
| VAOS-STAB-01 | Low, resolved | The Syslog TLS sender could time out during `unwrap()` while waiting for the peer's close-notify even after a successful event send. | Send `close_notify`, then close locally if the receiver does not return its alert. A focused loopback regression test passes. [RFC 5425](https://www.rfc-editor.org/rfc/rfc5425.html) permits the sender not to wait for the peer reply. |
| VAOS-OPS-01 | Low, remediated | New Windows virtual environments could start with an outdated bundled `pip`. | The launcher upgrades `pip` before installing app packages. The temporary audit environment was also upgraded before final advisory checks; this was not a committed application dependency. |
| VAOS-DEP-01 | Informational | GitHub Actions were using older action major versions with Node 20 deprecation notices. | CI uses current checkout/setup-python action majors and disables persisted checkout credentials. GitHub CI must still confirm the change on the pushed candidate. |
| VAOS-DEP-02 | Medium, remediation in progress | The first scan of the actual production CPU image found `setuptools 78.1.0` inherited from the Python base image. The separate isolated audit environment did not include this base-image distribution, so its clean result did not cover this finding. | The Docker build now upgrades setuptools after installing app and detector dependencies to the patched `>=84.2.1,<85` line. Rebuild and repeat the image scan; do not mark this finding resolved until that CI job passes. |

No claim is made that the absence of advisories proves a package is vulnerability-free. The dependency result is a time-specific known-advisory check; repeat it for release tags and rebuilt images.

## Validation evidence

Update the candidate SHA and workflow URL after the push:

- Full isolated suite: **309 tests passed in 65.883 seconds; 3 platform-specific tests skipped on this Windows run.** Skips are not treated as passes for the skipped platform behavior.
- Focused upload-cap, launcher-supply-chain and Syslog-TLS regression checks: **9 tests passed**.
- Python compilation: application, CLI, camera, middleware, MCP, hardware, dataset, bundle and observability modules compiled successfully.
- PowerShell parser check: `start_windows.ps1` parsed successfully.
- Documentation: internal relative links checked across 55 Markdown files; no broken relative links found. `git diff --check` passed.
- Dependency advisory check: `pip-audit 2.10.1` against the isolated Python 3.12 environment reported **no known vulnerabilities**; that environment contained 56 distributions. The subsequent CI scan of the production image found the base-image `setuptools` issue recorded as VAOS-DEP-02. A separate OSV query for core CV and optional detector packages returned zero matching advisories. These are dated database results, not guarantees that packages are vulnerability-free. The image must be rebuilt and rescanned after the setuptools remediation.
- CI workflow #28 on `5b063ed8d282733f0a49663c2ac5d92ca416c26c`: Windows/macOS/Ubuntu contract jobs and the desktop/mobile browser smoke passed; the production CPU-container job failed because of VAOS-DEP-02. The finding is being corrected in a follow-up candidate; record the new run and each final job result before closing this assessment.
- Live-device, staging, restore, performance, and external SIEM tests: **not run**; these remain release-readiness gates.

## Operational security recommendations

1. Keep the administrator origin behind VPN, a trusted identity-aware gateway, or an equivalent network policy. Do not publish the admin service directly to the Internet.
2. Use HTTPS for remote API/MCP traffic, generate separate high-entropy admin and internal-service secrets, scope and expire API tokens, and revoke tokens no longer needed.
3. Treat databases, face templates, photos, review images, models, logs, exports and backups as private data. Restrict filesystem and backup access; document consent, retention and deletion rules for biometric use.
4. Review third-party model and dataset licenses separately from VAOS's Apache-2.0 source license.
5. Before staging, validate clean installation, secrets, proxy separation, migration, encrypted backup/restore and rollback on the intended host. Run the device-specific CUDA/MPS, camera and performance checks there.
6. Re-run the repository suite, dependency scanners and image scan when producing a tagged build; review advisories and CI notices rather than suppressing them.

## Conclusion

The assessed source candidate includes targeted fixes and automated regression tests for the findings above. It has been pushed to the review Git branch, but VAOS-DEP-02 remains open until CI rebuilds and scans the image with the setuptools remediation. This is **not a VAPT certificate** and does **not** clear the separate v1.1.0 staging gate. See the [release-readiness report](RELEASE_READINESS_1.1.0.md) and [platform matrix](PLATFORM_SUPPORT.md) for remaining validation.
