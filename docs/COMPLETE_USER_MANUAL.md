# VAOS 1.1.0 complete user manual

This manual explains what VAOS does, how its parts fit together, how to install and operate it, and how to remove it safely. Read the [release status](PROJECT_STATUS.md) and [platform matrix](PLATFORM_SUPPORT.md) before using it outside a local workstation. A feature appearing in the interface is not proof that it has been certified for every operating system, camera, GPU or deployment.

## 1. What VAOS is

VAOS (Video Analytics Operating System) is a self-hosted application for receiving camera frames, running face and object analysis, reviewing samples, training a custom object detector, and inspecting recorded operational events. Processing and data stay on the VAOS host unless an administrator deliberately enables an external integration, public camera tunnel, or remote API client.

For v1.1.0, **AI Model Arena** and **Ready-to-Run Vision Applications** are deferred. VAOS retains its existing model selection, dataset preparation, training, inference, review, camera, face, API, MCP, tracking, hardware and observability capabilities. See the [feature catalogue](FEATURE_CATALOG.md) for the complete inventory and status.

### What it is not

- It is not a hosted/cloud service, multi-tenant account system, or high-availability cluster.
- It does not guarantee that an arbitrary model, dataset, image size or epoch count will fit a particular machine.
- Face similarity is not a probability or a verified identity. Object detections and tracks may be wrong or incomplete.
- It is not certified for safety-critical decisions, biometric compliance, every camera, every GPU vendor, or every supported OS release.
- Apache-2.0 licensing of VAOS does not grant rights to third-party weights, datasets, footage, faces, or model outputs.

## 2. How the system works

### Services and traffic

| Part | Local default | Responsibility |
|---|---:|---|
| Admin web service (`main:app`) | `127.0.0.1:8000` | Dashboard, settings, authenticated APIs, model and dataset workflows, reports, review and administration. |
| Camera web service (`camera_service:app`) | `127.0.0.1:8001` | Camera-facing pages, frame reception and consent-based enrollment submissions. Private admin API routes are not mounted here. |
| SQLite database | `<VAOS_DATA_DIR>/app.db` | Users/settings, camera records, enrollments, token records, events, training and observability metadata. |
| Persistent files | `<VAOS_DATA_DIR>` plus model/dataset paths | Images, face templates, model weights, prepared data, logs and reports. Exact contents depend on enabled workflows. |
| Optional reverse proxy | production Compose: Caddy on 80/443 | HTTPS for separate administrator and camera hostnames. Admin access still needs a VPN, identity gateway or equivalent policy. |
| Optional MCP bridge | local stdio subprocess | Lets a configured AI host call policy-checked tools through the VAOS API; it does not open a new network port. |

The Windows launcher manages the two local services and an optional temporary **camera-only** Cloudflare Quick Tunnel. The tunnel makes its public URL available to people who possess the link; use it only with informed authorization and treat camera-link tokens as secrets. It is not an admin tunnel. The Windows launcher verifies the hash of its managed cloudflared download against the pinned official release checksum. A cloudflared executable already installed on `PATH` is treated as an administrator-managed system dependency.

The Docker production arrangement runs an admin container, camera container and Caddy proxy on a private Compose network. It uses one persistent named volume for VAOS state. Keep one replica of each app service: training coordination and some runtime state are process-local.

### Where data lives

- Source setup defaults to a `data` directory beside the application. `VAOS_DATA_DIR` changes the root.
- The database is `app.db` in that root. `MODEL_DIR` selects the model-weight cache; the default is the data-root model directory.
- `VAOS_DATASETS_DIR` changes the dataset root; otherwise VAOS chooses a platform-native location.
- Docker defaults to `/var/lib/vaos` for application state, models and Ultralytics cache. The named `vaos-data` volume persists across normal container recreation.
- Data may include participant photographs, biometric face templates, camera images, reviewed samples, datasets, weights, logs, event history and integration configuration. Protect it as sensitive information.

The [database schema guide](DATABASE_SCHEMA.md), [administrator guide](ADMIN_GUIDE.md), and [model/training guide](MODEL_TRAINING.md) give additional detail. Exports and observability reports are not full backups.

## 3. Dependencies and hardware

### Software baseline

- Python **3.12** for the tested source installation and CI matrix. Do not infer that a different Python version is certified from the broad package metadata.
- Windows 10/11 for the maintained native launcher. macOS and Linux have manual source startup instructions; their CI contract jobs do not certify physical camera, GPU or training operation.
- Core Python packages are listed in `requirements.txt`: FastAPI, Uvicorn, OpenCV contrib headless, NumPy, Jinja2, multipart handling, QR generation, psutil and timezone data. Use the repository requirements instead of installing a competing GUI OpenCV wheel.
- PyTorch, torchvision, Ultralytics and their detector helpers are optional and installed separately. The Windows launcher uses pinned PyTorch/torchvision builds and the repository's pinned Ultralytics release. Container builds use a PyTorch wheel index selected through `VAOS_TORCH_INDEX_URL`.
- Production containers require Docker Engine plus the Docker Compose plugin; Caddy obtains/renews HTTPS certificates and requires usable DNS and inbound ports 80/443.
- The optional MCP subprocess requires the separately listed MCP SDK in `requirements-mcp.txt` and a compatible MCP host.

### Hardware and accelerator expectations

| Hardware | What VAOS can do | What must be checked on the actual machine |
|---|---|---|
| CPU-only | Web services, CPU inference/training when optional ML dependencies are installed | Processing speed and memory depend on model, input size, dataset, CPU generation and other workloads. CPU fallback is not a performance guarantee. |
| NVIDIA GPU + CUDA | Conditional CUDA inference/training when drivers, runtime and PyTorch build match | `nvidia-smi`, CUDA availability in the installed PyTorch, VRAM fit, OS/container passthrough and actual workload stability. Windows launcher support and Docker GPU support use different setup paths. |
| Apple Silicon + MPS | Conditional PyTorch MPS selection on a supported macOS/PyTorch combination | `torch.backends.mps.is_available()`, model/operator support, unified-memory pressure and real training/inference behavior. Current CI uses simulated contracts; it is not a physical Mac validation. |
| AMD/Intel/other accelerators | CPU fallback where supported | These accelerators are not release-certified. A PyTorch build that imports does not prove VAOS detects or uses the device. |
| RAM/storage | VAOS selects cautious starting profiles based on currently available resources | No universal minimum or maximum is certified. Keep extra free memory and disk for the OS, model, datasets, checkpoints and temporary files. Reduce batch/image size or use CPU when resources are tight. |
| Processor architecture | Windows, Linux, and macOS source paths are intended; CI contracts cover Windows/macOS/Ubuntu runners | This does not certify Windows ARM, every Linux distribution/architecture, every Mac generation, or every CPU instruction set. |

`auto` selects an available supported inference backend; `cpu` forces CPU inference. Training device choices are made in the training workflow and depend on the installed runtime. CPU threads are controlled by the training configuration/device choices. The [hardware guide](HARDWARE.md) and [platform support matrix](PLATFORM_SUPPORT.md) distinguish tested behavior from intended behavior. Check actual CPU, RAM, GPU, VRAM, temperature and free disk while running a large job.

## 4. Installation choices

Choose one of these methods:

1. **Windows source launcher:** simplest local route; uses `start_windows.bat`, Python 3.12 and a local virtual environment.
2. **macOS/Linux source services:** manual two-terminal development startup; no Windows restart/tunnel controls.
3. **Linux production containers:** Docker Compose, Caddy, protected admin origin, HTTPS camera origin, secrets and persistent volume. This is a deployable baseline, not a passed staging cutover.

Follow [INSTALLATION.md](INSTALLATION.md) for copyable commands, environment variables, upgrade, backup and uninstall procedures. Do not expose a development server directly to the public internet.

## 5. First login and orientation

1. Start the services and wait for the health checks.
2. Open the private admin site (`http://127.0.0.1:8000` for a local run, or the protected admin hostname in production).
3. Sign in as `admin`. On a fresh development install, VAOS generates a long random password and prints it in the Windows launcher; the same value is stored at `data/admin_password.txt` under the configured data directory. Protect that file. Production instead requires the administrator to set `VAOS_ADMIN_PASSWORD`.
4. Confirm the version, admin and camera health, hardware report, available model and free disk before starting a workflow.
5. Use the navigation pages described below. If a page remains on “checking” or “loading”, first verify both services and the logs; do not assume the status is current without checking the running source/version.

## 6. Main workflows

### Dashboard and camera operations

The dashboard summarizes the installation, cameras, participants, object activity and events. A camera link is a bearer capability: anyone who has the URL/QR may be able to submit frames within that link's scope. Create links only for authorized devices, share them privately, and revoke or rotate them when no longer needed. For remote phone use, use an HTTPS camera URL. A temporary Quick Tunnel URL can change when restarted; it only routes to the camera service.

See [Camera and streaming](CAMERA_STREAMING.md) for browser permission, network, device and recovery details. A camera is not active merely because a link exists; browser camera permission, secure context, network and camera-service health all matter.

### Face enrollment and recognition

Obtain explicit, informed consent before collecting a person's face image or enrolling them. Explain purpose, access, retention, deletion, error rates and how to withdraw consent. Avoid sensitive or legally restricted use. Enrollment links expire/revoke according to the app's lifecycle. Do not share enrollment URLs, downloaded photos, database backups or face-template files publicly.

A face match is a similarity comparison against stored templates, not a probability, definitive identity or legal finding. Test thresholds against a consented and representative dataset for the intended environment. Handle false matches and missed matches with human review. See the security and privacy guide for incident and deletion steps.

### Dataset preparation, review and training

1. Identify the dataset's source, license, label format, allowed use, class mapping, and storage size before importing it.
2. Use the dataset-preparation workflow to validate and convert source annotations. A “prepared” state means the checks reported by VAOS passed; it does not guarantee annotation quality or legal permission.
3. Review detections and reviewed samples. Correct labels and reject unsuitable images. Keep training and validation data separated to avoid leakage.
4. Choose a starting detector, dataset(s), epochs and available device in **ML Training**. Read the selected dataset/model names before submitting; the UI should preserve the selection for that run.
5. Monitor the session's overall progress separately from epoch progress. Record loss and validation metrics per epoch, speed, device and the chosen sources. Stop only through the supported control when needed.
6. Compare the resulting model with a known baseline on a held-out, representative validation set before replacing a production model.

Box/class/DFL losses are optimization quantities and are typically interpreted as “lower is better” only in context; implementation scale, class balance and validation results matter. Precision is the fraction of predicted positives that are correct; recall measures how many actual positives are found. mAP summarizes precision/recall across thresholds/classes; higher is generally better when measured on the same valid set. A single epoch or a low training loss cannot establish field accuracy. See [Model and training](MODEL_TRAINING.md) for the exact metric semantics, resource settings, failure recovery, purge distinctions and data lifecycle.

Training can consume substantial CPU, RAM, GPU memory and disk. More epochs, classes or samples do not automatically improve accuracy. Use a backup of the previous best weights and enough free storage for checkpoints and logs. Do not purge source/prepared data until the exact path and its reusability have been checked.

Uploads are rejected with HTTP `413` if a request exceeds its route ceiling. Current ceilings range from 3 MiB for project bundles to 2 GiB plus multipart overhead for model-source import; smaller per-file validation rules still apply. See [the configuration reference](CONFIGURATION_REFERENCE.md#http-request-size-safeguards) for the full route list and limits.

### Tracking and event rules

Object tracking joins detections across frames using the installed tracker and current camera conditions. It is approximate: occlusion, camera movement, similar objects and missed detections may split or merge tracks. Define regions and cooldowns carefully. Event rules create metadata events; they do not prove intent or identity. See [Tracking and events](TRACKING_AND_EVENTS.md).

### Statistics, logs and reports

Use **Statistics & observability** to inspect available counters, metric definitions, lifecycle records, logs and bounded exports. “Unavailable” is not zero; a measurement may not exist for the hardware or period. Reports contain recorded information, are not full backups, and expire according to the report guide. Data sent to an external SIEM is disabled by default and requires an explicit destination and enable action. Review the specific event fields before enabling it.

### REST API and CLI

API access is disabled by default. In **Settings → API and AI agent access**, enable only what you need, create a token with an expiry and narrow permission groups, store the one-time secret in a password manager, and revoke it when no longer needed. Use HTTPS for remote access. Read the [REST API reference](API.md) and the endpoint [Explorer](API_EXPLORER.md). The optional `vaos` CLI uses the same authentication and scopes; it does not bypass authorization.

### MCP agent connection

MCP is optional and uses a local stdio subprocess. Follow [MCP host setup](MCP_HOST_SETUP.md). Begin read-only with the diagnostics group. Write and sensitive-read capabilities require separate policy, and control-token operations may require explicit one-time approval in Settings. Inspect the exact pending action before approval. Never publish the MCP token, expose the MCP process through the tunnel, or give an agent broader permissions than its task needs.

### Project bundles

Project bundles share a small JSON configuration and references; they do not include models, images, secrets or executable code. Inspect the dry-run summary, then apply only settings you trust. A bundle's dataset/model references are informational; VAOS does not fetch or switch those assets by applying the bundle. See [Project bundle format](PROJECT_BUNDLES.md).

## 7. Settings and options

Settings are grouped by capability. API/MCP access, detector selection, data roots, camera controls and integrations have distinct consequences. Keep optional integrations disabled until their identity, destination and data scope are understood. Complete environment-variable documentation—including defaults and whether each value is a build-time or runtime setting—is in [Configuration reference](CONFIGURATION_REFERENCE.md).

Changing a model, device or environment setting may require a service restart. The Windows launcher owns local restart/tunnel controls. In macOS/Linux terminals, stop the matching Uvicorn process and restart it; in production use the Compose or host process manager. Avoid killing arbitrary Python processes by name.

## 8. Backups, updates and restore

1. Identify the actual data root and any external dataset/model roots first.
2. Stop both services for a simple consistent file backup, or use the documented SQLite backup procedure. Copy the database and all model, dataset and consent data that you intend to retain. Do not assume exporting a PDF/CSV is a backup.
3. Encrypt and restrict backup access. Keep a copy away from the source disk and test restore into a disposable location before relying on it.
4. Review release notes, the exact candidate commit and migrations. Test the upgrade on a copy. Do not run multiple admin/camera replicas against the same state without explicit concurrency support.
5. If an upgrade fails, preserve the failed copy/logs, stop the new process, restore the verified database and matching model/configuration set, then start the previous known-good release.

Production `docker compose down` preserves the named data volumes. `docker compose down --volumes` deletes them and is a destructive full-data removal; use only after a verified backup and explicit intent to erase the stored VAOS data.

## 9. Troubleshooting

| Symptom | Check |
|---|---|
| Admin page unavailable | Confirm the process listening on 8000 is this VAOS instance, inspect its current log, verify Python dependencies and the `/health` response. Do not kill an unrelated port owner. |
| Camera page unavailable | Check port 8001, the camera-service health endpoint, camera URL configuration, TLS origin and browser camera permissions. |
| Sign-in fails | Verify the configured admin password source. On local source install, inspect the protected password file. Production password comes from the environment. Do not paste credentials into logs/issues. |
| Object model unavailable | Verify optional PyTorch/torchvision/Ultralytics installation and device compatibility; inspect model status. CPU mode is the fallback. |
| Training stops or is slow | Review training log, selected dataset/model, labels and disk. Check RAM/VRAM and free space; reduce image size/batch or use CPU if needed. A progress estimate is not a guarantee. |
| Status remains loading | Confirm the page is served by the intended version and both services are healthy. Refresh after checking service logs; old processes can serve old frontend/backend code. |
| Tunnel URL fails | Restart the Windows launcher-managed camera tunnel and use its current URL. The quick tunnel is temporary and may be blocked by network policy. Do not expose admin port 8000. |
| MCP/API action is denied | Check API enablement, token expiration/revocation, permission group, transport security, configured MCP policy and pending approvals. A denial is intentional when policy does not permit the operation. |
| Disk fills | Inspect `VAOS_DATA_DIR`, dataset/model roots, training runs, logs, reports, backups and Docker volumes. Confirm each target before deleting anything. |

Use the [administrator guide](ADMIN_GUIDE.md), [security/privacy guide](SECURITY_PRIVACY.md), [testing guide](TESTING.md) and [project status](PROJECT_STATUS.md) for deeper investigation.

## 10. Uninstall and data retention

Decide first whether data must be kept. VAOS data can contain biometric records and user footage, so removal should follow the owner's retention and legal requirements.

### Windows local source installation

1. In the launcher's console press Enter to stop the services and temporary tunnel. Confirm ports 8000/8001 no longer serve this installation.
2. Back up the configured data directory and any `VAOS_DATASETS_DIR`/model directories you want to retain.
3. Uninstall Python only if no other application needs it. Remove the VAOS virtual environment with the application folder after confirming the repository and data paths.
4. To retain VAOS records, move or back up the data root to protected storage before removing the application source folder. To erase them, remove the exact configured data root and any separately configured dataset/model directories after verifying their full paths. The Windows launcher may also have installed `cloudflared.exe` under the application's `tools` directory; remove it with the application folder if it is not shared.
5. Review the Windows user account for any separate backups or exports; removing the source folder does not remove copies elsewhere.

### macOS/Linux source installation

1. Stop each service with Ctrl+C in its own terminal or stop the process through the manager that started it.
2. Back up or intentionally erase the configured data root and any separate model/dataset roots.
3. Remove the application checkout and its `.venv`; do not use `rm -rf` against a path assembled from an unchecked variable. Confirm the resolved absolute path first.
4. Remove separately installed PyTorch, cloudflared or other tools only if no other application uses them; use the operating system's package manager where applicable.

### Production Docker installation

1. From the Compose directory, stop/remove containers: `docker compose --env-file production.env -f compose.production.yaml down`. Include the GPU override if that is how the stack was started.
2. Back up the `vaos-data` volume and any external dataset/model directories you intend to keep. Retain `production.env` securely if needed for a future restore; it contains secrets.
3. Remove the source checkout and proxy configuration only after the stack is stopped.
4. To preserve stored data, keep the named volumes and secret backup. To delete all persisted VAOS and Caddy state permanently, explicitly remove the named volumes with `docker compose ... down --volumes` after verifying the project/volume names and backup status. This also removes TLS/proxy state.
5. Remove Docker images only if they are not used by another project; use `docker image ls` to verify the exact image IDs first.

After removal, rotate API, MCP, admin, integration and tunnel credentials that may have been retained or shared. Deleting local files does not revoke a remote token or delete an external backup.

## 11. Privacy, licensing and support

Use camera and face capabilities only with lawful authority, informed consent where required, transparent notice, suitable security, retention controls and human oversight. Restrict access to the data root and backups. Review dataset/model licenses separately. VAOS source code is Apache-2.0; preserve notices and third-party licenses when redistributing. See [SECURITY.md](../SECURITY.md), [security and privacy](SECURITY_PRIVACY.md), [credits](CREDITS.md) and [contributing](../CONTRIBUTING.md).
