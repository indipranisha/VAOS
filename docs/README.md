# VAOS documentation

VAOS was originally created by Prateek Dubey and is developed with contributions from the open-source community.

See [creator and community credits](CREDITS.md) for the creator's role and profile links, or open the in-app **About VAOS** page. VAOS is an AGPL-3.0-only open-source project. Community contributions are welcome; see the [contribution guide](../CONTRIBUTING.md). Keep existing project and third-party notices when reusing the code or assets.

## Guides

- [Project overview](PROJECT_OVERVIEW.md)
- [Installation and upgrade entry point](INSTALLATION.md)
- [Beginner's quick start](QUICK_START.md)
- [User guide](user-guide.md)
- [Administrator guide](ADMIN_GUIDE.md)
- [Model and training guide](MODEL_TRAINING.md)
- [Camera and streaming guide](CAMERA_STREAMING.md)
- [Security and privacy guide](SECURITY_PRIVACY.md)
- [Database schema overview](DATABASE_SCHEMA.md)
- [Frontend page and asset catalogue](FRONTEND_CATALOG.md)
- [Extension and contribution guide](EXTENDING_VAOS.md)
- [User and production setup](../README.md)
- [REST API](API.md) and [API Explorer](API_EXPLORER.md)
- [Application function catalogue](function-catalogue.md)
- [MCP agent bridge](MCP.md)
- [MCP host setup and safe first connection](MCP_HOST_SETUP.md)
- [MCP controls in VAOS Settings](MCP_SETTINGS.md)
- [Hardware Intelligence](HARDWARE.md)
- [Operating-system and hardware support matrix](PLATFORM_SUPPORT.md)
- [Apple Silicon Mac development handoff](MAC_DEVELOPMENT.md)
- [Observability & Analytics Centre](observability/overview.md), [metric dictionary](observability/metrics-dictionary.md), [reporting](observability/reporting.md), [security](observability/security.md), and [CLI](observability/cli-reference.md)
- [Observability storage and rollback](observability/database-schema.md), [lifecycle](observability/lifecycle.md), [Log Explorer](observability/log-explorer.md), [event schema](observability/log-schema.md), [signed webhook](observability/siem-soar.md), and [energy measurement limits](observability/energy-measurement.md)
- [Observability source and function catalogue](observability/source-function-catalogue.md)
- [Tracking and event rules](TRACKING_AND_EVENTS.md)
- [Project bundle format](PROJECT_BUNDLES.md)
- [Observability implementation plan](observability/implementation-plan.md), [validation record](observability/validation.md), and [Feature 8 release-readiness report](observability/release-readiness.md)
- [Architecture](ARCHITECTURE.md)
- [Testing and validation](TESTING.md)
- [Platform and MCP validation record](validation/platform-mcp.md)
- [v1.1.0 staging readiness report](RELEASE_READINESS_1.1.0.md)
- [Feature catalogue](FEATURE_CATALOG.md) and [traceability](TRACEABILITY_MATRIX.md)
- [Current release status](PROJECT_STATUS.md) and [release notes](../CHANGELOG.md)
- [License](../LICENSE) and [creator citation](../CITATION.cff)

This documentation describes the source tree and marks untested or incomplete behavior explicitly. It is not evidence that every feature has been released or tested on a live camera or GPU.

Current documentation release: **1.3.3**. See the main documentation index and release knowledgebase for current product behavior. Historical validation records retain their original versions.

API/MCP feature coverage and upgrade limits: docs/API.md, docs/MCP.md and docs/KNOWLEDGEBASE.md (see current feature contracts).

## Daksh-AI — training adviser and supervisor

- [Functional requirements (FRS)](TRAINING_ADVISER_FRS.md)
- [Software requirements (SRS)](TRAINING_ADVISER_SRS.md)

The FRS/SRS baseline is implemented in the 1.3.3 stable source. See [capabilities and guide](DAKSH_AI.md) and [validation](VALIDATION_1.3.3.md) for activation and platform limits.

Daksh-AI **1.3.3**: see [Daksh capabilities and API contracts](DAKSH_AI.md). The stable release includes supervision, calibration/retraining jobs and schedules, adviser promotion/rollback and verified checkpoint resume; see the guide for hardware validation limits.

VAOS **1.3.3** adds persistent Daksh engine Start/Stop in Settings, historical checkpoint-metric learning, a dedicated Detection workspace and full-width ML TRAINING workspace. See [release notes](RELEASE_NOTES_1.3.3.md) for behavior, rationale and validation limits.

[1.3.3 acceptance evidence](VALIDATION_1.3.3.md) records the test results, historical model evaluation and deployment limits.
