# Develop VAOS on an Apple Silicon Mac

This guide is for moving the current VAOS development checkout to an Apple Silicon Mac such as a MacBook Pro M3 Max with 36 GB unified memory. It describes the local source-development path; it does not certify every macOS release, camera, model, or training workload.

## What to expect on an M3 Max

- VAOS can run its local admin and camera services with Python on macOS.
- The app can use CPU inference/training. For Apple GPU acceleration, install a PyTorch build that supports Apple MPS and verify that MPS is available before selecting it in VAOS.
- Apple MPS is not NVIDIA CUDA. Windows-only launcher tunnels and restart controls are not provided by the macOS source launcher.
- The M3 Max's 36 GB is unified memory shared by macOS, CPU, GPU, and applications. It is not 36 GB of dedicated GPU memory. Start with a modest batch/image size, monitor memory pressure, and lower the workload if macOS begins swapping.
- NVIDIA-only utilization, temperature, power, and VRAM telemetry is not available on Apple GPUs. VAOS must show Apple GPU information as unavailable when it has no measured value.

## Move the current work to the Mac

The repository's `staging` branch at base commit `411ee50` already contains the tracked custom model at `data/ml_training/active/best.pt`. The newer Windows work is not on GitHub yet. It is provided separately in `VAOS-mac-handoff-2026-11-23.zip`; it contains only the intended source, tests, UI and documentation overlay, not the virtual environment, datasets, local database, passwords, or unrelated `*- Copy` files.

1. Install Apple's Xcode Command Line Tools and Git. Install Python 3.12 using your preferred trusted package manager or Python.org installer.
2. Clone VAOS and create a work branch at the handoff's stated base:

   ```sh
   git clone --branch staging https://github.com/indipranisha/VAOS.git
   cd VAOS
   git switch -c work/mac-m3max 411ee50
   ```

3. Copy the handoff ZIP to the Mac (for example with OneDrive or AirDrop), then unpack it into the repository root so it overlays the files:

   ```sh
   unzip -o ~/Downloads/VAOS-mac-handoff-2026-11-18.zip -d .
   ```

4. Create a Mac-local environment and run the regression suite:

   ```sh
   python3.12 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   python -m pip install -r requirements-dev.txt
   python -m unittest discover -s tests -q
   ```

5. For detector inference/training, install the macOS/Apple Silicon-compatible PyTorch and torchvision build using the official [PyTorch installation selector](https://pytorch.org/get-started/locally/). Then install VAOS's additional detector dependencies as described in [Platform support](PLATFORM_SUPPORT.md). Check the backend before choosing MPS:

   ```sh
   python -c "import torch; print('MPS available:', torch.backends.mps.is_available())"
   ```

   If MPS is unavailable, choose CPU. Some operations or models may fall back to CPU or may not fit in the available unified memory.

6. Keep Mac-only runtime data outside the Git checkout. For example:

   ```sh
   export VAOS_DATASETS_DIR="$HOME/VAOS_Datasets"
   python start_vaos.py --data-dir "$HOME/VAOS_Data"
   ```

   The dashboard opens at `http://127.0.0.1:8000`; camera service health is at `http://127.0.0.1:8001/health`. Stop the launcher with Ctrl+C. The source launcher binds only to loopback and does not create a public tunnel.

## Datasets and reviewed samples

The Windows `D:\VAOS_Datasets` library is intentionally **not part of this handoff**. Download or prepare only the datasets you need on the Mac, under a Mac-local location such as `~/VAOS_Datasets`, preserving each source's license and attribution files. Set `VAOS_DATASETS_DIR` before launching VAOS and register/validate the new paths in Dataset preparation.

Your own reviewed samples and training history are different from replaceable public datasets and may not be downloadable again. If you want to keep them, stop VAOS cleanly on Windows, make a private backup of the local VAOS `data` directory, and retain that backup separately. Do not restore it over the fresh Mac data directory without review: its database may contain Windows absolute paths and sensitive local credentials/tunnel state. Re-register datasets using Mac paths, create a new Mac admin password, and rotate/recreate API/MCP tokens and temporary tunnel URLs. Keep the backup until the reviewed images and history you care about have been verified on the Mac. If you do not need that history, start with a clean Mac data directory.

## Before pushing from the Mac

Run the tests on the Mac and review the exact diff. Do not add local data, API/MCP tokens, admin-password files, generated logs, or datasets to Git. Confirm model and dataset licensing separately. The GitHub Actions Windows/macOS/Ubuntu jobs should also pass; a local Mac run does not substitute for the Linux/Windows matrix. No staging deployment should be inferred from a green unit suite alone.
