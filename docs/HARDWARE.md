# Hardware Intelligence

The Hardware page (`/hardware`) summarizes locally measured CPU, RAM, storage, NVIDIA inventory and telemetry, plus Apple MPS availability and active inference when reported by the runtime. Open it from the left navigation on the Control Room, ML Training, Dataset Preparation, or Video Review pages. It refreshes every ten seconds and offers a downloadable diagnostic JSON report.

## What the page means

- **Processor:** logical CPU thread count, not a claim about physical cores or current worker assignment.
- **System memory:** sampled host RAM percentage and GB totals when `psutil` can read them.
- **Data storage:** free and total capacity for the volume containing VAOS's data directory. The report does not include the private full folder path.
- **NVIDIA inventory:** detected NVIDIA devices and the measured utilization, VRAM, temperature, and board-power readings returned by `nvidia-smi`. Board power may be unavailable on some drivers/devices; this does not disable utilization reporting. Additional inventory-only GPUs are explicitly labelled when live measurements are missing.
- **Apple MPS:** reports whether the installed PyTorch runtime says MPS is available and whether the camera detector currently reports MPS. This page does not measure Apple GPU utilization or separate GPU memory from unified system memory.
- **Inference accelerator:** current camera-worker detector status: CPU, CUDA, MPS, or not verified. A detected accelerator does not by itself prove that the detector is using it.
- **Training:** reports this admin process's current training state and selected device when available. It is distinct from the camera inference device.
- **Suggested settings:** conservative heuristics only. The page does not run a benchmark, start jobs, install drivers, alter power settings, or change training settings.

## During a training run

When VAOS reports training or data preparation as active, the page recommends against starting another benchmark or competing training job. High measured RAM use also raises a warning. Those messages do not stop or modify the current run. Estimates may become stale as other applications use resources.

The REST endpoint is `GET /api/v1/admin/hardware`, protected by the existing diagnostics capability. MCP requests use the same API policy. The response contains only a drive/root label, free/total capacity, OS release, resource measurements, and safe recommendations; it does not disclose the full data path, serial numbers, tokens, or camera links.

## Troubleshooting

- If NVIDIA values say **Not measured**, check that the NVIDIA driver and `nvidia-smi` are available. A CUDA-capable detector may still run when telemetry is unavailable, but VAOS will not guess.
- If board power is unavailable while utilization is present, the device or driver may not support the `power.draw` query. VAOS falls back to its original GPU telemetry and leaves energy unavailable; it never estimates watts from utilization.
- If **CUDA inference** is `Not verified`, make sure the camera service has loaded its detector. This is not a PyTorch CUDA compatibility test and does not prove training is on the GPU.
- If RAM use is high, avoid starting another data preparation, benchmark, or training task until the machine has headroom.
- CPU-only operation is supported. GPU-specific values can remain unknown without affecting camera or API behavior.
- Apple MPS depends on a supported Apple Silicon device, macOS version, and PyTorch build. The current MPS logic has unit-level selection coverage but has not been tested on an Apple GPU. See [Platform support](PLATFORM_SUPPORT.md) for the full compatibility boundary.

## Verified scope and limitations

Pure report logic is covered with injected CPU/GPU fixtures, including multiple GPU rows, unknown runtime state, high-memory warnings, training-active guidance, and path redaction. The hardware module does not probe devices in unit tests. This environment has not completed a clean-install browser usability run, a cross-platform hardware matrix, or benchmark. Guidance is not a compatibility certification, performance prediction, or promise of an OOM-free run.
