# Daksh-AI — Training Adviser and Supervisor — Software Requirements Specification

Status: development baseline implemented in 1.3.3. See [requirements traceability and validation limits](VALIDATION_1.3.3.md).
Date: 2026-11-26

Product name: **Daksh-AI**. Every recommendation/suggestion dialog must display the exact heading **“Daksh suggests:”**. Use Daksh-AI in Settings, navigation, documentation and feature status; retain existing technical identifiers until implementation defines its migration strategy.
Traceability: F01–F23 and A01–A11 in [FRS](TRAINING_ADVISER_FRS.md).

## 1. Architecture

Implement platform telemetry adapters, capability normalizer, resource coordinator, deterministic safety engine, optional LightGBM predictors, preflight service, supervisor, calibration worker, adviser trainer/registry, persistent scheduler, REST/MCP layer and UI. Keep training, telemetry collection and adviser inference outside request event loops. A single elected scheduler/coordinator owns job dispatch across admin/camera processes.

LightGBM is the initial candidate, not a guaranteed best predictor. Compare it against simple rules and a small regression baseline using held-out runs. Target combined model artifacts ≤10 MiB; enforce a hard artifact limit of 100 MiB. No LLM is required. Use deterministic reason templates referencing measured inputs.

## 2. Platform and telemetry contract — F01, F08

Every measurement includes value, unit, source, timestamp, freshness and availability reason. Memory uses bytes internally; UI consistently labels GiB or GB with correct conversion. macOS unified GPU memory is a subset of host memory, never added as separate capacity. RSS, available host memory and accelerator allocation are separate fields. Unknown capacity/temperature/pressure remains null.

Support CPU on macOS/Linux/Windows, Apple MPS on supported Macs and CUDA on supported NVIDIA installations. Other accelerators are capability-gated and unvalidated until tested. GPU utilization on unsupported platforms is unavailable, never inferred from memory. Backend/version and model signature identify calibration applicability.

Default sampling: five seconds; allowed 1–10 seconds. Batch/loss events arrive through training callbacks; validation metrics arrive at epoch completion. Collect slow privileged telemetry at a separately bounded cadence; do not require elevation for baseline operation. Telemetry older than max(30 seconds, three sample intervals) is stale for automated decisions.

## 3. Resource coordination — F05, F13

Use transactional, durable leases keyed by physical accelerator/host resource, with owner PID, process start identity, job ID and heartbeat. Atomic acquisition prevents multiple workers from launching competing jobs. Detect PID reuse, stale leases and crashes before releasing reservations. Default one accelerator training/calibration job per host; CPU work also respects memory/CPU budgets. Discovering unmanaged accelerator jobs is best effort and must be described honestly.

The hard safety engine checks fresh available memory, pressure, device/runtime compatibility and configured reserve. Initial conservative OS reserve: max(6 GiB, 20% of host RAM); tune only after platform validation. Prediction upper bounds and measured calibration peaks must fit remaining budget with an additional documented margin. Do not manufacture a universally safe bytes-per-batch constant. Unknown memory uses a conservative supported profile or blocks calibration. Protect existing jobs; never start a benchmark to prove capacity while conflicting work is active.

## 4. Preflight and recommendation — F02–F07

Input: dataset identifier, initial detector identifier, epochs, batch size 1–10/automatic, resolution, device and workers. Resolve identifiers server-side. Output: recommendation_id, workload fingerprint, requested/recommended/effective settings, measurements, reasons, confidence, policy/adviser/calibration versions and expiry (default 60 seconds).

Launch accepts recommendation_id, selected mode and idempotency key. Bind acceptance to workload/settings and authorization context. Recheck current conditions and reserve resources atomically. Return a conflict plus revised recommendation if material changes require different settings. Validate all numeric bounds server-side. Manual mode cannot disable hard safety policy.

## 5. Supervisor and checkpoint protocol — F08–F12

Maintain rolling speed windows separated by warmup, training, validation and checkpoint phases. Initial configurable warning defaults: host pressure ≥85% for 30 seconds; swap increase ≥512 MiB in 60 seconds; throughput fall ≥30% over a two-minute comparable-phase baseline. These are starting policies requiring calibration, not universal danger thresholds. Critical policy combines fresh platform pressure and reserve exhaustion; percentage alone must not trigger termination.

Non-finite losses trigger an immediate incident and, if automatic-stop mode is enabled, a checkpoint-stop request only when the trainer can safely save. A lack of batch progress uses phase-aware expected durations and telemetry freshness. Validation deterioration requires multiple comparable completed epochs; no universal mAP threshold proves overfitting. Alert cooldown default five minutes per run/condition, with recovery and severity escalation events.

States: queued, preparing, running, stop_requested, checkpointing, stopped, completed, failed, cancelled; resume links a new execution to its prior run/checkpoint. Serialize model, optimizer, scheduler, epoch, supported RNG/scaler state, data/config fingerprints and backend metadata using the trainer's supported checkpoint mechanism. Verify artifact readability and epoch before stopped is reported. Record checkpoint duration and pending-stop age. Never claim current-epoch preservation after an interrupt unless verified.

Resume must validate supported overrides and original planned epoch target. Changes to batch size may change accumulation/optimization semantics; explain this before acceptance. Unsupported combinations fail clearly. Do not substitute best weights for a resumable last checkpoint.

## 6. Calibration — F17–F18

Calibration job snapshots detector weights to an isolated workspace and uses an authorized bounded dataset subset. Split calibration from production validation records. Test candidate batches sequentially (1–10), with warmup, measurement windows and cancellation points; default time budget five minutes. Model, resolution, precision, runtime/backend, device and worker count are part of the key. Check memory before each candidate; stop escalation after pressure/failure. Never intentionally exhaust memory to measure its ceiling.

Capture throughput distributions, peak host/device allocations, swap delta, pressure and errors. Exclude startup/download time from steady-state throughput but retain phase timing. Results have sample counts, validity ranges and timestamps. Model/runtime/backend/device changes mark results stale. No detector weights, optimizer changes or training labels from calibration are promoted to production.

## 7. Adviser training and registry — F07, F19

Record schema: machine family/capabilities, OS/runtime versions, detector signature/parameter count, workload characteristics, candidate settings, measured outcome and run/phase identity. Exclude credentials, names, paths, camera URLs and raw imagery. Local history alone cannot establish cross-platform coverage.

Train bounded CPU-only throughput and memory predictors. Use memory quantile estimates and evaluate interval coverage; quantiles are not guaranteed upper limits. Group validation by complete run and machine to prevent correlated samples leaking between train/test. Hold out software/backend combinations when assessing generalization. Track MAE, relative throughput error, memory underprediction rate, interval coverage and unsafe recommendation rate against deterministic baseline. Numerical promotion thresholds must be approved and stored in a versioned policy before automatic promotion is enabled.

Restrict CPU threads to 1–2 and default retraining memory budget to min(1 GiB, 5% host RAM); pause/cancel if unavailable. Target inference worker RSS ≤150 MiB and monitoring average CPU <1% of one logical core under five-second sampling on reference hardware; measure, do not assume. Dependencies have a separate installation budget. Bound data retention by age and bytes; default 90 days/1 GiB with aggregates retained only under explicit policy.

Registry stores artifact hash, format, schema, feature order, training data summary, metrics, compatibility and promotion actor. Reject oversized/corrupt/untrusted artifacts. Use native validated model formats; do not load arbitrary uploaded pickle/joblib objects. Candidate evaluation and atomic promotion preserve previous active model; rollback is audited. Model learning runs only on explicit jobs, not uncontrolled adaptation during production training.

## 8. Scheduler and persistence — F14–F16, F20

Independent disabled-by-default schedules for adviser retraining and calibration. Store IANA timezone, local time, recurrence, defer window, resource limits and enabled state. Show next execution in user's timezone. Persist last trigger, unique occurrence key and job ID; dispatch is idempotent. On DST gap use next valid local time; on repeated time run once. On downtime skip missed occurrences and report; never catch up in a burst. Default busy defer window two hours, then skip. User jobs take priority. Scheduling does not imply automatic adviser promotion.

Suggested durable entities: adviser_settings, adviser_jobs, adviser_schedules, resource_leases, calibration_results, benchmark_records, adviser_versions, recommendation_acceptances and supervisor_incidents. Use migrations, foreign keys, bounded indices and transactional state transitions. Preserve settings/history across restart without reviving a job that cannot be safely resumed.

## 9. Proposed REST and MCP contracts — F21–F22

All routes use existing /api/v1/admin namespace. Final OpenAPI schemas must be typed, bounded and generated from implementation.

| Route | Method | Purpose |
|---|---|---|
| /training-adviser/settings | GET/PATCH | Read/update policy |
| /training-adviser/preflight | POST | Generate recommendation |
| /training-adviser/jobs | GET/POST | List/create calibration or adviser-retraining jobs |
| /training-adviser/jobs/{id} | GET | Job progress/results |
| /training-adviser/jobs/{id}/cancel | POST | Idempotent cancellation |
| /training-adviser/schedules | GET/POST | List/create schedules |
| /training-adviser/schedules/{id} | PATCH/DELETE | Update/remove schedule |
| /training-adviser/versions | GET | Registry and validation summary |
| /training-adviser/versions/{id}/promote | POST | Validated promotion |
| /training-adviser/versions/{id}/rollback | POST | Verified rollback |
| /training-adviser/incidents | GET | Authorized incident history |
| /ml/train | POST | Existing launch with bound recommendation acceptance |
| /ml/train/stop | POST | Existing safe-stop request with run binding |
| /ml/train/resume | POST | Explicit checkpoint resume with accepted settings |

Use existing settings/training/diagnostics capability mapping, with administrator-only promotion and safety-policy edits. Document MCP JSON tools and approval retries; schedules, calibration, retraining, resume, promotion and rollback are writes under existing sensitive-action approval policy. Return 400/422 invalid input, 403 policy denial, 409 busy/stale/conflict, 428 approval required and 503 unavailable. Use opaque IDs, idempotency keys, pagination and bounded response sizes. Never accept arbitrary executable commands or filesystem paths.

Audit events include actor, request/job/run IDs, requested/effective settings, policy/model versions, reason code and outcome. Feed existing observability/SIEM queues; never log tokens or raw data. Backpressure or failed SIEM delivery follows existing durable retry behavior and cannot repeat the training action.

## 10. UI, accessibility and failure requirements

Settings groups and dialogs follow the FRS. Keyboard operation, focus return, accessible labels, live status announcements, readable units and mobile scrolling are mandatory. Show real last/next timestamps and stale/unsupported markers. Disable controls with explanations while incompatible jobs run. Do not show fabricated probabilities or precision in ETA/performance estimates. Use measured ranges and label estimates. Failed model loading falls back to rules and an incident; scheduling/calibration failures remain inspectable.

## 11. Verification matrix and release gates

| Area | Required evidence |
|---|---|
| Functional | F01–F23 acceptance cases; bounded input, cancellation and fallback tests |
| Concurrency | Competing launches, stale lease/PID reuse, duplicate schedules and crash recovery |
| Checkpoints | Resume continuity and allowed overrides per CPU/MPS/CUDA backend; checkpoint corruption/failure tests |
| Predictor | Run/machine-held-out evaluation, underprediction/coverage metrics, unknown hardware fallback |
| Calibration | Pressure/timeout/cancellation fault injection; active weight hash unchanged |
| Security/API/MCP | Scope/read-only/approval checks, exact-request binding, idempotency, malicious artifact/path rejection |
| SIEM | Authorized audit events, redaction, receiver outage/retry and no duplicate action execution |
| System/performance | Reference Windows/Linux/macOS hardware; measured overhead with supervisor on/off; memory/swap and training throughput comparison |
| UI/UX | Desktop/mobile visual checks, keyboard/screen-reader dialog flow, scheduling timezone comprehension |
| Upgrade | Migration/rollback, restart recovery and old host compatibility messaging |

Require a documented hardware/runtime support matrix and explicit unsupported combinations before release. Deliver rules/advisory mode first, then checkpoint control, calibration, predictors and schedules behind feature flags. Do not enable automatic stop or model promotion until its relevant acceptance evidence is reviewed. No implementation or deployment is authorized merely by publication of this specification.

## 1.3.3 detailed requirements

- **S24 / F24:** SQLite engine state defaults enabled for compatibility and is independently persisted from monitoring policy. Lifecycle calls serialize within a process; elected supervisors observe the shared switch. Shutdown is bounded; a pending stop is visible. Detector resource leases, weights and progress are not modified by engine stop. Preflight remains a synchronous safety service. API/MCP lifecycle mutations retain authentication, group scope, write approval and audit events.
- **S25 / F25:** Import `results.csv`/`args.yaml` only from configured `runs/*` and `person_candidates/*` roots. Reject external symlinks, non-finite targets, missing resolved configuration and incomplete trailing rows. Use a unique run/epoch key and hashed path identities. Never deserialize `.pt` files or infer historical memory from present host usage. Retain finite timing/loss/validation/LR values. Duration-candidate features are batch, image size, workers, backend, hashed dataset ID and epoch; target is elapsed-time difference across adjacent complete epochs. Hold out complete runs, retain metrics and artifact hash, and set safety promotion eligibility false. Isolate LightGBM in a single-thread child. API jobs use existing idle/lease/budget/cancellation policy. The explicit CLI rebuild has a 120-second deadline, <=1 GiB / 5% RAM child budget and >=6 GiB / 20% host reserve, enabling a bounded CPU rebuild alongside detector training. No detector-model installation occurs.
- **S26 / F26:** `/detection` requires administrator authentication and shares the established navigation/current-page conventions. Server rendering includes review DOM and handlers on Detection and training controls/metrics on ML Training. No hidden duplicate training controls are needed on Detection. Preserve sample filtering, pagination, selection, batch review, modal exports, enlarged-view actions and arrow navigation. Use a single full-width training column, responsive summary cards, cross-page guidance and existing scroll behavior.
- **Acceptance:** tests shall cover separate rendered workspaces, engine persistence/start/stop, unchanged detector stop callback, history idempotency, incomplete rows, non-finite targets, path escape, held-out CPU candidate training, promotion refusal, versioned authenticated routes and MCP approval gates. Browser checks shall verify collection/review status, class-distribution modal, full-width training, engine stop/restart persistence/start and console diagnostics. Physical backend stress certification remains outside the local release evidence.
