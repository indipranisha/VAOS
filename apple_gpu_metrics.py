"""Read Apple GPU counters exposed by macOS without privileged commands."""
from __future__ import annotations

import math
import plistlib
import subprocess
import sys
from typing import Any


def read_apple_gpu_metrics() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {}
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-r", "-c", "AGXAccelerator", "-a"],
            capture_output=True, timeout=1.25, check=True,
        )
        rows = plistlib.loads(result.stdout)
        if not isinstance(rows, list):
            return {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            stats = row.get("PerformanceStatistics")
            if not isinstance(stats, dict):
                continue
            def number(key: str) -> float | None:
                value = stats.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    return float(value)
                return None
            utilization = number("Device Utilization %")
            memory = number("In use system memory")
            return {
                "gpu_available": True,
                "gpu_name": str(row.get("model") or "Apple GPU")[:120],
                "gpu_backend": "apple",
                "gpu_utilization_percent": utilization if utilization is not None and utilization <= 100 else None,
                "gpu_memory_used_mb": round(memory / 1024 ** 2) if memory is not None else None,
                "gpu_memory_kind": "shared",
                "gpu_temperature_c": None,
                "gpu_telemetry_note": "macOS does not expose GPU temperature through this monitor.",
            }
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
        pass
    return {}
