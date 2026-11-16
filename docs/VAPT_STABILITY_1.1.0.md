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
| VAOS-DEP-01 | Informational | GitHub Actions were using older action major versions with Node 20 deprecation notices. | CI uses current checkout/setup-python action majors and disables persisted checkout credentials. |
| VAOS-DEP-02 | Medium, resolved | The first scan of the actual production CPU image found `setuptools 78.1.0` inherited from the Python base image. The separate isolated audit environment did not include this base-image distribution, so its clean result did not cover this finding. | The Docker build upgrades setuptools after installing app and detector dependencies to the patched `>=84.2.1,<85` line. The rebuilt image scan in workflow #29 reported `No known vulnerabilities found`; the production CPU-container build and smoke job also passed in final candidate workflow #31. |
| VAOS-SEC-03 | Medium, fixed | Pascal VOC XML annotations from external datasets were parsed with the standard XML parser; a hostile annotation could use DTD/entity expansion during dataset preparation. | Kaggle Auto annotations now use `defusedxml`; malformed or forbidden DTD/entity input is rejected. Regression tests cover normal annotations and a DTD/entity payload. |
| VAOS-SEC-04 | Medium, fixed | Authenticated internal/CLI requests used the default redirect handler, which can carry custom authorization headers to a redirect destination. | Credential-bearing CLI and internal-service requests use a no-redirect opener. A loopback test confirms that a redirect receives a 302 error and the target never receives the bearer header. |
| VAOS-DEP-03 | Medium, fixed | The UVH-26 Hub download followed the repository's moving default branch, so a later upstream change could silently change a dataset preparation. | The downloader now pins the observed public dataset commit `59f82c57821e8a54dc40bc1f42e83909dbad0b70`; a regression contract requires a 40-character source revision. Updating the data snapshot requires an intentional source change and validation. |

No claim is made that the absence of advisories proves a package is vulnerability-free. The dependency result is a time-specific known-advisory check; repeat it for release tags and rebuilt images.

## Validation evidence

Final candidate tested: **`8ce5dc315139930edf1b0ed017fa33639af84b94`** on branch `codex/vaos-v1.1.0-candidate`. Final CI: [workflow #31](https://github.com/indipranisha/VAOS/actions/runs/43850018586).

- Full isolated suite: **315 tests passed in 68.596 seconds; 3 platform-specific tests skipped on this Windows run.** Skips are not treated as passes for the skipped platform behavior.
- Focused XML parser, credential-redirect and production-manifest regression checks: **8 tests passed**; the earlier upload-cap, launcher-supply-chain and Syslog-TLS focused checks also passed.
- Python compilation: application, CLI, camera, request-limit, safe HTTP/XML, MCP, hardware, dataset, bundle and observability modules compiled successfully. Installing the CLI package with `--no-deps` and invoking `vaos --help` passed.
- PowerShell parser check: `start_windows.ps1` parsed successfully.
- Documentation: internal relative links checked across 61 Markdown files; no broken relative links found. `git diff --check` passed.
- Dependency advisory check: `pip-audit 2.10.1` against the isolated Python 3.12 environment reported **no known vulnerabilities**; that environment contained 56 distributions. The initial CI image scan found the base-image `setuptools` issue recorded as VAOS-DEP-02; after remediation, workflow #29's rebuilt production image scan reported `No known vulnerabilities found`. A separate OSV query for core CV and optional detector packages returned zero matching advisories. These are time-specific known-advisory results, not guarantees that packages are vulnerability-free.
- Static source scan: Bandit 1.9.4 reported 0 high, 23 medium and 99 low findings after the XML, revision-pin and redirect changes. Medium `B314` XML and `B615` unpinned Hub findings are removed. The remaining medium heuristics are `B608` (18 dynamic-SQL constructions, checked as bound values and fixed/allow-listed query fragments) and `B310` (5 fixed HTTPS model/Hugging Face or local loopback test URLs). Low findings are grouped as 3 test-only asserts, 59 string/constant heuristics (`B105`/`B106`), 2 optional fallback `except/pass` paths, and 35 subprocess warnings (`B404`/`B603`/`B607`); the subprocess calls use fixed argument lists or escaped local paths and do not use `shell=True`. No `# nosec` suppressions were added. Reproduce with `bandit -r . -x '.git,tests,outputs,.venv'`.
- CI workflow #28 on `5b063ed8d282733f0a49663c2ac5d92ca416c26c` initially failed the container scan on VAOS-DEP-02; workflow #29 confirmed the setuptools remediation and passed its production CPU-container/OSV scan. Final workflow #31 on `8ce5dc315139930edf1b0ed017fa33639af84b94` completed **successfully across all eight jobs**: Windows, Ubuntu and macOS 15 Intel unit/contract suites; Windows, Ubuntu and macOS 15 Intel MCP stdio integration; browser UI smoke; and production CPU-container smoke. The macOS jobs use an Intel hosted runner to avoid the prior arm64 capacity issue. This is macOS OS-level contract coverage, not Apple Silicon/MPS hardware certification. Workflow #30's earlier run is superseded by #31.
- Live-device, staging, restore, performance, and external SIEM tests: **not run**; these remain release-readiness gates.

## Operational security recommendations

1. Keep the administrator origin behind VPN, a trusted identity-aware gateway, or an equivalent network policy. Do not publish the admin service directly to the Internet.
2. Use HTTPS for remote API/MCP traffic, generate separate high-entropy admin and internal-service secrets, scope and expire API tokens, and revoke tokens no longer needed.
3. Treat databases, face templates, photos, review images, models, logs, exports and backups as private data. Restrict filesystem and backup access; document consent, retention and deletion rules for biometric use.
4. Review third-party model and dataset licenses separately from VAOS's Apache-2.0 source license.
5. Before staging, validate clean installation, secrets, proxy separation, migration, encrypted backup/restore and rollback on the intended host. Run the device-specific CUDA/MPS, camera and performance checks there.
6. Re-run the repository suite, dependency scanners and image scan when producing a tagged build; review advisories and CI notices rather than suppressing them.

## Conclusion

The assessed source candidate includes targeted fixes and automated regression tests for the findings above. The local suite, remediated production-image advisory scan, and all eight final CI jobs pass on commit `8ce5dc315139930edf1b0ed017fa33639af84b94`. This is **not a third-party VAPT certificate** and does **not** clear the separate v1.1.0 staging gate: live target-host, camera/GPU, backup/restore, performance/soak, SIEM receiver and full persona/accessibility validation remain untested. See the [release-readiness report](RELEASE_READINESS_1.1.0.md) and [platform matrix](PLATFORM_SUPPORT.md) for remaining validation.
