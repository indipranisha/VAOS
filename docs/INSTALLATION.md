# Install and run VAOS

This guide covers the supported local source setup and the included production deployment. See [platform support](PLATFORM_SUPPORT.md) before choosing a non-Windows host: native macOS/Linux commands are documented, but they have not yet been validated on physical devices in this checkout.

## Local Windows setup

Use Windows 10/11 and Python 3.12. From the repository root, run `start_windows.bat` and keep its console open. Open `http://127.0.0.1:8000`; the launcher prints the initial administrator password and stores it in `data/admin_password.txt`. Protect that file. The launcher starts the camera site on port 8001 and may create a temporary HTTPS camera tunnel. The Windows launcher controls those services and tunnel; do not assume its restart controls are portable.

Stop the local launcher through its documented console prompt so it can stop the child services cleanly. Do not terminate Python processes by name when unrelated Python work may be active.

## Local macOS/Linux source setup

Python 3.12 and a POSIX shell are used in the example. Run from the repository root in two terminal sessions:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
export VAOS_DATA_DIR="$PWD/data"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

In a second terminal, activate the same environment, export the same data path, and run:

```sh
python -m uvicorn camera_service:app --host 127.0.0.1 --port 8001
```

Open `http://127.0.0.1:8000`. On a fresh data directory the generated password is written to `data/admin_password.txt`. Stop each service with Ctrl+C in its own terminal. These are development commands and bind to loopback; do not expose them directly to the internet. See the [platform matrix](PLATFORM_SUPPORT.md) for accelerator setup and limits.

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
