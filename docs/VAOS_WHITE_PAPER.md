# VAOS: a local-first video analytics platform

**Community white paper · 16 November 2026 · Version 1.1.0 candidate**

**Original creator:** Prateek Dubey · [Website](https://prateekdubey.in) · [LinkedIn](https://www.linkedin.com/in/progressiveprateek)

> VAOS was originally created by Prateek Dubey and is developed with contributions from the open-source community.

## Abstract

Video analytics projects often require separate tools for camera access, detection, dataset curation, model training, operational monitoring and agent integration. VAOS (Video Analytics Operating System) brings these workflows into a self-hostable application. Its design keeps camera and model data on the operator's machine by default, provides a browser interface and documented API, and lets administrators decide whether to enable remote access or AI-agent controls.

This paper describes the current VAOS architecture and its boundaries. VAOS 1.1.0 is an evolving community release candidate, not a claim that every camera, accelerator, dataset, deployment topology or regulatory environment is certified. The AI Model Arena and Ready-to-Run Vision Applications are deferred from this version by product scope decision.

## Motivation and design principles

VAOS is intended to make an inspectable computer-vision workflow available without requiring that every image or model be sent to a hosted service. The project follows five practical principles:

1. **Local-first:** application state, reviewed imagery, datasets and weights are stored on the configured VAOS host unless an administrator deliberately configures another destination.
2. **One policy path:** browser, REST API and optional MCP agent actions reuse VAOS authentication and authorization rather than implementing separate business rules.
3. **Human review:** collected or predicted samples can be reviewed and used to improve a detector; automated detections are not presented as ground truth.
4. **Operational visibility:** service lifecycle, training activity, hardware samples and security events can be inspected through the application and its observability APIs.
5. **Open extension:** the application is distributed under Apache License 2.0. Contributors can fork and adapt it while preserving existing copyright notices and the licenses of third-party assets.

## System architecture

The standard installation has two Python web services and a browser UI:

| Component | Responsibility | Persistence and boundary |
| --- | --- | --- |
| Admin service (`main.py`, normally port 8000) | Dashboard, settings, REST APIs, model and dataset workflows, training coordination, tracking rules, project bundles and observability | SQLite and local files in the configured VAOS data directory; the admin API should not be directly exposed to the public Internet |
| Camera service (`camera_service.py`, normally port 8001) | Camera station, enrollment pages and frame intake | Communicates with the admin service using an internal credential; public camera/enrollment links are capability URLs and must be treated as secrets |
| Browser application (`templates/`, `static/`) | Operator workflows for camera review, models, datasets, monitoring and configuration | Calls the same authenticated service APIs; it is not a separate data store |
| Vision pipeline | Object inference, face enrollment/matching where enabled, approximate tracking, and custom detector training | Uses local model files and available CPU/CUDA/MPS backends as supported by installed libraries and hardware; compatibility is conditional, not universal |
| Optional MCP bridge (`mcp_server.py`) | Exposes a defined set of VAOS API operations to a local agent host over standard input/output | Calls the REST API; it does not access the database, model files or operating-system shell directly |
| Production deployment | Separate admin/camera containers, persistent storage and reverse proxy configuration | The included CPU-container workflow is smoke-tested in CI; production proxy, backup/restore and GPU-host behavior require deployment-specific validation |

SQLite schemas are upgraded additively. Model, dataset, photo and review files are local assets rather than database blobs. Training and import jobs currently use in-process coordination, so abrupt process loss does not provide durable job recovery. Operators should back up the database and the corresponding data directory together.

## Operator workflow

An operator can install VAOS, configure local access, connect a supported camera, review detections, prepare a detector-ready dataset, choose an initial model and train a custom detector. Training activity and available metrics can be reviewed in the UI and through authenticated APIs. The system can use a CPU fallback; CUDA and Apple MPS selection depend on the operating system, driver/toolkit and compatible package build. A successful installation does not guarantee that a model fits available RAM or accelerator memory.

The camera workflow produces detections from a selected model, may associate them with approximate short-lived track identifiers, and can apply configured object-count or dwell rules. Those IDs and rule events are operational aids, not identity guarantees. Human review remains important when collecting samples or using detections to construct training data.

Dataset preparation validates and converts supported source formats into the detector's expected layout. Dataset availability alone does not establish image quality, label correctness, class alignment, license compatibility or suitability for an Indian deployment context. Data owners should inspect class maps, duplicates, train/validation separation, annotation samples and usage rights before training. Third-party dataset and pretrained-weight terms are independent of the VAOS source license.

## API and AI-agent integration

The REST API exposes supported application functions using OpenAPI documentation. The built-in API Explorer is a user interface for that API, not a second service. API access starts disabled and should be enabled only when needed. Administrators can issue scoped, expiring tokens and select feature groups; sensitive reads require explicit policy and token grants.

The optional MCP bridge communicates with local agent hosts over stdio and forwards requests through the same API controls. New installations begin with limited capabilities. MCP-designated write actions use a human approval process bound to the specific request; an agent cannot approve its own action, create API tokens or bypass the API policy. The bridge has no general-purpose shell or direct database tools. Do not place populated credentials in Git, public issue reports or this white paper.

## Security, privacy and governance

Local operation reduces routine transfer of imagery but does not by itself make a deployment secure or compliant. Operators are responsible for camera notice and consent, retention, access controls, encrypted transport for remote access, secure backups, incident response and applicable privacy or biometric laws. Face templates, enrollment photographs, review images, videos and logs may be sensitive. Restrict file-system and backup access, and delete data according to a documented policy.

Keep the admin service behind localhost, a VPN or an identity-aware gateway; do not publish it directly to the Internet. Use separate high-entropy credentials for internal service communication, scope API tokens narrowly, revoke unused tokens and avoid exposing MCP credentials through a public tunnel. Review third-party model, dataset and dependency licenses independently.

The project includes request-size controls, dependency checks, safe dataset parsing and automated security regressions. The latest candidate's VAPT-style source and stability assessment is documented in [`VAPT_STABILITY_1.1.0.md`](VAPT_STABILITY_1.1.0.md). That assessment is not a third-party penetration-test certificate. CI does not replace target-host security testing, physical-device validation or operational restore exercises.

## Hardware and operating-system boundaries

The codebase includes Windows launch support and manual startup guidance for Linux and macOS. CPU execution is the fallback path. NVIDIA CUDA and Apple MPS are selected conditionally when the required runtime and compatible libraries are available. Hardware inventory and utilization reporting depend on vendor interfaces, and some telemetry is NVIDIA-specific. CI contract tests ran on Windows, Ubuntu and macOS 15 Intel; this is not Apple Silicon/MPS certification.

There is no universal minimum RAM, maximum model size or guarantee for every GPU vendor. Training, batch size, image resolution, camera count and model architecture affect CPU, memory, VRAM and throughput. Review the [platform support matrix](PLATFORM_SUPPORT.md) and measure the intended workload on the actual target hardware before production use.

## Validation snapshot and limitations

For the current candidate, the isolated Windows/Python 3.12 suite passed **315 tests**, with three platform-specific skips. CI workflow [#35](https://github.com/indipranisha/VAOS/actions/runs/43853246493) passed all eight contract, MCP integration, browser smoke and production CPU-container jobs across Windows, Ubuntu and macOS 15 Intel. The browser smoke covers selected routes and interactions; it is not a complete accessibility audit or the seven full persona journeys. The CPU container smoke does not validate a production host, persistent restore, reverse-proxy rules or GPU container.

Physical camera behavior, real CUDA/MPS training and inference, extensive load/soak tests, production backup/restore, live SIEM receivers, broad accessibility, and target-specific security testing remain outside that evidence. Therefore, the candidate is pushed to a review branch, while the documented staging recommendation remains **NO-GO** until applicable target-environment gates are completed. See the [release-readiness report](RELEASE_READINESS_1.1.0.md).

## Community participation

VAOS welcomes contributors who can improve usability, accessibility, supported hardware, dataset validation, documentation, tests, security and integrations. Start with the [contribution guide](../CONTRIBUTING.md), read the [complete user documentation](README.md), and submit focused changes with tests and clear evidence. Report security issues through the repository's documented private reporting path rather than posting credentials or sensitive sample data publicly.

The attribution statement recognizes VAOS's original creator while leaving community and third-party ownership intact. Contributors should retain existing copyright notices and receive appropriate credit in changes and release notes. Forks, modification and redistribution remain governed by Apache 2.0 and applicable third-party licenses.

## References

- [VAOS README and source](../README.md)
- [Project overview](PROJECT_OVERVIEW.md)
- [Installation guide](INSTALLATION.md)
- [Complete user manual](COMPLETE_USER_MANUAL.md)
- [Configuration reference](CONFIGURATION_REFERENCE.md)
- [REST API documentation](API.md)
- [MCP reference](MCP.md)
- [VAPT and stability assessment](VAPT_STABILITY_1.1.0.md)
- [Creator and community credits](CREDITS.md)
