# VAOS 1.1.0

**VAOS — Video Analytics Operating System** is a self-hosted video analytics application. Camera processing, face templates, reviewed samples, model files and event records are stored on the VAOS host.

## Capabilities

- Private operations dashboard with camera links, QR codes, camera health, events, participant profiles and engine settings.
- A separate camera site for browser-based feeds and optional participant enrollment invitations.
- Local face detection and matching, object detection and tracking, plus CPU/GPU diagnostics.
- Human-reviewed object sample collection, dataset preparation, custom detector training and per-epoch history.
- An administrator-only API Explorer backed by the generated OpenAPI contract, plus a protected REST API and optional local MCP bridge.
- An administrator-only Observability & Analytics Centre for runtime sessions, measured host/GPU telemetry, training/API events, bounded CSV/PDF reports and opt-in security-event delivery over signed HTTPS, Syslog TLS or OTLP/HTTP. JSON/NDJSON, CEF and ECS-shaped mappings are available; OCSF is deferred until required identity/source fields can be mapped truthfully. This source-tree capability remains subject to the release gates in [project status](docs/PROJECT_STATUS.md).
- A local Hardware Intelligence page that reports measured device telemetry separately from unverified runtime states and non-applied estimates.
- Camera-scoped object count and dwell rules with a bounded metadata-only event history, reusing the existing detector and approximate tracker.
- SQLite persistence and local model/dataset storage.

### v1.1.0 scope decision

**AI Model Arena** and **Ready-to-Run Vision Applications** are not part of VAOS v1.1.0. This release will not include an Arena comparison workflow or a ready-made application/template gallery. Existing model selection, training, inference, and the separate Video Review workflow remain available. See [current project status](docs/PROJECT_STATUS.md) for the release scope and validation state.

Face templates are sensitive biometric data. Use only with informed consent and appropriate authorization. Validate accuracy for the intended setting, restrict administrator access, define retention/deletion practices, and meet applicable privacy and biometric requirements before operational use. This release uses one administrator account and one instance of each service; do not horizontally scale without first moving runtime state and training coordination into shared infrastructure.

## Run on Windows

1. Use Windows 10/11 and Python 3.12.
2. Start `start_windows.bat` and leave its window open while VAOS is in use.
3. Open `http://127.0.0.1:8000`. Sign in as `admin`; on a fresh data directory VAOS creates a long random password, prints it in the launcher and stores it in `data/admin_password.txt`. Protect that file.
4. The launcher starts the separate camera service on port 8001 and creates a temporary HTTPS Quick Tunnel for phone camera access. The temporary URL changes when the tunnel is recreated. Keep camera-link tokens private.
5. Create a camera link or QR code, enroll consenting participants, and use the **ML training** page for sample review and model training.

The Windows launcher installs local dependencies and optional object-detection support. If `cloudflared` is not installed by the administrator, it downloads the pinned official release and verifies its published SHA-256 before executing it. First use may download models. Press Enter in the launcher window to stop the services and tunnel.

## Cross-platform support status

The source includes CPU fallback, NVIDIA CUDA selection and conditional Apple MPS selection. The latest completed candidate CI at writing is branch commit `4c06c0b` in [run 43799338324](https://github.com/indipranisha/VAOS/actions/runs/43799338324): Windows/macOS/Ubuntu contract and MCP stdio jobs, an isolated CPU-container build/start smoke, and a limited Chromium desktop/mobile UI smoke passed. CI does not certify physical cameras, native MPS/CUDA drivers, or real-device model workloads. The Windows launcher and temporary tunnel controls remain Windows-specific. VAOS cannot promise operation on arbitrary RAM or every GPU vendor; see the [tested-versus-planned platform matrix](docs/PLATFORM_SUPPORT.md) for setup steps and boundaries.

## Deploy on a production server

A Docker deployment bundle is included. It runs the private admin and public camera services separately, stores app files in a persistent Docker volume, and uses Caddy for HTTPS.

### Requirements

- Linux server with Docker Engine and the Docker Compose plugin.
- DNS records for administrator and camera hostnames, both pointing to the server; inbound ports 80 and 443 must be available for Caddy TLS certificates.
- Put the administrator hostname behind a VPN, trusted identity-aware gateway, or equivalent network policy. Do not leave administrator access openly reachable without an access-control plan.
- For GPU acceleration, install the NVIDIA driver and NVIDIA Container Toolkit, then use the GPU Compose override.

### First deployment

1. Copy `production.env.example` to `production.env` and set both domain names.
2. Generate separate secrets (at least 32 characters) for `VAOS_ADMIN_PASSWORD` and `VAOS_INTERNAL_STATUS_TOKEN`. For each, run `python -c "import secrets; print(secrets.token_urlsafe(48))"` and paste the output into `production.env`. Protect this file and do not commit it.
3. Start VAOS:

   ```sh
   docker compose --env-file production.env -f compose.production.yaml up -d --build
   ```

4. For a server with NVIDIA Container Toolkit, choose a CUDA-compatible PyTorch wheel index from the official selector at [pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/), set `VAOS_TORCH_INDEX_URL` in `production.env`, then add `-f compose.production.gpu.yaml` before `up`. The GPU override deliberately requires this value; the base deployment installs CPU PyTorch by default.
5. Open `https://<VAOS_ADMIN_DOMAIN>` from the protected administrator network. Sign in as `admin` with the configured password. Camera URLs and QR codes use `https://<VAOS_CAMERA_DOMAIN>`.

Production mode rejects a missing/weak password or a non-HTTPS camera URL. It hides Windows Quick Tunnel controls; use Docker or the hosting platform for restarts. Keep one admin and one camera-service replica. Persist and back up the Docker volume containing the database, models and ML data. `docker compose down` preserves the volume; **do not use `down --volumes`** unless you intend to erase VAOS data.

### Production operations

- Restrict administrator access at the network/identity layer and use TLS for both hostnames.
- Configure encrypted, access-controlled backups and test restoration.
- Define consent withdrawal, retention and deletion procedures before enrollment.
- Monitor service health, disk space, model downloads and training logs.
- Review third-party model/package licenses and applicable biometric/privacy requirements.
- Validate in staging before operational use. This bundle is a deployable baseline; it does not provide high availability, multi-user roles, external identity management or compliance certification.

## API and MCP integrations

VAOS provides a versioned REST interface at `/api/v1`, a protected interactive API reference at `/api/docs`, and a generated OpenAPI document at `/api/openapi.json`. API access is off by default and can be configured in **Settings → API and AI agent access**. Create expiring read-only or control tokens there; secrets are displayed once and stored as hashes. See [the REST API guide](docs/API.md) for endpoint groups and [the MCP guide](docs/MCP.md) for the optional local agent bridge.

The MCP bridge is an optional stdio subprocess and does not add a permanent service or port. Install it only when needed with `requirements-mcp.txt`. It starts read-only, checks the VAOS policy for each call, and requires separate opt-ins for sensitive actions and deletions. MCP-bound control tokens also require a per-request, one-time administrator approval in Settings. Keep tokens out of Git and use HTTPS for remote API access.

The optional `vaos` terminal client uses the same versioned API permissions. See the [CLI guide](docs/observability/cli-reference.md). Observability endpoints and retention/report limits are documented in the [Observability Centre guide](docs/observability/overview.md).

## License

VAOS source code is distributed under the [Apache License 2.0](LICENSE). Review third-party model and dataset terms separately; the application license does not change those terms.

## Creator and community

VAOS was originally created by Prateek Dubey and is developed with contributions from the open-source community.

The [creator and community credits](docs/CREDITS.md) identify Prateek Dubey as Founder and Original Creator and provide the website and LinkedIn links. Contributors are acknowledged for their code, reviews, testing, documentation and other improvements. See [CONTRIBUTING.md](CONTRIBUTING.md) to take part. Attribution does not replace existing copyright notices or claim ownership of community or third-party work; the Apache 2.0 license and applicable third-party terms remain in force.

Structured creator and project metadata is recorded in [VAOS_PROJECT_METADATA.json](VAOS_PROJECT_METADATA.json) and [CITATION.cff](CITATION.cff). Product documentation begins at [docs/README.md](docs/README.md).

## Data and configuration

Windows stores files in the project `data` directory by default. Containers set `VAOS_DATA_DIR=/var/lib/vaos` on a persistent volume. It holds the SQLite database, profile photos and face templates, model weights, datasets, reviewed samples, training runs and logs. Treat backups as sensitive biometric data.

| Variable | Purpose |
|---|---|
| `VAOS_ENV` | Set to `production` for production safeguards. |
| `VAOS_ADMIN_PASSWORD` | Administrator password; required at 32+ characters in production. |
| `VAOS_PUBLIC_CAMERA_URL` | HTTPS camera-site origin, required in production. |
| `VAOS_CAMERA_INTERNAL_URL` | Private admin-to-camera diagnostics address; the container deployment sets this automatically. |
| `VAOS_INTERNAL_STATUS_TOKEN` | Shared private token for admin-to-camera diagnostics. |
| `VAOS_TRUSTED_PROXY_CIDRS` | Optional comma-separated proxy IP ranges allowed to supply trusted HTTPS-forwarding headers for remote API requests. Keep this limited to the actual reverse-proxy addresses. |
| `VAOS_DATA_DIR` | Persistent directory for the database, models and training data. |
| `MODEL_DIR` | Model-weight cache directory. |
| `OBJECT_DETECTOR_DEVICE` | `auto` (default) or `cpu` for object detection. |
| `MATCH_THRESHOLD` | Face similarity threshold, default `0.42`; evaluate against a consented set. |
| `EVENT_COOLDOWN_SECONDS` | Minimum time between repeated same-person/camera events, default `120`. |

The face pipeline uses OpenCV Zoo YuNet and SFace. Object detection uses the configured Ultralytics YOLO model when optional dependencies are installed. Custom object training uses reviewed samples and datasets that pass VAOS preparation. Labels, class coverage and data quality affect results; more epochs alone do not guarantee better accuracy. Face similarity is not a probability. Do not treat model predictions as validated safety-critical decisions.

YOLO and Ultralytics licensing may impose conditions on redistribution and deployment. Review licenses for all included and downloaded models/packages before commercial use.

## Documentation

- [Installation, upgrade and uninstall guide](docs/INSTALLATION.md)
- [Complete user manual](docs/COMPLETE_USER_MANUAL.md)
- [Configuration and environment-variable reference](docs/CONFIGURATION_REFERENCE.md)
- [Security, privacy and VAPT/stability assessment](docs/SECURITY_PRIVACY.md) · [assessment details](docs/VAPT_STABILITY_1.1.0.md)
- [API and MCP documentation](docs/API.md) · [MCP guide](docs/MCP.md)
- [Current release status and staging gates](docs/PROJECT_STATUS.md) · [release-readiness report](docs/RELEASE_READINESS_1.1.0.md)
