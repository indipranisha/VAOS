"""Conservative, local-only hardware facts and guidance for VAOS.

This module deliberately does not import PyTorch, initialize CUDA, run a
benchmark, or change any machine settings. Callers supply the measurements
already collected by VAOS's shared diagnostics path.
"""

from __future__ import annotations

import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_compat import available_cpu_threads


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        return result if result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def build_hardware_report(
    *,
    system_metrics: dict[str, Any],
    device_inventory: dict[str, Any],
    inference_runtime: dict[str, Any] | None,
    training_state: dict[str, Any],
    data_dir: str | Path,
) -> dict[str, Any]:
    """Return measured host facts and clearly-labeled, non-binding guidance."""
    metrics = system_metrics if isinstance(system_metrics, dict) else {}
    inventory = device_inventory if isinstance(device_inventory, dict) else {}
    training = training_state if isinstance(training_state, dict) else {}
    runtime = inference_runtime if isinstance(inference_runtime, dict) else None

    cpu_threads = max(1, int(inventory.get("cpu_threads") or available_cpu_threads()))
    ram_percent = _number(metrics.get("memory_percent"))
    ram_total = _number(metrics.get("memory_total_gb"))
    ram_used = _number(metrics.get("memory_used_gb"))
    training_status = str(training.get("status") or "unknown").casefold()
    training_active = training_status in {"preparing", "running", "stopping"}

    measured_gpu_rows = metrics.get("gpu_devices")
    if isinstance(measured_gpu_rows, list):
        gpus = list(measured_gpu_rows)
    else:
        gpus = []
        # Older telemetry provides a single scalar GPU record. Keep it usable,
        # but do not invent an index when no inventory is available.
        if metrics.get("gpu_available"):
            listed = inventory.get("gpus") if isinstance(inventory.get("gpus"), list) else []
            index = listed[0].get("index") if listed and isinstance(listed[0], dict) else None
            gpus.append({
                "index": index,
                "name": metrics.get("gpu_name") or "NVIDIA GPU",
                "utilization_percent": metrics.get("gpu_utilization_percent"),
                "memory_used_mb": metrics.get("gpu_memory_used_mb"),
                "memory_total_mb": metrics.get("gpu_memory_total_mb"),
                "temperature_c": metrics.get("gpu_temperature_c"),
                "power_draw_w": metrics.get("gpu_power_draw_w"),
            })

    inventory_gpus = inventory.get("gpus") if isinstance(inventory.get("gpus"), list) else []
    mps_available = inventory.get("mps_available") is True
    known_gpu_indices = {item.get("index") for item in gpus if isinstance(item, dict)}
    for device in inventory_gpus:
        if not isinstance(device, dict) or device.get("index") in known_gpu_indices:
            continue
        # Inventory-only devices have no current measurements. The response
        # intentionally leaves utilization and memory as null.
        gpus.append({"index": device.get("index"), "name": device.get("name"), "utilization_percent": None, "memory_used_mb": None, "memory_total_mb": None, "temperature_c": None, "power_draw_w": None})

    safe_gpus = []
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        gpu_measurements = [
            _number(gpu.get("utilization_percent")),
            _number(gpu.get("memory_used_mb")),
            _number(gpu.get("memory_total_mb")),
            _number(gpu.get("temperature_c")),
            _number(gpu.get("power_draw_w")),
        ]
        safe_gpus.append({
            "index": gpu.get("index") if isinstance(gpu.get("index"), int) else None,
            "name": str(gpu.get("name") or "NVIDIA GPU")[:120],
            "telemetry_measured": any(value is not None for value in gpu_measurements),
            "utilization_percent": _number(gpu.get("utilization_percent")),
            "memory_used_mb": _number(gpu.get("memory_used_mb")),
            "memory_total_mb": _number(gpu.get("memory_total_mb")),
            "temperature_c": _number(gpu.get("temperature_c")),
            "power_draw_w": _number(gpu.get("power_draw_w")),
        })

    loaded = bool(runtime and runtime.get("object_model_loaded"))
    cuda_value = runtime.get("cuda_available") if runtime else None
    cuda_runtime = runtime.get("cuda_runtime_available") if runtime else None
    reported_backend = str(runtime.get("inference_backend") or "").casefold() if runtime else ""
    if not loaded or cuda_value is None:
        inference_device = "unknown"
        inference_note = "The camera worker has not reported a loaded detector and its active device."
    elif reported_backend == "mps":
        inference_device = "mps"
        inference_note = "The camera worker reports the object detector loaded with Apple Metal Performance Shaders."
    elif reported_backend == "cuda" or bool(cuda_value):
        inference_device = "cuda"
        inference_note = "The camera worker reports the object detector loaded with CUDA."
    else:
        inference_device = "cpu"
        inference_note = "The camera worker reports a loaded detector on CPU."

    try:
        volume = Path(data_dir).expanduser().resolve().anchor or "unknown"
        disk = shutil.disk_usage(Path(data_dir))
        storage = {
            "volume": volume,
            "free_gb": round(disk.free / (1024**3), 2),
            "total_gb": round(disk.total / (1024**3), 2),
        }
    except (OSError, RuntimeError, ValueError):
        storage = {"volume": "unknown", "free_gb": None, "total_gb": None}

    per_gpu_available_mb = [
        max(0.0, float(item["memory_total_mb"]) - float(item["memory_used_mb"]))
        if item.get("memory_total_mb") is not None and item.get("memory_used_mb") is not None
        else float(item["memory_total_mb"])
        for item in safe_gpus
        if item.get("memory_total_mb") is not None
    ]
    conservative_gpu_available_gb = min(per_gpu_available_mb) / 1024 if per_gpu_available_mb else 0.0
    if training_active:
        workload_guidance = "Training or data preparation is active. Do not start a competing benchmark or second training job."
    else:
        workload_guidance = "No VAOS training job is reported active. A benchmark is still not run by this report."

    if ram_percent is not None and ram_percent >= 90:
        memory_guidance = "System memory use is high; defer additional preparation or training until it falls."
    elif ram_percent is None:
        memory_guidance = "Memory pressure is unknown because a RAM measurement is unavailable."
    else:
        memory_guidance = "Memory telemetry is available; actual free headroom changes while other apps run."

    measured_gpus = [gpu for gpu in safe_gpus if gpu.get("telemetry_measured")]
    if not safe_gpus and mps_available:
        suggested_image_size = 416
        suggested_batch = "Start with a small batch; Apple GPU memory shares system RAM."
        gpu_guidance = "Apple MPS is available. VAOS currently reports MPS availability but does not measure Apple GPU utilization or shared-memory use."
    elif not safe_gpus:
        suggested_image_size = 416
        suggested_batch = "CPU-safe starting point; validate with the chosen model and data."
        gpu_guidance = "No NVIDIA GPU was measured. CPU inference/training remains available."
    elif not measured_gpus:
        suggested_image_size = 416
        suggested_batch = "Use Auto batch; current GPU memory measurements are unavailable."
        gpu_guidance = "An NVIDIA device was detected, but its current memory capacity/use was not measured."
    elif conservative_gpu_available_gb and conservative_gpu_available_gb < 6:
        suggested_image_size = 416
        suggested_batch = "Auto batch, or begin conservatively at 2 if Auto is unavailable."
        gpu_guidance = "Smallest detected GPU memory suggests a conservative image size; verify with a real run."
    elif conservative_gpu_available_gb and conservative_gpu_available_gb < 10:
        suggested_image_size = 512
        suggested_batch = "Auto batch; verify memory use on the selected model."
        gpu_guidance = "Detected GPU memory supports a moderate starting profile, not a guaranteed fit."
    else:
        suggested_image_size = 640
        suggested_batch = "Auto batch; verify memory use on the selected model."
        gpu_guidance = "A 640-pixel starting profile may fit, but actual model/data memory is not benchmarked."

    if training_active:
        suggested_batch = "Do not launch another training job; keep the active run unchanged."
        suggested_image_size = None

    return {
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "facts": {
            "operating_system": platform.system() or "unknown",
            "os_release": platform.release() or "unknown",
            "architecture": platform.machine() or "unknown",
            "python_version": platform.python_version(),
            "cpu_logical_threads": cpu_threads,
            "ram_percent": ram_percent,
            "ram_used_gb": ram_used,
            "ram_total_gb": ram_total,
            "storage": storage,
            "gpus": safe_gpus,
        },
        "compatibility": {
            "nvidia_device_detected": bool(safe_gpus),
            "mps_device_detected": mps_available,
            "cuda_runtime_status": "available" if cuda_runtime is True else "unavailable" if cuda_runtime is False else "unknown",
            "cuda_inference_status": inference_device,
            "cuda_inference_explanation": inference_note,
            "inference_backend": inference_device,
            "mps_inference_status": "active" if inference_device == "mps" else "available_not_active" if mps_available else "unavailable",
            "mps_inference_explanation": (inference_note + " Apple GPU utilization is not measured by this diagnostics page.") if inference_device == "mps" else "Apple GPU utilization is not measured by this diagnostics page.",
            "inference_runtime_verified": inference_device != "unknown",
            "training_status": training_status,
            "training_device": str(training.get("device") or "unknown")[:120] if training_active else "not active",
        },
        "recommendations": {
            "status": "estimate_only",
            "applied_automatically": False,
            "image_size_pixels": suggested_image_size,
            "batch_size": suggested_batch,
            "data_workers": max(0, min(4, cpu_threads - 2)),
            "workload_guidance": workload_guidance,
            "memory_guidance": memory_guidance,
            "gpu_guidance": gpu_guidance,
            "limitations": "These are conservative heuristics, not a benchmark, compatibility certification, accuracy prediction, or guarantee of available resources.",
        },
    }
