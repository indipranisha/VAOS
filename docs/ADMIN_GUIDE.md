# VAOS administrator guide

The administrator site contains settings, camera management, ML training, dataset review, hardware information, tracking rules, API access, project bundles and observability. Features in the source tree may still have release gates; check [current status](PROJECT_STATUS.md) before treating them as production-ready.

## Access and service boundaries

- Keep the admin service on loopback for local use. For remote production access, put the admin hostname behind a VPN, identity-aware gateway or equivalent policy and use HTTPS.
- The camera site is a separate service on port 8001 in local development. It accepts camera frames and consent-based enrollment submissions; private admin endpoints are not exposed there.
- Local Windows service restart and temporary-tunnel controls use the Windows launcher. Production restart operations belong to Docker Compose or the hosting process manager.
- Configure API tokens in Settings. Issue separate read and control tokens, use expiry and narrow feature groups, and revoke tokens that are lost or no longer needed.

## Storage and backups

`VAOS_DATA_DIR` selects the location for the database and application data. Without an override, the local source setup uses the project data directory; the production Compose setup uses its persistent volume. Data may include participant photos, face embeddings, camera records, reviewed frames, model weights, training data, logs and reports. Protect the directory and backups with OS-level access controls and encryption appropriate to the deployment.

Back up while VAOS is stopped or use a consistent SQLite backup mechanism. Include the database and required model/configuration artifacts; a report export is not a backup. Store backup copies separately from the host and verify restoration on a disposable copy before relying on it. Never use `docker compose down --volumes` during a routine restart; that removes the named data volume.

## Routine operations

1. Check both service health endpoints and the admin status page.
2. Monitor free disk, CPU, RAM and GPU/VRAM before importing large datasets or starting training.
3. Inspect training history and logs after a run; retain a known-good model before replacing active weights.
4. Apply dataset purge only after confirming that its derived copy is no longer needed. Original datasets and prepared training copies are different paths; verify the exact target before removal.
5. Review MCP approvals and API audit records regularly; disable unused integrations and revoke credentials.
6. Back up before software upgrades and verify the data directory after service startup.

## Upgrade and recovery

There is no claim of zero-downtime or multi-instance upgrade support. Keep one admin and one camera-service instance because runtime training and coordination use in-process state. Before a release upgrade, save a verified database backup, application configuration and model files; review the changelog and migration notes; upgrade a copy first; run the documented checks; then switch the installation. If startup or data integrity fails, stop the affected release, preserve the failed data copy for diagnosis, and restore the verified backup using the [database and rollback guide](observability/database-schema.md).

## Troubleshooting

- **Admin loads but camera page does not:** check the camera service on port 8001 and confirm the admin's configured camera URL.
- **Training device says unavailable:** verify the OS driver and matching PyTorch build, then inspect the hardware page. Do not infer GPU readiness solely from device presence.
- **MCP write is pending:** the write has not run. Review the exact request in Settings and approve once only if it matches the intended operation.
- **Disk unexpectedly shrinks:** inspect the configured data path, dataset preparation output, model cache, backups and container volumes before deleting anything.

For credentials and biometric-data handling, see [Security and privacy](SECURITY_PRIVACY.md). For release suitability, see [platform support](PLATFORM_SUPPORT.md) and [project status](PROJECT_STATUS.md).
