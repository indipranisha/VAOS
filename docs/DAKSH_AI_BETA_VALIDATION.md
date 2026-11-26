# Daksh-AI 1.3.0-beta.2 — validation and FRS/SRS traceability

Date: 2026-11-26. Implementation of the proposed workflows is present; hardware certification and real-world predictor quality remain conditional on evidence. The original beta.1 partial validation is retained in DAKSH_AI_BETA1_VALIDATION.md.

## Evidence

- Full Python discovery: 374 tests passed (API/security, MCP bridge/stdio, local SIEM TLS integration, observability, resources, launcher, hardware, bundles, UI contracts and Daksh).
- JavaScript: 8 tests passed (class distribution and Daksh dialog behavior).
- Dedicated Daksh suites: 37 tests covering rules, orchestration and isolated learning/registry behavior, included in discovery.
- CPU smoke (tools/validate_daksh_cpu.py): generated tiny detector and images; one CPU thread; stop after epoch 1 preserved optimizer, resume with batch 2 completed original epoch 2; calibration produced a record; source weight hash unchanged. Rerun after checkpoint integrity/RNG checks also passed.
- Rules monitor benchmark (tools/benchmark_daksh_monitor.py): 100 generated samples, approximately 0.114 seconds CPU total, 11.7 MiB RSS growth; estimated 0.023% of one core at five-second intervals. This excludes accelerator probes and learned inference and is not a universal performance guarantee.
- Browser: isolated Settings instance; schedule create/edit disabled by default verified; desktop layout corrected after finding narrow action rows; mobile 390px viewport had no horizontal overflow and controls remained accessible by keyboard. The existing native confirm stalled the browser; redundant confirmation was removed in favor of the explicit Daksh acceptance dialog. Dialog text, Accept/Manual/Cancel, busy disabling, Escape and focus-return were verified by DOM-harness tests. Full end-to-end browser launch was not completed because the test browser input remained blocked. No production settings or training were changed.
- Initial full regression exposed an OpenMP crash from loading LightGBM beside PyTorch. Fixed by isolated learner processes and pure tree-JSON inference. No unsafe duplicate-runtime override is used.

## Requirements traceability

| FRS | Implementation | Evidence / boundary |
|---|---|---|
| F01 | Existing host adapters + daksh_runtime.host_snapshot, backend signatures | Hardware/telemetry suites; other OS execution pending |
| F02–F07 | Typed preflight schema, Daksh.recommend/accept, dialog, rules and promoted predictor | Bounds/expiry/replay/identity/fallback and API tests; GPU-specific measured coverage required |
| F08–F09 | Background monitor with persistence, phases, stale telemetry, cooldown/recovery and UI incidents | Runtime incident/swap/stall/validation tests; platform pressure thresholds remain beta policies |
| F10–F12 | No active mutation; verified checkpoint copy, accepted resume; opt-in stop requests | Real CPU stop/resume; CUDA/MPS continuity pending |
| F13 | Transactional leases, PID/start identity, elected scheduler, helper integration and contention detection | Cross-process/stale-lease tests; arbitrary third-party GPU detection remains best effort |
| F14–F18 | Manual jobs, Settings schedules/edit/cancel; isolated calibration; signature freshness | Scheduler DST/busy/idempotency tests, real CPU calibration, Settings UI checks |
| F19 | Grouped held-out CPU learner; evaluation gates; hashes; explicit promotion/rollback | Synthetic learning/registry tests; production predictor needs representative data |
| F20 | Job progress/results, next-run timestamps, failure reasons, supervisor health | API/UI checks; browser launch limitation above |
| F21 | Typed versioned REST contracts + existing MCP JSON bridge | API/MCP scope/approval tests and OpenAPI stability |
| F22 | Stable-ID durable event outbox → resilient audit/SIEM | Outbox retry tests plus existing SIEM integration suites; external customer SIEM not contacted |
| F23 | Bounded numeric retention/export, full recoverable clear/restore | Record validation/recovery tests; artifact/recovery-file storage separate |

## Release gates and limits

Implemented does not mean universally validated. CPU reference workflows are verified on this Mac. Windows/Linux native tests, CUDA/MPS checkpoint continuity, long-duration thermal/memory-pressure stress, screen-reader testing, real trained-adviser generalization and customer SIEM usability still need platform-specific execution. Automatic stop requests and schedules default disabled. Adviser promotion requires explicit authorization; a model failing evaluation cannot be promoted. Do not publish claims of industry certification or universal hardware safety.

The user's active training was not restarted, paused, reconfigured or used as a calibration target. Test jobs used generated data or isolated state. These source changes have not been committed/pushed or activated in the old running service.

Worker lifecycle hardening: monitor exceptions terminate isolated child workers before releasing their lease; a worker watchdog exits if its parent disappears or its time budget expires. This never force-kills detector training. A dedicated regression covers cleanup after telemetry failure. Calibration inputs are bounded to 32 images, 16 MiB each, with image pixel and label-size guards.
