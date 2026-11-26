# Daksh-AI — Training Adviser and Supervisor — Functional Requirements Specification

Status: development baseline implemented in 1.3.3; acceptance evidence and platform gaps are tracked in [validation](VALIDATION_1.3.3.md).
Date: 2026-11-26

Product name: **Daksh-AI**. Every recommendation/suggestion dialog must display the exact heading **“Daksh suggests:”**. Use Daksh-AI in Settings, navigation, documentation and feature status; retain existing technical identifiers until implementation defines its migration strategy.
Owner: VAOS / Prateek Dubey
Companion: [SRS](TRAINING_ADVISER_SRS.md)

## 1. Purpose and scope

Provide a local cross-platform training adviser, bounded hardware calibration, and ongoing supervision for VAOS detector training. Support macOS, Windows and Linux through capability detection. Recommend settings using measurements, conservative rules and optional trained predictors. A shared predictor is the goal; reliable support for every hardware/runtime combination is not assumed.

The adviser is distinct from the object detector. Retraining the adviser does not update best.pt. Calibration does not promote detector weights. This feature does not promise maximum utilization, eliminate out-of-memory failures, or guarantee better detector accuracy.

## 2. Roles

Administrator: configures policies, schedules and retention; approves manual overrides and model promotion.
Operator with training capability: requests recommendations, calibration and permitted training actions.
Read-only user/agent: reads authorized status and summaries. Existing VAOS authorization, sensitive-data and MCP approval policies apply.

## 3. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| F01 | Detect OS, CPU, memory, accelerator backend, shared/dedicated memory and runtime capabilities; distinguish missing values from zero. | Must |
| F02 | On Update best.pt, validate dataset/model and collect a current resource snapshot before presenting a recommendation dialog. | Must |
| F03 | Show requested and recommended batch 1–10, image size, loader workers and supported precision; explain each proposed change and confidence. Preserve user-selected epochs unless explicitly accepted otherwise. | Must |
| F04 | Offer Accept recommendations, Use manual settings and Cancel. Show any effective settings that differ before authorizing launch. | Must |
| F05 | Manual override may bypass advisory preferences, never hard authorization, incompatibility, concurrency or resource limits. Revalidate at launch; changed conditions require a new decision if effective settings change. | Must |
| F06 | Use conservative rules when predictions are unavailable, unvalidated or outside known hardware/workload coverage. Display limited confidence. | Must |
| F07 | Predict throughput and peak memory from bounded numeric telemetry; use upper memory estimates and safety margins. Predictions remain advisory. | Must |
| F08 | Monitor memory pressure, swap growth, batch progress/speed, finite losses, validation trends, telemetry freshness and competing jobs. Evaluate sustained trends, warmup and phase-specific behavior. | Must |
| F09 | Show warnings with evidence, time, severity and recommended action; deduplicate alerts with cooldowns. Offer Continue, Stop at next checkpoint and Review resume settings. | Must |
| F10 | Never silently change active-run batch size, image size, precision, optimizer or learning-rate schedule. Propose changes only for supported checkpoint resume. | Must |
| F11 | A safe stop waits for a verified checkpoint boundary. Show requested, pending, saved and stopped states; distinguish this from force termination. A stalled process cannot guarantee checkpoint completion. | Must |
| F12 | Default supervision mode is advisory. Admin may enable automatic checkpoint-stop requests for sustained critical memory pressure, non-finite losses or explicitly configured faults. Validation deterioration alone defaults to advice. | Must |
| F13 | Route all managed training and calibration jobs through one resource coordinator. Detect unmanaged conflicting jobs where possible and block new accelerator work pending resolution. | Must |
| F14 | Provide manual adviser retraining and manual hardware calibration in Settings. Clearly distinguish both actions from detector training. | Must |
| F15 | Provide separate retraining and calibration schedules with frequency, local time, timezone, next execution, enabled state and history. Default disabled. | Must |
| F16 | Default scheduled work runs only while training/video analysis/calibration is idle; postpone within a configured window, then skip with a reason. No automatic interruption of user jobs. | Must |
| F17 | Calibration is explicit opt-in, tests candidates sequentially, enforces time/memory/batch limits and permits cancellation. It uses isolated temporary weights and never modifies active detector weights. | Must |
| F18 | Mark calibration stale after relevant model/runtime/backend/hardware changes; show why and offer recalibration. | Must |
| F19 | Retrain on retained local benchmark/run records; evaluate a candidate against baseline, rules and held-out runs/machines before promotion. Keep the previous version for rollback. | Must |
| F20 | Show last/next run, progress, measurements, selected adviser version, data coverage, failures and cancellation reasons in Settings. | Must |
| F21 | Provide matching authorized REST/MCP operations for settings, preflight, jobs, scheduling, status, stop requests, promotion and rollback. | Must |
| F22 | Record supervisor decisions, approvals, manual overrides and job lifecycle in local audit/observability; deliver through existing SIEM policy without secrets or raw images. | Must |
| F23 | Bound telemetry retention and disk usage; permit export of authorized numeric benchmark records and recoverable deletion under existing policy. | Should |

## 4. Settings layout

Training adviser & supervisor contains: enable adviser; supervision mode; sampling/alert settings; protected resource budgets; current adviser and rollback; Retrain adviser now; Calibrate hardware now; independent schedules; retained data/coverage; last and next jobs; history and help. Advanced settings disclose meaning and risks. Controls unavailable on the current backend explain why. Do not remove existing training options.

Recommendation dialog contains current resource snapshot and age, workload, requested/recommended/effective settings, reasons, confidence, calibration freshness, expected speed range, memory budget and three explicit actions. During-run alerts link to the exact run, checkpoint state and proposed resume settings.

## 5. Workflow and failure behavior

Preflight → recommendation → user decision → fresh validation → resource reservation → training → supervision → checkpoint completion → finished/stopped/failed. Stop requests are idempotent. Resume is a distinct action and must verify checkpoint integrity and compatible state; otherwise offer a clearly labeled new run.

Telemetry loss disables learned automatic decisions and uses conservative fallback. A failed adviser does not kill otherwise healthy training. Scheduler downtime never launches a burst of missed jobs. Calibration failure cleans temporary resources and preserves active model/data. No silent force-kill is part of this feature.

## 6. Acceptance scenarios

A01: Each supported backend presents truthful available/unavailable telemetry and a fallback recommendation.
A02: Batch requests 1–10 are accepted as input; incompatible requests are explained and reduced only with visible effective settings and required acceptance.
A03: Launch-time resource changes invalidate stale acceptance when settings must change.
A04: Duplicate training/calibration requests cannot acquire the same exclusive resource; unmanaged contention is reported where detectable.
A05: Memory spikes shorter than persistence threshold do not repeatedly alert; sustained faults produce one actionable incident plus updates.
A06: A safe stop request produces a verified checkpoint and resumable status, or explicitly reports inability without claiming success.
A07: Manual/scheduled retraining and calibration obey idle policy, timeouts, cancellation and DST/timezone rules.
A08: Candidate adviser failure or poor validation leaves the active adviser unchanged; rollback restores a verified previous version.
A09: Unauthorized API/MCP operations are denied and sensitive writes require current approval policy.
A10: UI keyboard navigation, focus, dialog closure, status announcements and narrow-screen scrolling pass accessibility/usability checks.
A11: Active detector weights and source datasets remain unchanged by adviser training/calibration.

## 7. Delivery gates

Phase 1: coordinator, telemetry, rule-based preflight/dialog and advisory monitoring.
Phase 2: checkpoint-safe stop/resume with demonstrated continuity per backend.
Phase 3: bounded calibration and data collection.
Phase 4: learned predictors with validated coverage, promotion/rollback and schedules.
Automatic stop requests are enabled only after fault-injection and checkpoint recovery acceptance. No phase may present a later feature as operational.

## 1.3.3 additions

- **F24 — Explicit engine lifecycle:** Settings shall expose Start/Stop and live running/stopping/stopped state. The persisted choice shall survive restart. Stopping shall pause monitoring and schedules and cancel only adviser workers; detector training and preflight safety remain available. Reason: give operators direct control of background adviser resource use.
- **F25 — Historical checkpoint learning:** An administrator shall import numeric training/checkpoint companions from configured local run directories, receive counts/exclusions, and train an independently evaluated historical duration candidate. Missing memory/hardware provenance shall remain unknown and block its promotion as a safety predictor. Reason: use prior learning evidence without fabricating resource labels or executing old checkpoint objects.
- **F26 — Detection workspace:** Reviewed Samples, class distribution/export, collection, review, enlarged-image actions and keyboard navigation shall live on `/detection`; Custom detector shall use the full remaining ML Training workspace. Existing data, review APIs and training inputs shall remain compatible. Reason: separate labeling from training while preserving an obvious route between workflow stages.
