"""Capability-based host telemetry; unsupported counters remain absent."""
from __future__ import annotations

import math
import platform
import time
from pathlib import Path
from typing import Any

_previous_io: dict[str, float] = {}


def collect_host_telemetry() -> dict[str, Any]:
    import psutil

    result: dict[str, Any] = {"host_os": platform.system(), "host_architecture": platform.machine()}
    # Each probe is independent: a missing sensor must not discard other counters.
    probes = {
        "cpu_per_core_percent": lambda: psutil.cpu_percent(percpu=True),
        "swap_used_gb": lambda: round(psutil.swap_memory().used / 1024 ** 3, 2),
        "process_memory_mb": lambda: round(psutil.Process().memory_info().rss / 1024 ** 2),
        "process_threads": lambda: psutil.Process().num_threads(),
    }
    for key, probe in probes.items():
        try:
            result[key] = probe()
        except (OSError, AttributeError, NotImplementedError, psutil.Error):
            pass
    try:
        battery = psutil.sensors_battery()
        if battery is not None:
            result.update(battery_percent=battery.percent, battery_charging=battery.power_plugged)
    except (OSError, AttributeError, NotImplementedError, psutil.Error):
        pass
    try:
        temperatures = psutil.sensors_temperatures()
        result["temperature_sensors"] = [
            {"source": source, "label": sensor.label or source, "temperature_c": sensor.current}
            for source, sensors in temperatures.items() for sensor in sensors
            if isinstance(sensor.current, (int, float)) and math.isfinite(sensor.current)
        ]
    except (OSError, AttributeError, NotImplementedError, psutil.Error):
        pass
    now = time.monotonic()
    for kind, probe, fields in (
        ("network", psutil.net_io_counters, (("bytes_sent", "upload"), ("bytes_recv", "download"))),
        ("disk", psutil.disk_io_counters, (("read_bytes", "read"), ("write_bytes", "write"))),
    ):
        try:
            counters = probe()
            if counters is None:
                continue
            previous_time = _previous_io.get(kind)
            for field, label in fields:
                value = getattr(counters, field)
                key = f"{kind}_{label}"
                previous = _previous_io.get(key)
                if previous is not None and previous_time is not None and now > previous_time and value >= previous:
                    result[f"{key}_mb_s"] = round((value - previous) / (now - previous_time) / 1024 ** 2, 2)
                _previous_io[key] = value
            _previous_io[kind] = now
        except (OSError, AttributeError, NotImplementedError, psutil.Error):
            pass
    return result


def read_linux_gpu_metrics(root: Path = Path("/sys/class/drm")) -> dict[str, Any]:
    """Use driver-exposed DRM counters on Linux (e.g. AMD); never infer utilization."""
    if platform.system() != "Linux":
        return {}
    for card in sorted(root.glob("card[0-9]*")):
        if not card.name[4:].isdigit():
            continue
        device = card / "device"
        def read_number(path: Path) -> float | None:
            try:
                value = float(path.read_text().strip())
                return value if math.isfinite(value) and value >= 0 else None
            except (OSError, ValueError):
                return None
        busy = read_number(device / "gpu_busy_percent")
        if busy is None or busy > 100:
            continue
        used = read_number(device / "mem_info_vram_used")
        total = read_number(device / "mem_info_vram_total")
        try:
            driver = (device / "driver").resolve().name
        except OSError:
            driver = "DRM"
        # Select the explicitly labelled GPU edge temperature only.
        temperature = None
        for label in device.glob("hwmon/hwmon*/temp*_label"):
            try:
                if label.read_text().strip().lower() == "edge":
                    raw = read_number(label.with_name(label.name.replace("_label", "_input")))
                    temperature = raw / 1000 if raw is not None else None
            except OSError:
                pass
        return {"gpu_available": True, "gpu_backend": "drm", "gpu_name": f"{driver} · {card.name}",
                "gpu_utilization_percent": busy, "gpu_memory_used_mb": round(used / 1024 ** 2) if used is not None else None,
                "gpu_memory_total_mb": round(total / 1024 ** 2) if total is not None else None,
                "gpu_temperature_c": temperature}
    return {}
