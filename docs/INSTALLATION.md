# VAOS installation, upgrade and removal guide

This guide covers the three supported setup paths in this repository. Read the [complete user manual](COMPLETE_USER_MANUAL.md) for application concepts and operation, the [configuration reference](CONFIGURATION_REFERENCE.md) for every supported environment variable, and [platform support](PLATFORM_SUPPORT.md) for evidence and device limits.

## Before installing

### Supported paths

| Path | Best for | Current boundary |
|---|---|---|
| Windows 10/11 launcher | Local workstation setup | Python 3.12; native service and camera-only tunnel controls are Windows-specific. |
| macOS/Linux source | Local development/testing | Python 3.12 and two terminal processes; no built-in public tunnel or app-managed service restart controls. Real camera/GPU paths need host testing. |
| Linux production Compose | Self-hosted server | Docker Engine + Compose plugin, separate admin/camera DNS names, public HTTPS, persistent volume, secrets and an access policy for admin. Candidate release readiness is still NO-GO until target staging and rollback/hardware/user-journey gates pass. |

### Hardware and network checklist

- Use a supported 64-bit Python 3.12 runtime for the source build. CPU-only operation is the fallback. See the platform matrix before relying on a GPU or a non-x86 architecture.
- There is no certified universal minimum CPU, RAM, VRAM, disk size or camera model. Keep headroom for the operating system, model weights, datasets, decoded images, logs and training checkpoints. Large model/dataset workflows can require far more resources than the dashboard.
- Optional GPU use requires matching drivers and PyTorch/torchvision binaries. NVIDIA CUDA and Apple MPS are conditional runtime paths, not guarantees. Other GPU vendors are not release-certified.
- A local app needs ports 8000 (admin) and 8001 (camera) available. The production proxy needs DNS for both configured hostnames and inbound TCP ports 80/443 for Caddy certificates. Never publicly expose the admin service without a strong upstream access-control policy.
- First setup needs internet access to install Python packages and, depending on the chosen workflow, download model files or the Windows tunnel binary. Review their license/terms and integrity before using them.

## Windows local installation

1. Install 64-bit Python 3.12 from [python.org](https://www.python.org/downloads/). Enable the Python launcher (`py`) and PATH option.
2. Download/clone the VAOS repository or extract its release archive to a folder writable by your Windows account. Do not place the only copy of sensitive data in a synchronized/public folder.
3. Double-click `start_windows.bat`. Keep its terminal open. On first use it creates `.venv`, installs the base requirements, then attempts optional PyTorch/YOLO support. If GPU setup fails, the dashboard/camera service may still run with CPU; object detection may remain unavailable.
4. The launcher starts the admin service at `http://127.0.0.1:8000` and camera service at port 8001, creates run logs under `data/logs`, and tries to create a temporary HTTPS tunnel for the camera site. It does not tunnel the admin site. Tunnel access can be blocked by network policy and the temporary address can change after a restart.
5. Open `http://127.0.0.1:8000` on the same PC. Sign in as `admin` with the generated long random password printed in the launcher. It is also stored in `data/admin_password.txt`; protect that file and never commit/share it.
6. Confirm the admin and camera service health and hardware/model status before collecting images or starting a training run.
7. To stop, return to the launcher console and press Enter. Let it stop the processes/tunnel. Do not kill Python by process name when other Python applications may be active.

The Windows launcher installs a pinned PyTorch/torchvision version pair when required. For a missing system `cloudflared`, it downloads version `2026.11.0` from the official Cloudflare release and compares SHA-256 with the published release hash before executing it. A system-installed binary found on PATH is treated as an administrator-managed dependency. The official checksum is documented on the [cloudflared releases page](https://github.com/cloudflare/cloudflared/releases/tag/2026.11.0).

### Windows environment overrides

Set overrides in the launching PowerShell process before running `start_windows.ps1`; do not put secrets in a committed script. Example to move data to another local folder:

```powershell
$env:VAOS_DATA_DIR = 'D:\VAOS_Data'
$env:VAOS_DATASETS_DIR = 'D:\VAOS_Datasets'
.
start_windows.ps1
```

Check the full [configuration reference](CONFIGURATION_REFERENCE.md) for model, device, proxy, API, MCP and observability variables. Existing paths are not moved automatically when the root changes.

## macOS or Linux local source installation

These commands are for a local development setup. They bind both services to loopback; do not change the host to `0.0.0.0` for an internet-facing server.

### 1. Install the base service

Install Python 3.12 and its `venv`/pip support. In a terminal at the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
export VAOS_DATA_DIR="$PWD/data"
```

For Linux packages, ensure OpenCV's runtime shared-library requirements are met. The production Dockerfile installs `libgl1`, `libglib2.0-0`, and `libgomp1`; a native distro may call these packages by different names. The application intentionally uses `opencv-contrib-python-headless`; do not also install a competing OpenCV wheel.

### 2. Start admin and camera services

In the first terminal, with `.venv` active:

```sh
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open a second terminal at the same repository root, activate the same `.venv`, and use the same `VAOS_DATA_DIR`:

```sh
source .venv/bin/activate
export VAOS_DATA_DIR="$PWD/data"
python -m uvicorn camera_service:app --host 127.0.0.1 --port 8001
```

On a fresh development data root, VAOS generates a long random admin password at `data/admin_password.txt`. Keep it private. Stop each service with Ctrl+C in its own terminal. macOS/Linux local source startup does not supply Windows tunnel/restart buttons; use a deliberately configured secure reverse proxy when remote camera access is required.

### 3. Optional object detection and training

The base requirements do not install PyTorch or YOLO model weights. Use the official [PyTorch install selector](https://pytorch.org/get-started/locally/) to select a PyTorch/torchvision pair for the exact OS, Python, accelerator and driver. Verify `torch.cuda.is_available()` for CUDA or `torch.backends.mps.is_available()` for Apple MPS. Then:

```sh
python -m pip install -r requirements-object-detection.txt
python -m pip install --no-deps "ultralytics==8.4.183"
```

The `--no-deps` install is intentional: Ultralytics must not replace VAOS's contrib-headless OpenCV package with another `cv2` distribution. Inspect the model/device status page after install. A successful package install does not prove that a particular model operation or dataset will fit memory.

## Linux production container installation

### Prerequisites

- A maintained Linux host with Docker Engine and the Docker Compose plugin, adequate persistent storage, and an operator who can maintain updates/backups.
- DNS `A`/`AAAA` records for the admin and camera hostnames that reach the server. Ports 80/443 must be reachable by Caddy for HTTPS. Confirm firewall, proxy and DNS behavior before users receive links.
- A VPN, identity-aware gateway or equivalent policy in front of the admin hostname. VAOS has a single local admin account; this is not a replacement for network access control.
- For GPU containers: NVIDIA driver, NVIDIA Container Toolkit, a compatible CUDA-enabled PyTorch wheel index and GPU host. The CPU image does not require the GPU override.

### Prepare secrets and hostnames

```sh
cp production.env.example production.env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set `VAOS_ADMIN_DOMAIN` and `VAOS_CAMERA_DOMAIN`. Generate separate random values for `VAOS_ADMIN_PASSWORD` and `VAOS_INTERNAL_STATUS_TOKEN`; do not reuse the same value. Protect `production.env`, restrict its file permissions, and do not commit it. `VAOS_TORCH_INDEX_URL` is optional for CPU mode and required when using the GPU override.

### Start, inspect and stop

CPU production baseline:

```sh
docker compose --env-file production.env -f compose.production.yaml config
docker compose --env-file production.env -f compose.production.yaml up -d --build
docker compose --env-file production.env -f compose.production.yaml ps
docker compose --env-file production.env -f compose.production.yaml logs --tail=200
```

Review the rendered `config` output carefully; do not share it because it may include expanded secrets. The admin site is at `https://<VAOS_ADMIN_DOMAIN>` and camera site at `https://<VAOS_CAMERA_DOMAIN>`. Validate health, login, TLS, proxy restrictions and camera links. To stop without deleting stored data:

```sh
docker compose --env-file production.env -f compose.production.yaml down
```

For an NVIDIA host, select a CUDA wheel index from the official PyTorch selector, set `VAOS_TORCH_INDEX_URL`, and include `-f compose.production.gpu.yaml` in the `config`, `up`, `ps`, `logs` and `down` commands. Confirm both service containers see the expected GPU. Do not advertise the GPU path as tested merely because it builds.

Persistent state is the named `vaos-data` volume. Back it up and test restore before an upgrade. `down --volumes` removes persistent VAOS and Caddy data and is intentionally destructive. See [administrator guide](ADMIN_GUIDE.md) and [current readiness](PROJECT_STATUS.md).

## Validate an installation

For a source checkout:

```sh
python -m py_compile main.py camera_service.py mcp_server.py hardware_intelligence.py runtime_compat.py tracking_rules.py project_bundles.py project_metadata.py
python -m unittest discover -s tests -v
```

Confirm the web health endpoints on a local source run:

```sh
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8001/health').read().decode())"
```

The CI workflow additionally tests Python 3.12 contracts on Windows/macOS/Ubuntu, real MCP SDK stdio boundaries, Chromium routes/responsive smoke and a CPU production-container smoke. None of these alone replace physical camera, GPU, operator acceptance, backup/restore or staging validation. See [testing](TESTING.md) and [VAPT/stability assessment](VAPT_STABILITY_1.1.0.md).

## Upgrade, backup and rollback

1. Record the current Git commit and VAOS version, data paths, model file and configuration.
2. Stop the Windows launcher or both source processes. For containers, keep a tested copy of the named volume and `production.env`.
3. Back up the database and the model/dataset/profile/media assets that must be preserved. Store the backup away from the host and restrict access.
4. Upgrade a copy first. Review the release notes and any migration instructions, then run tests and health checks before pointing users at it.
5. Keep the previous application version and backup until the new version is accepted. For rollback, stop the new release and restore the database together with its matching model/configuration snapshot; do not mix incompatible files from separate backups.

No zero-downtime upgrade or multi-instance write coordination is promised. Test on the target staging host and rehearse restore/cutover before a production update.

## Uninstall

Decide whether data must be retained before removing code. The data root may contain sensitive faces, images, records, API settings, trained weights and consent information. See the [complete manual's uninstall chapter](COMPLETE_USER_MANUAL.md#10-uninstall-and-data-retention) for separate Windows, macOS/Linux and Docker procedures.

- **Remove code, keep data:** stop services; preserve the verified data/model/dataset roots and secret backup; remove the source checkout and its `.venv` only after checking their absolute paths. Keep system packages that other applications may use.
- **Remove code and data:** first verify backups, retention/legal duties and every configured path. Then stop services and remove only the intended application data, external dataset/model roots and (if applicable) Docker volumes. Do not use a wildcard or unverified environment variable as a recursive-delete target.
- **Docker:** `down` keeps volumes. Removing stored data requires explicitly removing the correct Compose volumes; inspect project/volume names and backups first. The Caddy data volume includes TLS/proxy state.

Rotate credentials and revoke remote API/MCP tokens as needed. Removing local files does not revoke a token already copied to another system.
