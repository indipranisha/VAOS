# VAOS v1.1.0 feature traceability

This is the baseline map before feature implementation. A cell marked “gap” is not a test pass. The source implementation status after Phase 1 is appended below; baseline evidence remains historical. By product-owner scope decision on 2026-11-12, AI Model Arena and Ready-to-Run Vision Applications are deferred beyond v1.1.0; their rows are historical only and are not current-release acceptance criteria. Detailed comparisons and blockers: [baseline audit](reviews/VAOS-1.1.0-baseline-audit.md).

| Requirement | Existing UI/source | Existing API/MCP | Required implementation | Test evidence at baseline | Documentation |
| --- | --- | --- | --- | --- | --- |
| AI Model Arena — deferred | Model picker/training history only; no Arena comparison UI | No comparison API/tool | Deferred beyond v1.1.0 by product-owner scope decision | Not a v1.1.0 acceptance criterion | Historical baseline only; see [deferred scope](FEATURE_CATALOG.md#deferred-beyond-v110) |
| Object Tracking & Event Rules | Existing detection/tracking toggle and sightings | Existing sightings API; no rules | Persistent line/zone/rule CRUD, event evaluator, alert/retention path | Gap: no event-rule unit/E2E tests | Feature catalogue only; dedicated guide pending |
| Built-in API Explorer | Swagger `/api/docs`; Settings token controls | 42 paths/50 methods, generated OpenAPI | Searchable embedded explorer, guarded Try-it, examples, route docs links | Schema stability and protected docs tested; no full browser test | `docs/API.md`; detailed Explorer guide pending |
| MCP Agent Control Centre | Settings policy/token panel and approval inbox | Optional stdio tools/resources; token-bound single-use approval-status API | Agent/policy/activity polish and full SDK runtime/usability validation | Official SDK v2.3 stdio client/process test passes locally against a synthetic API fixture; named host UI and Windows/macOS/Ubuntu CI results remain pending | [`docs/MCP.md`](MCP.md), [`docs/MCP_HOST_SETUP.md`](MCP_HOST_SETUP.md) |
| Hardware Intelligence | Dashboard/diagnostics telemetry and device inventory | Diagnostics endpoint | Compatibility matrix, measured backend checks, explainable recommendations and OOM handling | Gap: actual hardware/provider tests | Feature catalogue only; dedicated guide pending |
| Ready-to-Run Vision Applications — deferred | No template gallery | No app/template API | Deferred beyond v1.1.0 by product-owner scope decision | Not a v1.1.0 acceptance criterion | Historical baseline only; see [deferred scope](FEATURE_CATALOG.md#deferred-beyond-v110) |
| Shareable VAOS Project Bundles | No Projects UI | No bundle API | Versioned allowlisted manifest, safe import/export/preview/dry-run | Gap: no security/round-trip tests | Feature catalogue only; format guide pending |
| Observability & Analytics Centre (Feature 8) | Existing health/hardware/training logs but no unified analytics page or event store | Existing REST/MCP scopes, no unified metric/event/report API | Shared lifecycle, metric/event and restricted audit store; paginated query API; bounded CSV/PDF reports; CLI; opt-in signed HTTPS, Syslog TLS and OTLP/HTTP JSON delivery with event mappings | Not present at baseline; source work and focused tests tracked below | [Observability docs](observability/overview.md) |
| Creator attribution (cross-cutting) | No dedicated About page or footer attribution at baseline | No public metadata contract | Configurable creator identity and links, exact shared attribution statement, unobtrusive footer, project/citation metadata, community and license acknowledgement | Baseline gap; source-level and responsive browser verification required | `docs/CREDITS.md`, in-app About page and README |

## Cross-cutting safety requirements

| Cross-cutting requirement | Baseline state | Release evidence needed |
| --- | --- | --- |
| Preserve camera, face, dataset, model, and training behavior | Existing code retained; source/live versions differ | Legacy route regression tests and safe migration tests |
| Sensitive reads and actions | Sensitive reads now require global and per-token opt-ins; MCP-bound writes require exact-request approval | Add route-by-route privacy checks and full real-agent/browser validation; keep Basic-only decision controls |
| Database upgrades | Additive startup `CREATE`/`ALTER`; no migration ledger | Migration versioning, isolated upgrade/downgrade or restore evidence |
| Tests and CI | Isolated `unittest` suite and Windows/Linux compile+test workflow now exist | Add migration, full functional, dataset/camera/training and release suites; passing current tests is not staging readiness |
| Accessibility/usability | Responsive CSS and some semantic states; no actual test | Keyboard, contrast, focus, 1366x768/1920x1080/mobile browser evidence and persona journeys |
| Privacy/open-source license | Face/privacy notices and GNU AGPL-3.0-only `LICENSE` exist; bundled Ultralytics-trained model provenance is documented | Review third-party model/dataset terms and add bundle/API privacy tests |

## Working-tree progress after Phase 1

| Requirement | Current source addition | Validation and limitation |
| --- | --- | --- |
| Built-in API Explorer | Administrator-only `/api-explorer` renders the live generated OpenAPI schema; search, contract details, parameter/body entry, same-origin Try It, write confirmation, and credential-free examples | JavaScript syntax and Python compilation pass. The current admin health endpoint reports 1.1.0, but its loaded source path and authenticated browser session were not verified; no browser run is claimed and no existing service was restarted. |
| Hardware Intelligence | `/hardware` and authenticated `/api/v1/admin/hardware` report existing telemetry, measured GPU rows, explicit unknowns, and non-applied estimates | Injected-fact tests pass in the latest repository suite (215 tests, 37.843 seconds on 13 November 2026); Python and JavaScript syntax checks passed. No live GPU probe, benchmark, model load, or hardware matrix run was performed. |
| Object Tracking & Event Rules | `/tracking` editor and current-frame event evaluation reuse `process_frame` and approximate short-lived IDs; rules/events use additive SQLite tables | Pure evaluator/API authorization tests pass in the latest repository suite (215 tests, 37.843 seconds on 13 November 2026). No live-camera journey was run; IDs remain approximate and reset after restart. |
| API/MCP cross-cutting safety | Existing REST permissions remain authoritative; MCP-designated writes require exact-request administrator approval and the Explorer accepts only same-origin paths | Isolated API/MCP tests are tracked separately; live MCP-host and browser usability evidence is still pending. |
| Creator attribution | `VAOS_PROJECT_METADATA.json` is the app's canonical identity source; `/about`, the dashboard footer, README/docs, and `CITATION.cff` expose creator/community information while preserving Apache and third-party notices | Static source inspection only. Website opened; LinkedIn returned automated-access HTTP 999. Desktop/mobile rendering and real click-through remain unverified. |

## Creator attribution acceptance criteria

| ID | Requirement | Implementation source | Release acceptance evidence |
| --- | --- | --- | --- |
| ATTR-01 | About VAOS identifies Prateek Dubey as Founder and Original Creator. | VAOS_PROJECT_METADATA.json, project_metadata.py, templates/about.html | Page displays the configured name and role. |
| ATTR-02 | About VAOS links to the creator website and LinkedIn profile. | Central metadata fields rendered by templates/about.html | Click both links from desktop and mobile; confirm the destination is the configured HTTPS URL and the link remains keyboard accessible. |
| ATTR-03 | Footer attribution is subtle and does not interrupt normal workflows. | templates/_creator_attribution.html, static/style.css | Footer is readable, wraps on small screens, and creates no overlay, modal, splash or required acknowledgement. |
| ATTR-04 | GitHub README presents creator attribution followed by community-contributor acknowledgement. | README.md, docs/CREDITS.md, CONTRIBUTING.md | Confirm creator section and contributor language render as Markdown and retain Apache/third-party notices. |
| ATTR-05 | Product documentation and release notes include attribution. | docs/README.md, docs/PROJECT_OVERVIEW.md, CHANGELOG.md, docs/CREDITS.md | Confirm the documentation index, overview and v1.1.0 notes link to the canonical credits page. |
| ATTR-06 | Use the exact shared statement: “VAOS was originally created by Prateek Dubey and is developed with contributions from the open-source community.” | VAOS_PROJECT_METADATA.json and the canonical credits page; the application footer renders the configured name in that wording. | Verify wording and attribution consistency across About, footer, README, documentation and release notes. |
| ATTR-07 | Store structured project and creator metadata where supported. | VAOS_PROJECT_METADATA.json, CITATION.cff | Validate the JSON and citation fields; keep both aligned with the public About page. |
| ATTR-08 | Keep creator data centrally configurable and preserve other rights. | project_metadata.py loader and shared footer partial; AGPL-3.0-only LICENSE | Change creator links in the central metadata source and verify app pages update; retain notices and do not claim ownership of third-party or community contributions. |
| ATTR-09 | Verify desktop/mobile layout and links. | Responsive About CSS and shared attribution partial | Browser evidence at desktop and phone widths, including focus state and successful link navigation, is still required before release. |
| ATTR-10 | Do not add promotional interruptions or mandatory acknowledgements. | About page and normal page footer only | Inspect standard camera, review and training journeys for splash screens, repeated dialogs or required acknowledgement. |

Current status: implementation and static source checks are present. The website opens successfully. Automated retrieval of LinkedIn returned HTTP 999, so profile reachability could not be independently confirmed here; desktop/mobile browser and click-through evidence remain pending. This is not a release-ready verification claim.
This progress table records source changes, not completion of a feature's full release acceptance criteria. See [Phase 1 review record](reviews/VAOS-1.1.0-phase1-api-hardware-review.md) and [project status](PROJECT_STATUS.md).
