# VAOS 1.3.3 stable — acceptance evidence

Date: 2026-11-26. Local platform: macOS / Apple Silicon. This record describes the exact stable source; running detector jobs were not restarted.

## Local results

- **387 Python tests passed** in 30.479 seconds (`/tmp/vaos-133-final-python.log`). This includes API/security, MCP policy/bridge/stdio host, release discovery, adviser orchestration/history/native model evaluation, existing application contracts and rendered page separation.
- **8 JavaScript tests passed** (`/tmp/vaos-133-final-js.log`) for adviser dialogs and class-distribution behavior.
- The authenticated training-page regression asserts `<h3>ML TRAINING</h3>` and absence of the old `03 / TRAIN` and `Custom detector` card labels. Detection retains its review DOM; ML Training retains its training controls.
- Compilation of runtime/adviser/CLI/version modules, JavaScript syntax and CRLF-aware whitespace checks passed.
- Stable version 1.3.3 is synchronized in runtime, project metadata, package, CLI, SIEM agent, static About fallback, citation application version, maintained README/knowledgebase and UI asset references. CFF schema version remains 1.2.0.
- Existing historical [1.3.1-beta1 evidence](VALIDATION_1.3.1-beta1.md) records the manual browser workflow, 223-epoch historical CPU adviser rebuild, and its evaluation limits. It is retained as historical evidence rather than relabeled as a new manual run.

## CI-discovered corrections

The initial Linux run exposed a first-minute host-uptime audit coalescing bug. Its existing upstream fix is included with a direct early-monotonic-clock regression. Previous Windows evidence identified bounded SQLite counter/fallback contention and restored-file ACL problems; the existing upstream persistence fixes and storage-cap rollback regression are included. Credential-bearing HTTP operations now reject redirects, covered by loopback tests. The complete local suite was rerun after these changes. The rebuilt CLI wheel contains both its entry point and the redirect-safe HTTP helper.

## Publishing and data boundaries

Only application source, documentation, tests and build configuration are included in this release commit. Live `data/ml_training/active/best.pt` and `metadata.json` changes are excluded. Local databases, credentials, datasets, adviser records and generated candidates remain untracked. Synthetic test-only TLS fixtures retain their documented purpose.

The release is published from the existing `work/mac-m3max` release branch with annotated tag `v1.3.3`. Historical/default-branch assessments are not a claim about this tag. GitHub Actions runs the unit/contract and MCP-host suites on Ubuntu, Windows and macOS; current run results are available on the commit's Checks page. macOS CI installs OpenMP for isolated LightGBM workers; JavaScript contracts run in the unit-test matrix.

## Limits

Stable is the requested release channel, not physical GPU/thermal certification on every device. Native accelerator stress, broad camera/dataset accuracy and universal adviser generalization remain operator validation tasks. The historical duration candidate remains experimental and unpromoted because original memory/hardware measurements are absent. No new GPU training or production-service restart was performed. Activate this backend only after active jobs can finish safely, then verify the running version.
