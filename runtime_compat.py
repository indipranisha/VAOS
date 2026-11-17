"""Small, optional-runtime helpers for cross-platform VAOS operation."""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any


def default_datasets_dir(
    env_value: str | None = None,
    *,
    system_name: str | None = None,
    home: str | Path | None = None,
    windows_data_drive_exists: bool | None = None,
) -> Path:
    """Return the configured dataset directory or a sensible local default.

    Keep the historical D: location when it exists on Windows; use a home
    directory on macOS/Linux rather than interpreting a Windows path there.
    """
    configured = os.environ.get("VAOS_DATASETS_DIR") if env_value is None else env_value
    if configured and configured.strip():
        return Path(configured).expanduser()
    system = system_name or platform.system()
    if system == "Windows":
        drive_exists = Path("D:/").exists() if windows_data_drive_exists is None else windows_data_drive_exists
        if drive_exists:
            return Path("D:/VAOS_Datasets")
    return Path(home).expanduser() / "VAOS_Datasets" if home is not None else Path.home() / "VAOS_Datasets"


def available_cpu_threads() -> int:
    """Estimate CPUs allowed to this process, respecting affinity when exposed."""
    process_count = getattr(os, "process_cpu_count", None)
    if callable(process_count):
        try:
            count = process_count()
            if count:
                return max(1, int(count))
        except (OSError, TypeError, ValueError):
            pass
    try:
        import psutil

        affinity = psutil.Process().cpu_affinity()
        if affinity:
            return max(1, len(affinity))
    except Exception:
        pass
    try:
        affinity = os.sched_getaffinity(0)
        if affinity:
            return max(1, len(affinity))
    except (AttributeError, OSError, TypeError):
        pass
    return max(1, os.cpu_count() or 1)


def mps_is_available(torch_module: Any) -> bool:
    """Check Apple MPS without assuming that PyTorch includes that backend."""
    backend = getattr(getattr(torch_module, "backends", None), "mps", None)
    try:
        return bool(backend and backend.is_available())
    except Exception:
        return False


def cuda_is_available(torch_module: Any) -> bool:
    """Check CUDA defensively; CPU-only or partial PyTorch installs are valid."""
    backend = getattr(torch_module, "cuda", None)
    try:
        return bool(backend and backend.is_available())
    except Exception:
        return False


def resolve_inference_backend(torch_module: Any, preference: str = "auto") -> dict[str, Any]:
    """Choose CUDA, MPS, or CPU and report the actual backend selected."""
    preference = (preference or "auto").strip().casefold()
    cuda_available = cuda_is_available(torch_module)
    mps_available = mps_is_available(torch_module)
    if preference == "cpu":
        selected = "cpu"
    elif preference == "cuda":
        selected = "cuda" if cuda_available else "cpu"
    elif preference == "mps":
        selected = "mps" if mps_available else "cpu"
    elif preference == "auto":
        selected = "cuda" if cuda_available else "mps" if mps_available else "cpu"
    else:
        selected = "cuda" if cuda_available else "mps" if mps_available else "cpu"
    device: int | str = 0 if selected == "cuda" else selected
    return {
        "backend": selected,
        "device": device,
        "cuda_available": cuda_available,
        "mps_available": mps_available,
        "requested_backend_available": preference not in {"cuda", "mps"} or selected == preference,
    }


def resolve_training_device(
    torch_module: Any,
    choice: str,
    process_threads: int,
    *,
    resource_mode: bool = False,
) -> dict[str, Any]:
    """Validate a UI-selected CPU/CUDA/MPS training target without starting work."""
    kind, _, value = str(choice or "cpu:1").partition(":")
    index = int(value) if value.isdigit() else 0
    if kind == "gpu":
        cuda = getattr(torch_module, "cuda", None)
        available = cuda_is_available(torch_module)
        if value == "all":
            count = int(cuda.device_count()) if available and callable(getattr(cuda, "device_count", None)) else 0
            if count < 1:
                raise RuntimeError("CUDA GPUs are not available to the training runtime. Select a CPU option or install CUDA-enabled PyTorch support.")
            device = list(range(count))
            names = [str(cuda.get_device_name(gpu_index)) for gpu_index in device]
            return {
                "device": device,
                "label": "All NVIDIA GPUs · " + ", ".join(f"GPU {gpu_index} {name}" for gpu_index, name in zip(device, names)),
                "cpu_threads": max(1, int(process_threads or 1) - 1),
                "accelerator": "cuda",
                "gpu_count": count,
            }
        if not available:
            raise RuntimeError("CUDA is unavailable in this PyTorch environment. Choose a CPU thread option or install a compatible CUDA-enabled PyTorch build.")
        count = int(cuda.device_count())
        if index >= count:
            raise RuntimeError(f"GPU {index} is not available to the training runtime. Refresh the device list and choose an available device.")
        return {
            "device": str(index),
            "label": f"GPU {index} · {cuda.get_device_name(index)}",
            "cpu_threads": None,
            "accelerator": "cuda",
            "gpu_count": count,
        }
    if kind == "mps":
        if not mps_is_available(torch_module):
            raise RuntimeError("Apple MPS is unavailable in this PyTorch/macOS runtime. Choose CPU, or install a supported PyTorch build on Apple Silicon.")
        return {"device": "mps", "label": "Apple GPU · MPS", "cpu_threads": None, "accelerator": "mps", "gpu_count": 0}

    threads = max(1, min(index, int(process_threads or 1)))
    if value == "all" and resource_mode:
        threads = max(1, int(process_threads or 1) - 1)
    return {
        "device": "cpu",
        "label": f"CPU · {threads} thread{'s' if threads != 1 else ''}",
        "cpu_threads": threads,
        "accelerator": "cpu",
        "gpu_count": 0,
    }


def training_resource_profile(
    *,
    cpu_threads: int,
    available_ram_gb: float | None,
    accelerator: str,
    gpu_count: int = 0,
    resource_mode: bool = False,
) -> dict[str, int | str | None]:
    """Choose conservative training defaults from process CPU and free RAM.

    This is a starting profile, not an OOM guarantee. CUDA still uses
    Ultralytics' automatic batch sizing for a single GPU when resource mode is
    enabled. Unknown RAM deliberately selects zero loader workers and a small
    batch rather than assuming a large host.
    """
    threads = max(1, int(cpu_threads or 1))
    ram = None
    try:
        ram = max(0.0, float(available_ram_gb)) if available_ram_gb is not None else None
    except (TypeError, ValueError, OverflowError):
        pass

    worker_cap = 8 if resource_mode else 4
    workers = min(worker_cap, max(0, threads - 1))
    if ram is None:
        workers = 0
        batch = 2
        image_size = 512
        profile = "conservative · RAM unknown"
    else:
        memory_worker_cap = max(0, int(max(0.0, ram - 2.0) / 1.5))
        workers = min(workers, memory_worker_cap)
        if ram < 3.0:
            batch, image_size, profile = 1, 320, "low-memory · minimal batch"
        elif ram < 6.0:
            batch, image_size, profile = 1, 416, "low-memory · conservative"
        elif ram < 10.0:
            batch, image_size, profile = 2, 512, "balanced · limited memory"
        elif ram < 18.0:
            batch, image_size, profile = 4, 640, "balanced"
        else:
            batch, image_size, profile = 8, 640, "higher-memory"

    # A CUDA single-device run can let Ultralytics inspect VRAM. Multi-GPU
    # distributed runs keep an explicit conservative batch per device.
    if accelerator == "cuda" and gpu_count == 1 and resource_mode and ram is not None and ram >= 6.0:
        batch = -1
    return {
        "workers": workers,
        "batch": batch,
        "image_size": image_size,
        "profile": profile,
        "cpu_threads": threads,
    }\n