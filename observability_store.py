"""Local, privacy-conscious persistence primitives for VAOS observability.

This module deliberately has no dependency on ``main``.  That lets tests and
other VAOS processes use an explicitly selected database path without loading
the application's live data directory.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 12
RETENTION_DAY_CHOICES = (30, 90, 180, 365)
MAX_EVENT_TEXT = 1200
MAX_EVENT_DETAILS_BYTES = 16_384
MAX_METRIC_DIMENSIONS = 12
UNKNOWN_SESSION_AFTER_SECONDS = 120
GPU_POWER_MIN_INTERVAL_SECONDS = 20.0
GPU_POWER_MAX_INTERVAL_SECONDS = 90.0
FALLBACK_COUNTER_BUFFER_LIMIT = 5000
FALLBACK_COUNTER_BATCH_LIMIT = 5000
FALLBACK_COUNTER_MAX_BYTES = 32 * 1024 * 1024
_FALLBACK_COUNTER_METRICS = {
    "api.requests": {
        "event_type": "api.request_batch",
        "component": "api",
        "message": "Aggregated VAOS REST request counts recovered from the fallback queue.",
        "count_key": "request_count",
        "overflow_dimensions": {"route": "other", "method": "OTHER", "status": 0},
    },
    "inference.frames_processed": {
        "event_type": "inference.frame_batch",
        "component": "camera",
        "message": "Aggregated VAOS inference frame counts recovered from the fallback queue.",
        "count_key": "frame_count",
        "overflow_dimensions": {"camera_ref": "other", "source_type": "mixed"},
    },
}
_FALLBACK_COUNTER_INIT_LOCK = threading.Lock()
_FALLBACK_COUNTER_INITIALIZED: set[str] = set()

_SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|authorization|cookie|credential|embedding|biometric|image|photo|raw_frame|private_key)")
_SECRET_VALUE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+|\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret|authorization)\s*[:=]\s*[^\s,;]+")
_URL_QUERY = re.compile(r"(?i)(https?://[^\s?#]+)\?[^\s#]+")
_CAPABILITY_PATH = re.compile(r"(?i)(/api/(?:camera|enroll)/|/c/|/admin/(?:cameras|enrollment-links)/)([^/?\s]+)")
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\s\"']+")
_POSIX_LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9._~:/-])/(?:home|mnt|media|tmp|var|opt|srv|root|workspace|Users|private|Volumes|run|data|app|etc|dev)(?:/[^\s\"'<>]*)?")
_EVENT_SLUG = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")
_METRIC_SLUG = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")

# One opaque incarnation ID per Python process. PID alone is not a stable
# identity: Windows can reuse it after a fast restart.
_PROCESS_INSTANCE_ID = str(uuid.uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a UTC offset.")
    return parsed.astimezone(timezone.utc)


def _normalize_time(value: str) -> str:
    return _parse_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _safe_text(value: Any, limit: int = MAX_EVENT_TEXT) -> str:
    text = str(value).replace("\x00", " ")
    text = _CAPABILITY_PATH.sub(r"\1[REDACTED]", text)
    text = _SECRET_VALUE.sub(lambda match: (match.group(1) or match.group(2) or "secret") + "[REDACTED]", text)
    text = _URL_QUERY.sub(r"\1?[REDACTED]", text)
    text = _WINDOWS_PATH.sub("[local path]", text)
    text = _POSIX_LOCAL_PATH.sub("[local path]", text)
    return text[:limit]


def redact_details(value: Any, *, depth: int = 0) -> Any:
    """Return bounded JSON-safe metadata, omitting secrets and media payloads."""
    if depth > 5:
        return "[nested data omitted]"
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return "[binary data omitted]"
    if isinstance(value, str):
        return _safe_text(value, 600)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)[:80]
            if _SECRET_KEY.search(key):
                continue
            result[key] = redact_details(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_details(item, depth=depth + 1) for item in value[:100]]
    return _safe_text(type(value).__name__, 100)


@contextmanager
def _connection(path: Path, *, timeout_seconds: float = 15) -> Iterator[sqlite3.Connection]:
    timeout = max(0.05, min(30.0, float(timeout_seconds)))
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _current_schema_version(db: sqlite3.Connection) -> int:
    if not _table_exists(db, "vaos_schema_migrations"):
        return 0
    row = db.execute("SELECT COALESCE(MAX(version),0) AS version FROM vaos_schema_migrations").fetchone()
    return int(row["version"])


def _verified_backup(db_path: Path) -> Path | None:
    """Make a consistent SQLite online backup before the first schema change."""
    if not db_path.is_file() or db_path.stat().st_size <= 0:
        return None
    backup_dir = db_path.parent / "backups" / "observability-migrations"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if backup_dir.is_symlink():
        raise RuntimeError("Refusing to store an observability backup in a linked directory.")
    _restrict_private_path(backup_dir, directory=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"app-pre-observability-v{SCHEMA_VERSION}-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with closing(sqlite3.connect(db_path, timeout=15)) as source, closing(sqlite3.connect(temporary, timeout=15)) as target:
            source.execute("PRAGMA busy_timeout=15000")
            source.backup(target, pages=256, sleep=0.05)
            target.commit()
        with closing(sqlite3.connect(f"file:{temporary.as_posix()}?mode=ro", uri=True, timeout=15)) as check:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
        os.replace(temporary, destination)
        _restrict_private_path(destination, directory=False)
        digest = hashlib.sha256()
        with destination.open("rb") as backup_file:
            for block in iter(lambda: backup_file.read(1024 * 1024), b""):
                digest.update(block)
        manifest = {
            "backup_file": destination.name,
            "created_at": utc_now(),
            "sha256": digest.hexdigest(),
            "size_bytes": destination.stat().st_size,
            "integrity_check": "ok",
            "reason": f"pre-observability-schema-v{SCHEMA_VERSION}",
        }
        manifest_path = destination.with_suffix(destination.suffix + ".manifest.json")
        with manifest_path.open("x", encoding="utf-8", newline="\n") as manifest_file:
            manifest_file.write(_json(manifest))
        _restrict_private_path(manifest_path, directory=False)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _restrict_private_path(path: Path, *, directory: bool) -> None:
    """Restrict migration-backup storage to the current account and SYSTEM."""
    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
        return
    import csv
    import subprocess

    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if identity.returncode != 0:
        raise RuntimeError("Could not identify the current Windows account to secure the migration backup.")
    sid = next((row[-1].strip() for row in csv.reader(identity.stdout.splitlines()) if row and row[-1].strip().startswith("S-1-")), "")
    if not sid:
        raise RuntimeError("Could not determine the current Windows account SID for the migration backup ACL.")
    inherited = "(OI)(CI)F" if directory else "F"
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:{inherited}", f"*S-1-5-18:{inherited}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not apply a private Windows ACL to the migration backup path.")


_METRICS: tuple[dict[str, Any], ...] = (
    {"key": "app.starts", "name": "Application starts", "description": "Successful VAOS process starts recorded by lifecycle sessions.", "unit": "starts", "type": "counter", "source": "runtime_sessions", "scope": "installation", "dimensions": ["component"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": True},
    {"key": "app.clean_stops", "name": "Clean application stops", "description": "Process sessions closed through the graceful shutdown hook.", "unit": "stops", "type": "counter", "source": "runtime_sessions", "scope": "installation", "dimensions": ["component"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": True},
    {"key": "app.uptime_seconds", "name": "Known installation uptime", "description": "Wall-clock union of known runtime intervals; concurrent admin and camera processes count once, and unknown exits contribute only through their last heartbeat.", "unit": "seconds", "type": "gauge", "source": "runtime_sessions", "scope": "installation", "dimensions": [], "aggregation": "interval_union", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": True},
    {"key": "storage.schema_migration_backups", "name": "Verified schema migration backups", "description": "Non-empty SQLite online backups verified before additive observability schema migrations that later committed successfully.", "unit": "backups", "type": "counter", "source": "verified pre-migration SQLite backup", "scope": "installation", "dimensions": [], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "datasets.preparations_completed", "name": "Dataset preparations completed", "description": "Successful local or built-in dataset validation/preparation operations recorded by VAOS.", "unit": "runs", "type": "counter", "source": "dataset validation and import workers", "scope": "installation", "dimensions": ["dataset_kind"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "datasets.preparations_failed", "name": "Dataset preparations failed", "description": "Dataset validation/preparation operations that ended with a reported failure; raw exception text and filesystem paths are not stored.", "unit": "runs", "type": "counter", "source": "dataset validation and import workers", "scope": "installation", "dimensions": ["dataset_kind"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "datasets.images_prepared", "name": "Images in prepared datasets", "description": "Image count reported by a successful dataset preparation/import. It may represent source inventory or a prepared derivative, as stated in the associated event; it is not image exposures during training.", "unit": "images", "type": "counter", "source": "dataset validation and import workers", "scope": "dataset preparation", "dimensions": ["dataset_kind"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "datasets.annotations_validated", "name": "Validated object-box annotations", "description": "Object-box annotation count verified by a successful dataset validation/preparation worker. A source that does not calculate this count leaves it unavailable.", "unit": "annotations", "type": "counter", "source": "dataset validation and import workers", "scope": "dataset preparation", "dimensions": ["dataset_kind"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "datasets.preparation_duration_seconds", "name": "Dataset preparation duration", "description": "Monotonic elapsed time for one completed or failed dataset validation/preparation operation.", "unit": "seconds", "type": "gauge", "source": "dataset validation worker monotonic timer", "scope": "dataset preparation", "dimensions": ["dataset_kind"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.jobs_started", "name": "Training jobs started", "description": "Training jobs accepted and started by VAOS.", "unit": "jobs", "type": "counter", "source": "training lifecycle events", "scope": "installation", "dimensions": ["dataset_id", "model_id"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "training.jobs_completed", "name": "Training jobs completed", "description": "Training jobs that ended with complete status.", "unit": "jobs", "type": "counter", "source": "training lifecycle events", "scope": "installation", "dimensions": ["dataset_id", "model_id"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "training.jobs_failed", "name": "Training jobs failed", "description": "Training jobs that ended in a failed state.", "unit": "jobs", "type": "counter", "source": "training lifecycle events", "scope": "installation", "dimensions": ["dataset_id", "model_id"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "training.epochs_completed", "name": "Training epochs completed", "description": "Epoch-complete events emitted by the training worker.", "unit": "epochs", "type": "counter", "source": "training lifecycle events", "scope": "installation", "dimensions": ["dataset_id", "model_id"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "training.dataset_images", "name": "Images selected for training", "description": "Number of images in the resolved training source for one initialized run, including train and validation images when reported by the source manifest.", "unit": "images", "type": "gauge", "source": "resolved training dataset manifest", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.dataset_annotations", "name": "Annotations in selected dataset", "description": "Object-box annotation count across the train/validation splits after the retained YOLO label scan succeeds. It describes the dataset files and does not prove every annotation was consumed by the trainer.", "unit": "annotations", "type": "gauge", "source": "YOLO train/validation label scan", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.job_duration_seconds", "name": "Training job duration", "description": "Monotonic elapsed time from training worker start until its terminal complete, safe-stop or failed event; includes data/model preparation and training runtime.", "unit": "seconds", "type": "gauge", "source": "training worker monotonic clock", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.box_loss", "name": "Box loss", "description": "Per-epoch box localization loss reported by the trainer.", "unit": "loss", "type": "gauge", "source": "training epoch metrics", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id", "epoch"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.class_loss", "name": "Class loss", "description": "Per-epoch classification loss reported by the trainer.", "unit": "loss", "type": "gauge", "source": "training epoch metrics", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id", "epoch"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.dfl_loss", "name": "Distribution focal loss", "description": "Per-epoch DFL reported by the trainer.", "unit": "loss", "type": "gauge", "source": "training epoch metrics", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id", "epoch"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.precision", "name": "Precision", "description": "Per-epoch precision reported by the trainer.", "unit": "ratio", "type": "gauge", "source": "training epoch metrics", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id", "epoch"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.recall", "name": "Recall", "description": "Per-epoch recall reported by the trainer.", "unit": "ratio", "type": "gauge", "source": "training epoch metrics", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id", "epoch"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.map50", "name": "mAP50", "description": "Per-epoch mean average precision at IoU 0.50.", "unit": "ratio", "type": "gauge", "source": "training epoch metrics", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id", "epoch"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.map50_95", "name": "mAP50-95", "description": "Per-epoch mean average precision from IoU 0.50 through 0.95.", "unit": "ratio", "type": "gauge", "source": "training epoch metrics", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id", "epoch"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "training.learning_rate", "name": "Learning rate", "description": "Per-epoch learning rate reported by the trainer.", "unit": "rate", "type": "gauge", "source": "training epoch metrics", "scope": "training job", "dimensions": ["training_job_id", "dataset_id", "model_id", "epoch"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "365 days", "sampling_seconds": None, "available": False},
    {"key": "api.requests", "name": "API requests", "description": "Aggregated counts of VAOS REST requests, including the private and camera services.", "unit": "requests", "type": "counter", "source": "bounded request aggregation", "scope": "installation", "dimensions": ["route", "method", "status"], "aggregation": "sum", "estimated": False, "privacy": "security", "retention": "90 days", "sampling_seconds": None, "available": False},
    {"key": "mcp.tool_invocations", "name": "MCP tool invocations", "description": "Calls completed through the local VAOS MCP bridge, grouped by the fixed bridge tool name and success/error outcome. Tool arguments and returned content are excluded.", "unit": "calls", "type": "counter", "source": "local MCP bridge invocation wrapper", "scope": "MCP bridge", "dimensions": ["tool", "outcome"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "90 days", "sampling_seconds": None, "available": False},
    {"key": "mcp.tool_duration_ms", "name": "MCP tool execution duration", "description": "Elapsed time measured by the local MCP bridge around one tool operation, including VAOS REST round trips. Tool arguments and returned content are excluded.", "unit": "ms", "type": "gauge", "source": "local MCP bridge monotonic timer", "scope": "MCP bridge", "dimensions": ["tool", "outcome"], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": None, "available": False},
    {"key": "api.request_latency_p50_ms", "name": "API request latency p50", "description": "Median middleware duration from a bounded recent sample of instrumented REST responses.", "unit": "ms", "type": "gauge", "source": "monotonic API middleware timing", "scope": "API service", "dimensions": [], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "api.request_latency_p95_ms", "name": "API request latency p95", "description": "95th-percentile middleware duration from a bounded recent sample of instrumented REST responses.", "unit": "ms", "type": "gauge", "source": "monotonic API middleware timing", "scope": "API service", "dimensions": [], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "inference.frames_processed", "name": "Successful inference frames", "description": "Successful camera or local-video frame-processing responses. A video-review count records only source_type=local_video and never stores the video name, session token, or frame. Camera references are one-way hashes. A successful response does not guarantee that an object was detected.", "unit": "frames", "type": "counter", "source": "camera-frame and local-video-review response handlers", "scope": "inference", "dimensions": ["camera_ref", "source_type"], "aggregation": "sum", "estimated": False, "privacy": "operational", "retention": "lifetime", "sampling_seconds": None, "available": False},
    {"key": "inference.camera_frame_processing_latency_p50_ms", "name": "Camera frame processing latency p50", "description": "Median server-side time from dispatch to completion of a successful camera frame-processing call. Includes thread-pool scheduling, decode, face/object work and result preparation; excludes client upload/network time and is not pure model/GPU inference time.", "unit": "ms", "type": "gauge", "source": "monotonic camera frame processing timing", "scope": "camera service", "dimensions": [], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "inference.camera_frame_processing_latency_p95_ms", "name": "Camera frame processing latency p95", "description": "95th-percentile server-side time from dispatch to completion of a successful camera frame-processing call. Includes thread-pool scheduling, decode, face/object work and result preparation; excludes client upload/network time and is not pure model/GPU inference time.", "unit": "ms", "type": "gauge", "source": "monotonic camera frame processing timing", "scope": "camera service", "dimensions": [], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "inference.local_video_frame_processing_latency_p50_ms", "name": "Local-video frame processing latency p50", "description": "Median server-side elapsed time from dispatch to completion of a successful local-video frame-processing call; excludes browser upload/network time and is not pure model/GPU inference time.", "unit": "ms", "type": "gauge", "source": "monotonic local-video frame processing timing", "scope": "local video review", "dimensions": [], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "inference.local_video_frame_processing_latency_p95_ms", "name": "Local-video frame processing latency p95", "description": "95th-percentile server-side elapsed time from dispatch to completion of a successful local-video frame-processing call; excludes browser upload/network time and is not pure model/GPU inference time.", "unit": "ms", "type": "gauge", "source": "monotonic local-video frame processing timing", "scope": "local video review", "dimensions": [], "aggregation": "last", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "hardware.cpu_utilization_percent", "name": "CPU utilization", "description": "CPU utilization sampled from the existing VAOS telemetry collector at roughly 30-second intervals.", "unit": "%", "type": "gauge", "source": "get_system_metrics", "scope": "host", "dimensions": ["host_id"], "aggregation": "average", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "hardware.ram_used_bytes", "name": "RAM used", "description": "RAM in use sampled from the existing VAOS telemetry collector at roughly 30-second intervals.", "unit": "bytes", "type": "gauge", "source": "get_system_metrics", "scope": "host", "dimensions": ["host_id"], "aggregation": "average", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "hardware.disk_used_bytes", "name": "VAOS data-volume space used", "description": "Bytes used on the volume containing VAOS DATA_DIR, sampled by the existing telemetry cycle. The full data path is not stored.", "unit": "bytes", "type": "gauge", "source": "get_system_metrics/shutil.disk_usage", "scope": "VAOS data volume", "dimensions": ["host_id", "volume_id"], "aggregation": "average", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "hardware.disk_used_percent", "name": "VAOS data-volume space used", "description": "Percentage used on the volume containing VAOS DATA_DIR, sampled by the existing telemetry cycle. The full data path is not stored.", "unit": "%", "type": "gauge", "source": "get_system_metrics/shutil.disk_usage", "scope": "VAOS data volume", "dimensions": ["host_id", "volume_id"], "aggregation": "average", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "hardware.gpu_utilization_percent", "name": "GPU utilization", "description": "GPU utilization sampled from nvidia-smi through existing VAOS telemetry at roughly 30-second intervals.", "unit": "%", "type": "gauge", "source": "get_system_metrics/nvidia-smi", "scope": "device", "dimensions": ["device_id", "model_id"], "aggregation": "average", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "hardware.gpu_power_draw_w", "name": "GPU board power draw", "description": "Instantaneous device power reported by the NVIDIA board sensor; this is not wall or whole-system power.", "unit": "W", "type": "gauge", "source": "nvidia-smi power.draw", "scope": "GPU device", "dimensions": ["device_id", "model_id"], "aggregation": "average", "estimated": False, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "hardware.gpu_energy_interval_wh", "name": "GPU energy per sampled interval", "description": "Trapezoidal integration of consecutive measured NVIDIA GPU board-power readings. Intervals over 90 seconds are skipped; this does not represent wall energy or per-job energy.", "unit": "Wh", "type": "gauge", "source": "nvidia-smi power.draw trapezoidal integration", "scope": "GPU device sampled interval", "dimensions": ["device_id", "model_id"], "aggregation": "sum", "estimated": True, "privacy": "operational", "retention": "30 days", "sampling_seconds": 30, "available": False},
    {"key": "hardware.gpu_energy_wh", "name": "Estimated cumulative GPU board energy", "description": "Lifetime GPU board energy estimated by integrating measured NVIDIA GPU board-power samples. The interval calculation uses endpoint readings; unsampled gaps are excluded. Energy is not assigned to a training job without defensible job attribution.", "unit": "Wh", "type": "counter", "source": "nvidia-smi power.draw trapezoidal integration", "scope": "GPU device", "dimensions": ["device_id", "model_id"], "aggregation": "integral", "estimated": True, "privacy": "operational", "retention": "lifetime", "sampling_seconds": 30, "available": False},
)
METRIC_CATALOGUE = {item["key"]: item for item in _METRICS}


def _create_schema(db: sqlite3.Connection) -> None:
    script = """
        CREATE TABLE IF NOT EXISTS vaos_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            backup_name TEXT
        );
        CREATE TABLE IF NOT EXISTS observability_installation (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
            installation_id TEXT NOT NULL UNIQUE,
            first_started_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observability_runtime_sessions (
            session_id TEXT PRIMARY KEY,
            component TEXT NOT NULL,
            process_id INTEGER NOT NULL,
            process_instance_id TEXT,
            started_at TEXT NOT NULL,
            last_heartbeat_at TEXT NOT NULL,
            clean_shutdown_at TEXT,
            exit_reason TEXT NOT NULL CHECK(exit_reason IN ('running','clean','unknown')),
            app_version TEXT NOT NULL,
            installation_id TEXT NOT NULL,
            FOREIGN KEY(installation_id) REFERENCES observability_installation(installation_id)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_sessions_started ON observability_runtime_sessions(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_obs_sessions_status_heartbeat ON observability_runtime_sessions(exit_reason,last_heartbeat_at DESC);
        CREATE TABLE IF NOT EXISTS observability_metric_catalogue (
            metric_key TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            description TEXT NOT NULL,
            unit TEXT NOT NULL,
            metric_type TEXT NOT NULL CHECK(metric_type IN ('counter','gauge','histogram','event')),
            dimensions_json TEXT NOT NULL,
            aggregation TEXT NOT NULL,
            source TEXT NOT NULL,
            measurement_scope TEXT NOT NULL,
            estimated INTEGER NOT NULL CHECK(estimated IN (0,1)),
            privacy_classification TEXT NOT NULL,
            retention TEXT NOT NULL,
            sampling_interval_seconds REAL,
            available INTEGER NOT NULL CHECK(available IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS observability_events (
            event_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            component TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            session_id TEXT,
            correlation_id TEXT,
            details_json TEXT NOT NULL,
            expires_at TEXT,
            FOREIGN KEY(session_id) REFERENCES observability_runtime_sessions(session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_events_time ON observability_events(timestamp DESC,event_id DESC);
        CREATE INDEX IF NOT EXISTS idx_obs_events_type_time ON observability_events(event_type,timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_obs_events_component_time ON observability_events(component,timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_obs_events_level_time ON observability_events(level,timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_obs_events_session_time ON observability_events(session_id,timestamp DESC,event_id DESC);
        CREATE TABLE IF NOT EXISTS observability_counter_events (
            event_id TEXT NOT NULL,
            metric_key TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            delta INTEGER NOT NULL CHECK(delta >= 0),
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(event_id,metric_key),
            FOREIGN KEY(event_id) REFERENCES observability_events(event_id) ON DELETE RESTRICT,
            FOREIGN KEY(metric_key) REFERENCES observability_metric_catalogue(metric_key)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_counters_metric_time ON observability_counter_events(metric_key,timestamp DESC);
        CREATE TABLE IF NOT EXISTS observability_lifetime_counters (
            metric_key TEXT PRIMARY KEY,
            value INTEGER NOT NULL CHECK(value >= 0),
            updated_at TEXT NOT NULL,
            FOREIGN KEY(metric_key) REFERENCES observability_metric_catalogue(metric_key)
        );
        CREATE TABLE IF NOT EXISTS observability_counter_buffer (
            buffer_id TEXT PRIMARY KEY,
            metric_key TEXT NOT NULL,
            bucket_start TEXT NOT NULL,
            dimensions_json TEXT NOT NULL,
            delta INTEGER NOT NULL CHECK(delta > 0),
            updated_at TEXT NOT NULL,
            UNIQUE(metric_key,bucket_start,dimensions_json),
            FOREIGN KEY(metric_key) REFERENCES observability_metric_catalogue(metric_key)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_counter_buffer_time ON observability_counter_buffer(bucket_start,buffer_id);
        CREATE TABLE IF NOT EXISTS observability_metric_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_event_id TEXT NOT NULL,
            metric_key TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            value REAL NOT NULL,
            dimensions_json TEXT NOT NULL,
            provenance TEXT NOT NULL,
            measurement_scope TEXT NOT NULL,
            estimated INTEGER NOT NULL CHECK(estimated IN (0,1)),
            UNIQUE(source_event_id,metric_key),
            FOREIGN KEY(source_event_id) REFERENCES observability_events(event_id) ON DELETE RESTRICT,
            FOREIGN KEY(metric_key) REFERENCES observability_metric_catalogue(metric_key)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_metrics_key_time ON observability_metric_observations(metric_key,timestamp DESC,observation_id DESC);
        CREATE TABLE IF NOT EXISTS observability_security_audit (
            sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            outcome TEXT NOT NULL,
            actor_ref TEXT NOT NULL DEFAULT '',
            component TEXT NOT NULL,
            correlation_id TEXT,
            details_json TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            integrity_hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_obs_security_time ON observability_security_audit(timestamp DESC,sequence_id DESC);
        CREATE INDEX IF NOT EXISTS idx_obs_security_actor_time ON observability_security_audit(actor_ref,timestamp DESC,sequence_id DESC);
        CREATE TRIGGER IF NOT EXISTS trg_obs_security_no_update BEFORE UPDATE ON observability_security_audit BEGIN SELECT RAISE(ABORT,'security audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_obs_security_no_delete BEFORE DELETE ON observability_security_audit BEGIN SELECT RAISE(ABORT,'security audit is append-only'); END;
        CREATE TABLE IF NOT EXISTS observability_report_jobs (
            job_id TEXT PRIMARY KEY,
            report_type TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','complete','failed','cancelled','expired')),
            parameters_json TEXT NOT NULL,
            progress_percent INTEGER NOT NULL DEFAULT 0 CHECK(progress_percent BETWEEN 0 AND 100),
            output_name TEXT,
            output_sha256 TEXT,
            output_size_bytes INTEGER,
            expires_at TEXT,
            error_code TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_obs_reports_requested ON observability_report_jobs(requested_at DESC);
        CREATE TABLE IF NOT EXISTS observability_export_audit (
            export_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            report_job_id TEXT,
            actor_ref TEXT NOT NULL,
            report_type TEXT NOT NULL,
            format TEXT NOT NULL,
            filters_json TEXT NOT NULL,
            FOREIGN KEY(report_job_id) REFERENCES observability_report_jobs(job_id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS observability_siem_destinations (
            destination_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            transport TEXT NOT NULL CHECK(transport IN ('https_json','syslog_tls','otlp')),
            endpoint TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            secret_ref TEXT,
            redaction_profile TEXT NOT NULL DEFAULT 'default',
            event_format TEXT NOT NULL DEFAULT 'json' CHECK(event_format IN ('json','ndjson','cef','ecs')),
            archived_at TEXT,
            dropped_events INTEGER NOT NULL DEFAULT 0 CHECK(dropped_events>=0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observability_delivery_events (
            delivery_id TEXT PRIMARY KEY,
            destination_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','sent','retrying','dead_letter','disabled')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_status_code INTEGER,
            last_error_code TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(destination_id,event_id),
            FOREIGN KEY(destination_id) REFERENCES observability_siem_destinations(destination_id) ON DELETE RESTRICT,
            FOREIGN KEY(event_id) REFERENCES observability_events(event_id) ON DELETE RESTRICT
        );
        CREATE INDEX IF NOT EXISTS idx_obs_delivery_status_time ON observability_delivery_events(status,next_attempt_at);
        CREATE TABLE IF NOT EXISTS observability_saved_log_filters (
            owner_hash TEXT NOT NULL,
            filter_id TEXT NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE CHECK(length(name) BETWEEN 1 AND 80),
            filters_json TEXT NOT NULL CHECK(length(filters_json)<=8192),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(owner_hash,filter_id),
            UNIQUE(owner_hash,name)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_saved_log_filters_owner ON observability_saved_log_filters(owner_hash,name COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS observability_gpu_energy_state (
            device_id TEXT PRIMARY KEY,
            model_id TEXT NOT NULL,
            last_power_w REAL NOT NULL CHECK(last_power_w>=0),
            last_sample_at TEXT NOT NULL,
            lifetime_energy_wh REAL NOT NULL DEFAULT 0 CHECK(lifetime_energy_wh>=0),
            coverage_seconds REAL NOT NULL DEFAULT 0 CHECK(coverage_seconds>=0),
            gap_count INTEGER NOT NULL DEFAULT 0 CHECK(gap_count>=0)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_gpu_energy_sample ON observability_gpu_energy_state(last_sample_at DESC);
        """
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            if statement.strip():
                db.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Observability schema contains an incomplete SQL statement.")
    db.execute(
        "CREATE TABLE IF NOT EXISTS observability_retention_policy ("
        "singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),"
        "event_details_days INTEGER NOT NULL CHECK(event_details_days IN (30,90,180,365)),"
        "metric_observations_max_days INTEGER NOT NULL CHECK(metric_observations_max_days IN (30,90,180,365)),"
        "delivery_history_days INTEGER NOT NULL CHECK(delivery_history_days IN (30,90,180,365)),"
        "updated_at TEXT NOT NULL)"
    )
    db.execute(
        "INSERT OR IGNORE INTO observability_retention_policy"
        "(singleton_id,event_details_days,metric_observations_max_days,delivery_history_days,updated_at)"
        " VALUES(1,90,365,90,?)",
        (utc_now(),),
    )
    event_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(observability_events)").fetchall()}
    if "expires_at" not in event_columns:
        db.execute("ALTER TABLE observability_events ADD COLUMN expires_at TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_obs_events_expiry ON observability_events(expires_at)")
    siem_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(observability_siem_destinations)").fetchall()}
    if "event_format" not in siem_columns:
        db.execute("ALTER TABLE observability_siem_destinations ADD COLUMN event_format TEXT NOT NULL DEFAULT 'json' CHECK(event_format IN ('json','ndjson','cef','ecs'))")
    if "archived_at" not in siem_columns:
        db.execute("ALTER TABLE observability_siem_destinations ADD COLUMN archived_at TEXT")
    if "dropped_events" not in siem_columns:
        db.execute("ALTER TABLE observability_siem_destinations ADD COLUMN dropped_events INTEGER NOT NULL DEFAULT 0 CHECK(dropped_events>=0)")
    session_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(observability_runtime_sessions)").fetchall()}
    if "process_instance_id" not in session_columns:
        db.execute("ALTER TABLE observability_runtime_sessions ADD COLUMN process_instance_id TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_obs_sessions_process_instance ON observability_runtime_sessions(process_id,component,process_instance_id,exit_reason,last_heartbeat_at DESC)")


def initialize_database(db_path: str | Path) -> Path | None:
    """Apply additive, versioned schema after making a verified pre-change backup."""
    path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    with _connection(path) as db:
        if _current_schema_version(db) >= SCHEMA_VERSION:
            return None
    if path.is_file() and path.stat().st_size:
        backup = _verified_backup(path)
        if backup is None:
            raise RuntimeError("Refusing to migrate a non-empty VAOS database without a verified backup.")
    with _connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if _current_schema_version(db) >= SCHEMA_VERSION:
                db.rollback()
                return backup
            _create_schema(db)
            db.executemany(
                "INSERT INTO observability_metric_catalogue(metric_key,display_name,description,unit,metric_type,dimensions_json,aggregation,source,measurement_scope,estimated,privacy_classification,retention,sampling_interval_seconds,available) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(metric_key) DO UPDATE SET display_name=excluded.display_name,description=excluded.description,unit=excluded.unit,metric_type=excluded.metric_type,dimensions_json=excluded.dimensions_json,aggregation=excluded.aggregation,source=excluded.source,measurement_scope=excluded.measurement_scope,estimated=excluded.estimated,privacy_classification=excluded.privacy_classification,retention=excluded.retention,sampling_interval_seconds=excluded.sampling_interval_seconds,available=excluded.available",
                [(m["key"], m["name"], m["description"], m["unit"], m["type"], _json(m["dimensions"]), m["aggregation"], m["source"], m["scope"], int(m["estimated"]), m["privacy"], m["retention"], m["sampling_seconds"], int(m["available"])) for m in _METRICS],
            )
            db.execute("UPDATE observability_installation SET schema_version=? WHERE singleton_id=1", (SCHEMA_VERSION,))
            applied_at = utc_now()
            db.execute("INSERT INTO vaos_schema_migrations(version,applied_at,backup_name) VALUES(?,?,?)", (SCHEMA_VERSION, applied_at, backup.name if backup else None))
            if backup is not None:
                _insert_event(
                    db,
                    event_id=f"observability-migration-backup-v{SCHEMA_VERSION}",
                    timestamp=applied_at,
                    event_type="storage.schema_migration_backup_verified",
                    component="observability",
                    level="INFO",
                    message="A verified pre-migration database backup was recorded with the successful schema upgrade.",
                    session_id=None,
                    correlation_id=None,
                    details={
                        "schema_version": SCHEMA_VERSION,
                        "backup_bytes": backup.stat().st_size,
                        "integrity_check": "ok",
                    },
                    counter_deltas={"storage.schema_migration_backups": 1},
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return backup


def _insert_event(
    db: sqlite3.Connection,
    *,
    event_id: str,
    timestamp: str,
    event_type: str,
    component: str,
    level: str,
    message: str,
    session_id: str | None,
    correlation_id: str | None,
    details: dict[str, Any],
    counter_deltas: dict[str, int] | None = None,
    counter_dimensions: dict[str, dict[str, Any]] | None = None,
) -> bool:
    if not _EVENT_SLUG.fullmatch(event_type):
        raise ValueError("Event type must be a lowercase event key.")
    if not _EVENT_ID.fullmatch(event_id):
        raise ValueError("Event ID must be an opaque identifier of at most 160 safe characters.")
    safe_details = redact_details(details)
    details_json = _json(safe_details)
    if len(details_json.encode("utf-8")) > MAX_EVENT_DETAILS_BYTES:
        raise ValueError("Event metadata exceeds the 16 KB limit.")
    retention = db.execute(
        "SELECT event_details_days FROM observability_retention_policy WHERE singleton_id=1"
    ).fetchone()
    event_details_days = int(retention["event_details_days"]) if retention else 90
    inserted = db.execute(
        "INSERT OR IGNORE INTO observability_events(event_id,timestamp,event_type,component,level,message,session_id,correlation_id,details_json,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (event_id, timestamp, event_type, _safe_text(component, 80), _safe_text(level.upper(), 16), _safe_text(message), session_id, _safe_text(correlation_id, 120) if correlation_id else None, details_json, (_parse_time(timestamp) + timedelta(days=event_details_days)).isoformat(timespec="microseconds").replace("+00:00", "Z")),
    ).rowcount
    if not inserted:
        return False
    for metric_key, raw_delta in (counter_deltas or {}).items():
        metric = METRIC_CATALOGUE.get(metric_key)
        if not metric or metric["type"] != "counter":
            raise ValueError(f"Unknown counter metric: {metric_key}")
        delta = int(raw_delta)
        if delta < 0 or delta > 2**53:
            raise ValueError("Counter increments must be non-negative and bounded.")
        dimensions = (counter_dimensions or {}).get(metric_key, {})
        if not isinstance(dimensions, dict) or len(dimensions) > MAX_METRIC_DIMENSIONS or set(dimensions) - set(metric["dimensions"]):
            raise ValueError(f"Unsupported counter dimensions for {metric_key}.")
        if any(not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value) or isinstance(value, str) and len(value) > 120 for value in dimensions.values()):
            raise ValueError("Counter dimensions must contain only finite numbers, booleans, or short text values.")
        safe_dimensions = redact_details(dimensions)
        if len(_json(safe_dimensions).encode("utf-8")) > 4096:
            raise ValueError("Counter dimensions exceed the 4 KB limit.")
        db.execute("INSERT INTO observability_counter_events(event_id,metric_key,timestamp,delta,dimensions_json) VALUES(?,?,?,?,?)", (event_id, metric_key, timestamp, delta, _json(safe_dimensions)))
        db.execute("INSERT INTO observability_lifetime_counters(metric_key,value,updated_at) VALUES(?,?,?) ON CONFLICT(metric_key) DO UPDATE SET value=value+excluded.value,updated_at=excluded.updated_at", (metric_key, delta, timestamp))
        db.execute("UPDATE observability_metric_catalogue SET available=1 WHERE metric_key=?", (metric_key,))
    return True


def record_event(
    db_path: str | Path,
    *,
    event_type: str,
    component: str,
    level: str = "INFO",
    message: str = "",
    details: dict[str, Any] | None = None,
    session_id: str | None = None,
    correlation_id: str | None = None,
    event_id: str | None = None,
    counter_deltas: dict[str, int] | None = None,
    counter_dimensions: dict[str, dict[str, Any]] | None = None,
    metric_observations: list[dict[str, Any]] | None = None,
    timestamp: str | None = None,
) -> bool:
    path = Path(db_path).expanduser().resolve()
    event_id = event_id or str(uuid.uuid4())
    timestamp = _normalize_time(timestamp or utc_now())
    with _connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        inserted = _insert_event(db, event_id=event_id, timestamp=timestamp, event_type=event_type, component=component, level=level, message=message, session_id=session_id, correlation_id=correlation_id, details=details or {}, counter_deltas=counter_deltas, counter_dimensions=counter_dimensions)
        if inserted:
            for observation in metric_observations or []:
                _insert_metric_observation(
                    db, metric_key=str(observation.get("metric_key", "")), value=observation.get("value"),
                    source_event_id=event_id, dimensions=observation.get("dimensions"),
                    provenance=str(observation.get("provenance", "")),
                    measurement_scope=observation.get("measurement_scope"), estimated=observation.get("estimated"),
                    timestamp=str(observation.get("timestamp") or timestamp),
                )
        db.commit()
        return inserted


def buffer_counter_delta(
    db_path: str | Path,
    *,
    metric_key: str,
    delta: int = 1,
    dimensions: dict[str, Any] | None = None,
    timestamp: str | None = None,
    bucket_seconds: int = 10,
) -> None:
    """Durably aggregate a counter delta before returning to its caller.

    The small time bucket keeps high-volume sources compact while allowing the
    background worker to convert the durable buffer into normal counter events.
    """
    metric = METRIC_CATALOGUE.get(str(metric_key))
    if not metric or metric["type"] != "counter":
        raise ValueError(f"Unknown counter metric: {metric_key}")
    if isinstance(delta, bool):
        raise ValueError("Counter increments must be non-negative and bounded.")
    increment = int(delta)
    if increment <= 0 or increment > 2**53:
        raise ValueError("Buffered counter increments must be positive and bounded.")
    dims = dimensions or {}
    if not isinstance(dims, dict) or len(dims) > MAX_METRIC_DIMENSIONS or set(dims) - set(metric["dimensions"]):
        raise ValueError(f"Unsupported counter dimensions for {metric_key}.")
    if any(not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value) or isinstance(value, str) and len(value) > 120 for value in dims.values()):
        raise ValueError("Counter dimensions must contain only finite numbers, booleans, or short text values.")
    safe_dimensions = redact_details(dims)
    dimensions_json = _json(safe_dimensions)
    if len(dimensions_json.encode("utf-8")) > 4096:
        raise ValueError("Counter dimensions exceed the 4 KB limit.")
    seconds = max(1, min(3600, int(bucket_seconds)))
    normalized_time = _normalize_time(timestamp or utc_now())
    parsed_time = _parse_time(normalized_time)
    bucket_epoch = int(parsed_time.timestamp())
    bucket_epoch -= bucket_epoch % seconds
    bucket_start = datetime.fromtimestamp(bucket_epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    path = Path(db_path).expanduser().resolve()
    # Counter writes are on request/frame paths, but a quarter-second window
    # was too short when independent admin/camera workers briefly contended.
    # Keep a finite bound; callers retain their existing fallback on failure.
    with _connection(path, timeout_seconds=1.0) as db:
        db.execute(
            "INSERT INTO observability_counter_buffer(buffer_id,metric_key,bucket_start,dimensions_json,delta,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(metric_key,bucket_start,dimensions_json) DO UPDATE SET delta=observability_counter_buffer.delta+excluded.delta,updated_at=excluded.updated_at",
            (uuid.uuid4().hex, metric_key, bucket_start, dimensions_json, increment, normalized_time),
        )


def flush_counter_buffer(db_path: str | Path, *, maximum: int = 250) -> int:
    """Atomically convert durable buffered counters to standard event history."""
    path = Path(db_path).expanduser().resolve()
    limit = max(1, min(5000, int(maximum)))
    persisted = 0
    with _connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT buffer_id,metric_key,bucket_start,dimensions_json,delta FROM observability_counter_buffer ORDER BY bucket_start,buffer_id LIMIT ?",
            (limit,),
        ).fetchall()
        for row in rows:
            buffer_id = str(row["buffer_id"])
            metric_key = str(row["metric_key"])
            timestamp = str(row["bucket_start"])
            dimensions = json.loads(str(row["dimensions_json"]))
            delta = int(row["delta"])
            if metric_key == "api.requests":
                event_type, component, message, count_key = "api.request_batch", "api", "Aggregated VAOS REST request counts.", "request_count"
            elif metric_key == "inference.frames_processed":
                event_type, component, message, count_key = "inference.frame_batch", "camera", "Aggregated successfully processed inference frames.", "frame_count"
            else:
                event_type, component, message, count_key = "observability.counter_batch", "observability", "Aggregated VAOS counter values.", "counter_count"
            event_id = f"counter-buffer-{buffer_id}"
            inserted = _insert_event(
                db,
                event_id=event_id,
                timestamp=timestamp,
                event_type=event_type,
                component=component,
                level="INFO",
                message=message,
                session_id=None,
                correlation_id=None,
                details={count_key: delta},
                counter_deltas={metric_key: delta},
                counter_dimensions={metric_key: dimensions},
            )
            if not inserted:
                existing = db.execute(
                    "SELECT delta,dimensions_json FROM observability_counter_events WHERE event_id=? AND metric_key=?",
                    (event_id, metric_key),
                ).fetchone()
                if not existing or int(existing["delta"]) != delta or str(existing["dimensions_json"]) != str(row["dimensions_json"]):
                    raise RuntimeError("A buffered counter event ID conflicts with persisted history.")
            deleted = db.execute("DELETE FROM observability_counter_buffer WHERE buffer_id=?", (buffer_id,)).rowcount
            if deleted != 1:
                raise RuntimeError("A durable counter buffer row changed during flush.")
            persisted += delta
    return persisted


def counter_fallback_database_path(db_path: str | Path) -> Path:
    """Return the private, per-installation database used only during primary-store failures."""
    primary = Path(db_path).expanduser().resolve()
    return primary.parent / ".vaos-observability-fallback" / "counters.sqlite3"


@contextmanager
def _fallback_connection(path: Path, *, timeout_seconds: float = 0.25) -> Iterator[sqlite3.Connection]:
    with _connection(path, timeout_seconds=timeout_seconds) as db:
        db.execute("PRAGMA synchronous=FULL")
        page_size = max(1, int(db.execute("PRAGMA page_size").fetchone()[0]))
        configured_page_limit = max(1, FALLBACK_COUNTER_MAX_BYTES // page_size)
        page_limit = int(db.execute(f"PRAGMA max_page_count={configured_page_limit}").fetchone()[0])
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        if page_limit > configured_page_limit or page_count > configured_page_limit:
            raise RuntimeError("The VAOS observability fallback queue exceeded its SQLite page limit.")
        yield db


def initialize_counter_fallback_store(db_path: str | Path) -> Path:
    """Create a small private secondary queue for aggregated API/frame counters.

    This store contains no request bodies, camera tokens, media, or raw paths.
    It is separate from the main database so a lock or corruption in that file
    does not also prevent counters from being queued.
    """
    fallback_path = counter_fallback_database_path(db_path)
    key = str(fallback_path)
    with _FALLBACK_COUNTER_INIT_LOCK:
        if key in _FALLBACK_COUNTER_INITIALIZED and fallback_path.is_file() and not fallback_path.is_symlink() and not fallback_path.parent.is_symlink():
            return fallback_path
        if fallback_path.parent.is_symlink() or fallback_path.is_symlink():
            raise RuntimeError("Refusing to use a linked VAOS observability fallback path.")
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_private_path(fallback_path.parent, directory=True)
        with _connection(fallback_path, timeout_seconds=2.0) as db:
            db.execute("PRAGMA page_size=4096")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute(f"PRAGMA max_page_count={FALLBACK_COUNTER_MAX_BYTES // 4096}")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS fallback_counter_buffer (
                    buffer_id TEXT PRIMARY KEY,
                    metric_key TEXT NOT NULL,
                    bucket_start TEXT NOT NULL,
                    dimensions_json TEXT NOT NULL,
                    delta INTEGER NOT NULL CHECK(delta > 0),
                    updated_at TEXT NOT NULL,
                    UNIQUE(metric_key,bucket_start,dimensions_json)
                );
                CREATE TABLE IF NOT EXISTS fallback_store_metadata (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                    schema_version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO fallback_store_metadata(singleton_id,schema_version) VALUES(1,1);
                CREATE INDEX IF NOT EXISTS idx_fallback_counter_buffer_time
                    ON fallback_counter_buffer(bucket_start,buffer_id);
                CREATE TABLE IF NOT EXISTS fallback_counter_batches (
                    batch_id TEXT PRIMARY KEY,
                    metric_key TEXT NOT NULL,
                    bucket_start TEXT NOT NULL,
                    dimensions_json TEXT NOT NULL,
                    delta INTEGER NOT NULL CHECK(delta > 0),
                    created_at TEXT NOT NULL,
                    payload_version INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_fallback_counter_batches_time
                    ON fallback_counter_batches(bucket_start,batch_id);
                """
            )
            metadata = db.execute("SELECT schema_version FROM fallback_store_metadata WHERE singleton_id=1").fetchone()
            if not metadata or int(metadata["schema_version"]) != 1:
                raise RuntimeError("The VAOS observability fallback queue has an unsupported schema version.")
        _restrict_private_path(fallback_path, directory=False)
        _FALLBACK_COUNTER_INITIALIZED.add(key)
    return fallback_path


def _counter_fallback_ready(db_path: str | Path) -> Path:
    path = counter_fallback_database_path(db_path)
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise RuntimeError("The VAOS observability fallback queue is not initialized.")
    # Apply the database page bound to every connection; the row limits below
    # are the primary bound, and this is a second guard against unexpected growth.
    return path


def _fallback_dimensions_for(metric_key: str, dimensions: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    metric = METRIC_CATALOGUE.get(str(metric_key))
    fallback = _FALLBACK_COUNTER_METRICS.get(str(metric_key))
    if not metric or metric["type"] != "counter" or not fallback:
        raise ValueError("Only registered API-request and inference-frame counters can use the fallback queue.")
    dims = dimensions or {}
    if not isinstance(dims, dict) or len(dims) > MAX_METRIC_DIMENSIONS or set(dims) - set(metric["dimensions"]):
        raise ValueError(f"Unsupported counter dimensions for {metric_key}.")
    if any(
        not isinstance(value, (str, int, float, bool))
        or isinstance(value, float) and not math.isfinite(value)
        or isinstance(value, str) and len(value) > 120
        for value in dims.values()
    ):
        raise ValueError("Counter dimensions must contain only finite numbers, booleans, or short text values.")
    if metric_key == "api.requests":
        route = dims.get("route")
        method = dims.get("method")
        status = dims.get("status")
        reserved_overflow = dims == _FALLBACK_COUNTER_METRICS[metric_key]["overflow_dimensions"]
        if not reserved_overflow and (
            not isinstance(route, str)
            or not route.startswith("/api/")
            or "?" in route
            or "#" in route
            or "\\" in route
            or any(ord(character) < 32 for character in route)
            or not isinstance(method, str)
            or not re.fullmatch(r"[A-Za-z]{1,12}", method)
            or isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
        ):
            raise ValueError("Fallback API counters require a route template, method, and HTTP status only.")
    elif metric_key == "inference.frames_processed":
        source_type = dims.get("source_type")
        camera_ref = dims.get("camera_ref")
        if source_type not in {"camera", "local_video", "mixed"}:
            raise ValueError("Fallback frame counters require an approved source type.")
        if source_type == "camera" and (not isinstance(camera_ref, str) or not re.fullmatch(r"[a-f0-9]{20}", camera_ref)):
            raise ValueError("Fallback camera counters require a one-way camera reference.")
        if source_type == "local_video" and camera_ref is not None:
            raise ValueError("Local-video fallback counters cannot retain a camera or session reference.")
        if source_type == "mixed" and (camera_ref != "other" or set(dims) != {"camera_ref", "source_type"}):
            raise ValueError("Mixed frame overflow must use the reserved aggregate dimensions.")
    safe_dims = redact_details(dims)
    if len(_json(safe_dims).encode("utf-8")) > 4096:
        raise ValueError("Counter dimensions exceed the 4 KB limit.")
    overflow_dims = dict(fallback["overflow_dimensions"])
    if set(overflow_dims) - set(metric["dimensions"]):
        raise RuntimeError("The registered fallback dimensions are invalid for this counter.")
    return safe_dims, overflow_dims


def _upsert_fallback_counter(
    db: sqlite3.Connection,
    *,
    metric_key: str,
    bucket_start: str,
    dimensions_json: str,
    delta: int,
    updated_at: str,
    overflow: bool = False,
) -> None:
    if overflow:
        row = db.execute(
            "SELECT buffer_id,delta FROM fallback_counter_buffer WHERE metric_key=? AND dimensions_json=? ORDER BY bucket_start LIMIT 1",
            (metric_key, dimensions_json),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT buffer_id,delta FROM fallback_counter_buffer WHERE metric_key=? AND bucket_start=? AND dimensions_json=?",
            (metric_key, bucket_start, dimensions_json),
        ).fetchone()
    if row:
        total = int(row["delta"]) + delta
        if total > 2**53:
            raise ValueError("Fallback counter increments exceed the supported exact-integer bound.")
        db.execute(
            "UPDATE fallback_counter_buffer SET delta=?,updated_at=? WHERE buffer_id=?",
            (total, updated_at, str(row["buffer_id"])),
        )
        return
    db.execute(
        "INSERT INTO fallback_counter_buffer(buffer_id,metric_key,bucket_start,dimensions_json,delta,updated_at) VALUES(?,?,?,?,?,?)",
        (uuid.uuid4().hex, metric_key, bucket_start, dimensions_json, delta, updated_at),
    )


def buffer_counter_fallback(
    db_path: str | Path,
    *,
    metric_key: str,
    delta: int = 1,
    dimensions: dict[str, Any] | None = None,
    timestamp: str | None = None,
    bucket_seconds: int = 10,
) -> None:
    """Durably aggregate a counter after the primary SQLite buffer is unavailable.

    The secondary queue is bounded to 5,000 detailed aggregate rows plus one
    mixed-dimension overflow row per supported metric. Rows are later moved to
    an immutable, equally bounded replay queue. Overflow preserves the exact
    counter total while intentionally losing route/camera and time-bucket detail.
    """
    if isinstance(delta, bool):
        raise ValueError("Counter increments must be non-negative and bounded.")
    increment = int(delta)
    if increment <= 0 or increment > 2**53:
        raise ValueError("Fallback counter increments must be positive and bounded.")
    safe_dims, overflow_dims = _fallback_dimensions_for(metric_key, dimensions)
    seconds = max(1, min(3600, int(bucket_seconds)))
    normalized_time = _normalize_time(timestamp or utc_now())
    bucket_epoch = int(_parse_time(normalized_time).timestamp())
    bucket_epoch -= bucket_epoch % seconds
    bucket_start = datetime.fromtimestamp(bucket_epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    safe_json = _json(safe_dims)
    overflow_json = _json(redact_details(overflow_dims))
    fallback_path = _counter_fallback_ready(db_path)
    with _fallback_connection(fallback_path) as db:
        db.execute("BEGIN IMMEDIATE")
        overflow_row = db.execute(
            "SELECT 1 FROM fallback_counter_buffer WHERE metric_key=? AND dimensions_json=? LIMIT 1",
            (metric_key, overflow_json),
        ).fetchone()
        existing = db.execute(
            "SELECT 1 FROM fallback_counter_buffer WHERE metric_key=? AND bucket_start=? AND dimensions_json=?",
            (metric_key, bucket_start, safe_json),
        ).fetchone()
        overflow_json_values = tuple(
            _json(redact_details(definition["overflow_dimensions"]))
            for definition in _FALLBACK_COUNTER_METRICS.values()
        )
        current_rows = int(db.execute(
            "SELECT COUNT(*) FROM fallback_counter_buffer WHERE dimensions_json NOT IN (?,?)",
            overflow_json_values,
        ).fetchone()[0])
        is_overflow = False
        if safe_json != overflow_json and not existing and current_rows >= FALLBACK_COUNTER_BUFFER_LIMIT:
            safe_json = overflow_json
            is_overflow = True
            if overflow_row:
                prior = db.execute(
                    "SELECT bucket_start FROM fallback_counter_buffer WHERE metric_key=? AND dimensions_json=? ORDER BY bucket_start LIMIT 1",
                    (metric_key, overflow_json),
                ).fetchone()
                if prior:
                    bucket_start = str(prior["bucket_start"])
        elif safe_json == overflow_json:
            is_overflow = True
            if overflow_row:
                prior = db.execute(
                    "SELECT bucket_start FROM fallback_counter_buffer WHERE metric_key=? AND dimensions_json=? ORDER BY bucket_start LIMIT 1",
                    (metric_key, overflow_json),
                ).fetchone()
                if prior:
                    bucket_start = str(prior["bucket_start"])
        _upsert_fallback_counter(
            db,
            metric_key=metric_key,
            bucket_start=bucket_start,
            dimensions_json=safe_json,
            delta=increment,
            updated_at=normalized_time,
            overflow=is_overflow,
        )


def stage_fallback_counter_batches(db_path: str | Path, *, maximum: int = 250) -> int:
    """Move fallback aggregates to immutable replay batches in one transaction."""
    fallback_path = _counter_fallback_ready(db_path)
    limit = max(1, min(5000, int(maximum)))
    with _fallback_connection(fallback_path) as db:
        db.execute("BEGIN IMMEDIATE")
        pending_batches = int(db.execute("SELECT COUNT(*) FROM fallback_counter_batches").fetchone()[0])
        capacity = max(0, FALLBACK_COUNTER_BATCH_LIMIT - pending_batches)
        rows = db.execute(
            "SELECT buffer_id,metric_key,bucket_start,dimensions_json,delta FROM fallback_counter_buffer ORDER BY bucket_start,buffer_id LIMIT ?",
            (min(limit, capacity),),
        ).fetchall()
        staged = 0
        for row in rows:
            db.execute(
                "INSERT INTO fallback_counter_batches(batch_id,metric_key,bucket_start,dimensions_json,delta,created_at,payload_version) VALUES(?,?,?,?,?,?,1)",
                (str(row["buffer_id"]), str(row["metric_key"]), str(row["bucket_start"]), str(row["dimensions_json"]), int(row["delta"]), utc_now()),
            )
            deleted = db.execute("DELETE FROM fallback_counter_buffer WHERE buffer_id=?", (str(row["buffer_id"]),)).rowcount
            if deleted != 1:
                raise RuntimeError("A fallback counter changed while it was being staged.")
            staged += 1
        return staged


def _persist_fallback_counter_batch(db_path: str | Path, row: sqlite3.Row) -> int:
    if int(row["payload_version"]) != 1:
        raise ValueError("The fallback queue contains an unsupported counter payload version.")
    metric_key = str(row["metric_key"])
    definition = _FALLBACK_COUNTER_METRICS.get(metric_key)
    if not definition:
        raise ValueError("The fallback queue contains an unsupported metric.")
    batch_id = str(row["batch_id"])
    if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        raise ValueError("The fallback queue contains an invalid batch ID.")
    timestamp = _normalize_time(str(row["bucket_start"]))
    dimensions = json.loads(str(row["dimensions_json"]))
    if not isinstance(dimensions, dict):
        raise ValueError("The fallback queue contains invalid dimensions.")
    dimensions, _overflow = _fallback_dimensions_for(metric_key, dimensions)
    delta = int(row["delta"])
    if delta <= 0 or delta > 2**53:
        raise ValueError("The fallback queue contains an invalid counter total.")
    event_id = f"counter-fallback-{batch_id}"
    details = {str(definition["count_key"]): delta}
    with _connection(Path(db_path).expanduser().resolve(), timeout_seconds=1.0) as db:
        db.execute("BEGIN IMMEDIATE")
        inserted = _insert_event(
            db,
            event_id=event_id,
            timestamp=timestamp,
            event_type=str(definition["event_type"]),
            component=str(definition["component"]),
            level="INFO",
            message=str(definition["message"]),
            session_id=None,
            correlation_id=None,
            details=details,
            counter_deltas={metric_key: delta},
            counter_dimensions={metric_key: dimensions},
        )
        if not inserted:
            event = db.execute(
                "SELECT timestamp,event_type,component,level,message,session_id,correlation_id,details_json,expires_at FROM observability_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            expected_event = (
                timestamp,
                str(definition["event_type"]),
                str(definition["component"]),
                "INFO",
                str(definition["message"]),
                None,
                None,
                _json(redact_details(details)),
            )
            actual_event = tuple(event[key] for key in ("timestamp", "event_type", "component", "level", "message", "session_id", "correlation_id", "details_json")) if event else None
            exact_event_match = actual_event == expected_event
            retention_redacted_match = False
            if event and not exact_event_match:
                policy = db.execute(
                    "SELECT event_details_days FROM observability_retention_policy WHERE singleton_id=1"
                ).fetchone()
                if policy is None:
                    raise RuntimeError("The fallback counter replay cannot verify event retention state.")
                current_dt = _parse_time(utc_now())
                retention_cutoff = current_dt - timedelta(days=int(policy["event_details_days"]))
                event_dt = _parse_time(timestamp)
                expires_at = _parse_time(_normalize_time(str(event["expires_at"]))) if event["expires_at"] else None
                event_identity = tuple(event[key] for key in ("timestamp", "event_type", "component", "level", "session_id", "correlation_id"))
                expected_identity = (timestamp, str(definition["event_type"]), str(definition["component"]), "INFO", None, None)
                retention_redacted_match = (
                    event_identity == expected_identity
                    and str(event["message"]) == "Expired event details redacted by VAOS retention policy."
                    and str(event["details_json"]) == "{}"
                    and (event_dt < retention_cutoff or (expires_at is not None and expires_at <= current_dt))
                )
            if not exact_event_match and not retention_redacted_match:
                raise RuntimeError("A fallback counter event ID conflicts with persisted history.")
            existing_counter = db.execute(
                "SELECT delta,dimensions_json FROM observability_counter_events WHERE event_id=? AND metric_key=?",
                (event_id, metric_key),
            ).fetchone()
            if not existing_counter or int(existing_counter["delta"]) != delta or str(existing_counter["dimensions_json"]) != _json(dimensions):
                raise RuntimeError("A fallback counter event ID conflicts with its persisted counter payload.")
            counter_count = int(db.execute(
                "SELECT COUNT(*) FROM observability_counter_events WHERE event_id=?",
                (event_id,),
            ).fetchone()[0])
            if counter_count != 1:
                raise RuntimeError("A fallback counter event ID has an unexpected additional counter payload.")
    return delta


def flush_counter_fallback(db_path: str | Path, *, maximum: int = 250) -> int:
    """Replay durable fallback aggregates idempotently, preserving batches on failure."""
    fallback_path = counter_fallback_database_path(db_path)
    if not fallback_path.is_file() or fallback_path.is_symlink() or fallback_path.parent.is_symlink():
        return 0
    limit = max(1, min(5000, int(maximum)))
    stage_fallback_counter_batches(db_path, maximum=limit)
    with _fallback_connection(fallback_path) as db:
        rows = db.execute(
            "SELECT batch_id,metric_key,bucket_start,dimensions_json,delta,payload_version FROM fallback_counter_batches ORDER BY bucket_start,batch_id LIMIT ?",
            (limit,),
        ).fetchall()
    persisted = 0
    for row in rows:
        persisted += _persist_fallback_counter_batch(db_path, row)
        with _fallback_connection(fallback_path) as db:
            deleted = db.execute(
                "DELETE FROM fallback_counter_batches WHERE batch_id=? AND metric_key=? AND bucket_start=? AND dimensions_json=? AND delta=?",
                (str(row["batch_id"]), str(row["metric_key"]), str(row["bucket_start"]), str(row["dimensions_json"]), int(row["delta"])),
            ).rowcount
            if deleted not in (0, 1):
                raise RuntimeError("A replayed fallback counter batch could not be removed safely.")
            if deleted == 0 and db.execute("SELECT 1 FROM fallback_counter_batches WHERE batch_id=?", (str(row["batch_id"]),)).fetchone():
                raise RuntimeError("A fallback counter batch changed before it could be acknowledged.")
    return persisted


def counter_fallback_pending(db_path: str | Path) -> bool:
    """Report whether any durable fallback rows still need replay."""
    fallback_path = counter_fallback_database_path(db_path)
    if not fallback_path.is_file() or fallback_path.is_symlink() or fallback_path.parent.is_symlink():
        return False
    with _fallback_connection(fallback_path) as db:
        return bool(
            db.execute("SELECT 1 FROM fallback_counter_buffer LIMIT 1").fetchone()
            or db.execute("SELECT 1 FROM fallback_counter_batches LIMIT 1").fetchone()
        )


def counter_fallback_status(db_path: str | Path) -> dict[str, Any]:
    """Return bounded, path-free health details for the durable fallback queue."""
    fallback_path = counter_fallback_database_path(db_path)
    if not fallback_path.is_file() or fallback_path.is_symlink() or fallback_path.parent.is_symlink():
        return {
            "available": False,
            "pending_rows": 0,
            "pending_increments": 0,
            "overflow_rows": 0,
            "overflow_increments": 0,
            "database_bytes": 0,
            "database_limit_bytes": FALLBACK_COUNTER_MAX_BYTES,
            "live_row_limit": FALLBACK_COUNTER_BUFFER_LIMIT,
            "batch_row_limit": FALLBACK_COUNTER_BATCH_LIMIT,
        }
    with _fallback_connection(fallback_path) as db:
        metadata = db.execute("SELECT schema_version FROM fallback_store_metadata WHERE singleton_id=1").fetchone()
        if not metadata or int(metadata["schema_version"]) != 1:
            raise RuntimeError("The VAOS observability fallback queue has an unsupported schema version.")
        live_overflow = tuple(
            (metric_key, _json(redact_details(definition["overflow_dimensions"])))
            for metric_key, definition in _FALLBACK_COUNTER_METRICS.items()
        )
        live_rows = db.execute("SELECT metric_key,dimensions_json,delta FROM fallback_counter_buffer").fetchall()
        batch_rows = db.execute("SELECT metric_key,dimensions_json,delta FROM fallback_counter_batches").fetchall()
    all_rows = [(str(row["metric_key"]), str(row["dimensions_json"]), int(row["delta"])) for row in (*live_rows, *batch_rows)]
    overflow_keys = set(live_overflow)
    overflow = [item for item in all_rows if (item[0], item[1]) in overflow_keys]
    size_bytes = 0
    for entry in fallback_path.parent.iterdir():
        try:
            if entry.is_file() and not entry.is_symlink():
                size_bytes += entry.stat().st_size
        except OSError:
            continue
    with closing(sqlite3.connect(fallback_path, timeout=0.25)) as check:
        page_size = int(check.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(check.execute("PRAGMA page_count").fetchone()[0])
    return {
        "available": True,
        "pending_rows": len(all_rows),
        "pending_increments": sum(item[2] for item in all_rows),
        "overflow_rows": len(overflow),
        "overflow_increments": sum(item[2] for item in overflow),
        "database_bytes": max(size_bytes, page_size * page_count),
        "database_limit_bytes": FALLBACK_COUNTER_MAX_BYTES,
        "live_row_limit": FALLBACK_COUNTER_BUFFER_LIMIT,
        "batch_row_limit": FALLBACK_COUNTER_BATCH_LIMIT,
    }


def _insert_metric_observation(
    db: sqlite3.Connection, *, metric_key: str, value: Any, source_event_id: str,
    dimensions: dict[str, Any] | None, provenance: str, measurement_scope: str | None,
    estimated: bool | None, timestamp: str,
) -> bool:
    metric = METRIC_CATALOGUE.get(metric_key)
    if not metric or metric["type"] not in {"gauge", "histogram"}:
        raise ValueError("Metric key must name a registered gauge or histogram.")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("Metric values must be finite numbers.")
    dims = dimensions or {}
    if not isinstance(dims, dict) or len(dims) > MAX_METRIC_DIMENSIONS:
        raise ValueError("Metric dimensions must be a small object with at most 12 fields.")
    if any(not isinstance(item, (str, int, float, bool)) or isinstance(item, float) and not math.isfinite(item) or isinstance(item, str) and len(item) > 120 for item in dims.values()):
        raise ValueError("Metric dimensions must contain only finite numbers, booleans, or short text values.")
    unsupported = set(dims) - set(metric["dimensions"])
    if unsupported:
        raise ValueError(f"Unsupported dimensions for {metric_key}: {', '.join(sorted(unsupported))}")
    safe_dims = redact_details(dims)
    if len(_json(safe_dims).encode("utf-8")) > 4096:
        raise ValueError("Metric dimensions exceed the 4 KB limit.")
    timestamp = _normalize_time(timestamp)
    if not db.execute("SELECT 1 FROM observability_events WHERE event_id=?", (source_event_id,)).fetchone():
        raise ValueError("A metric observation must reference a stored source event.")
    inserted = db.execute(
        "INSERT OR IGNORE INTO observability_metric_observations(source_event_id,metric_key,timestamp,value,dimensions_json,provenance,measurement_scope,estimated) VALUES(?,?,?,?,?,?,?,?)",
        (source_event_id, metric_key, timestamp, numeric, _json(safe_dims), _safe_text(provenance, 160), _safe_text(measurement_scope or metric["scope"], 80), int(metric["estimated"] if estimated is None else bool(estimated))),
    ).rowcount
    if inserted:
        db.execute("UPDATE observability_metric_catalogue SET available=1 WHERE metric_key=?", (metric_key,))
    return bool(inserted)


def record_metric(
    db_path: str | Path,
    *,
    metric_key: str,
    value: float,
    source_event_id: str,
    dimensions: dict[str, Any] | None = None,
    provenance: str,
    measurement_scope: str | None = None,
    estimated: bool | None = None,
    timestamp: str | None = None,
) -> bool:
    timestamp = _normalize_time(timestamp or utc_now())
    with _connection(Path(db_path).expanduser().resolve()) as db:
        db.execute("BEGIN IMMEDIATE")
        inserted = _insert_metric_observation(db, metric_key=metric_key, value=value, source_event_id=source_event_id, dimensions=dimensions, provenance=provenance, measurement_scope=measurement_scope, estimated=estimated, timestamp=timestamp)
        db.commit()
        return inserted


def record_gpu_power_samples(
    db_path: str | Path,
    *,
    samples: list[dict[str, Any]],
    timestamp: str | None = None,
    session_id: str | None = None,
) -> dict[str, int]:
    """Persist measured NVIDIA board power and integrate only covered intervals.

    Power is an instantaneous hardware-sensor reading. Energy uses the
    trapezoidal rule between readings 20–90 seconds apart; shorter duplicate
    samples are ignored and longer gaps reset the baseline without estimating
    the missing interval. Device IDs must already be one-way hashed by the
    caller so raw hardware UUIDs never enter the observability store.
    """
    normalized_time = _normalize_time(timestamp or utc_now())
    if not isinstance(samples, list) or len(samples) > 64:
        raise ValueError("GPU power samples must be a list of at most 64 devices.")
    validated: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    for item in samples:
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("device_id", "")).strip().lower()
        model_id = _safe_text(item.get("model_id", "NVIDIA GPU"), 120) or "NVIDIA GPU"
        try:
            power_w = float(item.get("power_draw_w"))
        except (TypeError, ValueError):
            continue
        if not re.fullmatch(r"[a-f0-9]{24,64}", device_id) or not math.isfinite(power_w) or power_w < 0 or power_w > 10_000:
            continue
        if device_id in seen:
            raise ValueError("GPU power samples must contain each hashed device ID only once.")
        seen.add(device_id)
        validated.append((device_id, model_id, power_w))

    result = {"power_samples": 0, "energy_intervals": 0, "energy_gaps": 0, "ignored_samples": len(samples) - len(validated)}
    if not validated:
        return result

    current_dt = _parse_time(normalized_time)
    path = Path(db_path).expanduser().resolve()
    with _connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        for device_id, model_id, power_w in validated:
            previous = db.execute(
                "SELECT model_id,last_power_w,last_sample_at,lifetime_energy_wh,coverage_seconds,gap_count "
                "FROM observability_gpu_energy_state WHERE device_id=?",
                (device_id,),
            ).fetchone()
            interval_seconds: float | None = None
            interval_wh: float | None = None
            gap = False
            old_energy_wh = float(previous["lifetime_energy_wh"]) if previous else 0.0
            new_energy_wh = old_energy_wh
            coverage_seconds = float(previous["coverage_seconds"]) if previous else 0.0
            gap_count = int(previous["gap_count"]) if previous else 0
            if previous:
                interval_seconds = (current_dt - _parse_time(str(previous["last_sample_at"]))).total_seconds()
                if interval_seconds <= 0 or interval_seconds < GPU_POWER_MIN_INTERVAL_SECONDS:
                    result["ignored_samples"] += 1
                    continue
                if interval_seconds > GPU_POWER_MAX_INTERVAL_SECONDS:
                    gap = True
                    gap_count += 1
                else:
                    interval_wh = ((float(previous["last_power_w"]) + power_w) / 2.0) * interval_seconds / 3600.0
                    new_energy_wh += interval_wh
                    coverage_seconds += interval_seconds

            whole_wh_delta = max(0, math.floor(new_energy_wh + 1e-12) - math.floor(old_energy_wh + 1e-12)) if interval_wh is not None else 0
            event_digest = hashlib.sha256(f"{device_id}|{normalized_time}".encode("utf-8")).hexdigest()
            event_id = f"gpu-power-{event_digest}"
            event_details = {
                "device_id": device_id,
                "model_id": model_id,
                "sensor": "nvidia-smi power.draw",
                "power_draw_w": round(power_w, 4),
            }
            if interval_seconds is not None:
                event_details["sample_interval_seconds"] = round(interval_seconds, 3)
            if gap:
                event_details["energy_interval_skipped"] = True
            if interval_wh is not None:
                event_details["integrated_interval_wh"] = round(interval_wh, 9)
            inserted = _insert_event(
                db,
                event_id=event_id,
                timestamp=normalized_time,
                event_type="hardware.gpu_energy_gap" if gap else "hardware.gpu_power_sample",
                component="hardware",
                level="WARNING" if gap else "INFO",
                message=("A long gap between GPU power samples was skipped; energy baseline reset." if gap else "A measured NVIDIA GPU board-power sample was recorded."),
                session_id=session_id,
                correlation_id=None,
                details=event_details,
                counter_deltas={"hardware.gpu_energy_wh": whole_wh_delta} if whole_wh_delta else None,
                counter_dimensions={"hardware.gpu_energy_wh": {"device_id": device_id, "model_id": model_id}} if whole_wh_delta else None,
            )
            if not inserted:
                result["ignored_samples"] += 1
                continue
            dimensions = {"device_id": device_id, "model_id": model_id}
            _insert_metric_observation(
                db,
                metric_key="hardware.gpu_power_draw_w",
                value=power_w,
                source_event_id=event_id,
                dimensions=dimensions,
                provenance="Measured NVIDIA nvidia-smi power.draw sensor",
                measurement_scope="GPU board power only; not wall or whole-system power",
                estimated=False,
                timestamp=normalized_time,
            )
            result["power_samples"] += 1
            if interval_wh is not None:
                _insert_metric_observation(
                    db,
                    metric_key="hardware.gpu_energy_interval_wh",
                    value=interval_wh,
                    source_event_id=event_id,
                    dimensions=dimensions,
                    provenance="Trapezoidal integration of consecutive measured GPU board-power readings",
                    measurement_scope=f"Estimated interval over {interval_seconds:.1f} seconds; long gaps skipped; no training-job attribution",
                    estimated=True,
                    timestamp=normalized_time,
                )
                db.execute("UPDATE observability_metric_catalogue SET available=1 WHERE metric_key='hardware.gpu_energy_wh'")
                db.execute(
                    "INSERT INTO observability_gpu_energy_state(device_id,model_id,last_power_w,last_sample_at,lifetime_energy_wh,coverage_seconds,gap_count) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET model_id=excluded.model_id,last_power_w=excluded.last_power_w,last_sample_at=excluded.last_sample_at,lifetime_energy_wh=excluded.lifetime_energy_wh,coverage_seconds=excluded.coverage_seconds,gap_count=excluded.gap_count",
                    (device_id, model_id, power_w, normalized_time, new_energy_wh, coverage_seconds, gap_count),
                )
                result["energy_intervals"] += 1
            else:
                if gap:
                    result["energy_gaps"] += 1
                db.execute(
                    "INSERT INTO observability_gpu_energy_state(device_id,model_id,last_power_w,last_sample_at,lifetime_energy_wh,coverage_seconds,gap_count) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET model_id=excluded.model_id,last_power_w=excluded.last_power_w,last_sample_at=excluded.last_sample_at,lifetime_energy_wh=excluded.lifetime_energy_wh,coverage_seconds=excluded.coverage_seconds,gap_count=excluded.gap_count",
                    (device_id, model_id, power_w, normalized_time, new_energy_wh, coverage_seconds, gap_count),
                )
        db.commit()
    return result


def start_session(
    db_path: str | Path,
    *,
    component: str,
    app_version: str,
    now: str | None = None,
    process_id: int | None = None,
    process_instance_id: str | None = None,
) -> tuple[str, str]:
    path = Path(db_path).expanduser().resolve()
    now = _normalize_time(now or utc_now())
    current = _parse_time(now)
    process_id = int(os.getpid() if process_id is None else process_id)
    process_instance_id = _safe_text(process_instance_id or _PROCESS_INSTANCE_ID, 80)
    component = _safe_text(component, 80)
    session_id = str(uuid.uuid4())
    with _connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        installation = db.execute("SELECT installation_id FROM observability_installation WHERE singleton_id=1").fetchone()
        if not installation:
            installation_id = str(uuid.uuid4())
            db.execute("INSERT INTO observability_installation(singleton_id,installation_id,first_started_at,schema_version) VALUES(1,?,?,?)", (installation_id, now, SCHEMA_VERSION))
        else:
            installation_id = str(installation["installation_id"])
        stale_before = (current - timedelta(seconds=UNKNOWN_SESSION_AFTER_SECONDS)).isoformat(timespec="seconds").replace("+00:00", "Z")
        stale_sessions = db.execute(
            "SELECT session_id,last_heartbeat_at,process_id,component,process_instance_id "
            "FROM observability_runtime_sessions WHERE exit_reason='running' AND "
            "(last_heartbeat_at<? OR (process_id=? AND component=? AND "
            "(process_instance_id IS NULL OR process_instance_id<>?)))",
            (stale_before, process_id, component, process_instance_id),
        ).fetchall()
        for stale in stale_sessions:
            db.execute("UPDATE observability_runtime_sessions SET exit_reason='unknown' WHERE session_id=? AND exit_reason='running'", (stale["session_id"],))
            same_process_restarted = int(stale["process_id"]) == process_id and str(stale["component"]) == component and stale["process_instance_id"] != process_instance_id
            _insert_event(
                db,
                event_id=f"{stale['session_id']}:unknown",
                timestamp=now,
                event_type="lifecycle.session_reconciled",
                component=component,
                level="WARNING",
                message="A previous runtime session could not be confirmed as active; its exit reason is unknown.",
                session_id=str(stale["session_id"]),
                correlation_id=None,
                details={
                    "last_heartbeat_at": stale["last_heartbeat_at"],
                    "reconciliation": "process_instance_changed" if same_process_restarted else "stale_heartbeat",
                },
            )
        existing = db.execute(
            "SELECT session_id FROM observability_runtime_sessions WHERE process_id=? AND component=? "
            "AND process_instance_id=? AND exit_reason='running' AND last_heartbeat_at>=? "
            "ORDER BY started_at DESC LIMIT 1",
            (process_id, component, process_instance_id, stale_before),
        ).fetchone()
        if existing:
            db.execute("UPDATE observability_installation SET schema_version=? WHERE singleton_id=1", (SCHEMA_VERSION,))
            db.commit()
            return str(existing["session_id"]), installation_id
        db.execute("INSERT INTO observability_runtime_sessions(session_id,component,process_id,process_instance_id,started_at,last_heartbeat_at,clean_shutdown_at,exit_reason,app_version,installation_id) VALUES(?,?,?,?,?,?,NULL,'running',?,?)", (session_id, component, process_id, process_instance_id, now, now, _safe_text(app_version, 40), installation_id))
        db.execute("UPDATE observability_installation SET schema_version=? WHERE singleton_id=1", (SCHEMA_VERSION,))
        _insert_event(db, event_id=f"{session_id}:started", timestamp=now, event_type="lifecycle.session_started", component=component, level="INFO", message="VAOS runtime session started.", session_id=session_id, correlation_id=None, details={"app_version": app_version}, counter_deltas={"app.starts": 1}, counter_dimensions={"app.starts": {"component": component}})
        db.commit()
        return session_id, installation_id


def heartbeat_session(db_path: str | Path, session_id: str, *, now: str | None = None) -> bool:
    now = _normalize_time(now or utc_now())
    with _connection(Path(db_path).expanduser().resolve()) as db:
        result = db.execute("UPDATE observability_runtime_sessions SET last_heartbeat_at=? WHERE session_id=? AND exit_reason='running'", (now, session_id))
        return bool(result.rowcount)


def end_session(db_path: str | Path, session_id: str, *, now: str | None = None) -> bool:
    now = _normalize_time(now or utc_now())
    with _connection(Path(db_path).expanduser().resolve()) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT component FROM observability_runtime_sessions WHERE session_id=? AND exit_reason='running'", (session_id,)).fetchone()
        if not row:
            db.commit()
            return False
        db.execute("UPDATE observability_runtime_sessions SET last_heartbeat_at=?,clean_shutdown_at=?,exit_reason='clean' WHERE session_id=? AND exit_reason='running'", (now, now, session_id))
        component = str(row["component"])
        _insert_event(db, event_id=f"{session_id}:stopped", timestamp=now, event_type="lifecycle.session_stopped", component=component, level="INFO", message="VAOS runtime session stopped cleanly.", session_id=session_id, correlation_id=None, details={"exit_reason": "clean"}, counter_deltas={"app.clean_stops": 1}, counter_dimensions={"app.clean_stops": {"component": component}})
        db.commit()
        return True


def list_sessions(db_path: str | Path, *, limit: int = 50, after: str | None = None, before: str | None = None, cursor_before: tuple[str, str] | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    sql = "SELECT session_id,component,started_at,last_heartbeat_at,clean_shutdown_at,exit_reason,app_version FROM observability_runtime_sessions"
    clauses: list[str] = []
    args: list[Any] = []
    if after:
        after = _normalize_time(after)
        clauses.append("started_at >= ?")
        args.append(after)
    if before:
        before = _normalize_time(before)
        clauses.append("started_at < ?")
        args.append(before)
    if cursor_before:
        cursor_timestamp = _normalize_time(cursor_before[0])
        clauses.append("(started_at < ? OR (started_at = ? AND session_id < ?))")
        args.extend([cursor_timestamp, cursor_timestamp, cursor_before[1]])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at DESC,session_id DESC LIMIT ?"
    args.append(limit)
    with _connection(Path(db_path).expanduser().resolve()) as db:
        return [dict(row) for row in db.execute(sql, args).fetchall()]


def list_metrics(db_path: str | Path, *, key: str | None = None, limit: int = 100, before_id: int | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with _connection(Path(db_path).expanduser().resolve()) as db:
        if key:
            if key not in METRIC_CATALOGUE:
                raise ValueError("Unknown metric key.")
            row = db.execute("SELECT * FROM observability_metric_catalogue WHERE metric_key=?", (key,)).fetchone()
            if not row:
                return []
            item = dict(row)
            item["dimensions"] = json.loads(item.pop("dimensions_json"))
            item["estimated"] = bool(item["estimated"])
            item["available"] = bool(item["available"])
            item["series_aggregations"] = list(metric_series_aggregations(key))
            return [item]
        rows = db.execute("SELECT * FROM observability_metric_catalogue ORDER BY metric_key LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["dimensions"] = json.loads(item.pop("dimensions_json"))
            item["estimated"] = bool(item["estimated"])
            item["available"] = bool(item["available"])
            item["series_aggregations"] = list(metric_series_aggregations(item["metric_key"]))
            result.append(item)
        return result


def list_saved_log_filters(db_path: str | Path, *, owner_hash: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[a-f0-9]{64}", str(owner_hash)):
        raise ValueError("Saved-filter owner is invalid.")
    with _connection(Path(db_path).expanduser().resolve()) as db:
        rows = db.execute(
            "SELECT filter_id,name,filters_json,created_at,updated_at FROM observability_saved_log_filters WHERE owner_hash=? ORDER BY name COLLATE NOCASE,filter_id LIMIT 20",
            (owner_hash,),
        ).fetchall()
        return [
            {"filter_id": str(row["filter_id"]), "name": str(row["name"]), "filters": json.loads(str(row["filters_json"])), "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"])}
            for row in rows
        ]


def save_log_filter(db_path: str | Path, *, owner_hash: str, name: str, filters: dict[str, Any]) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", str(owner_hash)):
        raise ValueError("Saved-filter owner is invalid.")
    clean_name = str(name).strip()
    if not clean_name or len(clean_name) > 80 or any(ord(char) < 32 for char in clean_name):
        raise ValueError("Saved-filter name must contain 1–80 printable characters.")
    if not isinstance(filters, dict) or len(filters) > 12 or any(not isinstance(key, str) for key in filters):
        raise ValueError("Saved-filter settings must be a small object.")
    filters_json = _json(filters)
    if len(filters_json.encode("utf-8")) > 8192:
        raise ValueError("Saved-filter settings exceed the 8 KB limit.")
    filter_id = uuid.uuid4().hex
    now = utc_now()
    try:
        with _connection(Path(db_path).expanduser().resolve()) as db:
            db.execute("BEGIN IMMEDIATE")
            existing_count = int(db.execute("SELECT COUNT(*) FROM observability_saved_log_filters WHERE owner_hash=?", (owner_hash,)).fetchone()[0])
            if existing_count >= 20:
                raise ValueError("You can save up to 20 Log Explorer filters.")
            db.execute(
                "INSERT INTO observability_saved_log_filters(owner_hash,filter_id,name,filters_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (owner_hash, filter_id, clean_name, filters_json, now, now),
            )
            db.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError("A saved filter with that name already exists for this account.") from exc
    return {"filter_id": filter_id, "name": clean_name, "filters": filters, "created_at": now, "updated_at": now}


def delete_saved_log_filter(db_path: str | Path, *, owner_hash: str, filter_id: str) -> bool:
    if not re.fullmatch(r"[a-f0-9]{64}", str(owner_hash)) or not re.fullmatch(r"[a-f0-9]{32}", str(filter_id)):
        raise ValueError("Saved-filter identifier is invalid.")
    with _connection(Path(db_path).expanduser().resolve()) as db:
        result = db.execute("DELETE FROM observability_saved_log_filters WHERE owner_hash=? AND filter_id=?", (owner_hash, filter_id))
        return bool(result.rowcount)


def query_events(db_path: str | Path, *, limit: int = 100, after: str | None = None, before: str | None = None, cursor_before: tuple[str, str] | None = None, event_id: str | None = None, level: str | None = None, component: str | None = None, event_type: str | None = None, session_id: str | None = None, correlation_id: str | None = None, training_job_id: str | None = None, dataset_id: str | None = None, model_id: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    now = _normalize_time(utc_now())
    clauses: list[str] = []
    args: list[Any] = []
    if after:
        after = _normalize_time(after)
        clauses.append("timestamp >= ?")
        args.append(after)
    if before:
        before = _normalize_time(before)
        clauses.append("timestamp < ?")
        args.append(before)
    if event_id:
        clauses.append("event_id=?")
        args.append(_safe_text(event_id, 120))
    if level:
        if level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Unsupported log level.")
        clauses.append("level=?")
        args.append(level.upper())
    if component:
        clauses.append("component=?")
        args.append(_safe_text(component, 80))
    if event_type:
        if not _EVENT_SLUG.fullmatch(event_type):
            raise ValueError("Event type must be a lowercase event key.")
        clauses.append("event_type=?")
        args.append(event_type)
    if session_id:
        session_id = _safe_text(session_id, 120)
        clauses.append("session_id=?")
        args.append(session_id)
    if correlation_id:
        correlation_id = _safe_text(correlation_id, 120)
        clauses.append("correlation_id=?")
        args.append(correlation_id)
    for filter_name, value, detail_keys in (
        ("training_job_id", training_job_id, ("$.training_job_id", "$.run_id")),
        ("dataset_id", dataset_id, ("$.dataset_id", "$.source")),
        ("model_id", model_id, ("$.model_id", "$.initial_model")),
    ):
        if value:
            if len(str(value)) > 120:
                raise ValueError(f"{filter_name.replace('_', ' ').title()} must be 120 characters or fewer.")
            normalized = _safe_text(value, 120)
            if not normalized:
                continue
            clauses.append("(" + " OR ".join(f"json_extract(details_json, '{key}')=?" for key in detail_keys) + ")")
            args.extend([normalized] * len(detail_keys))
    if search:
        text = _safe_text(search, 200).casefold()
        clauses.append("(instr(lower(message),?)>0 OR instr(lower(event_type),?)>0 OR instr(lower(details_json),?)>0)")
        args.extend([text] * 3)
    if cursor_before:
        cursor_timestamp = _normalize_time(cursor_before[0])
        clauses.append("(timestamp < ? OR (timestamp = ? AND event_id < ?))")
        args.extend([cursor_timestamp, cursor_timestamp, _safe_text(cursor_before[1], 120)])
    with _connection(Path(db_path).expanduser().resolve()) as db:
        policy = db.execute(
            "SELECT event_details_days FROM observability_retention_policy WHERE singleton_id=1"
        ).fetchone()
        retention_days = int(policy["event_details_days"]) if policy else 90
        cutoff = (_parse_time(now) - timedelta(days=retention_days)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        clauses = ["timestamp >= ?", "(expires_at IS NULL OR expires_at > ?)"] + clauses
        args = [cutoff, now, *args]
        sql = "SELECT * FROM observability_events WHERE " + " AND ".join(clauses) + " ORDER BY timestamp DESC,event_id DESC LIMIT ?"
        args.append(limit)
        rows = db.execute(sql, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result


def overview(db_path: str | Path, *, period: str = "lifetime", after: str | None = None, before: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if period == "lifetime":
        start = None
        end = None
    elif period in {"24h", "7d", "30d"}:
        hours = {"24h": 24, "7d": 168, "30d": 720}[period]
        start = (now - timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")
        end = None
    elif period == "custom":
        if not after or not before:
            raise ValueError("Custom period requires both after and before timestamps.")
        start, end = _normalize_time(after), _normalize_time(before)
        begin_dt, end_dt = _parse_time(start), _parse_time(end)
        if begin_dt >= end_dt or end_dt - begin_dt > timedelta(days=366):
            raise ValueError("Custom range must be ordered and no longer than 366 days.")
    else:
        raise ValueError("Period must be lifetime, 24h, 7d, 30d, or custom.")
    with _connection(Path(db_path).expanduser().resolve()) as db:
        installation = db.execute("SELECT installation_id,first_started_at,schema_version FROM observability_installation WHERE singleton_id=1").fetchone()
        if period == "lifetime":
            counter_rows = db.execute("SELECT metric_key,value FROM observability_lifetime_counters").fetchall()
        else:
            clauses = []
            args: list[str] = []
            if start:
                clauses.append("timestamp >= ?")
                args.append(start)
            if end:
                clauses.append("timestamp < ?")
                args.append(end)
            counter_rows = db.execute("SELECT metric_key,SUM(delta) AS value FROM observability_counter_events WHERE " + " AND ".join(clauses) + " GROUP BY metric_key", args).fetchall()
        counters = {str(row["metric_key"]): int(row["value"] or 0) for row in counter_rows}
        session_rows = db.execute("SELECT started_at,last_heartbeat_at,clean_shutdown_at,exit_reason FROM observability_runtime_sessions").fetchall()
        intervals: list[tuple[datetime, datetime]] = []
        unknown_sessions = 0
        for row in session_rows:
            session_start = _parse_time(str(row["started_at"]))
            unknown = False
            if row["exit_reason"] == "clean" and row["clean_shutdown_at"]:
                session_end = _parse_time(str(row["clean_shutdown_at"]))
            elif row["exit_reason"] == "running":
                session_end = min(now, _parse_time(str(row["last_heartbeat_at"])) + timedelta(seconds=UNKNOWN_SESSION_AFTER_SECONDS))
            else:
                unknown = True
                session_end = _parse_time(str(row["last_heartbeat_at"]))
            segment_start = max(session_start, _parse_time(start)) if start else session_start
            segment_end = min(session_end, _parse_time(end)) if end else session_end
            if unknown and (not end or session_start < _parse_time(end)) and (not start or session_end >= _parse_time(start)):
                unknown_sessions += 1
            if segment_end > segment_start:
                intervals.append((segment_start, segment_end))
        # Installation availability is wall-clock time, not the sum of admin
        # and camera process-seconds. Merge overlapping service intervals.
        uptime = 0
        merged_start: datetime | None = None
        merged_end: datetime | None = None
        for segment_start, segment_end in sorted(intervals):
            if merged_start is None:
                merged_start, merged_end = segment_start, segment_end
            elif segment_start <= merged_end:
                merged_end = max(merged_end, segment_end)
            else:
                uptime += int((merged_end - merged_start).total_seconds())
                merged_start, merged_end = segment_start, segment_end
        if merged_start is not None and merged_end is not None:
            uptime += int((merged_end - merged_start).total_seconds())
        metric_availability = {str(row["metric_key"]): bool(row["available"]) for row in db.execute("SELECT metric_key,available FROM observability_metric_catalogue").fetchall()}
        if period == "lifetime":
            energy_state = db.execute(
                "SELECT COALESCE(SUM(lifetime_energy_wh),0) AS value,COALESCE(SUM(coverage_seconds),0) AS coverage_seconds,COALESCE(SUM(gap_count),0) AS gap_count "
                "FROM observability_gpu_energy_state"
            ).fetchone()
            energy_coverage = float(energy_state["coverage_seconds"] or 0)
            energy_gaps = int(energy_state["gap_count"] or 0)
            energy_available = energy_coverage > 0
            energy_value = float(energy_state["value"] or 0) if energy_available else None
            energy_reason = "Waiting for two supported GPU power readings at least 20 seconds apart." if metric_availability["hardware.gpu_power_draw_w"] else "No supported GPU board-power sensor readings are available."
            energy_intervals = None
        else:
            energy_after = _parse_time(start) if start else now - timedelta(hours=24)
            energy_before = _parse_time(end) if end else now
            if energy_after < now - timedelta(days=30):
                energy_value = None
                energy_available = False
                energy_coverage = None
                energy_gaps = None
                energy_intervals = None
                energy_reason = "Detailed GPU-energy intervals are retained for 30 days; the installation lifetime total remains available."
            else:
                energy_row = db.execute(
                    "SELECT COALESCE(SUM(value),0) AS value,COUNT(*) AS interval_count FROM observability_metric_observations "
                    "WHERE metric_key='hardware.gpu_energy_interval_wh' AND timestamp>=? AND timestamp<?",
                    (energy_after.isoformat(timespec="microseconds").replace("+00:00", "Z"), energy_before.isoformat(timespec="microseconds").replace("+00:00", "Z")),
                ).fetchone()
                energy_intervals = int(energy_row["interval_count"] or 0)
                energy_available = energy_intervals > 0
                energy_value = float(energy_row["value"] or 0) if energy_available else None
                energy_coverage = None
                energy_gap_row = db.execute(
                    "SELECT COUNT(*) AS gap_count FROM observability_events WHERE event_type='hardware.gpu_energy_gap' AND timestamp>=? AND timestamp<?",
                    (energy_after.isoformat(timespec="microseconds").replace("+00:00", "Z"), energy_before.isoformat(timespec="microseconds").replace("+00:00", "Z")),
                ).fetchone()
                energy_gaps = int(energy_gap_row["gap_count"] or 0)
                energy_power = db.execute(
                    "SELECT 1 FROM observability_metric_observations WHERE metric_key='hardware.gpu_power_draw_w' AND timestamp>=? AND timestamp<? LIMIT 1",
                    (energy_after.isoformat(timespec="microseconds").replace("+00:00", "Z"), energy_before.isoformat(timespec="microseconds").replace("+00:00", "Z")),
                ).fetchone()
                energy_reason = "Power is recorded, but there is not yet a complete pair of readings to integrate." if energy_power else "No supported GPU board-power readings were recorded in this date range."
        energy_kpi = {
            "value": energy_value,
            "unit": "Wh",
            "available": energy_available,
            "estimated": True,
            "quality": "estimated_sampled_power_integration" if energy_available else "unavailable",
            "measurement_method": "Trapezoidal integration of measured NVIDIA GPU board-power samples; intervals over 90 seconds are skipped.",
            "coverage_seconds": energy_coverage,
            "interval_count": energy_intervals,
            "skipped_gap_count": energy_gaps,
        }
        if not energy_available:
            energy_kpi["reason"] = energy_reason
    return {
        "period": period,
        "range": {"after": start, "before_exclusive": end, "timezone": "UTC"},
        "installation": dict(installation) if installation else None,
        "kpis": {
            "application_starts": {"value": counters.get("app.starts", 0), "unit": "starts", "available": True},
            "clean_stops": {"value": counters.get("app.clean_stops", 0), "unit": "stops", "available": True},
            "uptime": {"value": uptime, "unit": "seconds", "available": bool(session_rows), "quality": "lower_bound_unknown_sessions" if unknown_sessions else "complete", "unknown_sessions": unknown_sessions},
            "training_jobs_started": {"value": counters.get("training.jobs_started"), "unit": "jobs", "available": metric_availability["training.jobs_started"]},
            "training_jobs_completed": {"value": counters.get("training.jobs_completed"), "unit": "jobs", "available": metric_availability["training.jobs_completed"]},
            "training_jobs_failed": {"value": counters.get("training.jobs_failed"), "unit": "jobs", "available": metric_availability["training.jobs_failed"]},
            "training_epochs_completed": {"value": counters.get("training.epochs_completed"), "unit": "epochs", "available": metric_availability["training.epochs_completed"]},
            "api_requests": {"value": counters.get("api.requests"), "unit": "requests", "available": metric_availability["api.requests"]},
            "inference_frames": {"value": counters.get("inference.frames_processed"), "unit": "frames", "available": metric_availability["inference.frames_processed"]},
            "energy_wh": energy_kpi,
        },
        "updated_at": utc_now(),
    }


def _metric_dimension_filters(metric_key: str, dimensions: dict[str, Any] | None) -> tuple[list[str], list[Any]]:
    if dimensions is None:
        return [], []
    if not isinstance(dimensions, dict) or len(dimensions) > MAX_METRIC_DIMENSIONS:
        raise ValueError("Metric dimension filters must be an object with at most eight entries.")
    definition = METRIC_CATALOGUE[metric_key]
    clauses: list[str] = []
    args: list[Any] = []
    for key, value in dimensions.items():
        if key not in definition["dimensions"]:
            raise ValueError(f"Unsupported dimension for {metric_key}: {key}.")
        if not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Metric dimension values must be short text or finite scalar values.")
        text_value = str(value)
        if len(text_value) > 120:
            raise ValueError("Metric dimension values cannot exceed 120 characters.")
        clauses.append("CAST(json_extract(dimensions_json, ?) AS TEXT)=?")
        args.extend([f'$."{key}"', text_value])
    return clauses, args


def _metric_retention_cutoff(db: sqlite3.Connection, metric_key: str, *, now: str | None = None) -> str | None:
    """Return the effective observation cutoff without overriding lifetime metrics."""
    definition = METRIC_CATALOGUE[metric_key]
    match = re.fullmatch(r"(\d+) days", str(definition["retention"]))
    if not match:
        return None
    policy = db.execute(
        "SELECT metric_observations_max_days FROM observability_retention_policy WHERE singleton_id=1"
    ).fetchone()
    maximum_days = int(policy["metric_observations_max_days"]) if policy else 365
    retention_days = min(int(match.group(1)), maximum_days)
    current = _parse_time(_normalize_time(now or utc_now()))
    return (current - timedelta(days=retention_days)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def count_metric_series(db_path: str | Path, *, metric_key: str, after: str, before: str, dimensions: dict[str, Any] | None = None) -> int:
    if metric_key not in METRIC_CATALOGUE:
        raise ValueError("Unknown metric key.")
    start, end = _normalize_time(after), _normalize_time(before)
    if _parse_time(start) >= _parse_time(end) or _parse_time(end) - _parse_time(start) > timedelta(days=366):
        raise ValueError("Metric range must be ordered and no longer than 366 days.")
    clauses, args = _metric_dimension_filters(metric_key, dimensions)
    table = "observability_counter_events" if METRIC_CATALOGUE[metric_key]["type"] == "counter" else "observability_metric_observations"
    with _connection(Path(db_path).expanduser().resolve()) as db:
        cutoff = _metric_retention_cutoff(db, metric_key)
        retention_clause = ["timestamp>=?"] if cutoff else []
        retention_args = [cutoff] if cutoff else []
        where = ["metric_key=?", "timestamp>=?", "timestamp<?", *retention_clause, *clauses]
        return int(db.execute(f"SELECT COUNT(*) FROM {table} WHERE " + " AND ".join(where), [metric_key, start, end, *retention_args, *args]).fetchone()[0])


def count_metric_series_groups(
    db_path: str | Path, *, metric_key: str, group_by: str,
    after: str, before: str, dimensions: dict[str, Any] | None = None,
) -> int:
    if metric_key not in METRIC_CATALOGUE:
        raise ValueError("Unknown metric key.")
    if group_by not in METRIC_CATALOGUE[metric_key]["dimensions"]:
        raise ValueError(f"Unsupported grouping dimension for {metric_key}: {group_by}.")
    start, end = _normalize_time(after), _normalize_time(before)
    if _parse_time(start) >= _parse_time(end) or _parse_time(end) - _parse_time(start) > timedelta(days=366):
        raise ValueError("Metric range must be ordered and no longer than 366 days.")
    clauses, args = _metric_dimension_filters(metric_key, dimensions)
    table = "observability_counter_events" if METRIC_CATALOGUE[metric_key]["type"] == "counter" else "observability_metric_observations"
    with _connection(Path(db_path).expanduser().resolve()) as db:
        cutoff = _metric_retention_cutoff(db, metric_key)
        retention_clause = ["timestamp>=?"] if cutoff else []
        retention_args = [cutoff] if cutoff else []
        where = ["metric_key=?", "timestamp>=?", "timestamp<?", *retention_clause, *clauses]
        return int(db.execute(
            f"SELECT COUNT(DISTINCT COALESCE(CAST(json_extract(dimensions_json, ?) AS TEXT),'(not set)')) FROM {table} WHERE " + " AND ".join(where),
            [f'$.\"{group_by}\"', metric_key, start, end, *retention_args, *args],
        ).fetchone()[0])


def query_metric_series(db_path: str | Path, *, metric_key: str, after: str, before: str, limit: int = 500, dimensions: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return the newest bounded gauge or counter points and their dimensions."""
    if metric_key not in METRIC_CATALOGUE:
        raise ValueError("Unknown metric key.")
    start, end = _normalize_time(after), _normalize_time(before)
    if _parse_time(start) >= _parse_time(end) or _parse_time(end) - _parse_time(start) > timedelta(days=366):
        raise ValueError("Metric range must be ordered and no longer than 366 days.")
    limit = max(1, min(int(limit), 1000))
    dimension_clauses, dimension_args = _metric_dimension_filters(metric_key, dimensions)
    metric_type = METRIC_CATALOGUE[metric_key]["type"]
    if metric_type == "counter":
        table = "observability_counter_events"
        query = "SELECT timestamp,delta AS value,dimensions_json,event_id AS provenance,'counter event' AS measurement_scope,0 AS estimated FROM observability_counter_events"
        tie_breaker = "event_id"
    else:
        table = "observability_metric_observations"
        query = "SELECT timestamp,value,dimensions_json,provenance,measurement_scope,estimated FROM observability_metric_observations"
        tie_breaker = "observation_id"
    with _connection(Path(db_path).expanduser().resolve()) as db:
        cutoff = _metric_retention_cutoff(db, metric_key)
        retention_clauses = ["timestamp>=?"] if cutoff else []
        retention_args = [cutoff] if cutoff else []
        clauses = ["metric_key=?", "timestamp>=?", "timestamp<?", *retention_clauses, *dimension_clauses]
        rows = db.execute(
            query + " WHERE " + " AND ".join(clauses) + f" ORDER BY timestamp DESC,{tie_breaker} DESC LIMIT ?",
            [metric_key, start, end, *retention_args, *dimension_args, limit],
        ).fetchall()
    result = [{
        **dict(row),
        "dimensions": json.loads(row["dimensions_json"]),
        "estimated": bool(METRIC_CATALOGUE[metric_key]["estimated"] if metric_type == "counter" else row["estimated"]),
        "measurement_scope": (
            "Whole-Wh increase in lifetime trapezoidal integration; fractional Wh remain in the durable per-device total"
            if metric_key == "hardware.gpu_energy_wh" else row["measurement_scope"]
        ),
    } for row in reversed(rows)]
    return result


METRIC_SERIES_INTERVALS = {"minute": 60, "5min": 300, "hour": 3600, "day": 86400}


def metric_series_aggregations(metric_key: str) -> tuple[str, ...]:
    """Return only aggregations whose meaning is valid for this metric."""
    metric = METRIC_CATALOGUE.get(metric_key)
    if not metric:
        raise ValueError("Unknown metric key.")
    if metric["type"] == "counter":
        return ("sum",)
    if metric["aggregation"] == "interval_union":
        return ()
    choices = ["average", "min", "max"]
    if metric["aggregation"] in {"sum", "integral"}:
        choices.insert(0, "sum")
    return tuple(choices)


def query_metric_series_aggregated(
    db_path: str | Path, *, metric_key: str, after: str, before: str,
    aggregation: str, interval: str, limit: int = 500,
    dimensions: dict[str, Any] | None = None, group_by: str | None = None,
) -> dict[str, Any]:
    """Return bounded time-bucket aggregates, optionally split by one valid dimension.

    Counter events can only be summed. Gauge aggregation choices come from the
    metric's declared semantics; raw observations remain available separately.
    Group dimension values are normalized to text, matching exact-filter and
    group-count semantics for numeric and textual forms of the same value.
    """
    metric = METRIC_CATALOGUE.get(metric_key)
    if not metric:
        raise ValueError("Unknown metric key.")
    if aggregation not in metric_series_aggregations(metric_key):
        raise ValueError("This aggregation is not supported for the selected metric.")
    if interval not in METRIC_SERIES_INTERVALS:
        raise ValueError("Choose a supported metric time interval.")
    if group_by and group_by not in metric["dimensions"]:
        raise ValueError(f"Unsupported grouping dimension for {metric_key}: {group_by}.")
    start, end = _normalize_time(after), _normalize_time(before)
    if _parse_time(start) >= _parse_time(end) or _parse_time(end) - _parse_time(start) > timedelta(days=366):
        raise ValueError("Metric range must be ordered and no longer than 366 days.")
    limit = max(1, min(int(limit), 1000))
    dimension_clauses, dimension_args = _metric_dimension_filters(metric_key, dimensions)
    metric_type = metric["type"]
    table = "observability_counter_events" if metric_type == "counter" else "observability_metric_observations"
    value_column = "delta" if metric_type == "counter" else "value"
    estimate_column = "0" if metric_type == "counter" else "estimated"
    group_expression = "COALESCE(CAST(json_extract(dimensions_json, ?) AS TEXT),'(not set)')" if group_by else "'__all__'"
    group_params = [f'$."{group_by}"'] if group_by else []
    bucket_seconds = METRIC_SERIES_INTERVALS[interval]
    aggregate_sql = {"sum": "SUM", "average": "AVG", "min": "MIN", "max": "MAX"}[aggregation]
    with _connection(Path(db_path).expanduser().resolve()) as db:
        cutoff = _metric_retention_cutoff(db, metric_key)
        retention_clauses = ["timestamp>=?"] if cutoff else []
        retention_args = [cutoff] if cutoff else []
        clauses = ["metric_key=?", "timestamp>=?", "timestamp<?", *retention_clauses, *dimension_clauses]
        source = (
            "WITH source AS (SELECT timestamp,value,dimensions_json,estimated, "
            "(CAST(strftime('%s',timestamp) AS INTEGER)/?)*? AS bucket_epoch, "
            f"{group_expression} AS group_value FROM (SELECT metric_key,timestamp,{value_column} AS value,dimensions_json,{estimate_column} AS estimated FROM {table}) "
            "WHERE " + " AND ".join(clauses) + "), "
            "grouped AS (SELECT bucket_epoch,group_value," + aggregate_sql + "(value) AS value,COUNT(*) AS record_count,MAX(estimated) AS estimated "
            "FROM source GROUP BY bucket_epoch,group_value) "
        )
        args = [bucket_seconds, bucket_seconds, *group_params, metric_key, start, end, *retention_args, *dimension_args]
        total = int(db.execute(source + "SELECT COUNT(*) FROM grouped", args).fetchone()[0])
        group_count = int(db.execute(
            source + ("SELECT COUNT(*) FROM (SELECT group_value FROM source GROUP BY group_value)" if group_by else "SELECT CASE WHEN EXISTS(SELECT 1 FROM source) THEN 1 ELSE 0 END"),
            args,
        ).fetchone()[0])
        rows = db.execute(
            source + "SELECT strftime('%Y-%m-%dT%H:%M:%SZ',bucket_epoch,'unixepoch') AS timestamp,value,group_value,record_count,estimated "
            "FROM grouped ORDER BY bucket_epoch DESC,group_value ASC LIMIT ?",
            [*args, limit],
        ).fetchall()
    items = [{
        "timestamp": row["timestamp"],
        "value": row["value"],
        "dimensions": ({group_by: row["group_value"]} if group_by else {}),
        "provenance": f"{aggregation} of {row['record_count']} retained {metric_type} observation(s)",
        "measurement_scope": metric["scope"],
        "estimated": bool(row["estimated"]),
        "record_count": int(row["record_count"]),
    } for row in reversed(rows)]
    return {
        "items": items, "total": total, "group_count": group_count,
        "truncated": total > len(items), "aggregation": aggregation,
        "interval": interval, "group_by": group_by,
    }


def get_retention_policy(db_path: str | Path) -> dict[str, Any]:
    with _connection(Path(db_path).expanduser().resolve()) as db:
        row = db.execute(
            "SELECT event_details_days,metric_observations_max_days,delivery_history_days,updated_at "
            "FROM observability_retention_policy WHERE singleton_id=1"
        ).fetchone()
    if row is None:
        raise RuntimeError("Observability retention policy is not initialized.")
    return {
        "event_details_days": int(row["event_details_days"]),
        "metric_observations_max_days": int(row["metric_observations_max_days"]),
        "delivery_history_days": int(row["delivery_history_days"]),
        "updated_at": str(row["updated_at"]),
        "options": list(RETENTION_DAY_CHOICES),
    }


def set_retention_policy_in_transaction(
    db: sqlite3.Connection,
    *,
    event_details_days: int,
    metric_observations_max_days: int,
    delivery_history_days: int,
    confirm_shorter: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Update the policy inside the caller's active write transaction."""
    values = {
        "event_details_days": event_details_days,
        "metric_observations_max_days": metric_observations_max_days,
        "delivery_history_days": delivery_history_days,
    }
    if any(type(value) is not int or value not in RETENTION_DAY_CHOICES for value in values.values()):
        raise ValueError("Retention periods must be one of 30, 90, 180, or 365 days.")
    if type(confirm_shorter) is not bool:
        raise ValueError("Shorter-retention confirmation must be a boolean.")
    if not db.in_transaction:
        raise RuntimeError("Retention policy updates require an active SQLite transaction.")
    row = db.execute(
        "SELECT event_details_days,metric_observations_max_days,delivery_history_days "
        "FROM observability_retention_policy WHERE singleton_id=1"
    ).fetchone()
    if row is None:
        raise RuntimeError("Observability retention policy is not initialized.")
    shorter = [key for key, value in values.items() if value < int(row[key])]
    if shorter and not confirm_shorter:
        raise ValueError(
            "Shortening retention can permanently redact or remove older operational details; confirm the change to continue."
        )
    updated_at = _normalize_time(now or utc_now())
    db.execute(
        "UPDATE observability_retention_policy SET event_details_days=?,"
        "metric_observations_max_days=?,delivery_history_days=?,updated_at=? WHERE singleton_id=1",
        (event_details_days, metric_observations_max_days, delivery_history_days, updated_at),
    )
    return {
        **values,
        "updated_at": updated_at,
        "options": list(RETENTION_DAY_CHOICES),
        "shortened": shorter,
    }


def set_retention_policy(
    db_path: str | Path,
    *,
    event_details_days: int,
    metric_observations_max_days: int,
    delivery_history_days: int,
    confirm_shorter: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve()
    with _connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        policy = set_retention_policy_in_transaction(
            db,
            event_details_days=event_details_days,
            metric_observations_max_days=metric_observations_max_days,
            delivery_history_days=delivery_history_days,
            confirm_shorter=confirm_shorter,
            now=now,
        )
        db.commit()
        return policy


def apply_retention(db_path: str | Path, *, now: str | None = None) -> dict[str, int]:
    """Enforce metric, event and terminal SIEM delivery-history retention.

    Counter ledgers and their idempotency records remain durable. This keeps
    lifetime aggregates correct while removing old high-resolution samples and
    sensitive event text. The append-only security audit is never changed.
    """
    path = Path(db_path).expanduser().resolve()
    with _connection(path) as db:
        db.execute("BEGIN IMMEDIATE")
        return apply_retention_in_transaction(db, now=now)


def apply_retention_in_transaction(db: sqlite3.Connection, *, now: str | None = None) -> dict[str, int]:
    """Apply cleanup inside the caller's transaction so its audit can commit atomically."""
    if not db.in_transaction:
        raise RuntimeError("Retention cleanup requires an active SQLite transaction.")
    policy_row = db.execute(
        "SELECT event_details_days,metric_observations_max_days,delivery_history_days "
        "FROM observability_retention_policy WHERE singleton_id=1"
    ).fetchone()
    if policy_row is None:
        raise RuntimeError("Observability retention policy is not initialized.")
    policy = {key: int(policy_row[key]) for key in ("event_details_days", "metric_observations_max_days", "delivery_history_days")}
    current = _normalize_time(now or utc_now())
    current_dt = _parse_time(current)
    observations_removed = 0
    for metric in _METRICS:
        retention = metric["retention"]
        match = re.fullmatch(r"(\d+) days", retention)
        if not match:
            continue
        retention_days = min(int(match.group(1)), int(policy["metric_observations_max_days"]))
        cutoff = (current_dt - timedelta(days=retention_days)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        removed = db.execute("DELETE FROM observability_metric_observations WHERE metric_key=? AND timestamp<?", (metric["key"], cutoff)).rowcount
        observations_removed += max(0, removed)
    event_cutoff = (current_dt - timedelta(days=int(policy["event_details_days"]))).isoformat(timespec="microseconds").replace("+00:00", "Z")
    delivery_cutoff = (current_dt - timedelta(days=int(policy["delivery_history_days"]))).isoformat(timespec="microseconds").replace("+00:00", "Z")
    redacted_message = "Expired event details redacted by VAOS retention policy."
    redacted = db.execute(
        "UPDATE observability_events SET message=?,details_json='{}' "
        "WHERE ((expires_at IS NOT NULL AND expires_at<=?) OR timestamp<?) "
        "AND (message!=? OR details_json!='{}')",
        (redacted_message, current, event_cutoff, redacted_message),
    ).rowcount
    delivery_removed = db.execute("DELETE FROM observability_delivery_events WHERE status IN ('sent','dead_letter','disabled') AND updated_at<?", (delivery_cutoff,)).rowcount
    return {"metric_observations_removed": max(0, observations_removed), "event_details_redacted": max(0, redacted), "delivery_events_removed": max(0, delivery_removed), "security_audit_rows_changed": 0}


def _audit_hash(previous_hash: str, row: dict[str, Any]) -> str:
    fields = {key: row[key] for key in ("event_id", "timestamp", "event_type", "outcome", "actor_ref", "component", "correlation_id", "details_json")}
    payload = _json({"previous_hash": previous_hash, **fields}).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def record_security_event_in_transaction(db: sqlite3.Connection, *, event_type: str, outcome: str, component: str, actor_ref: str = "", details: dict[str, Any] | None = None, correlation_id: str | None = None, event_id: str | None = None, timestamp: str | None = None) -> bool:
    """Append one audit record using a caller-owned SQLite write transaction."""
    if not db.in_transaction:
        raise RuntimeError("Security audit insertion requires an active SQLite transaction.")
    if not _EVENT_SLUG.fullmatch(event_type):
        raise ValueError("Security event type must be a lowercase event key.")
    timestamp = _normalize_time(timestamp or utc_now())
    event_id = event_id or str(uuid.uuid4())
    if not _EVENT_ID.fullmatch(event_id):
        raise ValueError("Security event ID must be an opaque identifier of at most 160 safe characters.")
    details_json = _json(redact_details(details or {}))
    if len(details_json.encode("utf-8")) > MAX_EVENT_DETAILS_BYTES:
        raise ValueError("Security audit metadata exceeds the 16 KB limit.")
    if db.execute("SELECT 1 FROM observability_security_audit WHERE event_id=?", (event_id,)).fetchone():
        return False
    tail = db.execute("SELECT integrity_hash FROM observability_security_audit ORDER BY sequence_id DESC LIMIT 1").fetchone()
    previous_hash = str(tail["integrity_hash"]) if tail else "0" * 64
    row = {"event_id": event_id, "timestamp": timestamp, "event_type": event_type, "outcome": _safe_text(outcome, 40), "actor_ref": _safe_text(actor_ref, 120), "component": _safe_text(component, 80), "correlation_id": _safe_text(correlation_id, 120) if correlation_id else None, "details_json": details_json}
    integrity_hash = _audit_hash(previous_hash, row)
    db.execute("INSERT INTO observability_security_audit(event_id,timestamp,event_type,outcome,actor_ref,component,correlation_id,details_json,previous_hash,integrity_hash) VALUES(?,?,?,?,?,?,?,?,?,?)", (row["event_id"], row["timestamp"], row["event_type"], row["outcome"], row["actor_ref"], row["component"], row["correlation_id"], row["details_json"], previous_hash, integrity_hash))
    return True


def record_security_event(db_path: str | Path, *, event_type: str, outcome: str, component: str, actor_ref: str = "", details: dict[str, Any] | None = None, correlation_id: str | None = None, event_id: str | None = None, timestamp: str | None = None) -> bool:
    with _connection(Path(db_path).expanduser().resolve()) as db:
        db.execute("BEGIN IMMEDIATE")
        inserted = record_security_event_in_transaction(db, event_type=event_type, outcome=outcome, component=component, actor_ref=actor_ref, details=details, correlation_id=correlation_id, event_id=event_id, timestamp=timestamp)
        db.commit()
        return inserted


def query_security_audit(db_path: str | Path, *, limit: int = 100, after: str | None = None, before: str | None = None, cursor_before: tuple[str, int] | None = None, event_type: str | None = None, outcome: str | None = None, component: str | None = None, actor_ref: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
    """Read append-only security records with bounded, stable cursor pagination."""
    limit = max(1, min(int(limit), 200))
    clauses: list[str] = []
    args: list[Any] = []
    if after:
        clauses.append("timestamp >= ?")
        args.append(_normalize_time(after))
    if before:
        clauses.append("timestamp < ?")
        args.append(_normalize_time(before))
    if event_type:
        if not _EVENT_SLUG.fullmatch(event_type):
            raise ValueError("Event type must be a lowercase event key.")
        clauses.append("event_type=?")
        args.append(event_type)
    if outcome:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", outcome):
            raise ValueError("Outcome must be a lowercase key.")
        clauses.append("outcome=?")
        args.append(outcome)
    if component:
        clauses.append("component=?")
        args.append(_safe_text(component, 80))
    if actor_ref:
        if len(actor_ref) > 120:
            raise ValueError("Actor reference exceeds its 120-character limit.")
        clauses.append("actor_ref=?")
        args.append(_safe_text(actor_ref, 120))
    if search:
        text = _safe_text(search, 200).casefold()
        clauses.append("(instr(lower(event_type),?)>0 OR instr(lower(outcome),?)>0 OR instr(lower(details_json),?)>0 OR instr(lower(actor_ref),?)>0)")
        args.extend([text] * 4)
    if cursor_before:
        cursor_time = _normalize_time(cursor_before[0])
        cursor_sequence = int(cursor_before[1])
        clauses.append("(timestamp < ? OR (timestamp = ? AND sequence_id < ?))")
        args.extend([cursor_time, cursor_time, cursor_sequence])
    sql = "SELECT sequence_id,event_id,timestamp,event_type,outcome,actor_ref,component,correlation_id,details_json,previous_hash,integrity_hash FROM observability_security_audit"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY timestamp DESC,sequence_id DESC LIMIT ?"
    args.append(limit)
    with _connection(Path(db_path).expanduser().resolve()) as db:
        rows = db.execute(sql, args).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json"))
        result.append(item)
    return result


def verify_security_chain(db_path: str | Path) -> dict[str, Any]:
    with _connection(Path(db_path).expanduser().resolve()) as db:
        rows = db.execute("SELECT event_id,timestamp,event_type,outcome,actor_ref,component,correlation_id,details_json,previous_hash,integrity_hash FROM observability_security_audit ORDER BY sequence_id").fetchall()
    previous = "0" * 64
    for index, raw in enumerate(rows, start=1):
        row = dict(raw)
        if row["previous_hash"] != previous or row["integrity_hash"] != _audit_hash(previous, row):
            return {"valid": False, "checked": index, "failed_sequence": index}
        previous = row["integrity_hash"]
    return {"valid": True, "checked": len(rows), "failed_sequence": None}

