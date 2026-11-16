# Observability activation and rollback

The observability source is not proven active merely because it is present in this checkout or because a runtime reports the same version. A read-only health request to the existing admin listener on 127.0.0.1:8000 reported 1.1.0, but its loaded source path could not be established and the camera listener was not rechecked. A separate source process was smoke-tested on 127.0.0.1:8765 with a fresh temporary data directory; this did not touch the existing service or its database. Do not replace or restart existing services as part of documentation or isolated testing.

## Before activation

1. Confirm training and dataset preparation are stopped or safely completed. Record CPU, RAM, GPU and free disk; do not run model workloads as part of a telemetry acceptance test.
2. Make a service-owner-approved, access-restricted backup of the complete `VAOS_DATA_DIR`, including `.vaos-observability-fallback/` if present. Verify `app.db` and `.vaos-observability-fallback/counters.sqlite3` copies open and pass `PRAGMA integrity_check`; retain the manifest and record its path. The automatic migration backup is a safeguard, not a substitute for a tested whole-application backup. The fallback sidecar can contain unflushed API/frame counters absent from `app.db`.
3. Install into a separate test data directory first. Never point a test process at the user's live data directory.
4. Validate schema migration and restore from a temporary copy. The automated suite verifies a synthetic pre-migration backup and separate restore across 13 representative VAOS tables, including reviewed image bytes, settings, API, tracking and MCP records; it checks the manifest hash/size, SQLite integrity, foreign keys and preserved rows. It also copies a synthetic stopped-app data directory containing a matching `app.db` and fallback sidecar to a separate location, verifies both file hashes and SQLite integrity, then starts VAOS in a separate test process against that relocated copy to confirm startup replays pending counters exactly once. On Windows, a synthetic `copytree` probe found that the relocated fallback sidecar initially inherits the destination parent's ACL; VAOS startup reasserts current-account and SYSTEM-only ACLs on the fallback directory/database before replay. This does not validate `app.db` or the data-root ACL, and the destination must be access-restricted before the first start. These are isolated synthetic checks. A representative production-like database restore, target-filesystem/ACL validation and operator cutover drill are still required before staging. Check that existing records remain and that the migration fails closed if the verified backup cannot be completed.
5. Confirm endpoint scopes, sensitive-read behavior, MCP approval gates and outbound integration defaults before exposing the service.
6. Test a desktop/mobile browser session, keyboard flow, reports, expiry and a controlled webhook receiver in an isolated environment.

## Rollback

- Stop only the new test/release-candidate process that you started. Keep the pre-change source and database backup.
- Revert code to the previous tagged version using the documented release procedure. Do not delete observability tables or rows as an automatic rollback: they may contain operational history and are additive.
- If an upgrade partially failed, restore the verified database backup into a separate recovery directory first, validate it, then follow the operator's planned replacement procedure. Never overwrite the active DB while VAOS is running.
- Restore the fallback sidecar beside the matching `app.db` before starting VAOS so pending batches replay against the correct installation data. Do not restore a sidecar from another VAOS data directory.
- If SIEM delivery is involved, disable the destination and rotate its key if exposure is suspected. The remote receiver may already have accepted events; local rollback cannot recall them.
- Keep generated report files and backups access-controlled. Remove them only through a documented retention/deletion procedure after confirming they are not needed for recovery.

## Current gate

The Feature 8 change remains **NO-GO for staging**. Browser usability, production-like migration/restore drill, full regression/security suite, real receiver compatibility and measured performance evidence remain outstanding. No staging deployment, Git push or live service restart is authorized by this document.
