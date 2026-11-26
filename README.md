# VAOS 1.3.3

Stable release: [v1.3.3](https://github.com/indipranisha/VAOS/releases/tag/v1.3.3). [Release notes](docs/RELEASE_NOTES_1.3.3.md) · [Validation](docs/VALIDATION_1.3.3.md).

**VAOS — Video Analytics Operating System** is a self-hosted video analytics application. Camera processing, face templates, reviewed samples, model files and event records are stored on the VAOS host.

[Wiki](https://github.com/indipranisha/VAOS/wiki) · [Releases](https://github.com/indipranisha/VAOS/releases) · [Report an issue](https://github.com/indipranisha/VAOS/issues) · [Security policy](SECURITY.md)

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

**AI Model Arena** and **Ready-to-Run Vision Applications** are outside the current VAOS scope. The current release does not include an Arena comparison workflow or a ready-made application/template gallery. Existing model selection, training, inference, and the separate Video Review workflow remain available. See [current project status](docs/PROJECT_STATUS.md) for the release scope and validation state.

Face templates are sensitive biometric data. Use only with informed consent and appropriate authorization. Validate accuracy for the intended setting, restrict administrator access, define retention/deletion practices, and meet applicable privacy and biometric requirements before operational use. This release uses one administrator account and one instance of each service; do not horizontally scale without first moving runtime state and training coordination into shared infrastructure.

## Run locally

Use the native Windows launcher on Windows. On Windows, macOS or Linux, you can also create the virtual environment, install the base requirements, and run `python start_vaos.py` from the project folder. This cross-platform launcher chooses a random available dashboard port matching `8xx0` (`xx` from 11 to 99, giving 8110–8990) on each launch; use the dashboard URL it prints or opens. Pass `--admin-port` to choose a fixed port. It starts the admin and camera services together, waits for both health checks, opens the dashboard, writes separate logs, and stops only the services it started when you press Ctrl+C (or, on Unix-like systems, send it SIGTERM). It binds to loopback and does not create a public tunnel. See the [installation guide](docs/INSTALLATION.md) for setup and platform-specific accelerator instructions.

### Windows launcher

1. Use Windows 10/11 and Python 3.12.
2. Start `start_windows.bat` and leave its window open while VAOS is in use.
3. Open `http://127.0.0.1:8000`. Sign in as `admin`; local setup creates a five-digit password, prints it in the launcher and stores it in `data/admin_password.txt`.
4. The launcher starts the separate camera service on port 8001 and creates a temporary HTTPS Quick Tunnel for phone camera access. The temporary URL changes when the tunnel is recreated. Keep camera-link tokens private.
5. Create a camera link or QR code, enroll consenting participants, and use the **ML training** page for sample review and model training.

The Windows launcher installs local dependencies, optional object-detection support and `cloudflared` when needed. First use may download models. Press Enter in the launcher window to stop the services and tunnel.

## Cross-platform support status

The source includes CPU fallback, NVIDIA CUDA selection and conditional Apple MPS selection. Contract checks are configured for Windows, macOS and Ubuntu; the optional MCP stdio protocol check also runs against the official SDK client on that matrix. A passing CI result and native Mac/Linux device evidence are still required before those combinations are certified. The Windows launcher and temporary tunnel controls remain Windows-specific. VAOS cannot promise operation on arbitrary RAM or every GPU vendor; see the [platform matrix](docs/PLATFORM_SUPPORT.md) for setup steps and boundaries.

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

VAOS is distributed under the [GNU Affero General Public License v3.0 only](LICENSE) (`AGPL-3.0-only`). This includes the bundled custom object-detection checkpoint at `data/ml_training/active/best.pt`. Review third-party model, package and dataset terms separately; this license does not change those terms. The bundled checkpoint was trained using Ultralytics YOLO and COCO 2017 plus human-reviewed samples; see [model notices](data/ml_training/active/MODEL_NOTICES.md) and its accompanying [training metadata](data/ml_training/active/metadata.json).

## Creator and community

VAOS was originally created by Prateek Dubey and is developed with contributions from the open-source community.

The [creator and community credits](docs/CREDITS.md) identify Prateek Dubey as Founder and Original Creator and provide the website and LinkedIn links. Contributors are acknowledged for their code, reviews, testing, documentation and other improvements. See [CONTRIBUTING.md](CONTRIBUTING.md) to take part. Attribution does not replace existing copyright notices or claim ownership of community or third-party work; the AGPL-3.0-only license and applicable third-party terms remain in force.

Structured creator and project metadata is recorded in [VAOS_PROJECT_METADATA.json](VAOS_PROJECT_METADATA.json) and [CITATION.cff](CITATION.cff). Product documentation begins at [docs/README.md](docs/README.md).

## Data and configuration

Windows stores files in the project `data` directory by default. Containers set `VAOS_DATA_DIR=/var/lib/vaos` on a persistent volume. It holds the SQLite database, profile photos and face templates, model weights, datasets, reviewed samples, training runs and logs. Treat backups as sensitive biometric data.

| Variable | Purpose |
|---|---|
| `VAOS_ENV` | Set to `production` for production safeguards. |
| `VAOS_ADMIN_PASSWORD` | Administrator password; required at 32+ characters in production. Staging always uses `1122334455`, overriding this variable and saved passwords. |
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

The included custom object-detection checkpoint was trained using Ultralytics YOLO and is distributed with VAOS under AGPL-3.0-only. The official [Ultralytics license guidance](https://www.ultralytics.com/license) describes licensing obligations for applications using its code or trained models. Review third-party dataset terms separately; this repository does not include the training images or annotations.

For staging, launch with `VAOS_ENV=staging ./.venv/bin/python start_vaos.py --data-dir "$PWD/data"`. The staging administrator login is always `admin` / `1122334455`. Other environments retain their existing password behavior.

### Software update checks

VAOS checks published stable GitHub releases and shows a dismissible update notice. About includes release notes, issue reporting and a manual check. Installation remains manual in this first stage. See [software update behavior](docs/SOFTWARE_UPDATES.md) for offline handling, opt-out and safe upgrade guidance.

Current documentation release: **1.3.3**. See [knowledgebase](docs/KNOWLEDGEBASE.md) and [release notes](docs/RELEASE_NOTES_1.3.3.md).

API/MCP feature coverage and upgrade limits: docs/API.md, docs/MCP.md and docs/KNOWLEDGEBASE.md (see current feature contracts).

Daksh-AI **1.3.3**: see [Daksh capabilities and limitations](docs/DAKSH_AI.md). The stable release includes supervision, calibration/retraining jobs and schedules, adviser promotion/rollback and verified checkpoint resume; see the guide for hardware validation limits.

VAOS **1.3.3** adds persistent Daksh engine Start/Stop in Settings, historical checkpoint-metric learning, a dedicated Detection workspace and full-width ML TRAINING workspace. See [release notes](docs/RELEASE_NOTES_1.3.3.md) for behavior, rationale and validation limits.
