# VAOS 1.3.3 — stable

Release date: 2026-11-26. Tag: `v1.3.3`. Source release branch: `work/mac-m3max`.

## Changes

- The full-width training card is now titled **ML TRAINING**, replacing “03 / TRAIN” and “Custom detector”. This aligns the card with the page purpose and avoids a stage number disconnected from the separate Detection page.
- **Detection** owns Reviewed Samples, Live samples, Review detections, class distribution/export and enlarged-image review actions. Training data and review APIs remain compatible.
- **Daksh-AI** includes preflight recommendations, continuous supervision, persistent Settings engine Start/Stop, calibration/retraining jobs and schedules, adviser version promotion/rollback and verified checkpoint resume.
- Historical checkpoint companions can train an isolated, small CPU epoch-duration candidate. Missing historical memory labels remain unknown. Experimental duration candidates cannot replace the calibrated safety predictor.
- This release includes the pending training progress, loss/resource display, batch-selection safeguards, API/MCP contracts and documentation updates from the local 1.2.x/1.3 beta development work since v1.2.18.
- Version **1.3.3** is synchronized across runtime/admin/camera metadata, package, CLI, telemetry, About fallback, citation application version and maintained documentation. The citation format version remains 1.2.0, as required by CFF.

- Incorporates existing upstream Windows fixes for bounded SQLite counter contention, serialized fallback writes, storage-cap rollback and restored private-file ACLs. First failed logins are audited even during the first minute of host uptime. Credentialed CLI/internal-service HTTP calls reject redirects so tokens are not forwarded to another endpoint.

## Upgrade

Finish active training and video-analysis jobs before restarting services. Back up the configured data directory and preserve local changes. Install from the `v1.3.3` tag or its source archive, refresh Python dependencies, and use the existing launcher. Optional object-detection/MCP dependencies retain their separate installation steps. Verify the dashboard's running version after restart. Existing running processes may retain an older backend version until restarted.

No local credential files, local datasets, historical adviser records, or newly generated training weights are included in this release commit. The already-published baseline detector and its notices remain part of the repository.

## Validation and limits

See [1.3.3 acceptance evidence](VALIDATION_1.3.3.md), [Daksh guide](DAKSH_AI.md), [API](API.md), [MCP](MCP.md), [FRS](TRAINING_ADVISER_FRS.md) and [SRS](TRAINING_ADVISER_SRS.md).

The release channel is stable; this is not a claim of certified detection accuracy, universal adviser generalization or physical GPU stress validation on every OS. Historical duration learning remains experimental. Preflight memory estimates do not guarantee an OOM-free run, and calibration is opt-in. Production secrets and remote HTTPS remain operator responsibilities.
