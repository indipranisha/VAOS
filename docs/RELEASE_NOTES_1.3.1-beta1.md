# VAOS 1.3.1-beta1

- **Settings → Daksh-AI → Engine controls:** Start and Stop persist across service restarts. Stop pauses supervision and scheduled adviser work, cancels an in-flight adviser worker within its shutdown budget, and leaves detector training running. Preflight safety checks remain available. This makes background resource use explicit without interrupting model training.
- **Detection (`/detection`):** Reviewed Samples, class distribution with CSV/JSON export, Live samples, and Review detections now share a dedicated page. Existing sample APIs, selection, filtering, review actions and enlarged-image navigation are retained. This separates preparing labels from launching training.
- **ML Training:** Custom detector occupies the entire workspace width. Training maturity and Available datasets remain above it. Both pages link to the next workflow stage and retain page scrolling.
- **Historical Daksh learning:** Settings can import numeric epoch/checkpoint companion records from the configured VAOS training directories and queue adviser learning. Binary checkpoint files are never deserialized by the importer. Missing historical RAM/VRAM labels remain unknown.
- **Historical duration candidate:** A separate, small CPU model learns epoch duration from batch size, resolution, worker count, backend, dataset identity and epoch. It is experimental and cannot be promoted as the resource-safety predictor. This uses available history without inventing memory measurements or replacing safeguards.
- **API/MCP:** Engine start/stop, history status and history-import/retraining operations are exposed through the Training group. MCP writes continue to require human approval of the exact request.
- Fixed a Settings refresh handler that attempted to update a missing status element; Settings no longer refreshes hidden dashboard graphs.

## Local historical rebuild

The explicitly requested rebuild imported **223 completed epochs from eight runs**, associated with **16 checkpoint files**. The one-thread CPU worker generated a **145,750-byte** historical duration model. Evaluation held out whole runs: 167 training records, 56 test records; relative duration error approximately 28.0%, mean absolute error approximately 2,105 seconds across very different run sizes. These are local exploratory results, not a cross-platform accuracy claim. The candidate remains **unpromoted** because historical peak-memory labels and verified hardware provenance are unavailable.

Source version is 1.3.1-beta1. Existing running services require a restart to load backend changes. Do not interrupt an active detector job just to activate this build.

Validation: **383 Python tests and 8 JavaScript tests passed**. See [acceptance evidence and remaining platform limits](VALIDATION_1.3.1-beta1.md).
