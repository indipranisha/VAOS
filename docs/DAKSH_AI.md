# Daksh-AI — VAOS 1.3.3 stable

Daksh-AI is VAOS's local training adviser, supervisor and calibration manager. It provides conservative recommendations immediately and uses validated LightGBM predictors after sufficient measurements and explicit promotion. No pretrained universally accurate adviser is bundled.

## Before training

Click Update best.pt to open **Daksh suggests:**. The dialog shows requested/recommended batch, resolution, workers, precision, memory snapshot, reasons and confidence. Accept recommendations, use manual settings within hard limits, or cancel. Recommendations expire after 60 seconds and are bound to the requesting identity and unchanged workload. Fresh memory validation occurs at launch; if worker conditions require different settings after acceptance, the job fails before fitting and asks for a new preflight.

Batch choices are 1–10. OS reserve is at least 6 GiB or 20% of RAM. CUDA memory must be measurable; unknown capabilities are not treated as zero. Unified Apple GPU memory is part of host RAM. Rules are conservative estimates, not an OOM guarantee. Device/model calibration is preferred; unsupported combinations fall back to rules or refuse a start when capacity cannot be verified. All defaults are chosen to preserve responsiveness rather than consume all RAM.

## Continuous supervision

An elected background supervisor samples independently of browser tabs. It watches sustained memory pressure, swap growth, comparable-phase throughput decline, stalled batch progress, non-finite losses and validation decline across completed epochs. Stale telemetry cannot authorize automatic action. Validation/checkpoint phases are excluded from ordinary batch-stall decisions.

Advisory mode is default. Administrators may enable checkpoint-stop requests for non-finite loss or sustained critical memory pressure; these never force-kill detector training. Alerts use persistence, cooldown and recovery states. Validation deterioration alone remains advice. Settings and Training show incidents and supervisor health.

The resource coordinator uses SQLite transactions, process creation identities and durable leases to prevent overlapping managed training/calibration. The person-training helper uses the same coordinator. Other recognizable standalone trainers and other VAOS admin processes are conservatively treated as potential contention; detection of arbitrary third-party GPU workloads is best effort.

## Checkpoint stop and resume

Stop safely requests the next epoch boundary. Daksh copies the resumable last checkpoint before Ultralytics final evaluation strips optimizer state. It verifies optimizer/epoch state and finite model weights, records a content hash and dataset YAML fingerprint, and saves supported Python/NumPy/Torch RNG state. A stalled process may be unable to save; do not equate a stop request with a saved checkpoint.

Review checkpoint resume selects a preserved checkpoint, explains batch-change effects, obtains fresh Daksh acceptance and resumes toward the original epoch target. Modified or unknown checkpoint/dataset manifests are rejected. Model weights and optimizer/scheduler state resume using the training library; exact bitwise reproducibility across different backends is not promised. Older checkpoints stripped by prior VAOS versions are not silently treated as resumable. External helper checkpoint files remain local artifacts; the integrated resume UI lists the verified VAOS checkpoint registry.

## Manual and scheduled jobs

Settings → Daksh-AI provides:

- Calibrate hardware now: choose a registered dataset, local detector, device, maximum batch and time limit (30–300 seconds).
- Retrain adviser now: CPU-only fitting/evaluation from retained numeric benchmark windows.
- Separate daily/weekly schedules with IANA timezone, enable/disable/edit/remove and next-run time. Defaults disabled.
- Job progress, cancellation, results/failures, adviser version promotion/rollback, benchmark export and recoverable clear/restore.

Calibration copies up to 32 training images into an isolated workspace, tests candidates sequentially, and never promotes modified detector weights. Jobs reserve resources, wait for training/video work to be idle, and enforce time/memory budgets. Cancellation may terminate an isolated calibration/adviser worker; this is distinct from checkpoint stopping real detector training. Busy scheduled work defers up to two hours and then skips. Missed executions never catch up in a burst. DST gaps advance to the first valid local minute; repeated times execute once.

Calibration keys include weights, backend, resolution, runtime versions, OS, CPU and RAM characteristics. Changed signatures or results older than 30 days are not used as fresh calibration. Actual throughput depends on the workload; calibration is not an accuracy evaluation.

## Adviser training and registry

Minimum data gate: 100 numeric windows across six independent runs/calibrations, with at least 20 train and 20 held-out records. Hold out complete machines when possible, otherwise complete runs. A single-machine candidate is labeled local coverage and does not establish cross-platform validity.

LightGBM trains in a CPU-only isolated worker (one thread). The API/training process evaluates exported native tree JSON without importing LightGBM, avoiding a second OpenMP runtime beside PyTorch. Artifact hashes, feature schema/order and observed ranges are checked. Unknown signatures or inputs outside coverage use rules.

Policy v1 requires relative throughput error ≤30%, improvement over a constant baseline, and memory underprediction rate ≤5% after a 10% margin. These are local evaluation gates, not proof of safety. Promotion is always a separate authorized human decision; automatic promotion is disabled. Previous promoted versions can be rolled back after hash verification. Combined adviser artifact target is ≤10 MiB; no arbitrary pickle/joblib upload is accepted.

Retention defaults: 90 days, 50,000 records and 100 MiB serialized payload. API export is bounded to the latest 5,000 records. Clear writes a full local recovery JSONL snapshot before deleting active records; restore requires its returned recovery identifier. Recovery snapshots are sensitive local files; manage them under the host's data-directory backup/retention policy. Calibration workspaces, checkpoint artifacts and recovery snapshots have separate disk costs from numeric records.

## API and MCP

The canonical prefix is `/api/v1/admin`. Existing private dashboard aliases remain supported.

| Methods | Suffix | Purpose |
|---|---|---|
| GET/PATCH | /training-adviser/settings | Policy and supervisor configuration |
| POST | /training-adviser/preflight | Workload-bound recommendation |
| GET/POST | /training-adviser/jobs | List/create calibration or retraining |
| GET | /training-adviser/jobs/{id} | Job progress and result |
| POST | /training-adviser/jobs/{id}/cancel | Idempotent cancellation |
| GET/POST | /training-adviser/schedules | List/create schedules |
| PATCH/DELETE | /training-adviser/schedules/{id} | Full validated update/remove |
| GET | /training-adviser/versions | Registry and evaluation results |
| POST | /training-adviser/versions/{id}/promote | Explicit eligible-candidate promotion |
| POST | /training-adviser/versions/{id}/rollback | Restore previously active version |
| GET | /training-adviser/incidents | Recent sustained incidents |
| GET/DELETE | /training-adviser/records | Bounded export/recoverable clear |
| POST | /training-adviser/records/restore | Restore returned backup_id |
| GET | /ml/train/checkpoints | Verified checkpoint registry |
| POST | /ml/train/resume | Resume with accepted recommendation |
| POST | /ml/train/stop | Optional run_id binding; idempotent pending stop |

Preflight uses source, initial_model, device, epochs, batch_size, image_size, workers, precision and resource_mode; resume preflight also accepts checkpoint_id. Launch uses unchanged input plus daksh_recommendation_id and daksh_mode (recommended/manual). Manual settings cannot bypass the hard resource policy.

Create a job with kind (`calibration` or `retraining`), configuration and an 8–128 character idempotency_key. Calibration configuration accepts registered source/model identifiers, device cpu/mps/cuda:0–9, batches 1–10, supported image_size, precision fp32/amp and timeout_seconds. Retraining configuration is empty. Schedule schema includes kind, enabled, frequency daily/weekly, hour/minute, weekday (Monday=0), timezone, defer_seconds and payload (same calibration configuration or empty for retraining). No arbitrary filesystem paths are accepted.

MCP uses vaos_api_request against generated OpenAPI. The training capability is required. Writes honor control scope, sensitive-action policy, exact-request human approval and deletion restrictions. Keep payloads identical when retrying approvals. Validation returns 400/422, resource/expiry conflicts 409, authorization 401/403 and approvals 428. Job IDs are opaque. Recommendations are not credentials.

Dedicated decision, schedule, job, incident, promotion and checkpoint events enter a local outbox with stable event IDs, then the existing resilient security-audit/SIEM pipeline. No images, model paths, passphrases or API tokens are included. Delivery retries do not rerun training actions.

## Installation and validation boundary

Install updated requirements.txt (includes LightGBM), restart VAOS and the MCP host after active training finishes, then refresh pages. Docker includes the new modules and OpenMP runtime package. The currently running old service cannot load these changes without restart.

macOS CPU stop/resume, calibration and synthetic adviser training have been exercised. Windows/Linux, CUDA/MPS resume continuity and representative hardware stress tests require their own release evidence. This release is not certified as universally validated or guaranteed to avoid OS memory exhaustion. See [validation and traceability](VALIDATION_1.3.3.md).

## Engine controls and Detection workspace

Open **Settings → Daksh-AI → Engine controls** to start or stop the supervisor. The choice is stored locally and survives service restarts. Stop pauses monitoring/schedules and requests cancellation of Daksh's own isolated worker. Detector training continues. A stopping state is shown until shutdown finishes; Start becomes available once stopped. Required preflight resource checks remain callable while the background engine is off. Multiple processes sharing the state database observe the persisted engine switch.

**Detection** in the main navigation owns Reviewed Samples, Live samples, Review detections, enlarged review images and class-distribution exports. **ML Training** retains maturity, prepared datasets and the full-width ML TRAINING. Sample storage and APIs have not moved, so reviewed labels remain available for training.

### Learning from earlier checkpoints

Use **Import previous training history & train Daksh**. The importer reads `results.csv` and `args.yaml` beside runs in `ml_training/runs/*` and `ml_training/person_candidates/*`. It counts available `weights/last.pt` and `weights/best.pt`, but does not load them. Incomplete CSV rows, invalid numbers, unresolved auto-batch settings and unsafe paths are excluded. Reimporting the same run/epoch updates its existing record rather than duplicating it. IDs are hashed; private dataset paths do not appear in exported job metadata.

Historical data preserves finite loss/validation/LR metrics and elapsed/epoch timing. Historical memory measurements are unknown, not zero. A LightGBM epoch-duration candidate uses these run configurations and completed epoch timings with whole-run holdout. At least 20 usable epochs from two independent runs are required; each holdout partition needs five records. This candidate **cannot be promoted as the resource-safety predictor**, does not change detector weights, and is not used for live safety decisions or the existing training ETA. Future safety learning still requires measured benchmarks and its stricter promotion gates. Unsupported old runs appear in the import report rather than receiving invented measurements.

UI/API jobs wait until the engine is on and training/video analysis are idle. For an explicit, bounded CPU-only historical rebuild, an administrator may run:

```sh
./.venv/bin/python tools/retrain_daksh_history.py --data-dir "$PWD/data"
```

This command uses one CPU thread, a 120-second deadline, at most 1 GiB / 5% RAM for its child process, and at least 6 GiB / 20% system RAM reserved. It can run alongside a detector job; it never opens checkpoint blobs or uses the GPU. The resulting candidate is registered but not promoted. CLI execution is explicit even if the background engine is stopped. Models live under `data/daksh/versions/<candidate-id>/`.

Historical numeric records are stored separately from measured benchmarks. The benchmark export/clear/restore controls apply to measured benchmark records, not the original detector run files or the historical import table. Historical imports have their own configured count/age/payload limits, separate from measured benchmark storage: record cap (maximum 50,000), retained age and serialized MiB budget, plus 2,000 companion files, 10,000 rows/file and 16 MiB/file. Original checkpoints and datasets are unchanged.
