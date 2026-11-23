# Operating-system and hardware support

## Support promise

VAOS has source-level support paths for Windows, macOS and Linux, with CPU as the fallback and optional NVIDIA CUDA or Apple MPS when the installed PyTorch build reports the device as available. CI contract checks are configured for Windows, macOS and Ubuntu. This does **not** certify every OS release, GPU vendor, driver, Python build, camera, workload, or memory size. Staging remains blocked until clean-install and device-specific checks are recorded on representative hosts.

## Current implementation status

| Environment | Intended operation | Current evidence and limits |
| --- | --- | --- |
| Windows + CPU | Admin/camera services, CPU inference and CPU training | Native Windows launcher is maintained. CPU and dataset-path selection have automated tests; the repository suite passed on this host on 2026-11-15. This does not certify camera hardware or a full ML run. |
| Windows + NVIDIA CUDA | CUDA inference and training | NVIDIA detection uses `nvidia-smi`; the current Windows launcher installs the Windows CUDA runtime. A two-GPU Windows training run is not release-certified. |
| macOS + Apple Silicon | CPU or PyTorch MPS inference/training | MPS selection and fallback logic are simulated in unit tests; native startup commands are below. No Mac or Apple GPU is available in this verification environment. CI is configured, but a passing CI result and real-device check are still required. |
| Linux + CPU | Services and CPU inference/training | Native manual startup commands and Docker deployment are documented. Ubuntu CI is configured; this environment has no working Linux distribution, so there is no local Linux result. |
| Linux + NVIDIA CUDA | CUDA inference/training in the GPU Docker deployment | Compose has an NVIDIA runtime override and requires an explicitly selected compatible PyTorch wheel. It still requires clean-image build and GPU-host verification. |
| AMD, Intel, and other accelerators | Not a supported release target yet | No complete device discovery, telemetry, install matrix, or hardware validation exists for these vendors. CPU fallback may work if their PyTorch build can import, but that is not accelerator support. |

## Native macOS and Linux quick start

The Windows PowerShell launcher is not used on macOS/Linux. For a local single-machine development run, use Python 3.12 and the repository's cross-platform `start_vaos.py` from the project root. It starts the admin service on loopback port 8000 and camera service on loopback port 8001, verifies both health endpoints, and stops only its own child processes on Ctrl+C (or SIGTERM on Unix-like systems). It does not create a public tunnel. Settings still lets you save the selected detector, but local model changes require restarting the launcher. The Windows-only Quick Tunnel and restart buttons are hidden on macOS/Linux so they do not imply unsupported control.

Create and activate a virtual environment and install web-service dependencies:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Start both services:

```sh
python start_vaos.py
```

Open `http://127.0.0.1:8000`. On a fresh development data directory, VAOS creates an admin password in `data/admin_password.txt`; protect that file. Stop both services with Ctrl+C in the launcher terminal or send SIGTERM to the launcher on macOS/Linux. The launcher refuses non-loopback binds and occupied ports; it will not stop another process. Do not run the development services on an internet-facing interface. For production, follow the protected HTTPS reverse-proxy/container procedure in the [main README](../README.md).

### Optional object detection and training dependencies

The base requirements intentionally do not install PyTorch or model weights. Install the PyTorch and torchvision build for the machine using the official [PyTorch installation selector](https://pytorch.org/get-started/locally/), then install the detector dependencies and pinned Ultralytics package:

```sh
python -m pip install -r requirements-object-detection.txt
python -m pip install --no-deps "ultralytics==8.4.183"
```

VAOS installs Ultralytics without its default OpenCV wheel so it retains `opencv-contrib-python-headless` for `cv2.face`; the object-detection requirements explicitly include the remaining Ultralytics runtime dependencies, including NVIDIA telemetry and the optional platform SDK on Python 3.11+. On Apple Silicon, use a macOS/Python combination supported by that PyTorch build and check `torch.backends.mps.is_available()` before selecting MPS. On Linux, select CPU or a CUDA-compatible wheel matching the installed driver. A successful package install does not prove that a model, kernel or training workload will fit available memory.

The source launcher does not provide the Windows-only tunnel or in-app service-restart controls. A remote camera deployment needs a separately configured authenticated HTTPS reverse proxy and firewall policy; never expose the admin service directly.

## Memory and performance limits

No application can guarantee training on *any* amount of RAM. The minimum depends on the detector, image size, batch, dataset loader, OS and concurrently running programs. VAOS now derives CPU capacity from the process-visible CPU count where available and chooses a cautious batch/image-size/data-worker starting profile from currently available RAM. With unknown RAM it uses zero loader workers and a smaller profile. CUDA single-GPU enrichment may use Ultralytics automatic batch sizing. These are safeguards, not an out-of-memory guarantee or a speed promise; monitor RAM/VRAM and reduce the workload if the operating system is under pressure.

Apple unified memory is shared by the OS, GPU and other apps. VAOS reports whether MPS is available/active but does not currently measure Apple GPU utilization or split out GPU memory. Live utilization/temperature/power readings are currently NVIDIA-only. Unsupported telemetry is shown as unavailable, never estimated as measured.

## Cross-platform gaps before staging

- The source launcher now starts and supervises both local services on Windows, macOS and Linux. The temporary public tunnel and in-app restart controls remain Windows PowerShell features; on other platforms, stop and restart the source launcher or use the production process manager.
- The macOS and Linux clean-install, startup, camera, dataset, training and shutdown journeys need to pass in CI or on isolated target machines. Configured contract jobs are not yet recorded as passing results and cannot establish a real camera/GPU run.
- Verify model checkpoint load/save, MPS fallback behavior, supported PyTorch/Ultralytics versions, and actual training stability on an Apple Silicon Mac.
- Verify Linux CPU and NVIDIA Docker image builds, migrations, storage permissions, proxy configuration, and rollback on a representative host.
- Decide and document tested minimum RAM/CPU/disk profiles from repeatable tests. Do not advertise support for arbitrary hardware or memory sizes.
- Add hardware discovery and truthful telemetry for any additional GPU vendor before calling its accelerator supported.

## Verification policy

Platform support is recorded only for an OS/runtime/device combination actually exercised. Unit tests with mocked device inventory verify selection logic, not drivers, kernels, throughput, accuracy, or memory stability. See [the release status](PROJECT_STATUS.md) and [the validation log](observability/validation.md) for dated evidence and open gates.
