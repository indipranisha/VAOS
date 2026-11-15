# VAOS 1.1.0 review record: AI Model Arena

**Date:** 2026-11-12 (Asia/Kolkata)  
**Review type:** Read-only architecture, backend, computer-vision, security, UX and QA review.  
**Active-run constraint:** The prior training attempt failed at epoch 6/50 after a NumPy allocation error. A follow-up sample showed about 96.5% host RAM use and 11.6/12 GB GPU memory allocated. No model, GPU benchmark, test suite, camera stream or service was started for this review.

## Independent review record

| Role | Evidence | Finding / decision |
| --- | --- | --- |
| Software Architect / Backend | Local in-session read-only agent review | Reuse `list_ml_model_sources`, `/api/admin/ml/history`, registered dataset records and the existing inference/runtime conventions. Do not add a parallel model registry. Persist comparison provenance/results additively if a job is implemented. |
| Computer Vision Specialist | Local in-session read-only agent review | Compare candidates sequentially against exactly the same fixed validation manifest, class map, preprocessing, evaluator and device settings. Show per-class and aggregate metrics and mark incompatible class maps “not comparable.” Do not present training-set scores as an unbiased benchmark. |
| Security Engineer | Local in-session read-only agent review | Model checkpoints uploaded from outside the local training flow are not yet validated in a quarantine process. Loading arbitrary `.pt` files inside the admin service may invoke unsafe checkpoint deserialization; do not use such files in the Arena until that risk is addressed. |
| UX Specialist | Local in-session read-only agent review | Show eligibility, queue/wait reason, progress by candidate, failures, provenance, results and a clear limitation when no holdout exists. Never silently change the active detector. |
| QA Engineer | Local in-session read-only agent review | Add deterministic-manifest, class-map, leakage, metric, queue, cancellation, authorization, MCP-approval and non-mutation tests. Mock model execution in CI; do actual hardware runs only when idle and resource-safe. |

These were local read-only agents available in this Codex session. No external reviewer, hosted AI service, or production user was used. No files were changed by the reviewers and no tests/models were run.

## Existing-functionality comparison

- **Retain:** The model-source picker, local training history and current detector are the source of model metadata; do not create competing registries.
- **Reuse:** Registered detector-ready dataset YAMLs and their validation splits; the Arena must not use reviewed samples that are also training inputs as a supposedly independent holdout.
- **Missing:** Fixed manifests with image/label hashes, fair sequential comparisons, persistent comparison reports, queue/cancel states and a dedicated user interface.
- **Security conflict:** Arbitrary imported `.pt` checkpoints are not safely quarantined. Until a separate validation boundary exists, an Arena must not load them in-process. Locally trained checkpoints and an allowlisted official source may be eligible only after their provenance and class compatibility are verified.

## Implementation decision

**Decision: implement only after the resource and checkpoint-safety gates are present.** The comparison design is approved, but no Arena execution path is added in this phase. The machine is under severe RAM/VRAM pressure and the current checkpoint-import path is not a safe place to load untrusted model files. The feature stays explicitly **not implemented**; no benchmark numbers are claimed. A later implementation should use an immutable, hashed validation manifest, fixed class map and preprocessing, sequential single-candidate evaluation, a safe worker boundary, explicit queue and cancellation semantics, and never alter `best.pt` or the live model.

## Validation status

No model inference, benchmark, UI journey, test, performance measurement or security test was performed. At the time of this review, AI Model Arena was a release gap. On 2026-11-12 the product owner deferred it beyond v1.1.0; this document is retained as historical safety analysis, not a current-version release blocker.
