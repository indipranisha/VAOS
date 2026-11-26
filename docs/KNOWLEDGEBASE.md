# VAOS knowledgebase — 1.3.3

See [release notes](RELEASE_NOTES_1.3.3.md) for current behavior and upgrade requirements.

## Training

Settings → Detection → Processing and performance → Training status refresh controls polling from 1 to 10 seconds. Save controls and refresh Training. Older services use a browser preference fallback. ETAs use recent batch speed over up to two minutes; validation/checkpoint time is additional. Image counts are approximate from batch size.

Review detections opens a zoom dialog with Previous/Next, arrow keys, metadata and Approve, Reject, Correct label, Don’t know and Save for later. Boxes require updated backend metadata. Browsing follows the current filtered page.

Only one GPU training job should run at a time. Standalone tools do not share the application's lock. Resume from a checkpoint after verifying no other GPU training run is active.

## Project bundles

See [bundle guide](PROJECT_BUNDLES.md). Export/import uses an eight-character case-sensitive alphanumeric passphrase. Forgotten passphrases cannot be recovered. Imported configuration is stored unencrypted locally. A Method Not Allowed export response means the backend needs restarting after jobs finish.

## Current feature contracts — 1.3.3

- Settings: GET/POST `/api/v1/admin/engine-settings`, field `training_status_refresh_seconds` integer 1–10, default 5.
- Training: POST `/api/v1/admin/ml/train`, optional `batch_size`: null (automatic), or any integer 1–10. The worker clamps requested batches using available RAM/VRAM and memory pressure at startup, reserving OS headroom. Requested/effective values and adjustment reason are reported in training status. This is a conservative estimate, not an OOM guarantee. This starts a new run; active-run batch mutation and checkpoint resume are not exposed.
- Review: POST `/api/v1/admin/ml/samples/{id}/review` with `choice` correct, reject, incorrect, dont_know or later; incorrect requires label. Sample listing includes stored bbox metadata.
- Video frames: binary POST accepts `X-Analysis-Device: auto|cpu`, `X-Video-Time`, `X-Save-Samples`. CPU may run alongside accelerator training, subject to server checks.
- Projects: POST `/api/v1/admin/projects/export` with JSON name/passphrase. Response is encrypted binary; MCP returns data_base64. Multipart import uses file and passphrase, with dry_run=true for preview. Never place passphrases in URLs.
- UI playback speeds, image zoom/navigation and recent-speed ETA displays are browser features; they do not require separate mutation endpoints.

MCP uses `vaos_api_request` for JSON operations, `vaos_upload_file` with fields_json containing passphrase for imports, and `vaos_send_binary` with headers_json for CPU/automatic video frames. Existing capability groups, sensitive-read rules, read-only restrictions and exact-request human approvals continue to apply. A token needs matching projects/training/settings/review/video groups as appropriate. Retrying approved writes must preserve all input fields.

Upgrade the backend and restart the MCP host to load new contracts. Do this after active training finishes. The installed source version can differ from the running service version. System RAM is used/total host memory, not Python RSS; Apple GPU memory shares physical RAM and must not be added to host RAM as separate memory. YOLO26 records L1 loss rather than DFL; completed-epoch fallback values are labeled.

Daksh-AI **1.3.3**: see [Daksh capabilities and API contracts](DAKSH_AI.md). The stable release includes supervision, calibration/retraining jobs and schedules, adviser promotion/rollback and verified checkpoint resume; see the guide for hardware validation limits.

VAOS **1.3.3** adds persistent Daksh engine Start/Stop in Settings, historical checkpoint-metric learning, a dedicated Detection workspace and full-width ML TRAINING workspace. See [release notes](RELEASE_NOTES_1.3.3.md) for behavior, rationale and validation limits.
