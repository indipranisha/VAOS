# Install and run VAOS

This guide covers local source setup and the included production deployment. The repository provides a loopback-only source launcher for Windows, macOS and Linux; platform and accelerator certification still depends on the evidence in [platform support](PLATFORM_SUPPORT.md).

## Local Windows setup

Use Windows 10/11 and Python 3.12. From the repository root, run `start_windows.bat` and keep its console open. Open `http://127.0.0.1:8000`; the launcher prints the initial administrator password and stores it in `data/admin_password.txt`. Protect that file. The launcher starts the camera site on port 8001 and may create a temporary HTTPS camera tunnel. The Windows launcher controls those services and tunnel; do not assume its restart controls are portable.

Stop the local launcher through its documented console prompt so it can stop the child services cleanly. Do not terminate Python processes by name when unrelated Python work may be active.

## Local macOS/Linux source setup

Python 3.12 and a POSIX shell are used in the example. Run from the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python start_vaos.py
```

The launcher starts both services on loopback, opens the dashboard, writes logs under `data/logs/local-launcher-*`, and stops only the child services it started when you press Ctrl+C (or, on Unix-like systems, send it SIGTERM). Use `python start_vaos.py --help` to select a separate data folder or suppress automatic browser opening. It does not create a public tunnel. On a fresh data directory the generated password is written to `data/admin_password.txt`; protect that file. Never expose the development services directly to the internet. The Windows-specific `start_windows.bat` remains available when its Quick Tunnel and launcher controls are needed.

**Apple Silicon:** the base application runs on CPU. For object detection/training, install the PyTorch and torchvision build recommended for your Mac/Python combination, then install the detector requirements. VAOS offers MPS only when the installed runtime reports it available and falls back to CPU otherwise. No Mac device was available for native verification in this checkout; see [platform support](PLATFORM_SUPPORT.md) before relying on GPU behavior.

## Optional object detection and training

The base web requirements intentionally do not install PyTorch or download model weights. Install a PyTorch/torchvision build appropriate for the OS, Python and accelerator using the official [PyTorch selector](https://pytorch.org/get-started/locally/). Then install the repository's object-detection requirements. Check available devices in VAOS before starting a job. CPU fallback is available; CUDA and Apple MPS selection depend on the installed runtime reporting the device as available. Model downloads, training time, disk use, and memory needs depend on the model and dataset.

## Production container setup

The included Docker Compose setup targets Linux with Docker Engine/Compose and Caddy. Follow the production section in the [main README](../README.md): set distinct administrator/camera hostnames and strong secrets in an untracked `production.env`; protect the admin hostname behind a VPN or equivalent access policy; and persist the `vaos-data` volume. The base image uses CPU PyTorch. GPU deployment needs NVIDIA Container Toolkit and an explicitly selected compatible CUDA wheel. Keep a tested backup before upgrades. `docker compose down` keeps the volume; avoid `down --volumes` unless deleting all stored VAOS data is intended.

## Verify a source install

```sh
python -m py_compile main.py camera_service.py mcp_server.py
python -m unittest discover -s tests -v
```

These checks are not a substitute for a browser, camera, GPU or production deployment test. Use [testing and validation](TESTING.md) and [project status](PROJECT_STATUS.md) for current evidence and release gates.
