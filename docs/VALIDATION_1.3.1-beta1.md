# VAOS 1.3.1-beta1 acceptance evidence

Executed locally on macOS / Apple Silicon on 2026-11-26. Production detector process PID 78961 was left running. Browser tests used separate temporary data/models on ports 18762/18763; both test services were stopped afterward.

## Results

- Full Python suite: **383 passed**, 23.921 seconds (`/tmp/vaos-131-acceptance.log`). Includes API/authentication/security, MCP bridge/policy, isolated adviser learning, scheduling, lifecycle, UI shell and existing application contracts. Expected negative-test launcher/error messages are present; the final unittest result is OK.
- JavaScript: **8 passed** (`/tmp/vaos-131-acceptance-js.log`), covering adviser modal and class-distribution behavior.
- Python compilation, JavaScript syntax checks, rendered Detection/Training inline-script syntax, synchronized metadata and CRLF-aware whitespace checks passed.
- Historical artifact SHA-256 matches its registered manifest. Candidate ID: `d53fe84fc4f376c9fee0fed7b540a96f`.

## New acceptance coverage

| Requirement | Evidence |
|---|---|
| F24 / S24 engine lifecycle | Start/Stop API routes, versioned routing, stored disabled state respected by a new Runtime, re-enable/stop, detector stop callback never invoked by engine controls; browser stop, isolated service restart remained stopped, browser start restored running state |
| F25 / S25 historical learning | Idempotent run/epoch import, unknown memory labels, incomplete trailing row, timing gaps, non-finite values, external symlink refusal, isolated CPU candidate with whole-run holdout, no safety-predictor eligibility, authenticated valid-history queuing and empty-history refusal |
| F26 / S26 workspace separation | Authenticated `/detection`; reviewed card, collection, review and modal included only there; training start controls included only on ML Training; existing sample storage/routes retained |
| API/MCP authorization | New mutation routes require admin authorization; MCP-bound Training tokens receive exact-request approval-required responses; old endpoints remain covered |
| Browser UI | Detection refresh reached an empty-data state, class-distribution dialog and export controls opened, Custom detector measured 1,224 px across a 1,224 px workspace, no document horizontal overflow, page scrolling available; Settings engine controls reflected their current state |
| Settings refresh | Hidden dashboard graphs excluded from Settings refresh; no console errors after the corrected page reload/polling |

## Actual historical rebuild

Explicit one-thread CPU rebuild used all usable recorded history in the configured VAOS run directories: **223 completed epochs, eight runs, 16 checkpoint files**. It never deserialized binary checkpoints or opened GPU training. Duration candidate:

- Model JSON: **145,750 bytes**; data/artifact locations remain local under `data/daksh`.
- Training/held-out records: **167 / 56**, partitioned by complete runs.
- Held-out relative duration error: **0.28025**; MAE: **2,105.36 seconds** across runs with widely different epoch durations.
- Promotion: **false**. Historical RAM/VRAM peaks and reliable original hardware snapshots were not recorded. This duration model does not replace resource safeguards, modify detector weights, or drive live ETA/supervision. Measured calibration remains necessary for the resource-safety predictor.

## Scope and limits

No physical Windows/Linux/CUDA stress or cross-device generalization certification was performed. No claim of industry certification or universal model accuracy is made. Browser checks used an empty isolated review database; populated zoom/review behavior remains covered by the existing implementation/contracts, not a new manual populated-gallery exercise. Production backend was not restarted, so new routes/version activate only after a service restart when the current training can finish safely.
