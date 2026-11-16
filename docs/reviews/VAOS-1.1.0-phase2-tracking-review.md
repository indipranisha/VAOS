# VAOS v1.1.0 Phase 2 review record: Tracking and Event Rules

**Date:** 2026-11-12 (Asia/Kolkata)  
**Scope:** Source implementation of camera-scoped object count and dwell rules.  
**Active-run constraint:** Training was at epoch 6/50 and host RAM use was about 90% during implementation. It later failed with a DataLoader NumPy allocation error; a follow-up sample showed about 96.5% host RAM use and 11.6/12 GB VRAM allocated. No service restart, camera session, GPU work, or test process was started.

## Existing-functionality decision

The existing `process_frame()` pipeline already performs YOLO inference, class-aware IoU matching, temporary track assignment, and object sighting aggregation. Existing `/api/admin/events` serves person sightings. Decision: enhance the shared detector path and add separate object-rule history; do not replace person events, introduce another detector/tracker, or reinterpret approximate IDs as identity.

## Independent read-only review

| Role | Review source | Finding / resolution |
| --- | --- | --- |
| Software Architect / Backend / CV | Local in-session read-only agent review | Recommended additive `object_event_rules`/`object_rule_events`, camera scoping, consecutive frames, cooldown, metadata-only history, and explicit disclosure that IDs are ephemeral. Adopted. The reviewer advised no new detector or tracker. |
| Security Engineer | Independent code review | Camera tokens appear in rule responses and must be treated as sensitive reads; MCP rule mutations need sensitive-action controls plus the existing one-time approval, with delete policy for deletion. Added route policy coverage in source/tests. |
| Frontend Engineer / UX Specialist | Independent code review | Use a dedicated rule page with clear object label, camera, rule type, percentage region, confirmation frames and cooldown; show loading/empty/error states and manual refresh. Implemented in source; real browser review pending. |
| QA Engineer | Independent code review | Add pure synthetic evaluator tests and API permission/approval tests; do not run service/GPU/camera tests while the training process is active. Added tests, deferred execution during memory pressure. |
| Documentation Specialist | Independent code review | Describe rule semantics, approximate track limits, privacy, API scopes, retention, and untested release gates. Added the tracking guide and traceability updates. |

The only multi-agent review was the local read-only in-session agent pass above. Other roles were independent source reviews by Codex, not external agents. No external reviewer or production user participated. No unresolved design disagreement was identified.

## Implementation decisions and boundaries

- Rule types in this slice: `count_in_region` and `dwell_in_region`, with normalized rectangular regions.
- Requires a detector-provided class label, configurable confidence, consecutive-frame confirmation, camera scope and cooldown.
- Rule definitions and alert metadata use additive SQLite `CREATE TABLE IF NOT EXISTS` statements. Existing camera, person sighting, object sighting, review sample, and training tables are untouched.
- Existing camera event clearing now also clears rule alert history, while preserving saved rules. Deleting a rule retains its past alerts. History is capped to 5,000 newest rows.
- Rule definitions are cached for three seconds; rules use the existing event API capability, and their reads require global plus token sensitive-read grants. MCP changes use the existing exact-request approval.
- No video, image, face, or personal identity is stored with a rule alert.
- Line crossing and alert delivery are not in this implementation. Track continuity remains approximate and only within the existing short-lived tracker.

## Verification status

- Python compilation and browser JavaScript syntax have passed after the Phase 2 edits; whitespace checking is clean. OpenAPI path/method count assertions, pure evaluator tests, and API security tests are **pending**.
- Automated tests are deferred while training telemetry shows high host RAM. No real camera test, UI browser journey, GPU benchmark, or service activation has been performed.
- Staging remains **NO-GO**. The other feature areas, regression suite, and usability/security/performance gates remain incomplete.
