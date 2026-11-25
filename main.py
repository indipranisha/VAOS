from __future__ import annotations

import hmac
import ipaddress
import contextvars
import base64
import binascii
import csv
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import hashlib
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request
import zipfile
from collections import deque
from copy import deepcopy
from io import BytesIO
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import cv2
import numpy as np
import qrcode
import qrcode.image.svg
from hardware_intelligence import build_hardware_report
import observability_store
import observability_reports
import observability_siem
from project_bundles import BundleError, build_bundle, inspect_bundle, encrypt_bundle, decrypt_bundle, MAX_ENCRYPTED_BYTES
from project_metadata import PROJECT_METADATA, load_project_metadata
from release_updates import check_release
from runtime_compat import available_cpu_threads, default_datasets_dir, mps_is_available, resolve_inference_backend, resolve_training_device, training_resource_profile
from tracking_rules import evaluate_rules, validate_rule
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, StrictBool, StrictInt

APP_DIR = Path(__file__).resolve().parent
INSTANCE_ID = hashlib.sha256(str(APP_DIR).casefold().encode("utf-8")).hexdigest()[:16]
DATA_DIR = Path(os.getenv("VAOS_DATA_DIR", str(APP_DIR / "data"))).expanduser().resolve()
MODEL_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "app.db"
OBSERVABILITY_REPORT_DIR = DATA_DIR / "observability" / "reports"
PASSWORD_PATH = DATA_DIR / "admin_password.txt"
TUNNEL_URL_PATH = DATA_DIR / "public_camera_url.txt"
TUNNEL_STATE_PATH = DATA_DIR / "tunnel_state.json"
LAUNCHER_COMMAND_PATH = DATA_DIR / "launcher_command.json"
ADMIN_TUNNEL_STATE_PATH = DATA_DIR / "admin_tunnel_state.json"
ADMIN_TUNNEL_URL_PATH = DATA_DIR / "public_admin_url.txt"
ADMIN_TUNNEL_COMMAND_PATH = DATA_DIR / "admin_tunnel_command.json"
ADMIN_TUNNEL_MANAGER_PATH = APP_DIR / "admin_tunnel_manager.ps1"

DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vaos")

# OpenCV Zoo model files. YuNet is used for detection, SFace for matching.
MODEL_URLS = {
    # GitHub's raw URL serves the small Git LFS pointer, not the model. Use the
    # media endpoint to fetch the actual ONNX payload.
    "yunet": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "sface": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}

# Models may be placed beside main.py or under data/models. MODEL_DIR can
# override both locations without editing the source (for example, an absolute
# path supplied by the user).
MODEL_DIR = Path(os.getenv("MODEL_DIR", str(APP_DIR))).expanduser()
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATHS = {
    "yunet": MODEL_DIR / "face_detection_yunet_2023mar.onnx",
    "sface": MODEL_DIR / "face_recognition_sface_2021dec.onnx",
} 	

MODEL_MIN_BYTES = {"yunet": 100_000, "sface": 10_000_000}
# Published OpenCV Zoo model hashes. Files are additionally parsed by OpenCV.
MODEL_SHA256 = {
    "yunet": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    "sface": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
}

APP_VERSION = "1.2.18"
PRODUCTION_MODE = os.getenv("VAOS_ENV", "development").strip().casefold() == "production"
LOCAL_WINDOWS_LAUNCHER = os.name == "nt"
PUBLIC_CAMERA_URL = os.getenv("VAOS_PUBLIC_CAMERA_URL", "").strip().rstrip("/")
CAMERA_INTERNAL_URL = os.getenv("VAOS_CAMERA_INTERNAL_URL", "http://127.0.0.1:8001").strip().rstrip("/")
if PRODUCTION_MODE:
    camera_url_parts = urlsplit(PUBLIC_CAMERA_URL)
    if camera_url_parts.scheme != "https" or not camera_url_parts.hostname or camera_url_parts.username or camera_url_parts.password or camera_url_parts.path not in {"", "/"} or camera_url_parts.query or camera_url_parts.fragment:
        raise RuntimeError("Production requires VAOS_PUBLIC_CAMERA_URL set to the HTTPS camera-site origin, for example https://camera.example.com")
    internal_status_token = os.getenv("VAOS_INTERNAL_STATUS_TOKEN", "")
    if len(internal_status_token) < 32 or internal_status_token.startswith("REPLACE_"):
        raise RuntimeError("Production requires a unique VAOS_INTERNAL_STATUS_TOKEN with at least 32 characters.")
MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "0.42"))
EVENT_COOLDOWN_SECONDS = int(os.getenv("EVENT_COOLDOWN_SECONDS", "120"))
ENGINE_SETTING_DEFAULTS = {
    "face_matching_enabled": "1",
    "object_detection_enabled": "1",
    "object_tracking_enabled": "1",
    "match_threshold": str(MATCH_THRESHOLD),
    "event_cooldown_seconds": str(EVENT_COOLDOWN_SECONDS),
    "object_confidence": "0.25",
    "object_image_size": "512",
    "camera_frame_interval_ms": "500",
    "training_status_refresh_seconds": "5",
    "camera_max_dimension": "640",
}
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_ENROLLMENT_PHOTOS = 10
MAX_ENROLLMENT_TOTAL_BYTES = 30 * 1024 * 1024
MAX_ML_SAMPLES = 50_000
TRAINING_EPOCH_OPTIONS = (10, 15, 20, 25, 30, 35, 40, 45, 50)

app = FastAPI(title="VAOS (Video Analytics - Operating System)", version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None)


class TrackingInputModel(BaseModel):
    class Config:
        extra = "forbid"


class TrackingRegionInput(TrackingInputModel):
    x: float = Field(default=0, ge=0, le=1)
    y: float = Field(default=0, ge=0, le=1)
    width: float = Field(default=1, gt=0, le=1)
    height: float = Field(default=1, gt=0, le=1)


class TrackingRuleInput(TrackingInputModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: StrictBool = True
    rule_type: Literal["count_in_region", "dwell_in_region"]
    class_name: str = Field(min_length=1, max_length=80)
    min_confidence: float = Field(default=0.35, ge=0.05, le=0.99)
    threshold_count: StrictInt = Field(default=1, ge=1, le=100)
    min_consecutive_frames: StrictInt = Field(default=3, ge=1, le=120)
    cooldown_seconds: StrictInt = Field(default=60, ge=5, le=3600)
    dwell_seconds: float = Field(default=10, ge=1, le=3600)
    camera_token: str | None = Field(default=None, max_length=160)
    region: TrackingRegionInput = Field(default_factory=TrackingRegionInput)


class TrackingRulePatch(TrackingInputModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: StrictBool | None = None
    rule_type: Literal["count_in_region", "dwell_in_region"] | None = None
    class_name: str | None = Field(default=None, min_length=1, max_length=80)
    min_confidence: float | None = Field(default=None, ge=0.05, le=0.99)
    threshold_count: StrictInt | None = Field(default=None, ge=1, le=100)
    min_consecutive_frames: StrictInt | None = Field(default=None, ge=1, le=120)
    cooldown_seconds: StrictInt | None = Field(default=None, ge=5, le=3600)
    dwell_seconds: float | None = Field(default=None, ge=1, le=3600)
    camera_token: str | None = Field(default=None, max_length=160)
    region: TrackingRegionInput | None = None


class MCPInvocationInput(BaseModel):
    class Config:
        extra = "forbid"

    tool: str = Field(min_length=3, max_length=64)
    outcome: Literal["success", "error"]
    duration_ms: float = Field(ge=0, le=300_000)
    invocation_id: str | None = Field(default=None, min_length=32, max_length=32)


class ObservabilityRetentionPolicyInput(BaseModel):
    class Config:
        extra = "forbid"

    event_details_days: StrictInt
    metric_observations_max_days: StrictInt
    delivery_history_days: StrictInt
    confirm_shorter: StrictBool = False


class ObservabilityRetentionApplyInput(BaseModel):
    class Config:
        extra = "forbid"

    confirm_expired_data_redaction: StrictBool
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
security = HTTPBasic(auto_error=False)
api_bearer = HTTPBearer(auto_error=False, scheme_name="VAOSBearer", bearerFormat="VAOS API token")
observability_correlation_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vaos_observability_correlation_id", default=None
)
OBSERVABILITY_CORRELATION_HEADER = "X-VAOS-Correlation-ID"
OBSERVABILITY_CORRELATION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}\Z")


@contextmanager
def observability_request_context(request: Request):
    """Assign a bounded request correlation ID and propagate it to request events."""
    supplied = request.headers.get(OBSERVABILITY_CORRELATION_HEADER.lower(), "").strip()
    correlation_id = supplied if OBSERVABILITY_CORRELATION_ID_PATTERN.fullmatch(supplied) else secrets.token_hex(16)
    request.state.vaos_correlation_id = correlation_id
    context_token = observability_correlation_context.set(correlation_id)
    try:
        yield correlation_id
    finally:
        observability_correlation_context.reset(context_token)


def _start_context_preserving_thread(target: Any, *, name: str, args: tuple[Any, ...] = ()) -> threading.Thread:
    """Run a user-requested worker with a snapshot of its request context."""
    context = contextvars.copy_context()
    thread = threading.Thread(target=context.run, args=(target, *args), name=name, daemon=True)
    thread.start()
    return thread


@app.middleware("http")
async def versioned_api_alias(request: Request, call_next):
    """Serve the stable /api/v1 URL family through the existing handlers."""
    with observability_request_context(request) as correlation_id:
        request_started = time.perf_counter()
        original_path = request.scope.get("path", "")
        path = original_path
        if path == "/api/v1":
            request.scope["path"] = "/api"
            request.scope["raw_path"] = b"/api"
        elif path.startswith("/api/v1/"):
            suffix = path[len("/api/v1"):]
            target = "/health" if suffix == "/health" else "/api" + suffix
            request.scope["path"] = target
            request.scope["raw_path"] = target.encode("ascii", "ignore")
        response = await call_next(request)
        response.headers[OBSERVABILITY_CORRELATION_HEADER] = correlation_id
        response_elapsed_ms = max(0.0, (time.perf_counter() - request_started) * 1000.0)
        if response.status_code in {401, 403} and not getattr(request.state, "vaos_auth_audit_handled", False):
            route = getattr(request.scope.get("route"), "path", None)
            await run_in_threadpool(_record_authorization_denial, request, response.status_code, route)
        if original_path.startswith(("/api/", "/api/v1/")):
            route = getattr(request.scope.get("route"), "path", None)
            await run_in_threadpool(queue_observability_api_request, route or "unmatched", request.method, response.status_code)
            record_observability_api_latency(response_elapsed_ms)
        return response

model_lock = threading.RLock()
recognition_lock = threading.Lock()
admin_auth_lock = threading.Lock()
admin_auth_failures: dict[str, list[float]] = {}
admin_auth_audit_last: dict[str, float] = {}
admin_auth_success_audit_last: dict[str, float] = {}
authorization_denial_audit_last: dict[str, float] = {}
ADMIN_AUTH_FAILURE_LIMIT = 10
ADMIN_AUTH_FAILURE_WINDOW = 900.0
face_detector = None
face_recognizer = None
object_detector = None
object_detector_attempted = False
object_detector_last_attempt = 0.0
compute_device = "not initialized"
model_errors: dict[str, str] = {}
last_emitted: dict[tuple[str, int], float] = {}
last_object_emitted: dict[tuple[str, str], float] = {}
live_tracks: dict[str, list[dict[str, Any]]] = {}
tracking_rule_state: dict[tuple[Any, ...], dict[str, Any]] = {}
tracking_rule_state_lock = threading.Lock()
tracking_rule_cache: dict[str, Any] = {"loaded_at": 0.0, "rows": []}
tracking_rule_cache_lock = threading.Lock()
TRACKING_RULE_CACHE_SECONDS = 3.0
MAX_OBJECT_RULE_EVENTS = 5000
last_ml_capture: dict[tuple[str, str], float] = {}
OBJECT_CLASSES = ("person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush")
OBJECT_EVENTS_COOLDOWN = 60
ML_DATA_DIR = DATA_DIR / "ml_training"
ML_DATA_DIR.mkdir(parents=True, exist_ok=True)
ML_MODEL_SOURCES_DIR = ML_DATA_DIR / "model_sources"
ML_MODEL_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
HF_YOLO_REPO_ID = "Ultralytics/YOLO26"
HF_YOLO_FILES = {f"yolo26{size}.pt" for size in "nsmlx"}
ML_DATASETS_DIR = ML_DATA_DIR / "public_datasets"
ML_DATASETS_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_BUNDLE_DIR = DATA_DIR / "projects"
PROJECT_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
ML_TRAINING_LOG_PATH = ML_DATA_DIR / "training.log"
ml_training_state: dict[str, Any] = {"status": "idle", "message": "No training job has been started."}
ml_dataset_import_state: dict[str, Any] = {"status": "idle", "message": "No public dataset import is running."}
ml_dataset_prepare_state: dict[str, Any] = {"status": "idle", "message": "No local dataset validation is running."}
ml_training_lock = threading.Lock()
ml_dataset_import_lock = threading.Lock()
ml_dataset_prepare_lock = threading.Lock()
ml_training_log_lock = threading.Lock()
observability_lifecycle_lock = threading.Lock()
observability_session_id: str | None = None
observability_installation_id: str | None = None
observability_heartbeat_stop: threading.Event | None = None
observability_heartbeat_thread: threading.Thread | None = None
observability_api_lock = threading.Lock()
observability_api_latency_lock = threading.Lock()
OBSERVABILITY_API_LATENCY_SAMPLE_LIMIT = 4096
observability_api_latency_samples: deque[float] = deque(maxlen=OBSERVABILITY_API_LATENCY_SAMPLE_LIMIT)
observability_api_latency_dropped = 0
observability_inference_latency_lock = threading.Lock()
OBSERVABILITY_INFERENCE_LATENCY_SAMPLE_LIMIT = 4096
observability_inference_latency_samples: deque[float] = deque(maxlen=OBSERVABILITY_INFERENCE_LATENCY_SAMPLE_LIMIT)
observability_inference_latency_dropped = 0
observability_video_review_latency_lock = threading.Lock()
observability_video_review_latency_samples: deque[float] = deque(maxlen=OBSERVABILITY_INFERENCE_LATENCY_SAMPLE_LIMIT)
observability_video_review_latency_dropped = 0
observability_host_sample_lock = threading.Lock()
observability_host_sample_loss_count = 0
observability_host_sample_loss_metric_counts: dict[str, int] = {}
observability_host_sample_loss_event_id: str | None = None
# The private SQLite sidecar is the durable fallback when the primary counter
# buffer cannot be written. These bounded maps are the final fallback only when
# both SQLite stores are unavailable; a hard process exit can lose those counts.
observability_api_aggregates: dict[tuple[str, str, int], int] = {}
observability_frame_aggregates: dict[str, int] = {}
OBSERVABILITY_API_MAX_KEYS = 500
observability_training_lock = threading.Lock()
observability_training_queue: deque[dict[str, Any]] = deque()
observability_training_dropped = 0
observability_training_recovery_lock = threading.Lock()
observability_training_recovery_started = False
OBSERVABILITY_TRAINING_QUEUE_LIMIT = 500
OBSERVABILITY_TRAINING_LOG_MAX_BYTES = 1_000_000
OBSERVABILITY_TRAINING_LOG_ROTATIONS = 20
system_metrics_lock = threading.Lock()
system_metrics_cache: dict[str, Any] = {"sampled_at": 0.0, "data": {}}
training_devices_cache: dict[str, Any] = {"sampled_at": 0.0, "data": {}}


def _add_observability_api_counter_fallback(
    route: str,
    method: str,
    status: int,
    count: int = 1,
    *,
    target: dict[tuple[str, str, int], int] | None = None,
    max_keys: int | None = None,
) -> None:
    """Aggregate failed durable API counts without exceeding the key bound.

    Overflow uses route=other, method=OTHER, status=0 to mean a mixed bucket
    whose individual route/method/status could not be retained during storage
    unavailability. Counts remain exact while detail is intentionally coarsened.
    ``target`` and ``max_keys`` permit an isolated deterministic unit test.
    """
    aggregates = observability_api_aggregates if target is None else target
    limit = max(1, int(OBSERVABILITY_API_MAX_KEYS if max_keys is None else max_keys))
    key = (str(route)[:120], str(method).upper()[:12], int(status))
    overflow_key = ("other", "OTHER", 0)
    increment = max(0, int(count))
    if increment == 0:
        return
    with observability_api_lock:
        if key in aggregates:
            aggregates[key] += increment
            return
        if len(aggregates) >= limit:
            if overflow_key not in aggregates:
                retained = sum(aggregates.values())
                aggregates.clear()
                aggregates[overflow_key] = retained
            aggregates[overflow_key] += increment
            return
        if key != overflow_key and len(aggregates) >= limit - 1:
            aggregates[overflow_key] = aggregates.get(overflow_key, 0) + increment
            return
        aggregates[key] = aggregates.get(key, 0) + increment


def _add_observability_frame_counter_fallback(
    camera_ref: str,
    count: int = 1,
    *,
    target: dict[str, int] | None = None,
    max_keys: int | None = None,
) -> None:
    """Keep frame-counter fallback detail bounded while preserving its total."""
    aggregates = observability_frame_aggregates if target is None else target
    limit = max(1, int(OBSERVABILITY_API_MAX_KEYS if max_keys is None else max_keys))
    key = str(camera_ref)[:120]
    overflow_key = "other"
    increment = max(0, int(count))
    if increment == 0:
        return
    with observability_api_lock:
        if key in aggregates:
            aggregates[key] += increment
            return
        if len(aggregates) >= limit:
            if overflow_key not in aggregates:
                retained = sum(aggregates.values())
                aggregates.clear()
                aggregates[overflow_key] = retained
            aggregates[overflow_key] += increment
            return
        if key != overflow_key and len(aggregates) >= limit - 1:
            aggregates[overflow_key] = aggregates.get(overflow_key, 0) + increment
            return
        aggregates[key] = aggregates.get(key, 0) + increment


def record_observability_api_latency(duration_ms: float) -> None:
    """Retain a bounded recent sample for low-frequency API latency quantiles."""
    global observability_api_latency_dropped
    try:
        sample = float(duration_ms)
    except (TypeError, ValueError, OverflowError):
        return
    if not math.isfinite(sample) or sample < 0:
        return
    with observability_api_latency_lock:
        if len(observability_api_latency_samples) >= OBSERVABILITY_API_LATENCY_SAMPLE_LIMIT:
            observability_api_latency_dropped += 1
        observability_api_latency_samples.append(sample)


def record_observability_inference_latency(duration_ms: float) -> None:
    """Retain bounded successful camera-frame processing durations."""
    global observability_inference_latency_dropped
    try:
        sample = float(duration_ms)
    except (TypeError, ValueError, OverflowError):
        return
    if not math.isfinite(sample) or sample < 0:
        return
    with observability_inference_latency_lock:
        if len(observability_inference_latency_samples) >= OBSERVABILITY_INFERENCE_LATENCY_SAMPLE_LIMIT:
            observability_inference_latency_dropped += 1
        observability_inference_latency_samples.append(sample)


def record_observability_video_review_latency(duration_ms: float) -> None:
    """Retain bounded successful local-video frame-processing durations."""
    global observability_video_review_latency_dropped
    try:
        sample = float(duration_ms)
    except (TypeError, ValueError, OverflowError):
        return
    if not math.isfinite(sample) or sample < 0:
        return
    with observability_video_review_latency_lock:
        if len(observability_video_review_latency_samples) >= OBSERVABILITY_INFERENCE_LATENCY_SAMPLE_LIMIT:
            observability_video_review_latency_dropped += 1
        observability_video_review_latency_samples.append(sample)


def _queue_observability_counter_fallback(metric_key: str, delta: int, dimensions: dict[str, Any]) -> None:
    """Try the durable private sidecar before falling back to bounded process memory."""
    try:
        observability_store.buffer_counter_fallback(DB_PATH, metric_key=metric_key, delta=delta, dimensions=dimensions)
        return
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.debug("Could not durably queue a VAOS observability counter; retaining bounded memory fallback", exc_info=True)
    if metric_key == "api.requests":
        _add_observability_api_counter_fallback(
            str(dimensions.get("route", "other")),
            str(dimensions.get("method", "OTHER")),
            int(dimensions.get("status", 0)),
            delta,
        )
        return
    camera_ref = str(dimensions.get("camera_ref", ""))
    source_type = str(dimensions.get("source_type", "camera"))
    if source_type != "camera":
        camera_ref = f"source:{source_type}"
    _add_observability_frame_counter_fallback(camera_ref or "other", delta)


def queue_observability_api_request(route: str, method: str, status: int) -> None:
    """Durably aggregate REST request counts with a bounded recovery fallback."""
    route = str(route or "unmatched")[:120]
    if not route.startswith("/api/"):
        route = "unmatched"
    method_value = str(method).upper()[:12]
    status_value = max(100, min(599, int(status)))
    dimensions = {"route": route, "method": method_value, "status": status_value}
    try:
        observability_store.buffer_counter_delta(DB_PATH, metric_key="api.requests", dimensions=dimensions)
        return
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        _queue_observability_counter_fallback("api.requests", 1, dimensions)


def queue_observability_inference_frame(camera_token: str) -> None:
    """Durably count a successful frame using a one-way camera reference."""
    camera_ref = hashlib.sha256(str(camera_token).encode("utf-8", errors="replace")).hexdigest()[:20]
    dimensions = {"camera_ref": camera_ref, "source_type": "camera"}
    try:
        observability_store.buffer_counter_delta(DB_PATH, metric_key="inference.frames_processed", dimensions=dimensions)
        return
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        _queue_observability_counter_fallback("inference.frames_processed", 1, dimensions)


def queue_observability_video_review_frame() -> None:
    """Durably count local-video analysis without retaining file or session identity."""
    dimensions = {"source_type": "local_video"}
    try:
        observability_store.buffer_counter_delta(DB_PATH, metric_key="inference.frames_processed", dimensions=dimensions)
        return
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        _queue_observability_counter_fallback("inference.frames_processed", 1, dimensions)


def flush_observability_api_requests(max_events: int = 500) -> int:
    """Retry memory fallbacks, then flush primary and secondary durable queues."""
    with observability_api_lock:
        api_batch = list(observability_api_aggregates.items())
        frame_batch = list(observability_frame_aggregates.items())
        observability_api_aggregates.clear()
        observability_frame_aggregates.clear()
    for (route, method, status), count in api_batch:
        dimensions = {"route": route, "method": method, "status": status}
        try:
            observability_store.buffer_counter_delta(DB_PATH, metric_key="api.requests", delta=count, dimensions=dimensions)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            _queue_observability_counter_fallback("api.requests", count, dimensions)
    for camera_ref, count in frame_batch:
        if camera_ref.startswith("source:"):
            dimensions = {"source_type": camera_ref.partition(":")[2]}
        elif camera_ref == "other":
            dimensions = {"camera_ref": "other", "source_type": "mixed"}
        else:
            dimensions = {"camera_ref": camera_ref, "source_type": "camera"}
        try:
            observability_store.buffer_counter_delta(DB_PATH, metric_key="inference.frames_processed", delta=count, dimensions=dimensions)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            _queue_observability_counter_fallback("inference.frames_processed", count, dimensions)
    persisted = 0
    try:
        persisted += observability_store.flush_counter_buffer(DB_PATH, maximum=max(1, int(max_events)))
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.debug("Could not flush durable VAOS counter buffer", exc_info=True)
    try:
        persisted += observability_store.flush_counter_fallback(DB_PATH, maximum=max(1, int(max_events)))
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.debug("Could not replay the durable VAOS observability fallback queue", exc_info=True)
    return persisted


def observability_counter_fallback_pending() -> bool:
    """Best-effort check for durable secondary rows awaiting replay."""
    try:
        return observability_store.counter_fallback_pending(DB_PATH)
    except (OSError, sqlite3.Error, RuntimeError):
        return observability_store.counter_fallback_database_path(DB_PATH).exists()


def observability_counter_queue_status() -> dict[str, Any]:
    """Report counter persistence health without exposing paths or dimensions."""
    primary = {"available": False, "pending_rows": 0, "pending_increments": 0}
    try:
        with observability_store._connection(DB_PATH, timeout_seconds=0.25) as db:
            row = db.execute(
                "SELECT COUNT(*) AS row_count,COALESCE(SUM(delta),0) AS increments FROM observability_counter_buffer"
            ).fetchone()
        primary = {"available": True, "pending_rows": int(row["row_count"]), "pending_increments": int(row["increments"])}
    except (OSError, sqlite3.Error, RuntimeError):
        logger.debug("Could not inspect the primary observability counter buffer", exc_info=True)
    try:
        fallback = observability_store.counter_fallback_status(DB_PATH)
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.debug("Could not inspect the durable observability fallback queue", exc_info=True)
        fallback = {
            "available": False,
            "pending_rows": 0,
            "pending_increments": 0,
            "overflow_rows": 0,
            "overflow_increments": 0,
            "database_bytes": 0,
            "database_limit_bytes": observability_store.FALLBACK_COUNTER_MAX_BYTES,
            "live_row_limit": observability_store.FALLBACK_COUNTER_BUFFER_LIMIT,
            "batch_row_limit": observability_store.FALLBACK_COUNTER_BATCH_LIMIT,
        }
    try:
        security_fallback = observability_siem.security_event_fallback_status(DB_PATH)
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.debug("Could not inspect the authentication security-event fallback queue", exc_info=True)
        security_fallback = {
            "available": False, "pending_rows": 0, "dropped_events": 0, "database_bytes": 0,
        }
    with observability_api_lock:
        memory_keys = len(observability_api_aggregates) + len(observability_frame_aggregates)
        memory_increments = sum(observability_api_aggregates.values()) + sum(observability_frame_aggregates.values())
        memory_overflow = observability_api_aggregates.get(("other", "OTHER", 0), 0) + observability_frame_aggregates.get("other", 0)
    memory = {"pending_keys": memory_keys, "pending_increments": memory_increments, "overflow_increments": memory_overflow}
    if not primary["available"] or not fallback["available"] or not security_fallback["available"] or memory_keys or security_fallback["dropped_events"]:
        state = "degraded"
    elif fallback["pending_rows"] or security_fallback["pending_rows"]:
        state = "recovery_pending"
    else:
        state = "no_backlog_observed"
    return {
        "state": state,
        "scope": "admin_process_snapshot",
        "writes_verified": False,
        "primary": primary,
        "fallback": fallback,
        "security_event_fallback": security_fallback,
        "memory": memory,
        "updated_at": utc_now(),
    }


def training_device_inventory() -> dict[str, Any]:
    """List process-usable CPU threads and installed NVIDIA/Apple accelerators."""
    if time.monotonic() - float(training_devices_cache["sampled_at"]) < 30:
        return training_devices_cache["data"]
    cpu_threads = available_cpu_threads()
    gpus: list[dict[str, Any]] = []
    mps_available = False
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
            capture_output=True, text=True, check=True, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 1)]
            if len(parts) == 2 and parts[0].isdigit():
                gpus.append({"index": int(parts[0]), "name": parts[1]})
    except (OSError, subprocess.SubprocessError):
        pass
    if sys.platform == "darwin":
        try:
            import torch
            mps_available = mps_is_available(torch)
        except Exception:
            logger.debug("Apple MPS is unavailable to this PyTorch runtime", exc_info=True)
    training_devices_cache.update({"sampled_at": time.monotonic(), "data": {"cpu_threads": cpu_threads, "gpus": gpus, "mps_available": mps_available}})
    return training_devices_cache["data"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _training_log_process_lock(path: Path, *, timeout_seconds: float = 15.0):
    """Serialize training-log reads, appends and rotation across VAOS processes."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.fstat(lock_file.fileno()).st_size == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        if os.name == "nt":
            import msvcrt

            while True:
                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for the training-log file lock")
                    time.sleep(0.01)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for the training-log file lock")
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def write_training_log(level: str, message: str, **details: Any) -> None:
    """Append concise, private training events to the bounded local log history."""
    record = {"timestamp": utc_now(), "level": level.upper(), "message": message, **details}
    if "observability_session_id" not in record:
        record["observability_session_id"] = observability_session_id
    request_correlation_id = observability_correlation_context.get()
    if request_correlation_id and "correlation_id" not in record:
        record["correlation_id"] = request_correlation_id
    try:
        with ml_training_log_lock:
            with _training_log_process_lock(ML_TRAINING_LOG_PATH):
                if ML_TRAINING_LOG_PATH.exists() and ML_TRAINING_LOG_PATH.stat().st_size > OBSERVABILITY_TRAINING_LOG_MAX_BYTES:
                    # Keep a bounded history so startup recovery can restore events
                    # across multiple rotations without allowing unbounded disk use.
                    oldest = ML_TRAINING_LOG_PATH.with_suffix(f".log.{OBSERVABILITY_TRAINING_LOG_ROTATIONS}")
                    oldest.unlink(missing_ok=True)
                    for index in range(OBSERVABILITY_TRAINING_LOG_ROTATIONS - 1, 0, -1):
                        source = ML_TRAINING_LOG_PATH.with_suffix(f".log.{index}")
                        if source.exists():
                            source.replace(ML_TRAINING_LOG_PATH.with_suffix(f".log.{index + 1}"))
                    ML_TRAINING_LOG_PATH.replace(ML_TRAINING_LOG_PATH.with_suffix(".log.1"))
                with ML_TRAINING_LOG_PATH.open("a", encoding="utf-8") as log_file:
                    log_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.exception("Could not write a VAOS training event to the local training log")
        # If the append failed (for example, transient disk pressure), try the
        # durable store immediately instead of leaving this record only in the
        # bounded in-memory queue. The event ID is deterministic, so the normal
        # queue flush or startup log replay remains safe if either path retries.
        if not _persist_observability_training_event(record):
            logger.warning("Training event could not be written to the log or observability database")
    queue_observability_training_event(record)


def _persist_observability_training_event(record: dict[str, Any], *, use_current_session: bool = True) -> bool:
    """Mirror a training record and its epoch metrics in one SQLite transaction."""
    message = str(record.get("message", ""))
    normalized = re.sub(r"[^a-z0-9]+", "_", message.casefold()).strip("_") or "event"
    details = {key: value for key, value in record.items() if key not in {"timestamp", "level", "message"}}
    run_id = str(details.get("run_id") or ml_training_state.get("run_id") or "")
    try:
        details_fingerprint = json.dumps(details, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        details_fingerprint = normalized
    event_id_seed = "|".join((str(record.get("timestamp", "")), run_id, normalized, str(details.get("epoch", "")), str(details.get("batch", "")), details_fingerprint))
    event_id = hashlib.sha256(event_id_seed.encode("utf-8", errors="replace")).hexdigest()
    counter_deltas: dict[str, int] = {}
    if message == "Training run initialized":
        counter_deltas["training.jobs_started"] = 1
    elif message == "Training epoch completed":
        counter_deltas["training.epochs_completed"] = 1
    elif message == "Training finished":
        status = str(details.get("status", "")).casefold()
        if status == "complete":
            counter_deltas["training.jobs_completed"] = 1
        elif status == "failed":
            counter_deltas["training.jobs_failed"] = 1
    elif message == "Training failed":
        counter_deltas["training.jobs_failed"] = 1
    metric_observations: list[dict[str, Any]] = []
    if message == "Training epoch completed" and isinstance(details.get("stats"), dict):
        raw_stats = details["stats"]
        metric_names = {
            "box_loss": "training.box_loss", "class_loss": "training.class_loss",
            "dfl_loss": "training.dfl_loss", "precision": "training.precision",
            "recall": "training.recall", "map50": "training.map50",
            "map50_95": "training.map50_95", "learning_rate": "training.learning_rate",
        }
        dimensions = {
            key: value for key, value in {
                "training_job_id": run_id or None,
                "dataset_id": details.get("source") or ml_training_state.get("source"),
                "model_id": details.get("initial_model") or ml_training_state.get("initial_model"),
                "epoch": details.get("epoch"),
            }.items() if value is not None
        }
        for stat_name, metric_key in metric_names.items():
            value = raw_stats.get(stat_name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metric_observations.append({
                    "metric_key": metric_key, "value": float(value), "dimensions": dimensions,
                    "provenance": "VAOS training epoch callback",
                    "timestamp": str(record.get("timestamp") or utc_now()),
                })
    run_metric_values: dict[str, Any] = {}
    if message == "Training run initialized":
        run_metric_values["training.dataset_images"] = details.get("images")
    if message == "Training finished":
        run_metric_values["training.dataset_annotations"] = details.get("annotations")
        run_metric_values["training.job_duration_seconds"] = details.get("elapsed_seconds")
    elif message == "Training failed":
        run_metric_values["training.job_duration_seconds"] = details.get("elapsed_seconds")
    if run_metric_values:
        run_dimensions = {
            key: value for key, value in {
                "training_job_id": run_id or None,
                "dataset_id": details.get("source") or ml_training_state.get("source"),
                "model_id": details.get("initial_model") or ml_training_state.get("initial_model"),
            }.items() if value is not None
        }
        for metric_key, value in run_metric_values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0:
                metric_observations.append({
                    "metric_key": metric_key,
                    "value": float(value),
                    "dimensions": run_dimensions,
                    "provenance": "Resolved VAOS training run metadata",
                    "measurement_scope": "Selected dataset inventory" if metric_key.startswith("training.dataset_") else "Training worker monotonic elapsed time",
                    "timestamp": str(record.get("timestamp") or utc_now()),
                })
    try:
        observability_store.record_event(
            DB_PATH,
            event_type=f"training.{normalized[:75]}",
            component="training",
            level=str(record.get("level", "INFO")),
            message=message,
            details=details,
            session_id=record.get("observability_session_id") if "observability_session_id" in record else (observability_session_id if use_current_session else None),
            correlation_id=details.get("correlation_id") or observability_correlation_context.get(),
            event_id=event_id,
            timestamp=str(record.get("timestamp") or utc_now()),
            counter_deltas=counter_deltas,
            metric_observations=metric_observations,
        )
        return True
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.debug("Could not persist the redacted training event to observability storage", exc_info=True)
        return False


def reconcile_training_log_observability() -> dict[str, int]:
    """Replay retained training JSONL records after restart, oldest first and idempotently."""
    with ml_training_log_lock:
        segments = []
        with _training_log_process_lock(ML_TRAINING_LOG_PATH):
            retained_paths = [
                ML_TRAINING_LOG_PATH.with_suffix(f".log.{index}")
                for index in range(OBSERVABILITY_TRAINING_LOG_ROTATIONS, 0, -1)
            ]
            retained_paths.append(ML_TRAINING_LOG_PATH)
            for path in retained_paths:
                try:
                    if path.is_file():
                        segments.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    logger.warning("Could not read a retained training log during observability recovery")
    parsed = persisted = invalid = failed = 0
    for segment in segments:
        for line in segment.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if not isinstance(record, dict) or not record.get("timestamp") or not record.get("message"):
                invalid += 1
                continue
            parsed += 1
            if _persist_observability_training_event(record, use_current_session=False):
                persisted += 1
            else:
                failed += 1
    if invalid or failed:
        logger.warning("Training observability recovery examined %d record(s); %d malformed and %d not persisted", parsed, invalid, failed)
    elif persisted:
        logger.info("Training observability recovery reconciled %d retained training record(s) idempotently", persisted)
    return {"parsed": parsed, "processed": persisted, "invalid": invalid, "failed": failed}


def queue_observability_training_event(record: dict[str, Any]) -> None:
    """Add a bounded, redacted training record to the background telemetry queue."""
    global observability_training_dropped
    safe = observability_store.redact_details(record)
    priority = str(record.get("level", "INFO")).upper() in {"WARNING", "ERROR", "CRITICAL"} or str(record.get("message", "")) in {"Training run initialized", "Training epoch completed", "Training finished", "Training failed"}
    with observability_training_lock:
        if len(observability_training_queue) >= OBSERVABILITY_TRAINING_QUEUE_LIMIT:
            replaceable = next((item for item in observability_training_queue if item.get("message") == "Training progress update"), None)
            if replaceable is not None:
                observability_training_queue.remove(replaceable)
                observability_training_dropped += 1
            elif priority:
                observability_training_queue.popleft()
                observability_training_dropped += 1
            else:
                observability_training_dropped += 1
                return
        observability_training_queue.append(safe)


def _requeue_observability_training_events(records: list[dict[str, Any]]) -> None:
    """Restore failed batch records in order while keeping the queue bounded."""
    global observability_training_dropped
    for item in reversed(records):
        priority = str(item.get("level", "INFO")).upper() in {"WARNING", "ERROR", "CRITICAL"} or str(item.get("message", "")) in {
            "Training run initialized", "Training epoch completed", "Training finished", "Training failed"
        }
        with observability_training_lock:
            if len(observability_training_queue) >= OBSERVABILITY_TRAINING_QUEUE_LIMIT:
                replaceable = next((queued for queued in observability_training_queue if queued.get("message") == "Training progress update"), None)
                if replaceable is not None:
                    observability_training_queue.remove(replaceable)
                    observability_training_dropped += 1
                elif priority:
                    observability_training_queue.popleft()
                    observability_training_dropped += 1
                else:
                    observability_training_dropped += 1
                    continue
            observability_training_queue.appendleft(item)


def flush_observability_training_events(max_events: int = 10) -> int:
    global observability_training_dropped
    with observability_training_lock:
        batch = [observability_training_queue.popleft() for _ in range(min(max(1, int(max_events)), len(observability_training_queue)))]
        dropped = observability_training_dropped
        observability_training_dropped = 0
    persisted = 0
    if dropped:
        try:
            event_id = hashlib.sha256(f"telemetry-drop|{os.getpid()}|{utc_now()}|{dropped}".encode()).hexdigest()
            observability_store.record_event(DB_PATH, event_id=event_id, event_type="observability.queue_overflow", component="observability", level="WARNING", message="Training telemetry queue overflowed; lower-priority records were omitted.", details={"dropped_records": dropped})
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            with observability_training_lock:
                observability_training_dropped += dropped
    for index, item in enumerate(batch):
        if _persist_observability_training_event(item):
            persisted += 1
        else:
            _requeue_observability_training_events(batch[index:])
            break
    return persisted


def read_training_log() -> list[dict[str, Any]]:
    try:
        with ml_training_log_lock:
            with _training_log_process_lock(ML_TRAINING_LOG_PATH):
                if not ML_TRAINING_LOG_PATH.is_file():
                    return []
                lines = ML_TRAINING_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        records = []
        invalid = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                invalid += 1
        if invalid:
            logger.warning("Training log view skipped %d malformed or non-object line(s)", invalid)
        return records
    except OSError:
        logger.exception("Could not read the VAOS local training log")
        return []


def estimate_training_eta(elapsed_seconds: float, completed_epochs: float, total_epochs: int) -> int | None:
    """Estimate remaining time from real completed epoch fractions, ignoring warmup noise."""
    total = max(1, int(total_epochs))
    completed = min(float(total), max(0.0, float(completed_epochs)))
    if completed < 0.05:
        return None
    return int(max(0.0, float(elapsed_seconds) * (total - completed) / completed))


def read_launcher_training_progress() -> dict[str, Any] | None:
    """Recover live batch progress from the launcher's captured Ultralytics output.

    Ultralytics can keep advancing its console progress bar even when its Python
    callback stops updating the in-memory training state. The launcher log is
    the durable fallback for the training page and its stall warning.
    """
    logs_dir = DATA_DIR / "logs"
    try:
        candidates = list(logs_dir.glob("*/admin_service.log"))
        if not candidates:
            return None
        log_path = max(candidates, key=lambda path: path.stat().st_mtime)
        modified_at = log_path.stat().st_mtime
        if time.time() - modified_at > 180:
            return None
        with log_path.open("rb") as log_file:
            log_file.seek(max(0, log_path.stat().st_size - 128_000))
            output = log_file.read().decode("utf-8", errors="replace")
        output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
        progress_pattern = re.compile(
            r"\b(\d{1,3})\s*/\s*(\d{1,3})\b.*?\b(\d{1,3})%.*?"
            r"\b(\d+)\s*/\s*(\d+)\s+([\d.]+)\s*it/s",
            re.IGNORECASE,
        )
        matches = [match for line in re.split(r"[\r\n]+", output) if (match := progress_pattern.search(line))]
        if not matches:
            return None
        epoch, epochs, percent, batch, batches, rate = matches[-1].groups()
        epoch, epochs, batch, batches = int(epoch), int(epochs), int(batch), int(batches)
        if epochs < 1 or batches < 1 or batch > batches:
            return None
        return {
            "epoch": epoch,
            "epochs": epochs,
            "batch": batch,
            "batches": batches,
            "progress": min(99, max(0, int(((epoch - 1) + (batch / batches)) / epochs * 100))),
            "epoch_progress": round(min(1.0, max(0.0, batch / batches)) * 100, 2),
            "message": f"Epoch {epoch}/{epochs} · batch {batch:,}/{batches:,}",
            "output_rate": float(rate),
            "output_updated_at": modified_at,
        }
    except (OSError, ValueError):
        logger.debug("Could not read launcher training progress", exc_info=True)
        return None


def model_runtime_status() -> dict[str, Any]:
    """Return model state from this process (the camera worker owns inference)."""
    model_status = []
    for name, path in MODEL_PATHS.items():
        loaded = face_detector is not None if name == "yunet" else face_recognizer is not None
        model_status.append({"name": name, "path": str(path), "valid": path.is_file() and path.stat().st_size >= MODEL_MIN_BYTES[name] if path.exists() else False, "loaded": loaded, "error": model_errors.get(name)})
    trained_model_path = ML_DATA_DIR / "active" / "best.pt"
    object_model_path = Path(object_detector.model_path) if object_detector is not None else selected_object_model_path()
    trained_active = bool(object_detector and object_model_path.resolve() == trained_model_path.resolve())
    model_status.append({"name": "yolo26m", "path": str(object_model_path), "valid": object_model_path.is_file(), "loaded": object_detector is not None, "custom_model_active": trained_active, "classes": list(object_detector.class_names) if object_detector else [], "error": model_errors.get("yolo26m")})
    return {"opencv": cv2.__version__, "device": compute_device, "inference_backend": object_detector.device_kind if object_detector else "unknown", "cuda_available": bool(object_detector and object_detector.use_cuda), "mps_available": bool(object_detector and object_detector.mps_available), "custom_model_active": trained_active, "object_model_loaded": object_detector is not None, "models": model_status}


def get_selected_object_model() -> str:
    try:
        with db_connection() as db:
            row = db.execute("SELECT value FROM app_settings WHERE key='object_model_choice'").fetchone()
        selected = row["value"] if row else ""
    except (sqlite3.Error, OSError):
        selected = ""
    if selected == "custom":
        return "custom" if (ML_DATA_DIR / "active" / "best.pt").is_file() else "yolo26m"
    if selected == "yolo26m":
        return selected
    return "custom" if (ML_DATA_DIR / "active" / "best.pt").is_file() else "yolo26m"


def selected_object_model_path() -> Path:
    custom = ML_DATA_DIR / "active" / "best.pt"
    base = MODEL_DIR / "yolo26m.pt"
    if get_selected_object_model() == "custom" and custom.is_file():
        return custom
    return base


def is_enrollment_link_valid(token: str) -> bool:
    with db_connection() as db:
        row = db.execute(
            "SELECT 1 FROM enrollment_links WHERE token=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?",
            (token, utc_now()),
        ).fetchone()
    return row is not None


def get_or_create_admin_password() -> str:
    configured = os.getenv("VAOS_ADMIN_PASSWORD", "")
    if PRODUCTION_MODE:
        if len(configured) < 32 or configured.startswith("REPLACE_"):
            raise RuntimeError("Production requires VAOS_ADMIN_PASSWORD with at least 32 characters. Set a unique secret before starting VAOS.")
        return configured
    if os.getenv("VAOS_ENV", "development").strip().casefold() == "staging":
        return "1122334455"
    if configured:
        if len(configured) < 20:
            raise RuntimeError("VAOS_ADMIN_PASSWORD must contain at least 20 characters when configured.")
        return configured
    if PASSWORD_PATH.exists():
        saved = PASSWORD_PATH.read_text(encoding="utf-8").strip()
        if len(saved) >= 20:
            return saved
    password = secrets.token_urlsafe(32)
    PASSWORD_PATH.write_text(password + "\n", encoding="utf-8")
    try:
        os.chmod(PASSWORD_PATH, 0o600)
    except OSError:
        pass
    logger.warning("Dashboard password was generated or upgraded. It is stored in %s.", PASSWORD_PATH)
    return password


# The public camera process imports this module only to share recognition and
# database helpers; it must never initialize or print admin credentials.
ADMIN_PASSWORD = "" if os.getenv("CAMERA_SERVICE_IMPORT") == "1" else get_or_create_admin_password()


def _record_observability_security_event(event_type: str, outcome: str, *, component: str, details: dict[str, Any] | None = None, actor_ref: str | None = None, correlation_id: str | None = None) -> bool:
    try:
        return bool(observability_siem.record_security_event_resilient(
            DB_PATH, event_type=event_type, outcome=outcome, component=component,
            actor_ref=actor_ref or ("local-admin" if outcome not in {"denied", "failed"} else "unauthenticated-client"),
            details=details,
            correlation_id=correlation_id if correlation_id is not None else observability_correlation_context.get(),
        ))
    except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError):
        logger.warning(
            "VAOS security event persistence was not confirmed; the atomic audit/event/outbox transaction may have rolled back, and the related operation may already have completed",
            exc_info=True,
        )
        return False


def _record_observability_security_event_in_transaction(
    db: sqlite3.Connection,
    event_type: str,
    outcome: str,
    *,
    component: str,
    details: dict[str, Any] | None = None,
    actor_ref: str | None = None,
    correlation_id: str | None = None,
) -> bool:
    """Couple a state change and its audit/outbox event in one DB transaction."""
    try:
        return observability_siem.record_security_event_and_enqueue_in_transaction(
            db, event_type=event_type, outcome=outcome, component=component,
            actor_ref=actor_ref or ("local-admin" if outcome not in {"denied", "failed"} else "unauthenticated-client"),
            details=details,
            correlation_id=correlation_id if correlation_id is not None else observability_correlation_context.get(),
        )
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        # Normalize persistence failures to an OperationalError so API routes
        # can return their existing sanitized 503 response and roll back the
        # caller-owned transaction, regardless of the underlying I/O failure.
        raise sqlite3.OperationalError("VAOS observability audit persistence failed.") from exc


def _record_observability_dataset_result(
    dataset_kind: str,
    *,
    succeeded: bool,
    duration_seconds: float,
    image_count: int | None = None,
    train_image_count: int | None = None,
    validation_image_count: int | None = None,
    class_count: int | None = None,
    annotation_count: int | None = None,
) -> None:
    """Record privacy-limited dataset preparation facts without paths or image data."""
    if dataset_kind not in {"managed_local", "uvh26", "kaggle_auto", "coco2017"}:
        return

    def safe_count(value: int | None) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if 0 <= number <= 2**53 else None

    counts = {
        "image_count": safe_count(image_count),
        "train_image_count": safe_count(train_image_count),
        "validation_image_count": safe_count(validation_image_count),
        "class_count": safe_count(class_count),
        "annotation_count": safe_count(annotation_count),
    }
    try:
        elapsed = float(duration_seconds)
    except (TypeError, ValueError, OverflowError):
        elapsed = 0.0
    if not math.isfinite(elapsed) or elapsed < 0:
        elapsed = 0.0

    metric_dimensions = {"dataset_kind": dataset_kind}
    counter_deltas = {"datasets.preparations_completed" if succeeded else "datasets.preparations_failed": 1}
    if succeeded and counts["image_count"]:
        counter_deltas["datasets.images_prepared"] = counts["image_count"]
    if succeeded and counts["annotation_count"]:
        counter_deltas["datasets.annotations_validated"] = counts["annotation_count"]
    counter_dimensions = {key: metric_dimensions for key in counter_deltas}
    details: dict[str, Any] = {"dataset_kind": dataset_kind}
    if succeeded:
        details.update({
            key: value for key, value in {
                "prepared_count": counts["image_count"],
                "train_count": counts["train_image_count"],
                "validation_count": counts["validation_image_count"],
                "class_count": counts["class_count"],
                "box_count": counts["annotation_count"],
            }.items() if value is not None
        })
    try:
        observability_store.record_event(
            DB_PATH,
            event_type="dataset.validation_succeeded" if succeeded else "dataset.validation_failed",
            component="datasets",
            level="INFO" if succeeded else "WARNING",
            message=f"Dataset preparation {'completed' if succeeded else 'failed'} ({dataset_kind}).",
            details=details,
            counter_deltas=counter_deltas,
            counter_dimensions=counter_dimensions,
            correlation_id=observability_correlation_context.get(),
            metric_observations=[{
                "metric_key": "datasets.preparation_duration_seconds",
                "value": elapsed,
                "dimensions": metric_dimensions,
                "provenance": "monotonic dataset preparation worker timer",
                "measurement_scope": "dataset validation or import worker",
                "estimated": False,
            }],
        )
    except Exception as exc:
        # Dataset preparation remains successful even if optional analytics storage is unavailable.
        logger.warning("Dataset preparation observability could not be recorded (%s).", type(exc).__name__)


def _effective_client_ip(request: Request) -> str:
    """Trust forwarding headers only when the direct peer is a configured proxy."""
    peer_text = request.client.host if request.client and request.client.host else "unknown"
    try:
        peer_ip = ipaddress.ip_address(peer_text)
    except ValueError:
        return peer_text
    trusted_proxy = False
    for network_text in os.getenv("VAOS_TRUSTED_PROXY_CIDRS", "").split(","):
        try:
            if peer_ip in ipaddress.ip_network(network_text.strip(), strict=False):
                trusted_proxy = True
                break
        except ValueError:
            continue
    if trusted_proxy:
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        if not forwarded:
            forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        try:
            if forwarded:
                return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return str(peer_ip)


def _record_authorization_denial(request: Request, status_code: int, route_template: str | None) -> None:
    """Audit coalesced authentication/authorization denials without persisting client addresses."""
    client_key = _effective_client_ip(request)
    route = str(route_template or _redact_api_path(request.url.path))[:240]
    method = str(request.method or "GET").upper()[:12]
    now = time.monotonic()
    key = f"{client_key}|{int(status_code)}|{method}|{route}"
    should_record = False
    with admin_auth_lock:
        previous = authorization_denial_audit_last.get(key)
        if previous is None or now - previous >= 60:
            if key not in authorization_denial_audit_last and len(authorization_denial_audit_last) >= 512:
                oldest_key = min(authorization_denial_audit_last, key=authorization_denial_audit_last.get)
                authorization_denial_audit_last.pop(oldest_key, None)
            authorization_denial_audit_last[key] = now
            should_record = True
    request.state.vaos_auth_audit_handled = True
    if not should_record:
        return
    token_prefix = getattr(request.state, "vaos_api_token_prefix", None)
    actor_ref = f"api-token:{token_prefix}" if token_prefix else ("local-admin" if getattr(request.state, "vaos_basic_admin", False) else "unauthenticated-client")
    persisted = _record_observability_security_event(
        "authz.request_denied", "denied", component="authorization", actor_ref=actor_ref,
        correlation_id=getattr(request.state, "vaos_correlation_id", None),
        details={
            "http_status": int(status_code), "method": method, "route": route,
            "reason": "authentication_required_or_invalid" if status_code == 401 else "authorization_denied",
        },
    )
    if not persisted:
        with admin_auth_lock:
            if authorization_denial_audit_last.get(key) == now:
                authorization_denial_audit_last.pop(key, None)


def require_basic_admin(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    global admin_auth_audit_last
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Sign in to the local dashboard",
            headers={"WWW-Authenticate": "Basic realm=\"VAOS Admin\""},
        )
    username_ok = hmac.compare_digest(credentials.username.encode(), b"admin")
    password_ok = hmac.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (username_ok and password_ok):
        client_key = _effective_client_ip(request)
        now = time.monotonic()
        audit_failure = False
        with admin_auth_lock:
            admin_auth_success_audit_last.pop(client_key, None)
            attempts = [attempt for attempt in admin_auth_failures.get(client_key, []) if now - attempt < ADMIN_AUTH_FAILURE_WINDOW]
            if len(attempts) >= ADMIN_AUTH_FAILURE_LIMIT:
                admin_auth_failures[client_key] = attempts
                raise HTTPException(status_code=429, detail="Too many sign-in attempts. Wait 15 minutes and try again.", headers={"Retry-After": "900"})
            attempts.append(now)
            admin_auth_failures[client_key] = attempts
            if now - admin_auth_audit_last.get(client_key, 0.0) >= 60:
                admin_auth_audit_last[client_key] = now
                audit_failure = True
            if len(admin_auth_audit_last) > 512:
                admin_auth_audit_last = {key: seen for key, seen in admin_auth_audit_last.items() if now - seen < ADMIN_AUTH_FAILURE_WINDOW}
            if len(admin_auth_failures) > 512:
                for key, values in list(admin_auth_failures.items()):
                    if not any(now - attempt < ADMIN_AUTH_FAILURE_WINDOW for attempt in values):
                        admin_auth_failures.pop(key, None)
        if audit_failure:
            persisted = _record_observability_security_event(
                "auth.login_failed", "denied", component="auth",
                details={"reason": "invalid dashboard credentials; repeated attempts coalesced for one minute"},
                correlation_id=getattr(request.state, "vaos_correlation_id", None),
            )
            if not persisted:
                with admin_auth_lock:
                    if admin_auth_audit_last.get(client_key) == now:
                        admin_auth_audit_last.pop(client_key, None)
        request.state.vaos_auth_audit_handled = True
        raise HTTPException(
            status_code=401,
            detail="Incorrect dashboard credentials",
            headers={"WWW-Authenticate": "Basic realm=\"VAOS Admin\""},
        )
    client_key = _effective_client_ip(request)
    audit_success = False
    with admin_auth_lock:
        admin_auth_failures.pop(client_key, None)
        admin_auth_audit_last.pop(client_key, None)
        now = time.monotonic()
        previous_success = admin_auth_success_audit_last.get(client_key)
        if previous_success is None or now - previous_success >= 60:
            # HTTP Basic credentials accompany every request. Coalesce repeated
            # accepted requests so normal page/API use does not flood the audit.
            if client_key not in admin_auth_success_audit_last and len(admin_auth_success_audit_last) >= 512:
                oldest_client = min(admin_auth_success_audit_last, key=admin_auth_success_audit_last.get)
                admin_auth_success_audit_last.pop(oldest_client, None)
            admin_auth_success_audit_last[client_key] = now
            audit_success = True
    if audit_success:
        persisted = _record_observability_security_event(
            "auth.login_succeeded", "accepted", component="auth",
            details={"mechanism": "http_basic", "repeated_successes_coalesced_seconds": 60},
            correlation_id=getattr(request.state, "vaos_correlation_id", None),
        )
        if not persisted:
            with admin_auth_lock:
                admin_auth_success_audit_last.pop(client_key, None)
            request.state.vaos_auth_audit_handled = True
            raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable because VAOS could not save its security audit. No dashboard action was run; try again shortly.")
    request.state.vaos_basic_admin = True
    return credentials.username


API_MCP_GROUPS = ("diagnostics", "cameras", "people", "events", "review", "training", "datasets", "models", "settings", "tunnels", "video", "projects")
MCP_BRIDGE_TOOL_NAMES = frozenset({
    "vaos_api_request", "vaos_get_statistics", "vaos_query_metric", "vaos_list_runtime_sessions",
    "vaos_search_logs", "vaos_get_system_health", "vaos_list_report_types", "vaos_generate_report",
    "vaos_get_report_status", "vaos_check_approval", "vaos_upload_file", "vaos_send_binary",
    "vaos_openapi_reference", "vaos_agent_policy",
})
MCP_INVOCATION_TELEMETRY_PATHS = frozenset({
    "/api/admin/observability/mcp/invocations",
    "/api/v1/admin/observability/mcp/invocations",
})
API_DEFAULT_TOKEN_GROUPS = ("diagnostics",)
MCP_APPROVAL_TTL_SECONDS = 600
MCP_APPROVAL_MAX_PENDING_PER_TOKEN = 10
API_DEFAULTS: dict[str, Any] = {
    "api_enabled": False,
    "api_allow_sensitive_reads": False,
    "mcp_enabled": False,
    "mcp_read_only": True,
    # Enabling MCP starts from diagnostics only; administrators opt into more.
    "mcp_allowed_groups": list(API_DEFAULT_TOKEN_GROUPS),
    "mcp_allow_sensitive": False,
    "mcp_allow_deletes": False,
    "api_rate_limit": 120,
    "api_token_default_days": 90,
}
_api_request_lock = threading.Lock()
_api_request_times: dict[str, list[float]] = {}
_api_token_touch: dict[str, float] = {}
_api_last_audit_cleanup = 0.0


def get_api_settings() -> dict[str, Any]:
    settings = dict(API_DEFAULTS)
    try:
        with db_connection() as db:
            rows = db.execute("SELECT key,value FROM app_settings WHERE key LIKE 'integration_%'").fetchall()
        saved = {str(row["key"]): str(row["value"]) for row in rows}
        settings["api_enabled"] = saved.get("integration_api_enabled", "0") == "1"
        settings["api_allow_sensitive_reads"] = saved.get("integration_api_allow_sensitive_reads", "0") == "1"
        settings["mcp_enabled"] = saved.get("integration_mcp_enabled", "0") == "1"
        settings["mcp_read_only"] = saved.get("integration_mcp_read_only", "1") == "1"
        settings["mcp_allow_sensitive"] = saved.get("integration_mcp_allow_sensitive", "0") == "1"
        settings["mcp_allow_deletes"] = saved.get("integration_mcp_allow_deletes", "0") == "1"
        settings["api_rate_limit"] = min(600, max(30, int(saved.get("integration_api_rate_limit", "120"))))
        settings["api_token_default_days"] = min(365, max(1, int(saved.get("integration_api_token_default_days", "90"))))
        groups = json.loads(saved.get("integration_mcp_allowed_groups", json.dumps(API_DEFAULT_TOKEN_GROUPS)))
        settings["mcp_allowed_groups"] = [group for group in groups if group in API_MCP_GROUPS] if isinstance(groups, list) else list(API_DEFAULT_TOKEN_GROUPS)
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        logger.exception("Could not read API and MCP settings; using locked-down defaults")
    return settings


def _api_request_group(path: str) -> str:
    normalized = path.split("?", 1)[0]
    if "/mcp-approvals/" in normalized:
        return "settings"
    if normalized.startswith(("/api/admin/observability/integrations", "/api/v1/admin/observability/integrations")):
        return "settings"
    if "/observability" in normalized or "/statistics" in normalized:
        return "diagnostics"
    if normalized.startswith("/api/v1/health") or normalized.endswith("/status") or normalized.endswith("/logs") or normalized.endswith("/history") or normalized.endswith("/hardware"):
        return "diagnostics" if normalized.endswith(("/health", "/status", "/hardware")) else "training"
    if "/cameras" in normalized or "/camera/" in normalized or "/enrollment-links" in normalized:
        return "cameras"
    if "/participants" in normalized or "/enroll/" in normalized:
        return "people"
    if "/events" in normalized or "/sightings" in normalized or "/tracking/rules" in normalized:
        return "events"
    if "/samples" in normalized or "/review" in normalized or "/capture" in normalized:
        return "review"
    if "/train" in normalized or "/history" in normalized or "/logs" in normalized:
        return "training"
    if "/engine-settings" in normalized or "/model-selection" in normalized:
        return "settings"
    if "/datasets" in normalized or "/dataset" in normalized:
        return "datasets"
    if "/model" in normalized or "/huggingface" in normalized or "/enrichment" in normalized:
        return "models"
    if "/tunnel" in normalized or "/admin-tunnel" in normalized:
        return "tunnels"
    if "/video-review" in normalized:
        return "video"
    if "/projects" in normalized:
        return "projects"
    if "/services/" in normalized:
        return "settings"
    return "unclassified"


def _api_read_is_sensitive(path: str) -> bool:
    """Identify read endpoints that can reveal biometrics or capability URLs."""
    normalized = path.split("?", 1)[0]
    if normalized.startswith("/api/v1/"):
        normalized = "/api" + normalized[len("/api/v1"):]
    exact_paths = {
        "/api/admin/cameras", "/api/admin/tunnel", "/api/admin/admin-tunnel",
        "/api/admin/participants", "/api/admin/events", "/api/admin/tracking/rules", "/api/admin/tracking/events",
    }
    if normalized in exact_paths or normalized.startswith(("/api/admin/participants/", "/api/admin/tracking/rules/", "/api/admin/tracking/events/")):
        return True
    if normalized.startswith("/api/admin/cameras/") and normalized.endswith("/qr"):
        return True
    if normalized.startswith("/api/admin/enrollment-links/") and normalized.endswith("/qr"):
        return True
    if normalized.startswith("/api/admin/ml/samples/") and normalized.endswith("/image"):
        return True
    if normalized.startswith(("/api/admin/observability/events", "/api/admin/observability/lifecycle", "/api/admin/observability/security-audit", "/api/admin/observability/reports", "/api/admin/observability/integrations", "/api/admin/observability/counter-queue")):
        return True
    return False


def _redact_api_path(path: str) -> str:
    """Remove capability tokens from paths before writing the audit trail."""
    parts = path.split("?")[0].split("/")
    secret_after = {
        ("api", "camera"), ("api", "enroll"), ("api", "admin", "cameras"),
        ("api", "admin", "enrollment-links"),
    }
    for prefix in secret_after:
        if tuple(parts[1:1 + len(prefix)]) == prefix and len(parts) > len(prefix) + 1:
            token_index = len(prefix) + 1
            parts[token_index] = "[redacted]"
            break
    return "/".join(parts)[:240]


def _api_mcp_sensitive_action(method: str, path: str) -> bool:
    """Keep high-impact MCP actions behind the existing explicit Settings switch."""
    method = method.upper()
    normalized = path.split("?", 1)[0]
    if normalized == "/api/admin/observability/retention" and method in {"PUT", "POST"}:
        return True
    if normalized.startswith("/api/admin/tracking/rules") and method in {"POST", "PATCH", "DELETE"}:
        return True
    if normalized.startswith("/api/admin/observability/integrations/") and method in {"PUT", "PATCH", "DELETE"}:
        return True
    if method != "POST":
        return False
    if "/projects/import" in normalized or ("/projects/" in normalized and normalized.endswith("/apply")):
        return True
    return normalized == "/api/admin/cameras" or any(part in normalized for part in (
        "/services/restart", "/tunnel/", "/admin-tunnel/", "/train/stop",
        "/observability/retention",
        "/observability/reports",
        "/observability/integrations",
        "/ml/train", "/ml/enrichment", "/ml/datasets/prepare", "/ml/datasets/import",
        "/ml/model-sources", "/ml/capture", "/model-selection", "/huggingface-models",
        "/video-review/sessions", "/participants", "/enrollment-links", "/enroll/", "/camera/",
    ))


async def _mcp_request_fingerprint(request: Request) -> tuple[str, dict[str, Any]]:
    """Hash the exact effective request without persisting request bodies or private values."""
    method = request.method.upper()
    path = request.url.path
    query = request.url.query
    raw_content_type = request.headers.get("content-type", "")
    media_type = raw_content_type.split(";", 1)[0].strip().casefold()
    behavior_headers = {
        name: request.headers.get(name, "")
        for name in ("x-video-time", "x-save-samples")
        if request.headers.get(name) is not None
    }
    body_bytes = 0
    parameter_summary: list[dict[str, Any]] = []
    if media_type == "multipart/form-data":
        # FastAPI may already have parsed this form for UploadFile/Form parameters.
        # Canonicalize parts so a regenerated MIME boundary does not break approval.
        form = await request.form()
        parts: list[dict[str, Any]] = []
        for key, value in form.multi_items():
            filename = getattr(value, "filename", None)
            file_obj = getattr(value, "file", None)
            if filename is not None and file_obj is not None:
                digest = hashlib.sha256()
                size = 0
                while chunk := await value.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                await value.seek(0)
                parts.append({
                    "field": str(key), "kind": "file", "filename": str(filename),
                    "content_type": str(getattr(value, "content_type", "") or "").casefold(),
                    "size": size, "sha256": digest.hexdigest(),
                })
                parameter_summary.append({"field": str(key)[:64], "type": "file", "size": size, "content_type": str(getattr(value, "content_type", "") or "").casefold()[:80]})
                body_bytes += size
            else:
                text_value = str(value)
                encoded = text_value.encode("utf-8")
                parts.append({"field": str(key), "kind": "text", "size": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()})
                parameter_summary.append({"field": str(key)[:64], "type": "text", "characters": len(text_value)})
                body_bytes += len(encoded)
        body_digest = hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        media_type = "multipart/form-data"
    else:
        body = await request.body()
        body_bytes = len(body)
        body_digest = hashlib.sha256(body).hexdigest()
        if media_type == "application/json":
            try:
                payload = json.loads(body) if body else None
                if isinstance(payload, dict):
                    for key, value in list(payload.items())[:32]:
                        item: dict[str, Any] = {"field": str(key)[:64]}
                        if isinstance(value, bool):
                            item.update({"type": "boolean", "value": value})
                        elif isinstance(value, (int, float)) and not isinstance(value, bool):
                            item.update({"type": "number", "value": value})
                        elif isinstance(value, str):
                            item.update({"type": "text", "characters": len(value)})
                        elif isinstance(value, list):
                            item.update({"type": "list", "items": len(value)})
                        elif isinstance(value, dict):
                            item.update({"type": "object", "fields": len(value)})
                        else:
                            item["type"] = "null"
                        parameter_summary.append(item)
            except (json.JSONDecodeError, UnicodeDecodeError):
                parameter_summary.append({"field": "request body", "type": "unparsed content"})
        elif body:
            parameter_summary.append({"field": "request body", "type": "binary", "bytes": body_bytes})
    record = {
        "method": method, "path": path, "query": query, "content_type": media_type,
        "body_sha256": body_digest, "headers": behavior_headers,
    }
    fingerprint = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    preview = {
        "method": method,
        "path": _redact_api_path(path),
        "content_type": media_type or "none",
        "request_bytes": body_bytes,
        "parameters": parameter_summary[:32],
        "risk": "deletion" if method == "DELETE" or path.endswith("/revoke") else ("sensitive action" if _api_mcp_sensitive_action(method, path) else "standard change"),
    }
    return fingerprint, preview


def _new_mcp_approval(token_hash: str, token_prefix: str, request: Request, fingerprint: str, preview: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(seconds=MCP_APPROVAL_TTL_SECONDS)).isoformat(timespec="seconds")
    request_id = secrets.token_urlsafe(24)
    with db_connection() as db:
        db.execute("DELETE FROM mcp_approvals WHERE (status IN ('expired','denied','used') AND created_at < ?) OR (status IN ('pending','approved') AND expires_at <= ?)", ((now - timedelta(days=30)).isoformat(timespec="seconds"), created_at))
        pending = db.execute("SELECT COUNT(*) FROM mcp_approvals WHERE token_hash=? AND status='pending' AND expires_at>?", (token_hash, created_at)).fetchone()[0]
        if pending >= MCP_APPROVAL_MAX_PENDING_PER_TOKEN:
            raise HTTPException(status_code=429, detail="This MCP token already has too many pending approvals. Review or let them expire before requesting another.")
        db.execute(
            "INSERT INTO mcp_approvals(request_id,token_hash,token_prefix,method,request_path,request_fingerprint,preview,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)",
            (request_id, token_hash, token_prefix, request.method.upper(), _redact_api_path(request.url.path), fingerprint, json.dumps(preview, separators=(",", ":")), created_at, expires_at),
        )
    return {"approval_required": True, "request_id": request_id, "expires_at": expires_at, "preview": preview, "message": "An administrator must approve this exact request in Settings before the MCP agent can retry it."}


def _consume_mcp_approval(request_id: str, token_hash: str, fingerprint: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", request_id):
        raise HTTPException(status_code=400, detail="Approval ID format is not valid.")
    now = utc_now()
    expired = False
    with db_connection() as db:
        row = db.execute("SELECT request_fingerprint,status,expires_at,preview FROM mcp_approvals WHERE request_id=? AND token_hash=?", (request_id, token_hash)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval request was not found for this token.")
        if row["expires_at"] <= now and row["status"] in {"pending", "approved"}:
            db.execute("UPDATE mcp_approvals SET status='expired' WHERE request_id=? AND status IN ('pending','approved')", (request_id,))
            expired = True
        elif row["status"] == "pending":
            raise HTTPException(status_code=428, detail={"approval_required": True, "request_id": request_id, "expires_at": row["expires_at"], "preview": json.loads(row["preview"]), "message": "This action is awaiting administrator approval."})
        elif row["status"] != "approved":
            raise HTTPException(status_code=409, detail=f"This approval is {row['status']} and cannot be used.")
        elif not hmac.compare_digest(str(row["request_fingerprint"]), fingerprint):
            raise HTTPException(status_code=409, detail="The request differs from the approved action. The approval remains unused.")
        else:
            cursor = db.execute(
                "UPDATE mcp_approvals SET status='used',used_at=? WHERE request_id=? AND token_hash=? AND request_fingerprint=? AND status='approved' AND expires_at>?",
                (now, request_id, token_hash, fingerprint, now),
            )
            if cursor.rowcount != 1:
                raise HTTPException(status_code=409, detail="This approval was already used, expired, or changed before it could be applied.")
    if expired:
        raise HTTPException(status_code=409, detail="This approval has expired. Submit the action again to request a fresh approval.")


def _record_api_access(token_hash: str, token_prefix: str, request: Request) -> None:
    global _api_last_audit_cleanup
    now = time.monotonic()
    with _api_request_lock:
        recent = [stamp for stamp in _api_request_times.get(token_hash, []) if now - stamp < 60]
        limit = int(get_api_settings()["api_rate_limit"])
        if len(recent) >= limit:
            _api_request_times[token_hash] = recent
            raise HTTPException(status_code=429, detail=f"API rate limit reached ({limit} requests per minute).", headers={"Retry-After": "60"})
        recent.append(now)
        _api_request_times[token_hash] = recent
        last_touch = _api_token_touch.get(token_hash, 0.0)
        should_touch = now - last_touch >= 60
        if should_touch:
            _api_token_touch[token_hash] = now
    if should_touch:
        try:
            with db_connection() as db:
                db.execute("UPDATE api_tokens SET last_used_at=? WHERE token_hash=? AND revoked_at IS NULL", (utc_now(), token_hash))
        except sqlite3.Error:
            logger.debug("Could not update API token last-used time", exc_info=True)
    try:
        with db_connection() as db:
            db.execute("INSERT INTO api_access_log(timestamp,token_prefix,method,path) VALUES(?,?,?,?)", (utc_now(), token_prefix, request.method, _redact_api_path(request.url.path)))
            if now - _api_last_audit_cleanup >= 300:
                db.execute("DELETE FROM api_access_log WHERE id NOT IN (SELECT id FROM api_access_log ORDER BY id DESC LIMIT 5000)")
                db.execute("DELETE FROM api_access_log WHERE timestamp < ?", ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds"),))
                _api_last_audit_cleanup = now
    except sqlite3.Error:
        logger.debug("Could not record API access audit entry", exc_info=True)


async def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
    bearer: HTTPAuthorizationCredentials | None = Depends(api_bearer),
) -> str:
    if bearer is None:
        request.state.vaos_api_scope = "admin"
        return require_basic_admin(request, credentials)
    settings = get_api_settings()
    if not settings["api_enabled"]:
        raise HTTPException(status_code=403, detail="API access is disabled in Settings.")
    supplied = bearer.credentials
    token_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    try:
        with db_connection() as db:
            row = db.execute("SELECT token_hash,token_prefix,scopes,expires_at,revoked_at FROM api_tokens WHERE token_hash=?", (token_hash,)).fetchone()
    except sqlite3.Error as exc:
        logger.exception("Could not validate an API token")
        raise HTTPException(status_code=503, detail="API token storage is unavailable.") from exc
    if not row or row["revoked_at"] or (row["expires_at"] and row["expires_at"] <= utc_now()):
        raise HTTPException(status_code=401, detail="API token is invalid, expired, or revoked.", headers={"WWW-Authenticate": "Bearer"})
    try:
        stored_access = json.loads(row["scopes"])
        if isinstance(stored_access, dict):
            token_scope = str(stored_access.get("access", "read"))
            token_groups = [group for group in stored_access.get("groups", API_DEFAULT_TOKEN_GROUPS) if group in API_MCP_GROUPS]
            token_sensitive_read = stored_access.get("sensitive_read") is True
            token_mcp_agent = stored_access.get("mcp_agent") is True
        else:  # Accept tokens created by the earlier list-only format.
            legacy_scopes = set(stored_access if isinstance(stored_access, list) else [])
            token_scope = "control" if "control" in legacy_scopes else "read"
            token_groups = [item[6:] for item in legacy_scopes if item.startswith("group:") and item[6:] in API_MCP_GROUPS] or list(API_DEFAULT_TOKEN_GROUPS)
            token_sensitive_read = False
            token_mcp_agent = False
    except (TypeError, json.JSONDecodeError):
        token_scope, token_groups, token_sensitive_read, token_mcp_agent = "", [], False, False
    # Refuse remote clear-text bearer requests before any approval row or other
    # application-side effect can be created. Forwarded HTTPS is trusted only
    # from an explicitly configured reverse-proxy network.
    peer_ip = None
    try:
        peer_ip = ipaddress.ip_address(request.client.host) if request.client and request.client.host else None
    except ValueError:
        pass
    peer_loopback = bool(peer_ip and peer_ip.is_loopback)
    trusted_proxy = False
    if peer_ip and not peer_loopback:
        for network_text in os.getenv("VAOS_TRUSTED_PROXY_CIDRS", "").split(","):
            try:
                if peer_ip in ipaddress.ip_network(network_text.strip(), strict=False):
                    trusted_proxy = True
                    break
            except ValueError:
                continue
    forwarded_https = trusted_proxy and request.headers.get("x-forwarded-proto", "").casefold() == "https"
    cloudflare_https = trusted_proxy and '"scheme":"https"' in request.headers.get("cf-visitor", "").replace(" ", "").casefold()
    if request.url.scheme != "https" and not peer_loopback and not forwarded_https and not cloudflare_https:
        raise HTTPException(status_code=400, detail="Bearer API tokens require HTTPS when connecting remotely.")
    request_path = request.url.path
    is_mcp_invocation_telemetry = (
        request.method.upper() == "POST"
        and request_path in MCP_INVOCATION_TELEMETRY_PATHS
        and token_mcp_agent
    )
    if token_scope != "control" and request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and not is_mcp_invocation_telemetry:
        raise HTTPException(status_code=403, detail="This API token is read-only. Issue a control token in Settings for write access.")
    if token_scope not in {"read", "control"}:
        raise HTTPException(status_code=403, detail="This API token has no valid access scope.")
    request.state.vaos_api_scope = token_scope
    request.state.vaos_api_groups = token_groups
    request.state.vaos_api_sensitive_read = token_sensitive_read
    request.state.vaos_api_mcp_agent = token_mcp_agent
    request.state.vaos_api_token_hash = token_hash
    request.state.vaos_api_token_prefix = str(row["token_prefix"])
    if request_path not in {"/api/openapi.json", "/api/admin/integrations/mcp-policy"} and not is_mcp_invocation_telemetry:
        required_group = _api_request_group(request_path)
        if required_group not in token_groups:
            raise HTTPException(status_code=403, detail=f"This API token is not allowed to access the '{required_group}' capability group.")
    if request.method.upper() in {"GET", "HEAD"} and _api_read_is_sensitive(request_path):
        if not settings["api_allow_sensitive_reads"] or not token_sensitive_read:
            raise HTTPException(status_code=403, detail="Sensitive profile, camera, event, or sample-image data requires both the Settings opt-in and a token explicitly granted sensitive-read access.")
    is_write = request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
    if token_mcp_agent:
        if not settings["mcp_enabled"]:
            raise HTTPException(status_code=403, detail="The MCP bridge is disabled in VAOS Settings.")
        if is_write and not is_mcp_invocation_telemetry:
            if settings["mcp_read_only"]:
                raise HTTPException(status_code=403, detail="MCP is in read-only mode. Turn off read-only mode in VAOS Settings before requesting a change.")
            if request.method.upper() == "DELETE" or request_path.endswith("/revoke"):
                if not settings["mcp_allow_deletes"]:
                    raise HTTPException(status_code=403, detail="MCP deletion actions are disabled in VAOS Settings.")
            elif _api_mcp_sensitive_action(request.method, request_path) and not settings["mcp_allow_sensitive"]:
                raise HTTPException(status_code=403, detail="This sensitive MCP action is disabled in VAOS Settings.")
        required_group = _api_request_group(request_path)
        if request_path not in {"/api/openapi.json", "/api/admin/integrations/mcp-policy"} and not is_mcp_invocation_telemetry and required_group not in settings["mcp_allowed_groups"]:
            raise HTTPException(status_code=403, detail=f"The '{required_group}' capability is disabled for MCP in VAOS Settings.")
        if is_write and not is_mcp_invocation_telemetry:
            fingerprint, preview = await _mcp_request_fingerprint(request)
            approval_id = request.headers.get("x-vaos-mcp-approval-id", "").strip()
            if not approval_id:
                raise HTTPException(status_code=428, detail=_new_mcp_approval(token_hash, str(row["token_prefix"]), request, fingerprint, preview))
            _consume_mcp_approval(approval_id, token_hash, fingerprint)
    if not hmac.compare_digest(str(row["token_hash"]), token_hash):
        raise HTTPException(status_code=401, detail="API token is invalid.")
    _record_api_access(token_hash, str(row["token_prefix"]), request)
    return "api:" + str(row["token_prefix"])


@contextmanager
def db_connection():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_engine_settings() -> dict[str, Any]:
    """Load persisted, bounded runtime controls; defaults preserve legacy behavior."""
    values = dict(ENGINE_SETTING_DEFAULTS)
    try:
        with db_connection() as db:
            rows = db.execute("SELECT key,value FROM app_settings WHERE key IN (" + ",".join("?" for _ in values) + ")", tuple(values)).fetchall()
        values.update({str(row["key"]): str(row["value"]) for row in rows})
    except sqlite3.Error:
        logger.exception("Could not load engine settings; using safe defaults")
    try:
        return {
            "face_matching_enabled": values["face_matching_enabled"] == "1",
            "object_detection_enabled": values["object_detection_enabled"] == "1",
            "object_tracking_enabled": values["object_tracking_enabled"] == "1",
            "match_threshold": min(0.85, max(0.2, float(values["match_threshold"]))),
            "event_cooldown_seconds": min(3600, max(15, int(values["event_cooldown_seconds"]))),
            "object_confidence": min(0.95, max(0.05, float(values["object_confidence"]))),
            "object_image_size": int(values["object_image_size"]) if int(values["object_image_size"]) in {320, 416, 512, 640, 768} else 512,
            "camera_frame_interval_ms": int(values["camera_frame_interval_ms"]) if int(values["camera_frame_interval_ms"]) in {250, 500, 750, 1000, 1500} else 500,
            "training_status_refresh_seconds": min(10, max(1, int(values["training_status_refresh_seconds"]))),
            "camera_max_dimension": int(values["camera_max_dimension"]) if int(values["camera_max_dimension"]) in {480, 640, 800, 960, 1280} else 640,
        }
    except (KeyError, TypeError, ValueError):
        logger.exception("Invalid saved engine setting; using defaults")
        return {key: value == "1" if key.endswith("_enabled") else int(value) if value.isdigit() else float(value) for key, value in ENGINE_SETTING_DEFAULTS.items()}


def get_system_metrics() -> dict[str, Any]:
    """Return lightweight host telemetry, with GPU fields when NVIDIA tooling is available."""
    now = time.monotonic()
    with system_metrics_lock:
        if now - float(system_metrics_cache.get("sampled_at", 0.0)) < 3:
            return dict(system_metrics_cache.get("data", {}))
        metrics: dict[str, Any] = {
            "cpu_percent": None,
            "memory_percent": None,
            "memory_used_gb": None,
            "memory_total_gb": None,
            "disk_used_bytes": None,
            "disk_free_bytes": None,
            "disk_total_bytes": None,
            "disk_used_percent": None,
            "gpu_available": False,
            "gpu_name": "Unavailable",
            "gpu_utilization_percent": None,
            "gpu_memory_used_mb": None,
            "gpu_memory_total_mb": None,
            "gpu_temperature_c": None,
            "gpu_power_draw_w": None,
            "gpu_devices": [],
        }
        try:
            import psutil
            memory = psutil.virtual_memory()
            metrics.update({"cpu_percent": psutil.cpu_percent(interval=None), "memory_percent": memory.percent, "memory_used_bytes": int(memory.total - memory.available), "memory_total_bytes": int(memory.total), "memory_used_gb": round((memory.total - memory.available) / (1024 ** 3), 2), "memory_total_gb": round(memory.total / (1024 ** 3), 2)})
        except Exception:
            logger.debug("Host CPU/RAM metrics are unavailable", exc_info=True)
        try:
            disk = shutil.disk_usage(DATA_DIR)
            metrics.update({
                "disk_used_bytes": int(disk.used),
                "disk_free_bytes": int(disk.free),
                "disk_total_bytes": int(disk.total),
                "disk_used_percent": round((disk.used / disk.total) * 100, 2) if disk.total > 0 else None,
            })
        except (OSError, RuntimeError, ValueError):
            logger.debug("VAOS data-volume disk metrics are unavailable", exc_info=True)
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            try:
                command = [nvidia_smi, "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"]
                legacy_command = [nvidia_smi, "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"]
                try:
                    result = subprocess.run(command, capture_output=True, text=True, timeout=1.25, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except subprocess.CalledProcessError:
                    # Some supported devices/drivers report utilization but do not expose power.draw.
                    # Preserve the existing utilization telemetry instead of losing all GPU fields.
                    result = subprocess.run(legacy_command, capture_output=True, text=True, timeout=1.25, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                as_int = lambda value: int(value.strip()) if value.strip().isdigit() else None
                def as_float(value: str) -> float | None:
                    try:
                        number = float(value.strip())
                    except (TypeError, ValueError):
                        return None
                    return number if math.isfinite(number) and number >= 0 else None

                gpu_rows = []
                for line in result.stdout.splitlines():
                    row = next(csv.reader([line]))
                    if len(row) >= 7:
                        index = as_int(row[0])
                        name = row[2].strip()[:120] or "NVIDIA GPU"
                        raw_uuid = row[1].strip()
                        stable_device_key = raw_uuid if raw_uuid.startswith("GPU-") and len(raw_uuid) <= 120 else f"index:{index}|name:{name}"
                        installation_key = observability_installation_id or "local-installation"
                        device_id = hashlib.sha256(f"vaos-device|{installation_key}|{stable_device_key}".encode("utf-8")).hexdigest()[:32]
                        gpu_rows.append({
                            "index": index,
                            "device_id": device_id,
                            "name": name,
                            "utilization_percent": as_int(row[3]),
                            "memory_used_mb": as_int(row[4]),
                            "memory_total_mb": as_int(row[5]),
                            "temperature_c": as_int(row[6]),
                            "power_draw_w": as_float(row[7]) if len(row) >= 8 else None,
                        })
                if gpu_rows:
                    first = gpu_rows[0]
                    metrics.update({"gpu_available": True, "gpu_name": first["name"], "gpu_utilization_percent": first["utilization_percent"], "gpu_memory_used_mb": first["memory_used_mb"], "gpu_memory_total_mb": first["memory_total_mb"], "gpu_temperature_c": first["temperature_c"], "gpu_power_draw_w": first["power_draw_w"], "gpu_devices": gpu_rows})
            except (OSError, subprocess.SubprocessError, IndexError, csv.Error):
                logger.debug("NVIDIA system metrics are unavailable", exc_info=True)
        from host_telemetry import collect_host_telemetry, read_linux_gpu_metrics
        try:
            metrics.update(collect_host_telemetry())
        except Exception:
            logger.debug("Extended host telemetry unavailable", exc_info=True)
        if not metrics["gpu_available"]:
            metrics.update(read_linux_gpu_metrics())
        if not metrics["gpu_available"]:
            from apple_gpu_metrics import read_apple_gpu_metrics
            metrics.update(read_apple_gpu_metrics())
        system_metrics_cache.update({"sampled_at": now, "data": metrics})
        return dict(metrics)


def init_db() -> None:
    with db_connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cameras (
                token TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                map_x REAL NOT NULL DEFAULT 0.5,
                map_y REAL NOT NULL DEFAULT 0.5,
                heading REAL NOT NULL DEFAULT 0,
                source_type TEXT NOT NULL DEFAULT 'camera',
                source_ref TEXT
            );
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                embedding TEXT NOT NULL,
                profile_photo BLOB,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS participant_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                photo BLOB NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(person_id) REFERENCES participants(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS enrollment_links (
                token TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                camera_token TEXT NOT NULL,
                person_id INTEGER NOT NULL,
                score REAL NOT NULL,
                kind TEXT NOT NULL,
                FOREIGN KEY(camera_token) REFERENCES cameras(token),
                FOREIGN KEY(person_id) REFERENCES participants(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sightings_timestamp ON sightings(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_sightings_person ON sightings(person_id);
            CREATE TABLE IF NOT EXISTS camera_status (
                camera_token TEXT PRIMARY KEY, last_frame_at TEXT NOT NULL,
                faces_detected INTEGER NOT NULL DEFAULT 0, matched_count INTEGER NOT NULL DEFAULT 0,
                objects_json TEXT NOT NULL DEFAULT '[]', error TEXT,
                FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS object_sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                camera_token TEXT NOT NULL, class_name TEXT NOT NULL, count INTEGER NOT NULL,
                confidence REAL NOT NULL,
                FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_objects_timestamp ON object_sightings(timestamp DESC);
            CREATE TABLE IF NOT EXISTS object_event_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                rule_type TEXT NOT NULL,
                class_name TEXT NOT NULL,
                min_confidence REAL NOT NULL DEFAULT 0.35,
                threshold_count INTEGER NOT NULL DEFAULT 1,
                min_consecutive_frames INTEGER NOT NULL DEFAULT 3,
                cooldown_seconds INTEGER NOT NULL DEFAULT 60,
                dwell_seconds REAL,
                camera_token TEXT,
                region_json TEXT NOT NULL DEFAULT '{"x":0,"y":0,"width":1,"height":1}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_object_event_rules_active ON object_event_rules(enabled,camera_token,id);
            CREATE TABLE IF NOT EXISTS object_rule_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                camera_token TEXT NOT NULL,
                rule_id INTEGER,
                rule_name TEXT NOT NULL,
                rule_type TEXT NOT NULL,
                class_name TEXT NOT NULL,
                object_count INTEGER NOT NULL,
                message TEXT NOT NULL,
                FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE,
                FOREIGN KEY(rule_id) REFERENCES object_event_rules(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_object_rule_events_timestamp ON object_rule_events(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_object_rule_events_camera ON object_rule_events(camera_token,timestamp DESC);
            CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_tokens (
                token_hash TEXT PRIMARY KEY,
                token_prefix TEXT NOT NULL,
                label TEXT NOT NULL,
                scopes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                last_used_at TEXT,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_api_tokens_prefix ON api_tokens(token_prefix);
            CREATE TABLE IF NOT EXISTS api_access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                token_prefix TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_api_access_log_timestamp ON api_access_log(timestamp DESC);
            CREATE TABLE IF NOT EXISTS mcp_approvals (
                request_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL,
                token_prefix TEXT NOT NULL,
                method TEXT NOT NULL,
                request_path TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                preview TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','expired','used')),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT,
                decision_note TEXT NOT NULL DEFAULT '',
                used_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_approvals_status_expiry ON mcp_approvals(status,expires_at);
            CREATE INDEX IF NOT EXISTS idx_mcp_approvals_token_created ON mcp_approvals(token_hash,created_at DESC);
            CREATE TABLE IF NOT EXISTS ml_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                camera_token TEXT NOT NULL, suggested_label TEXT NOT NULL,
                image BLOB NOT NULL, bbox TEXT NOT NULL DEFAULT '0.5,0.5,0.9,0.9', confidence REAL NOT NULL DEFAULT 0,
                review_state TEXT NOT NULL DEFAULT 'pending', source_time_seconds REAL,
                confirmed_label TEXT, FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_ml_samples_review ON ml_samples(review_state, id DESC);
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(cameras)").fetchall()}
        for name, declaration in (("map_x", "REAL NOT NULL DEFAULT 0.5"), ("map_y", "REAL NOT NULL DEFAULT 0.5"), ("heading", "REAL NOT NULL DEFAULT 0")):
            if name not in columns:
                db.execute(f"ALTER TABLE cameras ADD COLUMN {name} {declaration}")
        if "source_type" not in columns:
            db.execute("ALTER TABLE cameras ADD COLUMN source_type TEXT NOT NULL DEFAULT 'camera'")
        if "source_ref" not in columns:
            db.execute("ALTER TABLE cameras ADD COLUMN source_ref TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_cameras_source ON cameras(source_type, source_ref)")
        participant_columns = {row["name"] for row in db.execute("PRAGMA table_info(participants)").fetchall()}
        sample_columns = {row["name"] for row in db.execute("PRAGMA table_info(ml_samples)").fetchall()}
        if "bbox" not in sample_columns:
            db.execute("ALTER TABLE ml_samples ADD COLUMN bbox TEXT NOT NULL DEFAULT '0.5,0.5,0.9,0.9'")
        if "confidence" not in sample_columns:
            db.execute("ALTER TABLE ml_samples ADD COLUMN confidence REAL NOT NULL DEFAULT 0")
        if "source_time_seconds" not in sample_columns:
            db.execute("ALTER TABLE ml_samples ADD COLUMN source_time_seconds REAL")
        if "profile_photo" not in participant_columns:
            db.execute("ALTER TABLE participants ADD COLUMN profile_photo BLOB")
    try:
        observability_store.initialize_database(DB_PATH)
    except (OSError, sqlite3.Error, RuntimeError):
        # Observability is additive. A failed backup or migration must not
        # prevent the existing dashboard, camera, or training APIs from starting.
        logger.exception("Observability schema upgrade was skipped; existing VAOS data was left unchanged")
    try:
        observability_store.initialize_counter_fallback_store(DB_PATH)
    except Exception as exc:
        # The process-local capped maps remain available if the secondary store
        # cannot be secured or created; this must not block the application.
        logger.warning("The durable observability fallback could not be initialized (%s); memory fallback remains enabled", type(exc).__name__)
    try:
        observability_siem.initialize_security_event_fallback_store(DB_PATH)
    except Exception as exc:
        # This sidecar is an optional recovery layer; primary audit writes still run normally.
        logger.warning("The authentication security-event recovery queue could not be initialized (%s)", type(exc).__name__)


init_db()


def get_active_tracking_rules() -> list[dict[str, Any]]:
    """Read active rule definitions through a short process-local cache."""
    now = time.monotonic()
    with tracking_rule_cache_lock:
        if now - float(tracking_rule_cache.get("loaded_at", 0.0)) < TRACKING_RULE_CACHE_SECONDS:
            return [dict(rule) for rule in tracking_rule_cache.get("rows", [])]
        with db_connection() as db:
            rows = db.execute(
                "SELECT id,name,enabled,rule_type,class_name,min_confidence,threshold_count,"
                "min_consecutive_frames,cooldown_seconds,dwell_seconds,camera_token,region_json "
                "FROM object_event_rules WHERE enabled=1 ORDER BY id"
            ).fetchall()
        active: list[dict[str, Any]] = []
        for row in rows:
            try:
                stored = dict(row)
                region = json.loads(stored.pop("region_json"))
                rule = validate_rule({**stored, "region": region})
                rule.update({"id": int(stored["id"]), "enabled": bool(stored["enabled"])})
                active.append(rule)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                logger.warning("Skipping malformed object tracking rule id=%s", row["id"])
        tracking_rule_cache.update({"loaded_at": now, "rows": active})
        return [dict(rule) for rule in active]


def invalidate_tracking_rule_cache(rule_id: int | None = None) -> None:
    with tracking_rule_cache_lock:
        tracking_rule_cache["loaded_at"] = 0.0
    with tracking_rule_state_lock:
        if rule_id is None:
            tracking_rule_state.clear()
        else:
            for key in [key for key in tracking_rule_state if len(key) > 1 and key[1] == rule_id]:
                tracking_rule_state.pop(key, None)


def save_object_rule_events(camera_token: str, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    with db_connection() as db:
        db.executemany(
            "INSERT INTO object_rule_events(timestamp,camera_token,rule_id,rule_name,rule_type,class_name,object_count,message) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(utc_now(), camera_token, event["rule_id"], event["rule_name"], event["rule_type"],
              event["class_name"], event["object_count"], event["message"]) for event in events],
        )
        db.execute(
            "DELETE FROM object_rule_events WHERE id NOT IN "
            "(SELECT id FROM object_rule_events ORDER BY id DESC LIMIT ?)",
            (MAX_OBJECT_RULE_EVENTS,),
        )


def ensure_model_files() -> None:
    for model_name, path in MODEL_PATHS.items():
        # Prefer either requested project-root models or previously downloaded
        # data/models copies. Never replace a valid local model.
        candidates = [path, DATA_DIR / "models" / path.name]
        valid = next((candidate for candidate in candidates if _valid_model_file(model_name, candidate)), None)
        if valid is not None:
            MODEL_PATHS[model_name] = valid
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        url = MODEL_URLS[model_name]
        logger.info("Downloading %s model from OpenCV Zoo (first use only)...", model_name)
        # Keep .onnx as the final suffix: OpenCV chooses its ONNX parser from
        # the extension when constructing FaceRecognizerSF/FaceDetectorYN.
        tmp_path = path.with_name(f"{path.stem}.part{path.suffix}")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "VAOS/1.0.0", "Accept": "application/octet-stream"})
            with urllib.request.urlopen(request, timeout=60) as response, tmp_path.open("wb") as output:
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    raise RuntimeError("The model host returned an HTML page instead of a model file")
                total = 0
                digest = hashlib.sha256()
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 250 * 1024 * 1024:
                        raise RuntimeError(f"Model download unexpectedly large: {model_name}")
                    output.write(chunk)
                    digest.update(chunk)
            if total < MODEL_MIN_BYTES[model_name]:
                raise RuntimeError(f"Downloaded {model_name} model is only {total:,} bytes; expected at least {MODEL_MIN_BYTES[model_name]:,}. The server may have returned an error page or Git LFS pointer.")
            expected_hash = MODEL_SHA256.get(model_name)
            if expected_hash and not hmac.compare_digest(digest.hexdigest(), expected_hash):
                raise RuntimeError(f"Downloaded {model_name} model checksum did not match the published file")
            if not _valid_model_file(model_name, tmp_path):
                raise RuntimeError(f"Downloaded {model_name} file is not a readable ONNX model")
            tmp_path.replace(path)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"Could not prepare the {model_name} model at {path}. Check the model path and internet access. {exc}") from exc


def _valid_model_file(model_name: str, path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < MODEL_MIN_BYTES[model_name]:
            return False
        expected_hash = MODEL_SHA256.get(model_name)
        if expected_hash:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if not hmac.compare_digest(digest, expected_hash):
                return False
        # Creating the OpenCV wrapper parses the ONNX graph and rejects LFS
        # pointers, HTML, truncation, or other invalid model files.
        if model_name == "yunet":
            cv2.FaceDetectorYN.create(str(path), "", (320, 320), 0.7, 0.3, 5000)
        elif model_name == "sface":
            cv2.FaceRecognizerSF.create(str(path), "")
        else:
            cv2.dnn.readNet(str(path))
        return True
    except (OSError, cv2.error, RuntimeError):
        return False


def get_models():
    global face_detector, face_recognizer
    with model_lock:
        if face_detector is None or face_recognizer is None:
            ensure_model_files()
            face_detector = cv2.FaceDetectorYN.create(str(MODEL_PATHS["yunet"]), "", (320, 320), 0.70, 0.30, 5000)
            face_recognizer = cv2.FaceRecognizerSF.create(str(MODEL_PATHS["sface"]), "")
            logger.info("Local YuNet and SFace face models are ready on CPU")
        return face_detector, face_recognizer


def get_object_detector():
    global object_detector, object_detector_attempted, object_detector_last_attempt, compute_device
    with model_lock:
        # Face enrollment and matching must still work when object-detection
        # packages, weights, CUDA drivers, or the network are unavailable.
        # Retry failed initialization periodically. A one-time transient startup
        # failure must not disable detection until the next full restart.
        can_retry = not object_detector_attempted or time.monotonic() - object_detector_last_attempt >= 20
        if object_detector is None and can_retry:
            object_detector_attempted = True
            object_detector_last_attempt = time.monotonic()
            try:
                model_path = selected_object_model_path()
                object_detector = YOLO26Runtime(str(model_path if model_path.is_file() else "yolo26m.pt"))
                compute_device = object_detector.device_label + "; face models on CPU"
                logger.info("YOLO26m object detector is ready (%s)", compute_device)
            except Exception as exc:
                model_errors["yolo26m"] = str(exc)[:400]
                compute_device = "YOLO26m unavailable; face models on CPU"
                logger.exception("YOLO26m could not be loaded; face recognition remains available")
        return object_detector


class YOLO26Runtime:
    """YOLO object detector using CUDA, Apple MPS, or CPU with safe fallback."""
    def __init__(self, model_path: str, device_override: str | None = None):
        import torch
        from ultralytics import YOLO

        selected = resolve_inference_backend(torch, device_override or os.getenv("OBJECT_DETECTOR_DEVICE", "auto"))
        self.device_kind = str(selected["backend"])
        self.use_cuda = self.device_kind == "cuda"
        self.use_mps = self.device_kind == "mps"
        self.mps_available = bool(selected["mps_available"])
        self.device = selected["device"]
        if self.use_cuda:
            try:
                self.device_label = f"YOLO26m · NVIDIA GPU 0 ({torch.cuda.get_device_name(0)})"
            except Exception:
                self.device_label = "YOLO26m · CUDA GPU 0"
        elif self.use_mps:
            self.device_label = "YOLO26m · Apple GPU (MPS)"
        else:
            self.device_label = "YOLO26m · CPU"
        if not selected["requested_backend_available"]:
            logger.warning("Requested object-inference backend %s is unavailable; using CPU", os.getenv("OBJECT_DETECTOR_DEVICE"))
        self.model = YOLO(model_path)
        self.class_names = [str(name) for _, name in sorted(self.model.names.items())] if isinstance(self.model.names, dict) else [str(name) for name in self.model.names]
        self.model_path = str(Path(self.model.ckpt_path).resolve())
        logger.info("YOLO26m object model loaded from %s on %s", self.model_path, self.device_label)

    def infer(self, image: np.ndarray, confidence: float = 0.25, image_size: int = 512) -> list[dict[str, Any]]:
        try:
            predictions = self.model.predict(
                source=image,
                imgsz=image_size,
                conf=confidence,
                device=self.device,
                half=self.use_cuda,
                verbose=False,
            )
        except Exception:
            if self.device_kind == "cpu":
                raise
            failed_backend = self.device_kind
            logger.exception("YOLO26m %s inference failed; switching to CPU", failed_backend.upper())
            self.device_kind = "cpu"
            self.use_cuda = False
            self.use_mps = False
            self.device = "cpu"
            self.device_label = f"YOLO26m · CPU ({failed_backend.upper()} inference failed)"
            global compute_device
            compute_device = self.device_label + "; face models on CPU"
            predictions = self.model.predict(source=image, imgsz=image_size, conf=confidence, device="cpu", verbose=False)
        if not predictions or predictions[0].boxes is None:
            return []
        boxes = predictions[0].boxes
        xyxy = boxes.xyxy.detach().cpu().numpy()
        scores = boxes.conf.detach().cpu().numpy()
        class_ids = boxes.cls.detach().cpu().numpy().astype(int)
        results = []
        names = self.model.names
        for box, score, class_id in zip(xyxy, scores, class_ids):
            if class_id not in names:
                continue
            x1, y1, x2, y2 = (int(v) for v in box)
            results.append({"label": str(names[class_id]), "score": round(float(score), 3), "box": [x1, y1, x2, y2]})
        return results


def decode_image(content: bytes) -> np.ndarray:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError("Image is empty or larger than 6 MB")
    array = np.frombuffer(content, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not read the uploaded image")
    return image


def encode_profile_photo(content: bytes) -> bytes:
    image = decode_image(content)
    height, width = image.shape[:2]
    if width * height > 24_000_000:
        raise ValueError("Each photo must be smaller than 24 megapixels")
    longest = max(width, height)
    if longest > 1600:
        scale = 1600 / longest
        image = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("Could not save this profile photo. Please choose another image.")
    return encoded.tobytes()


def detect_faces(image: np.ndarray):
    detector, recognizer = get_models()
    height, width = image.shape[:2]
    detector.setInputSize((width, height))
    _, faces = detector.detect(image)
    if faces is None:
        return [], recognizer
    return list(faces), recognizer


def feature_for_face(image: np.ndarray, face: np.ndarray, recognizer) -> np.ndarray:
    aligned = recognizer.alignCrop(image, face)
    feature = recognizer.feature(aligned).astype(np.float32).reshape(-1)
    norm = float(np.linalg.norm(feature))
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("Could not calculate a usable face template")
    return feature / norm


def encode_embedding(embedding: np.ndarray) -> str:
    # Store only the numerical representation, not the source photograph.
    return json.dumps([round(float(x), 7) for x in embedding.tolist()], separators=(",", ":"))


def extract_enrollment_embedding(content: bytes) -> np.ndarray:
    image = decode_image(content)
    # Serialise model calls because OpenCV's detector input size is mutable.
    with recognition_lock:
        faces, recognizer = detect_faces(image)
        if not faces:
            raise ValueError("No face found. Upload a clear, front-facing photo with good lighting.")
        if len(faces) > 1:
            raise ValueError("More than one face found. Upload a photo with only one consenting participant.")
        face = faces[0]
        if float(face[2]) < 60 or float(face[3]) < 60:
            raise ValueError("The face is too small. Upload a closer, clearer photo.")
        return feature_for_face(image, face, recognizer)


def find_best_match(embedding: np.ndarray, threshold: float | None = None) -> tuple[int, str, float] | None:
    with db_connection() as db:
        rows = db.execute("SELECT id, name, embedding FROM participants").fetchall()
    best = None
    for row in rows:
        try:
            reference = np.asarray(json.loads(row["embedding"]), dtype=np.float32)
            ref_norm = float(np.linalg.norm(reference))
            if ref_norm == 0:
                continue
            score = float(np.dot(embedding, reference / ref_norm))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if best is None or score > best[2]:
            best = (int(row["id"]), str(row["name"]), score)
    if best and best[2] >= (MATCH_THRESHOLD if threshold is None else threshold):
        return best
    return None


def process_frame(token: str, content: bytes) -> dict[str, Any]:
    image = decode_image(content)
    engine_settings = get_engine_settings()
    # Keep object analytics independent from face-model availability.
    with recognition_lock:
        face_error = ""
        if engine_settings["face_matching_enabled"]:
            try:
                faces, recognizer = detect_faces(image)
            except Exception:
                faces, recognizer = [], None
                face_error = "Face analysis is unavailable; object detection continues."
                logger.exception("Face analysis failed; continuing object detection")
        else:
            faces, recognizer = [], None
        detector = get_object_detector() if engine_settings["object_detection_enabled"] else None
        objects = detector.infer(image, confidence=engine_settings["object_confidence"], image_size=engine_settings["object_image_size"]) if detector else []
        # Dataset capture is opt-in from the private ML page. Only detected object
        # crops are retained, never the full camera frame.
        with db_connection() as db:
            capture_enabled = db.execute("SELECT value FROM app_settings WHERE key='ml_capture_enabled'").fetchone()
            sample_count = int(db.execute("SELECT COUNT(*) FROM ml_samples").fetchone()[0])
        if capture_enabled and capture_enabled["value"] == "1":
            for obj in objects:
                if sample_count >= MAX_ML_SAMPLES:
                    break
                capture_key = (token, obj["label"])
                if time.monotonic() - last_ml_capture.get(capture_key, 0.0) < 3:
                    continue
                x1, y1, x2, y2 = obj["box"]
                box_width, box_height = x2 - x1, y2 - y1
                pad_x, pad_y = max(4, int(box_width * 0.18)), max(4, int(box_height * 0.18))
                left, top = max(0, x1 - pad_x), max(0, y1 - pad_y)
                right, bottom = min(image.shape[1], x2 + pad_x), min(image.shape[0], y2 + pad_y)
                crop = image[top:bottom, left:right]
                if crop.size == 0:
                    continue
                ok, encoded_crop = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
                if ok:
                    with db_connection() as db:
                        normalized_box = f"{(x1-left)/max(1,right-left):.6f},{(y1-top)/max(1,bottom-top):.6f},{box_width/max(1,right-left):.6f},{box_height/max(1,bottom-top):.6f}"
                        inserted = db.execute("INSERT INTO ml_samples(created_at,camera_token,suggested_label,image,bbox,confidence) SELECT ?,?,?,?,?,? WHERE (SELECT COUNT(*) FROM ml_samples) < ?", (utc_now(), token, obj["label"], encoded_crop.tobytes(), normalized_box, float(obj["score"]), MAX_ML_SAMPLES)).rowcount
                    if inserted:
                        sample_count += 1
                        last_ml_capture[capture_key] = time.monotonic()
        if engine_settings["object_tracking_enabled"]:
            now_track = time.monotonic()
            existing_tracks = [track for track in live_tracks.get(token, []) if now_track - track["seen"] <= 5.0]
            next_track_id = max((track["id"] for track in existing_tracks), default=0) + 1
            for obj in objects:
                x1, y1, x2, y2 = obj["box"]
                candidates = []
                for track in existing_tracks:
                    if track["label"] != obj["label"] or track.get("used"):
                        continue
                    ax1, ay1, ax2, ay2 = track["box"]
                    intersection = max(0, min(x2, ax2) - max(x1, ax1)) * max(0, min(y2, ay2) - max(y1, ay1))
                    union = max(1, (x2-x1)*(y2-y1) + (ax2-ax1)*(ay2-ay1) - intersection)
                    candidates.append((intersection / union, track))
                best = max(candidates, key=lambda item: item[0], default=(0, None))
                if best[0] >= 0.25:
                    track = best[1]
                    track.setdefault("rule_uid", secrets.token_hex(12))
                    track["box"], track["seen"], track["used"] = obj["box"], now_track, True
                else:
                    track = {"id": next_track_id, "rule_uid": secrets.token_hex(12), "label": obj["label"], "box": obj["box"], "seen": now_track, "used": True}
                    existing_tracks.append(track)
                    next_track_id += 1
                obj["track_id"] = track["id"]
                obj["_tracking_key"] = track["rule_uid"]
            for track in existing_tracks:
                track.pop("used", None)
            live_tracks[token] = existing_tracks
        else:
            live_tracks.pop(token, None)
            for obj in objects:
                obj["track_id"] = None
        matched: list[dict[str, Any]] = []
        for face in faces:
            if float(face[2]) < 45 or float(face[3]) < 45:
                continue
            try:
                embedding = feature_for_face(image, face, recognizer)
            except (ValueError, cv2.error):
                continue
            match = find_best_match(embedding, engine_settings["match_threshold"])
            if not match:
                # Unknown faces are intentionally not enrolled or stored.
                continue
            person_id, name, score = match
            matched.append({"id": person_id, "name": name, "score": round(score, 3)})
            key = (token, person_id)
            now = time.monotonic()
            previous = last_emitted.get(key, 0.0)
            if now - previous >= engine_settings["event_cooldown_seconds"]:
                with db_connection() as db:
                    count = db.execute(
                        "SELECT COUNT(*) AS n FROM sightings WHERE person_id = ?", (person_id,)
                    ).fetchone()["n"]
                    kind = "REPEAT" if count else "FIRST SEEN"
                    db.execute(
                        "INSERT INTO sightings(timestamp, camera_token, person_id, score, kind) VALUES (?, ?, ?, ?, ?)",
                        (utc_now(), token, person_id, score, kind),
                    )
                last_emitted[key] = now
    object_counts: dict[str, list[float]] = {}
    for obj in objects:
        entry = object_counts.setdefault(obj["label"], [0, 0.0])
        entry[0] += 1
        entry[1] += obj["score"]
    person_count = int(object_counts.get("person", [0])[0])
    occupancy = "Quiet" if person_count <= 2 else "Busy" if person_count <= 6 else "Crowded"
    with db_connection() as db:
        db.execute(
            "INSERT INTO camera_status(camera_token,last_frame_at,faces_detected,matched_count,objects_json,error) VALUES(?,?,?,?,?,'') "
            "ON CONFLICT(camera_token) DO UPDATE SET last_frame_at=excluded.last_frame_at,faces_detected=excluded.faces_detected,matched_count=excluded.matched_count,objects_json=excluded.objects_json,error=''",
            (token, utc_now(), len(faces), len(matched), json.dumps(object_counts)),
        )
        now_mono = time.monotonic()
        for label, (count, confidence_sum) in object_counts.items():
            key = (token, label)
            if now_mono - last_object_emitted.get(key, 0) >= OBJECT_EVENTS_COOLDOWN:
                db.execute("INSERT INTO object_sightings(timestamp,camera_token,class_name,count,confidence) VALUES(?,?,?,?,?)", (utc_now(), token, label, count, confidence_sum / count))
                last_object_emitted[key] = now_mono
    rule_events: list[dict[str, Any]] = []
    try:
        active_rules = get_active_tracking_rules()
        if active_rules:
            with tracking_rule_state_lock:
                rule_events = evaluate_rules(
                    rules=active_rules,
                    camera_token=token,
                    objects=objects,
                    frame_size=(int(image.shape[1]), int(image.shape[0])),
                    state=tracking_rule_state,
                    now=time.monotonic(),
                    tracking_enabled=engine_settings["object_tracking_enabled"],
                )
            save_object_rule_events(token, rule_events)
    except Exception:
        # Rule storage/evaluation must never take the camera's core detection
        # path down; the admin activity feed will show the last saved event.
        logger.exception("Object event rule processing failed; frame detection is preserved")
    for obj in objects:
        obj.pop("_tracking_key", None)
    return {
        "faces_detected": len(faces),
        "matched": matched,
        "objects": objects,
        "face_analysis_error": face_error,
        "frame_size": [int(image.shape[1]), int(image.shape[0])],
        "person_count": person_count,
        "occupancy": occupancy,
        "threshold": engine_settings["match_threshold"],
        "face_matching_enabled": engine_settings["face_matching_enabled"],
        "object_detection_enabled": engine_settings["object_detection_enabled"],
        "object_tracking_enabled": engine_settings["object_tracking_enabled"],
        "note": "Similarity scores are not probabilities. Unknown faces are not saved.",
    }


video_cpu_detector = None


def process_video_review_frame(token: str, content: bytes, source_time_seconds: float, save_samples: bool, analysis_device: str = "auto") -> dict[str, Any]:
    """Detect objects in a browser-decoded local video frame and optionally queue crops for review."""
    image = decode_image(content)
    engine_settings = get_engine_settings()
    if not engine_settings["object_detection_enabled"]:
        raise RuntimeError("Object detection is turned off in Settings. Enable it, then retry.")
    with recognition_lock:
        global video_cpu_detector
        if analysis_device == "cpu":
            model_path = str(selected_object_model_path())
            stamp = (model_path, Path(model_path).stat().st_mtime_ns)
            if video_cpu_detector is None or video_cpu_detector[0] != stamp:
                video_cpu_detector = (stamp, YOLO26Runtime(model_path, device_override="cpu"))
            detector = video_cpu_detector[1]
        else:
            detector = get_object_detector()
        if detector is None:
            raise RuntimeError("The object detector is not ready. Check Model details and retry.")
        if analysis_device == "cpu":
            import torch
            previous_threads = torch.get_num_threads()
            try:
                torch.set_num_threads(available_cpu_threads())
                objects = detector.infer(image, confidence=engine_settings["object_confidence"], image_size=engine_settings["object_image_size"])
            finally:
                torch.set_num_threads(previous_threads)
        else:
            objects = detector.infer(image, confidence=engine_settings["object_confidence"], image_size=engine_settings["object_image_size"])

    with db_connection() as db:
        remaining = max(0, MAX_ML_SAMPLES - int(db.execute("SELECT COUNT(*) FROM ml_samples").fetchone()[0])) if save_samples else 0
    saved = 0
    for obj in objects:
        if not save_samples or remaining <= 0:
            break
        x1, y1, x2, y2 = (int(value) for value in obj["box"])
        box_width, box_height = x2 - x1, y2 - y1
        if box_width < 2 or box_height < 2:
            continue
        pad_x, pad_y = max(4, int(box_width * 0.18)), max(4, int(box_height * 0.18))
        left, top = max(0, x1 - pad_x), max(0, y1 - pad_y)
        right, bottom = min(image.shape[1], x2 + pad_x), min(image.shape[0], y2 + pad_y)
        crop = image[top:bottom, left:right]
        if crop.size == 0:
            continue
        ok, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            continue
        normalized_box = f"{(x1-left)/max(1,right-left):.6f},{(y1-top)/max(1,bottom-top):.6f},{box_width/max(1,right-left):.6f},{box_height/max(1,bottom-top):.6f}"
        with db_connection() as db:
            inserted = db.execute(
                "INSERT INTO ml_samples(created_at,camera_token,suggested_label,image,bbox,confidence,source_time_seconds) "
                "SELECT ?,?,?,?,?,?,? WHERE (SELECT COUNT(*) FROM ml_samples) < ?",
                (utc_now(), token, obj["label"], encoded.tobytes(), normalized_box, float(obj["score"]), max(0.0, source_time_seconds), MAX_ML_SAMPLES),
            ).rowcount
        if inserted:
            saved += 1
            remaining -= 1

    return {
        "objects": objects,
        "frame_size": [int(image.shape[1]), int(image.shape[0])],
        "samples_saved": saved,
        "sample_limit_reached": save_samples and remaining <= 0,
        "samples_enabled": save_samples,
    }


@app.get("/api/openapi.json", include_in_schema=False)
def api_openapi_schema(_: str = Depends(require_admin)) -> dict[str, Any]:
    """Serve an authenticated OpenAPI 3 contract for the stable versioned API."""
    # FastAPI caches the generated schema. Transform a copy so repeated requests
    # keep the underlying unversioned schema intact.
    schema = deepcopy(app.openapi())
    versioned_paths: dict[str, Any] = {}
    for path, operations in schema.get("paths", {}).items():
        if (not path.startswith("/api/") and path != "/health") or path in {"/api/openapi.json", "/api/docs"}:
            continue
        versioned_path = "/health" if path == "/health" else path[len("/api"):]
        for method, operation in operations.items():
            if not isinstance(operation, dict) or method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if path.startswith("/api/admin/"):
                if "/integrations" in path or "/api-tokens" in path:
                    operation["security"] = [{"HTTPBasic": []}]
                else:
                    operation["security"] = [{"HTTPBasic": []}, {"VAOSBearer": []}]
        versioned_paths[versioned_path] = operations
    schema["paths"] = versioned_paths
    schema["servers"] = [{"url": "/api/v1", "description": "VAOS stable API v1"}]
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["VAOSBearer"] = {
        "type": "http", "scheme": "bearer", "bearerFormat": "VAOS API token",
        "description": "Short-lived or expiring token issued in VAOS Settings. Read tokens only perform safe HTTP methods.",
    }
    return schema


@app.get("/api/docs", response_class=HTMLResponse, include_in_schema=False)
def api_documentation(_: str = Depends(require_basic_admin)) -> HTMLResponse:
    """Interactive, administrator-only API reference."""
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="VAOS API reference")


@app.get("/api-explorer", response_class=HTMLResponse, include_in_schema=False)
def api_explorer(request: Request, _: str = Depends(require_basic_admin)) -> HTMLResponse:
    """Serve the same-origin, admin-only interactive REST API explorer."""
    return templates.TemplateResponse(request, "api_explorer.html", {"version": APP_VERSION, "project": PROJECT_METADATA})


@app.get("/api/admin/integrations", include_in_schema=False)
def read_api_integrations(request: Request, _: str = Depends(require_basic_admin)) -> dict[str, Any]:
    settings = get_api_settings()
    with db_connection() as db:
        tokens = db.execute("SELECT token_prefix,label,scopes,created_at,expires_at,last_used_at,revoked_at FROM api_tokens ORDER BY created_at DESC").fetchall()
        accesses = db.execute("SELECT timestamp,token_prefix,method,path FROM api_access_log ORDER BY id DESC LIMIT 20").fetchall()
    return {
        "settings": settings,
        "tokens": [{"prefix": row["token_prefix"], "label": row["label"], "scopes": json.loads(row["scopes"]), "created_at": row["created_at"], "expires_at": row["expires_at"], "last_used_at": row["last_used_at"], "revoked_at": row["revoked_at"]} for row in tokens],
        "recent_access": [dict(row) for row in accesses],
        "api_docs": "/api/docs",
        "openapi": "/api/openapi.json",
        "mcp_command": "python mcp_server.py",
        "mcp_config_template": {"mcpServers": {"vaos": {"command": sys.executable, "args": [str(APP_DIR / "mcp_server.py")], "env": {"VAOS_BASE_URL": str(request.base_url).rstrip("/"), "VAOS_API_TOKEN": "PASTE_A_READ_OR_CONTROL_TOKEN_HERE"}}}},
    }


@app.get("/api/admin/mcp-approvals", include_in_schema=False)
def list_mcp_approvals(_: str = Depends(require_basic_admin)) -> dict[str, Any]:
    now = utc_now()
    with db_connection() as db:
        db.execute("UPDATE mcp_approvals SET status='expired' WHERE status IN ('pending','approved') AND expires_at<=?", (now,))
        rows = db.execute(
            "SELECT request_id,token_prefix,method,request_path,preview,status,created_at,expires_at,decided_at,decided_by,decision_note,used_at "
            "FROM mcp_approvals ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC LIMIT 100"
        ).fetchall()
    return {"approvals": [{**dict(row), "preview": json.loads(row["preview"])} for row in rows]}


@app.get("/api/admin/mcp-approvals/{approval_id}")
def read_mcp_approval_status(approval_id: str, request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    token_hash = getattr(request.state, "vaos_api_token_hash", None)
    if not token_hash:
        raise HTTPException(status_code=404, detail="Approval request was not found for this token.")
    now = utc_now()
    with db_connection() as db:
        db.execute("UPDATE mcp_approvals SET status='expired' WHERE request_id=? AND token_hash=? AND status IN ('pending','approved') AND expires_at<=?", (approval_id, token_hash, now))
        row = db.execute(
            "SELECT request_id,status,preview,created_at,expires_at,decided_at,decision_note,used_at "
            "FROM mcp_approvals WHERE request_id=? AND token_hash=?",
            (approval_id, token_hash),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Approval request was not found for this token.")
    return {**dict(row), "preview": json.loads(row["preview"])}


@app.post("/api/admin/mcp-approvals/{approval_id}/decision", include_in_schema=False)
async def decide_mcp_approval(approval_id: str, request: Request, admin_name: str = Depends(require_basic_admin)) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Send an approve or deny decision as JSON.") from exc
    decision = str(payload.get("decision", "")).casefold() if isinstance(payload, dict) else ""
    note = str(payload.get("note", "")).strip()[:240] if isinstance(payload, dict) else ""
    if decision not in {"approve", "deny", "revoke"}:
        raise HTTPException(status_code=400, detail="Decision must be approve, deny, or revoke.")
    now = utc_now()
    expired = False
    target = "approved" if decision == "approve" else "denied"
    try:
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,expires_at FROM mcp_approvals WHERE request_id=?", (approval_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Approval request was not found.")
            if row["expires_at"] <= now:
                db.execute("UPDATE mcp_approvals SET status='expired' WHERE request_id=? AND status IN ('pending','approved')", (approval_id,))
                expired = True
            elif decision == "revoke" and row["status"] != "approved":
                raise HTTPException(status_code=409, detail="Only an approved, unused request can be revoked.")
            elif decision != "revoke" and row["status"] != "pending":
                raise HTTPException(status_code=409, detail=f"This request is already {row['status']} and cannot be changed.")
            else:
                cursor = db.execute(
                    "UPDATE mcp_approvals SET status=?,decided_at=?,decided_by=?,decision_note=? WHERE request_id=? AND status=? AND expires_at>?",
                    (target, now, admin_name[:80], note or ("Approval revoked by administrator." if decision == "revoke" else ""), approval_id, "approved" if decision == "revoke" else "pending", now),
                )
                if cursor.rowcount != 1:
                    raise HTTPException(status_code=409, detail="The approval request changed before the decision could be saved.")
                _record_observability_security_event_in_transaction(
                    db, "mcp.approval_decided", decision, component="mcp", details={"result": target},
                )
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        logger.exception("Could not atomically save the MCP approval decision and security event")
        raise HTTPException(status_code=503, detail="The approval decision could not be saved with its security audit; the request remains unchanged. Try again shortly.") from exc
    if expired:
        raise HTTPException(status_code=409, detail="This approval request has expired. The agent must submit a new request.")
    message = "The one-time MCP approval was revoked." if decision == "revoke" else f"MCP action {decision}d. The agent must retry the exact same request if approved."
    return {"ok": True, "status": target, "message": message}


@app.post("/api/admin/integrations", include_in_schema=False)
async def save_api_integrations(request: Request, _: str = Depends(require_basic_admin)) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Send integration settings as JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Integration settings must be a JSON object.")
    groups = payload.get("mcp_allowed_groups", list(API_DEFAULT_TOKEN_GROUPS))
    if not isinstance(groups, list) or any(group not in API_MCP_GROUPS for group in groups):
        raise HTTPException(status_code=400, detail="Choose valid MCP capability groups.")
    try:
        rate_limit = min(600, max(30, int(payload.get("api_rate_limit", 120))))
        token_days = min(365, max(1, int(payload.get("api_token_default_days", 90))))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Rate limit or token lifetime is not valid.") from exc
    updates = {
        "integration_api_enabled": "1" if payload.get("api_enabled") is True else "0",
        "integration_api_allow_sensitive_reads": "1" if payload.get("api_allow_sensitive_reads") is True else "0",
        "integration_mcp_enabled": "1" if payload.get("mcp_enabled") is True else "0",
        "integration_mcp_read_only": "1" if payload.get("mcp_read_only", True) is True else "0",
        "integration_mcp_allow_sensitive": "1" if payload.get("mcp_allow_sensitive") is True else "0",
        "integration_mcp_allow_deletes": "1" if payload.get("mcp_allow_deletes") is True else "0",
        "integration_api_rate_limit": str(rate_limit),
        "integration_api_token_default_days": str(token_days),
        "integration_mcp_allowed_groups": json.dumps(list(dict.fromkeys(groups))),
    }
    try:
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", list(updates.items()))
            _record_observability_security_event_in_transaction(
                db, "config.api_mcp_settings_changed", "updated", component="settings",
                details={"api_enabled": updates["integration_api_enabled"] == "1", "mcp_enabled": updates["integration_mcp_enabled"] == "1", "mcp_read_only": updates["integration_mcp_read_only"] == "1", "allowed_groups": list(dict.fromkeys(groups))},
            )
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        logger.exception("Could not atomically save API/MCP settings and security event")
        raise HTTPException(status_code=503, detail="API/MCP settings were not saved because their security audit could not be stored. Try again shortly.") from exc
    return {"settings": get_api_settings(), "message": "API and MCP controls saved. Changes apply to the next request."}


@app.get("/api/admin/integrations/mcp-policy", include_in_schema=False)
def read_mcp_policy(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    settings = get_api_settings()
    return {"api_enabled": settings["api_enabled"], "mcp_enabled": settings["mcp_enabled"], "read_only": settings["mcp_read_only"], "allowed_groups": settings["mcp_allowed_groups"], "token_groups": getattr(request.state, "vaos_api_groups", list(API_DEFAULT_TOKEN_GROUPS)), "token_sensitive_read": getattr(request.state, "vaos_api_sensitive_read", False), "token_mcp_agent": getattr(request.state, "vaos_api_mcp_agent", False), "allow_sensitive_reads": settings["api_allow_sensitive_reads"], "allow_sensitive": settings["mcp_allow_sensitive"], "allow_deletes": settings["mcp_allow_deletes"], "token_scope": getattr(request.state, "vaos_api_scope", "admin")}


@app.post("/api/admin/api-tokens", include_in_schema=False)
async def create_api_token(request: Request, _: str = Depends(require_basic_admin)) -> dict[str, Any]:
    try:
        payload = await request.json()
        label = str(payload.get("label", "")).strip()
        scope = str(payload.get("scope", "read")).strip().lower()
        lifetime = int(payload.get("expires_days", get_api_settings()["api_token_default_days"]))
        groups = payload.get("groups", list(API_DEFAULT_TOKEN_GROUPS))
        sensitive_read = payload.get("sensitive_read") is True
        mcp_agent = payload.get("mcp_agent") is True
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Provide a label, scope, and valid expiry.") from exc
    if not label or len(label) > 80:
        raise HTTPException(status_code=400, detail="Token label must be 1–80 characters.")
    if scope not in {"read", "control"}:
        raise HTTPException(status_code=400, detail="Token scope must be read or control.")
    if not isinstance(groups, list) or not groups or any(group not in API_MCP_GROUPS for group in groups):
        raise HTTPException(status_code=400, detail="Choose at least one valid API capability group.")
    if sensitive_read and not get_api_settings()["api_allow_sensitive_reads"]:
        raise HTTPException(status_code=400, detail="Enable sensitive reads in Settings before granting this token access to private data.")
    if not 1 <= lifetime <= 365:
        raise HTTPException(status_code=400, detail="Token expiry must be from 1 to 365 days.")
    token = "vaos_" + secrets.token_urlsafe(36)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    prefix = token[:14]
    created = datetime.now(timezone.utc)
    expires = (created + timedelta(days=lifetime)).isoformat(timespec="seconds")
    try:
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO api_tokens(token_hash,token_prefix,label,scopes,created_at,expires_at) VALUES(?,?,?,?,?,?)", (token_hash, prefix, label, json.dumps({"access": scope, "groups": list(dict.fromkeys(groups)), "sensitive_read": sensitive_read, "mcp_agent": mcp_agent}), created.isoformat(timespec="seconds"), expires))
            _record_observability_security_event_in_transaction(
                db, "auth.api_token_created", "created", component="api",
                details={"access": scope, "groups": list(dict.fromkeys(groups)), "sensitive_read": sensitive_read, "mcp_agent": mcp_agent, "expires_days": lifetime},
            )
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        logger.exception("Could not atomically create API token and security event")
        raise HTTPException(status_code=503, detail="The API token was not issued because its security audit could not be stored. Try again shortly.") from exc
    return {"token": token, "prefix": prefix, "label": label, "scopes": {"access": scope, "groups": list(dict.fromkeys(groups)), "sensitive_read": sensitive_read, "mcp_agent": mcp_agent}, "created_at": created.isoformat(timespec="seconds"), "expires_at": expires, "message": "Copy this token now. VAOS stores only its hash and cannot show it again."}


@app.delete("/api/admin/api-tokens/{token_prefix}", include_in_schema=False)
def revoke_api_token(token_prefix: str, _: str = Depends(require_basic_admin)) -> dict[str, Any]:
    try:
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            revoked_at = utc_now()
            row = db.execute("SELECT token_hash FROM api_tokens WHERE token_prefix=? AND revoked_at IS NULL", (token_prefix,)).fetchone()
            cursor = db.execute("UPDATE api_tokens SET revoked_at=? WHERE token_prefix=? AND revoked_at IS NULL", (revoked_at, token_prefix))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Active API token not found.")
            if row is not None:
                db.execute("UPDATE mcp_approvals SET status='denied',decided_at=?,decided_by='token revoked',decision_note='The associated API token was revoked.' WHERE token_hash=? AND status IN ('pending','approved')", (revoked_at, row["token_hash"]))
            _record_observability_security_event_in_transaction(
                db, "auth.api_token_revoked", "revoked", component="api",
                details={"approval_requests_invalidated": True},
            )
    except HTTPException:
        raise
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        logger.exception("Could not atomically revoke API token and security event")
        raise HTTPException(status_code=503, detail="The API token was not revoked because its security audit could not be stored. Try again shortly.") from exc
    return {"ok": True, "message": f"API token {token_prefix} was revoked."}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_admin)):
    settings = get_engine_settings()
    return templates.TemplateResponse(request, "index.html", {
        "threshold": settings["match_threshold"],
        "cooldown": settings["event_cooldown_seconds"],
        "admin_password": "" if PRODUCTION_MODE else ADMIN_PASSWORD,
        "version": APP_VERSION,
        "production_mode": PRODUCTION_MODE,
        "local_windows_launcher": LOCAL_WINDOWS_LAUNCHER,
        "project": PROJECT_METADATA,
    })


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "index.html", {"threshold": get_engine_settings()["match_threshold"], "cooldown": get_engine_settings()["event_cooldown_seconds"], "admin_password": "" if PRODUCTION_MODE else ADMIN_PASSWORD, "version": APP_VERSION, "production_mode": PRODUCTION_MODE, "local_windows_launcher": LOCAL_WINDOWS_LAUNCHER, "settings_only": True, "project": PROJECT_METADATA})


@app.get("/hardware", response_class=HTMLResponse)
def hardware_page(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(request, "hardware.html", {"version": APP_VERSION, "project": PROJECT_METADATA})


@app.get("/statistics", response_class=HTMLResponse)
def observability_page(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(request, "observability.html", {"version": APP_VERSION, "project": PROJECT_METADATA})


@app.get("/tracking", response_class=HTMLResponse)
def tracking_page(request: Request, _: str = Depends(require_basic_admin)) -> HTMLResponse:
    return templates.TemplateResponse(request, "tracking.html", {"version": APP_VERSION, "project": PROJECT_METADATA})


@app.get("/docs/HARDWARE.md", include_in_schema=False)
def hardware_guide() -> Response:
    guide_path = APP_DIR / "docs" / "HARDWARE.md"
    try:
        return Response(guide_path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="The Hardware Intelligence guide is not installed in this build.") from exc


@app.get("/docs/MCP_SETTINGS.md", include_in_schema=False)
def mcp_settings_guide() -> Response:
    guide_path = APP_DIR / "docs" / "MCP_SETTINGS.md"
    try:
        return Response(guide_path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="The MCP Settings guide is not installed in this build.") from exc


@app.get("/training", response_class=HTMLResponse)
def ml_training_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "training.html", {"version": APP_VERSION, "admin_password": "" if PRODUCTION_MODE else ADMIN_PASSWORD, "project": PROJECT_METADATA})


@app.get("/training/activity", response_class=HTMLResponse)
def ml_training_activity_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "training_activity.html", {"version": APP_VERSION, "admin_password": "" if PRODUCTION_MODE else ADMIN_PASSWORD, "project": PROJECT_METADATA})


@app.get("/datasets", response_class=HTMLResponse)
def dataset_manager_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "dataset_manager.html", {"version": APP_VERSION, "admin_password": "" if PRODUCTION_MODE else ADMIN_PASSWORD, "project": PROJECT_METADATA})


@app.get("/video-review", response_class=HTMLResponse)
def video_review_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "video_review.html", {"version": APP_VERSION, "admin_password": "" if PRODUCTION_MODE else ADMIN_PASSWORD, "project": PROJECT_METADATA})


@app.get("/projects", response_class=HTMLResponse)
def project_bundles_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "projects.html", {"version": APP_VERSION, "project": PROJECT_METADATA})


@app.get("/api/admin/releases", include_in_schema=False)
def release_status(_: str = Depends(require_admin)):
    return check_release(load_project_metadata()["version"])


@app.get("/about", response_class=HTMLResponse)
def about_vaos_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "about.html", {"version": APP_VERSION, "project": PROJECT_METADATA})


@app.get("/docs/LICENSE", include_in_schema=False)
def license_document() -> Response:
    license_path = APP_DIR / "LICENSE"
    try:
        return Response(content=license_path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="The AGPL-3.0-only license text is not installed in this build.") from exc


@app.get("/docs/CONTRIBUTING.md", include_in_schema=False)
def contribution_document() -> Response:
    contribution_path = APP_DIR / "CONTRIBUTING.md"
    try:
        return Response(content=contribution_path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="The contribution guide is not installed in this build.") from exc


@app.get("/live-system")
def live_system_page(request: Request, _: str = Depends(require_admin)):
    return RedirectResponse(url="/#system-overview", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok", "app": "vaos-admin", "instance_id": INSTANCE_ID, "version": APP_VERSION, "model_loaded": face_detector is not None}


@app.get("/c/{token}", response_class=HTMLResponse)
def camera_page(token: str, request: Request):
    with db_connection() as db:
        camera = db.execute("SELECT token, name, active FROM cameras WHERE token = ?", (token,)).fetchone()
    if not camera or not camera["active"]:
        raise HTTPException(status_code=404, detail="This camera link is invalid or has been revoked.")
    settings = get_engine_settings()
    return templates.TemplateResponse(request, "camera.html", {"camera_name": camera["name"], "token": token, "frame_interval_ms": settings["camera_frame_interval_ms"], "max_dimension": settings["camera_max_dimension"], "project": PROJECT_METADATA})


def public_enrollment_page(token: str, request: Request):
    if not is_enrollment_link_valid(token):
        raise HTTPException(status_code=404, detail="This enrollment link is invalid, expired, or already used.")
    return templates.TemplateResponse(request, "enroll.html", {"token": token, "project": PROJECT_METADATA})


@app.post("/api/admin/cameras")
def create_camera(payload: dict[str, Any], _: str = Depends(require_admin)):
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Enter a camera name")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="Camera name must be 80 characters or fewer")
    token = secrets.token_urlsafe(24)
    with db_connection() as db:
        index = db.execute("SELECT COUNT(*) AS n FROM cameras WHERE source_type='camera'").fetchone()["n"]
        map_x = 0.18 + (index % 4) * 0.21
        map_y = 0.25 + ((index // 4) % 3) * 0.28
        db.execute("INSERT INTO cameras(token, name, created_at, active,map_x,map_y) VALUES (?, ?, ?, 1,?,?)", (token, name, utc_now(), map_x, map_y))
    return {"token": token, "name": name, "active": True, "created_at": utc_now(), "map_x": map_x, "map_y": map_y, "heading": 0}


@app.get("/api/admin/cameras")
def list_cameras(_: str = Depends(require_admin)):
    with db_connection() as db:
        rows = db.execute("SELECT token, name, created_at, active FROM cameras WHERE source_type='camera' ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/admin/cameras/{token}/map")
def update_camera_map(token: str, payload: dict[str, Any], _: str = Depends(require_admin)):
    try:
        x, y, heading = float(payload["x"]), float(payload["y"]), float(payload["heading"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Map placement needs x, y, and heading values") from exc
    if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= heading < 360):
        raise HTTPException(status_code=400, detail="Map coordinates must be between 0 and 1 and heading between 0 and 360")
    with db_connection() as db:
        cur = db.execute("UPDATE cameras SET map_x=?,map_y=?,heading=? WHERE token=?", (x,y,heading,token))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Camera not found")
    return {"ok": True}


@app.get("/api/admin/status")
def diagnostics(_: str = Depends(require_admin)):
    with db_connection() as db:
        rows = db.execute("SELECT c.token,c.name,c.active,c.map_x,c.map_y,c.heading,s.last_frame_at,s.faces_detected,s.matched_count,s.objects_json,s.error FROM cameras c LEFT JOIN camera_status s ON c.token=s.camera_token WHERE c.source_type='camera' ORDER BY c.created_at DESC").fetchall()
        object_rows = db.execute("SELECT o.timestamp,o.class_name,o.count,o.confidence,c.name AS camera_name FROM object_sightings o JOIN cameras c ON c.token=o.camera_token ORDER BY o.id DESC LIMIT 60").fetchall()
        matches_today = int(db.execute("SELECT COUNT(*) FROM sightings WHERE timestamp LIKE ?", (datetime.now(timezone.utc).strftime("%Y-%m-%d") + "%",)).fetchone()[0])
        objects_last_hour = int(db.execute("SELECT COUNT(*) FROM object_sightings WHERE timestamp >= ?", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"),)).fetchone()[0])
    cameras = []
    now = datetime.now(timezone.utc)
    for row in rows:
        item = dict(row)
        seen = item.get("last_frame_at")
        try:
            online = bool(seen) and (now - datetime.fromisoformat(seen)).total_seconds() < 12
        except (ValueError, TypeError):
            online = False
        item["online"] = online and bool(item["active"])
        item["objects"] = json.loads(item.pop("objects_json") or "{}")
        item["person_count"] = int(item["objects"].get("person", [0])[0])
        item["occupancy"] = "Quiet" if item["person_count"] <= 2 else "Busy" if item["person_count"] <= 6 else "Crowded"
        cameras.append(item)
    runtime = model_runtime_status()
    internal_token = os.getenv("VAOS_INTERNAL_STATUS_TOKEN", "")
    camera_service_running = False
    if internal_token:
        request = urllib.request.Request(
            f"{CAMERA_INTERNAL_URL}/internal/model-status",
            headers={"X-VAOS-Internal-Token": internal_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=0.8) as response:
                worker_status = json.loads(response.read().decode("utf-8"))
            if isinstance(worker_status, dict) and isinstance(worker_status.get("models"), list):
                runtime = worker_status
                camera_service_running = True
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            pass  # Keep showing the last process-local status when the worker is unavailable.
    settings = get_engine_settings()
    model_map = {str(item.get("name")): item for item in runtime.get("models", [])}
    def engine_state(enabled: bool, model_names: tuple[str, ...] = ()) -> str:
        if not enabled:
            return "stopped"
        if not camera_service_running:
            return "starting"
        if model_names and not all(bool(model_map.get(name, {}).get("loaded")) for name in model_names):
            return "starting"
        return "running"
    service_status = {
        "admin": "running",
        "camera": "running" if camera_service_running else "stopped",
        "tunnel": read_tunnel_state().get("status", "stopped"),
        "training": "stopping" if ml_training_state.get("stop_requested") else str(ml_training_state.get("status", "idle")),
        "face_matching": engine_state(bool(settings.get("face_matching_enabled")), ("yunet", "sface")),
        "object_detection": engine_state(bool(settings.get("object_detection_enabled")), ("yolo26m",)),
        "object_tracking": engine_state(bool(settings.get("object_tracking_enabled")), ("yolo26m",)),
    }
    if LAUNCHER_COMMAND_PATH.exists():
        try:
            command = json.loads(LAUNCHER_COMMAND_PATH.read_text(encoding="utf-8")).get("command", "")
            if command in {"services_restart", "restart"}:
                service_status["admin"] = "stopping"
                service_status["camera"] = "stopping"
        except (OSError, json.JSONDecodeError, AttributeError):
            service_status["admin"] = "starting"
    return {**runtime, "services": service_status, "cameras": cameras, "objects": [dict(r) for r in object_rows], "matches_today": matches_today, "objects_last_hour": objects_last_hour, "system_metrics": get_system_metrics()}


@app.get("/api/admin/hardware")
def hardware_intelligence(_: str = Depends(require_admin)) -> dict[str, Any]:
    """Report measured host hardware and conservative, non-applied guidance."""
    runtime = model_runtime_status()
    internal_token = os.getenv("VAOS_INTERNAL_STATUS_TOKEN", "")
    if internal_token:
        request = urllib.request.Request(
            f"{CAMERA_INTERNAL_URL}/internal/model-status",
            headers={"X-VAOS-Internal-Token": internal_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=0.8) as response:
                worker_status = json.loads(response.read().decode("utf-8"))
            if isinstance(worker_status, dict) and isinstance(worker_status.get("models"), list):
                runtime = worker_status
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            runtime = {}
    return build_hardware_report(
        system_metrics=get_system_metrics(),
        device_inventory=training_device_inventory(),
        inference_runtime=runtime,
        training_state=dict(ml_training_state),
        data_dir=DATA_DIR,
    )


@app.get("/api/admin/observability/overview")
def observability_overview(period: str = "lifetime", after: str | None = None, before: str | None = None, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        return observability_store.overview(DB_PATH, period=period, after=after, before=before)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not read observability statistics")
        raise HTTPException(status_code=503, detail="Observability storage is not ready. Existing VAOS features remain available.") from exc


@app.get("/api/admin/observability/counter-queue", include_in_schema=False)
def observability_counter_queue(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    """Return a path-free, process-scoped snapshot of the local counter queues."""
    _require_observability_sensitive_read(request)
    return observability_counter_queue_status()


@app.get("/api/admin/observability/metrics")
def observability_metrics(metric: str | None = None, limit: int = 100, _: str = Depends(require_admin)) -> dict[str, Any]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="Metric page size must be between 1 and 500.")
    try:
        items = observability_store.list_metrics(DB_PATH, key=metric, limit=limit)
        return {"items": items, "count": len(items), "updated_at": utc_now()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not read observability metric catalogue")
        raise HTTPException(status_code=503, detail="Observability storage is not ready. Existing VAOS features remain available.") from exc


@app.post("/api/admin/observability/mcp/invocations", include_in_schema=False)
def record_mcp_invocation(request: Request, payload: MCPInvocationInput, _: str = Depends(require_admin)) -> dict[str, Any]:
    """Receive minimal invocation telemetry from the authenticated local MCP bridge."""
    if not getattr(request.state, "vaos_api_mcp_agent", False):
        raise HTTPException(status_code=403, detail="This endpoint is reserved for an MCP-designated API token.")
    if payload.tool not in MCP_BRIDGE_TOOL_NAMES:
        raise HTTPException(status_code=400, detail="Unknown MCP bridge tool.")
    if not math.isfinite(payload.duration_ms):
        raise HTTPException(status_code=400, detail="Tool duration must be a finite number.")
    if payload.invocation_id is not None and not re.fullmatch(r"[0-9a-f]{32}", payload.invocation_id):
        raise HTTPException(status_code=400, detail="Invocation ID must be a 32-character lowercase hexadecimal value.")
    details = {"tool": payload.tool, "outcome": payload.outcome, "duration_ms": round(payload.duration_ms, 3)}
    try:
        inserted = observability_store.record_event(
            DB_PATH,
            event_type="mcp.tool_invoked",
            component="mcp",
            level="INFO" if payload.outcome == "success" else "WARNING",
            message="MCP bridge tool completed." if payload.outcome == "success" else "MCP bridge tool returned an error.",
            details=details,
            event_id=f"mcp-invocation-{payload.invocation_id}" if payload.invocation_id else None,
            correlation_id=observability_correlation_context.get(),
            counter_deltas={"mcp.tool_invocations": 1},
            counter_dimensions={"mcp.tool_invocations": {"tool": payload.tool, "outcome": payload.outcome}},
            metric_observations=[{
                "metric_key": "mcp.tool_duration_ms",
                "value": payload.duration_ms,
                "dimensions": {"tool": payload.tool, "outcome": payload.outcome},
                "provenance": "local MCP bridge monotonic timer",
                "measurement_scope": "MCP tool execution including VAOS REST round trips",
                "estimated": False,
            }],
        )
        return {"ok": True, "duplicate": not inserted}
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.warning("Could not record MCP invocation telemetry", exc_info=True)
        raise HTTPException(status_code=503, detail="MCP invocation telemetry is temporarily unavailable.") from exc


@app.get("/api/admin/observability/series")
def observability_metric_series(
    metric: str, after: str, before: str, limit: int = 500, dimensions: str = "{}",
    aggregation: str = "raw", interval: str = "hour", group_by: str = "",
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="Series page size must be between 1 and 1,000.")
    try:
        dimension_filters = json.loads(dimensions)
        if not isinstance(dimension_filters, dict):
            raise ValueError("dimensions must be a JSON object of exact dimension filters.")
        definition = observability_store.list_metrics(DB_PATH, key=metric, limit=1)
        if not definition:
            raise ValueError("Unknown metric key.")
        if interval not in observability_store.METRIC_SERIES_INTERVALS:
            raise ValueError("Choose a supported metric time interval.")
        grouping = group_by or None
        if grouping and grouping not in definition[0]["dimensions"]:
            raise ValueError(f"Unsupported grouping dimension for {metric}: {grouping}.")
        if aggregation == "raw":
            items = observability_store.query_metric_series(DB_PATH, metric_key=metric, after=after, before=before, limit=limit, dimensions=dimension_filters)
            total = observability_store.count_metric_series(DB_PATH, metric_key=metric, after=after, before=before, dimensions=dimension_filters)
            group_count = observability_store.count_metric_series_groups(
                DB_PATH, metric_key=metric, group_by=grouping, after=after, before=before, dimensions=dimension_filters,
            ) if grouping else (1 if total else 0)
            truncated = total > len(items)
        else:
            result = observability_store.query_metric_series_aggregated(
                DB_PATH, metric_key=metric, after=after, before=before, limit=limit,
                dimensions=dimension_filters, group_by=grouping, aggregation=aggregation, interval=interval,
            )
            items, total = result["items"], result["total"]
            group_count, truncated = result["group_count"], result["truncated"]
        return {
            "metric": definition[0], "items": items, "count": len(items), "total": total,
            "dimensions": dimension_filters, "group_by": grouping, "group_count": group_count,
            "aggregation": aggregation, "interval": interval,
            "range": {"after": after, "before_exclusive": before, "timezone": "UTC"},
            "truncated": truncated, "updated_at": utc_now(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not read observability metric history")
        raise HTTPException(status_code=503, detail="Observability storage is not ready. Existing VAOS features remain available.") from exc


@app.get("/api/admin/observability/retention")
def observability_get_retention_policy(_: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        return observability_store.get_retention_policy(DB_PATH)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        logger.exception("Could not read observability retention policy")
        raise HTTPException(status_code=503, detail="Retention settings are not available right now.") from exc


@app.put("/api/admin/observability/retention")
def observability_save_retention_policy(
    payload: ObservabilityRetentionPolicyInput,
    actor: str = Depends(require_basic_admin),
) -> dict[str, Any]:
    try:
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            policy = observability_store.set_retention_policy_in_transaction(
                db,
                event_details_days=payload.event_details_days,
                metric_observations_max_days=payload.metric_observations_max_days,
                delivery_history_days=payload.delivery_history_days,
                confirm_shorter=payload.confirm_shorter,
            )
            _record_observability_security_event_in_transaction(
                db,
                "observability.retention_policy_changed", "complete", component="observability",
                actor_ref=actor,
                details={"event_details_days": policy["event_details_days"], "metric_observations_max_days": policy["metric_observations_max_days"], "delivery_history_days": policy["delivery_history_days"], "shortened": policy["shortened"]},
            )
        policy["message"] = (
            "Shorter retention was saved. Older operational details may be permanently redacted or removed during the next scheduled cleanup."
            if policy["shortened"] else "Retention settings saved. Existing expired records are not restored."
        )
        return {"ok": True, **policy}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        logger.exception("Could not save observability retention policy")
        raise HTTPException(status_code=503, detail="Retention settings were not saved because their security audit could not be stored. Try again shortly.") from exc


@app.post("/api/admin/observability/retention")
def observability_apply_retention(
    payload: ObservabilityRetentionApplyInput,
    actor: str = Depends(require_basic_admin),
) -> dict[str, Any]:
    if not payload.confirm_expired_data_redaction:
        raise HTTPException(status_code=400, detail="Confirm that applying retention can permanently remove expired operational details.")
    try:
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = observability_store.apply_retention_in_transaction(db)
            _record_observability_security_event_in_transaction(
                db,
                "observability.retention_applied", "complete", component="observability",
                actor_ref=actor, details=result,
            )
        return {"ok": True, **result, "message": "Metric samples past their configured retention were removed and expired event text was redacted. Lifetime counters and the security audit were preserved. Generated CSV/PDF reports are separate copies and remain available until their own 24-hour expiry."}
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        logger.exception("Could not apply observability retention")
        raise HTTPException(status_code=503, detail="Retention was not applied because its security audit could not be stored. Try again shortly.") from exc


@app.get("/api/admin/observability/events")
def observability_events(
    after: str | None = None,
    before: str | None = None,
    level: str | None = None,
    component: str | None = None,
    event_type: str | None = None,
    session_id: str | None = None,
    correlation_id: str | None = None,
    training_job_id: str | None = Query(default=None, description="Exact training run ID in safe event details; max 120 characters; only events recording it match."),
    dataset_id: str | None = Query(default=None, description="Exact dataset ID in safe event details; max 120 characters; only events recording it match."),
    model_id: str | None = Query(default=None, description="Exact model ID in safe event details; max 120 characters; only events recording it match."),
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="Event page size must be between 1 and 200.")
    if search and len(search) > 200:
        raise HTTPException(status_code=400, detail="Search text must be 200 characters or fewer.")
    if correlation_id and len(correlation_id) > 120:
        raise HTTPException(status_code=400, detail="Correlation ID must be 120 characters or fewer.")
    if session_id and len(session_id) > 120:
        raise HTTPException(status_code=400, detail="Session ID must be 120 characters or fewer.")
    for label, value in (("Training job ID", training_job_id), ("Dataset ID", dataset_id), ("Model ID", model_id)):
        if value and len(value) > 120:
            raise HTTPException(status_code=400, detail=f"{label} must be 120 characters or fewer.")
    cursor_before = None
    if cursor:
        if len(cursor) > 512:
            raise HTTPException(status_code=400, detail="Event cursor is invalid.")
        try:
            decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            value = json.loads(decoded.decode("utf-8"))
            if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, str) for item in value):
                raise ValueError("invalid cursor payload")
            cursor_before = (value[0], value[1])
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise HTTPException(status_code=400, detail="Event cursor is invalid.") from exc
    try:
        rows = observability_store.query_events(DB_PATH, limit=limit + 1, after=after, before=before, cursor_before=cursor_before, level=level, component=component, event_type=event_type, session_id=session_id, correlation_id=correlation_id, training_job_id=training_job_id, dataset_id=dataset_id, model_id=model_id, search=search)
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last["timestamp"], last["event_id"]], separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not query the observability event log")
        raise HTTPException(status_code=503, detail="Observability storage is not ready. Existing VAOS features remain available.") from exc


@app.get("/api/admin/observability/events/{event_id}")
def observability_event(event_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    if not event_id or len(event_id) > 120:
        raise HTTPException(status_code=400, detail="Event ID must be between 1 and 120 characters.")
    try:
        rows = observability_store.query_events(DB_PATH, limit=1, event_id=event_id)
        if not rows:
            raise HTTPException(status_code=404, detail="Operational event was not found or has expired.")
        return rows[0]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not read the observability event record")
        raise HTTPException(status_code=503, detail="Observability storage is not ready. Existing VAOS features remain available.") from exc


def _saved_log_filter_owner(principal: str) -> str:
    """Derive an opaque per-admin/token namespace for saved UI filters."""
    value = str(principal)
    identity = value if value.startswith("api:") else "basic:" + value
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _validate_saved_log_filter(filters: Any) -> dict[str, Any]:
    if not isinstance(filters, dict) or set(filters) - {"period", "after", "before", "level", "component", "event_type", "session_id", "correlation_id", "training_job_id", "dataset_id", "model_id", "search"}:
        raise ValueError("Saved filters contain unsupported fields.")
    period = str(filters.get("period", "lifetime"))
    if period not in {"lifetime", "24h", "7d", "30d", "custom"}:
        raise ValueError("Saved filter period is invalid.")
    clean: dict[str, Any] = {"period": period}
    if period == "custom":
        raw_after, raw_before = filters.get("after"), filters.get("before")
        if not isinstance(raw_after, str) or not isinstance(raw_before, str) or len(raw_after) > 64 or len(raw_before) > 64:
            raise ValueError("Custom saved filter times must be valid UTC timestamps of at most 64 characters.")
        try:
            after = observability_store._normalize_time(raw_after)
            before = observability_store._normalize_time(raw_before)
            start = observability_store._parse_time(after)
            end = observability_store._parse_time(before)
        except (ValueError, TypeError) as exc:
            raise ValueError("Custom saved filters need valid UTC start and end times.") from exc
        if start >= end or end - start > timedelta(days=366):
            raise ValueError("Custom saved filter range must be ordered and no longer than 366 days.")
        clean.update({"after": after, "before": before})
    elif "after" in filters or "before" in filters:
        raise ValueError("Only a custom saved filter can include start and end times.")
    if "level" in filters and not isinstance(filters["level"], str):
        raise ValueError("Saved filter severity must be text.")
    level = str(filters.get("level", "")).upper().strip()
    if level:
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Saved filter severity is invalid.")
        clean["level"] = level
    for key, maximum in (("component", 80), ("event_type", 96), ("session_id", 120), ("correlation_id", 120), ("training_job_id", 120), ("dataset_id", 120), ("model_id", 120), ("search", 200)):
        if key in filters and not isinstance(filters[key], str):
            raise ValueError(f"Saved filter {key.replace('_', ' ')} must be text.")
        value = str(filters.get(key, "")).strip()
        if len(value) > maximum:
            raise ValueError(f"Saved filter {key.replace('_', ' ')} must be {maximum} characters or fewer.")
        if key == "event_type" and value and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,95}", value):
            raise ValueError("Saved event type must be a lowercase event key.")
        if value:
            clean[key] = value
    return clean


@app.get("/api/admin/observability/log-filters")
def list_observability_log_filters(principal: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        items = observability_store.list_saved_log_filters(DB_PATH, owner_hash=_saved_log_filter_owner(principal))
        return {"items": items, "count": len(items), "maximum": 20}
    except sqlite3.Error as exc:
        logger.exception("Could not list saved Log Explorer filters")
        raise HTTPException(status_code=503, detail="Saved filters could not be loaded.") from exc


@app.post("/api/admin/observability/log-filters", status_code=201)
def create_observability_log_filter(payload: dict[str, Any], principal: str = Depends(require_admin)) -> dict[str, Any]:
    if set(payload) != {"name", "filters"}:
        raise HTTPException(status_code=400, detail="Provide only a filter name and its settings.")
    if not isinstance(payload.get("name"), str):
        raise HTTPException(status_code=400, detail="Saved-filter name must be text.")
    try:
        filters = _validate_saved_log_filter(payload.get("filters"))
        item = observability_store.save_log_filter(DB_PATH, owner_hash=_saved_log_filter_owner(principal), name=payload["name"], filters=filters)
        return {"item": item, "message": "Saved for this authenticated account."}
    except ValueError as exc:
        status = 409 if "already exists" in str(exc) or "up to 20" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not save a Log Explorer filter")
        raise HTTPException(status_code=503, detail="This filter could not be saved.") from exc


@app.delete("/api/admin/observability/log-filters/{filter_id}")
def remove_observability_log_filter(filter_id: str, principal: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        removed = observability_store.delete_saved_log_filter(DB_PATH, owner_hash=_saved_log_filter_owner(principal), filter_id=filter_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not remove a saved Log Explorer filter")
        raise HTTPException(status_code=503, detail="This saved filter could not be removed.") from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Saved filter not found for this authenticated account.")
    return {"ok": True, "message": "Saved filter removed."}


@app.get("/api/admin/observability/lifecycle")
def observability_lifecycle(limit: int = 50, after: str | None = None, before: str | None = None, cursor: str | None = None, _: str = Depends(require_admin)) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="Lifecycle page size must be between 1 and 200.")
    try:
        cursor_before = None
        if cursor:
            if len(cursor) > 512:
                raise HTTPException(status_code=400, detail="Lifecycle cursor is invalid.")
            try:
                decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                value = json.loads(decoded.decode("utf-8"))
                if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, str) for item in value):
                    raise ValueError("invalid cursor payload")
                cursor_before = (value[0], value[1])
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
                raise HTTPException(status_code=400, detail="Lifecycle cursor is invalid.") from exc
        rows = observability_store.list_sessions(DB_PATH, limit=limit + 1, after=after, before=before, cursor_before=cursor_before)
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last["started_at"], last["session_id"]], separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more, "updated_at": utc_now()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not read runtime lifecycle sessions")
        raise HTTPException(status_code=503, detail="Observability storage is not ready. Existing VAOS features remain available.") from exc


@app.get("/api/admin/observability/security-audit/integrity")
def observability_security_integrity(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        _require_observability_sensitive_read(request)
        return observability_store.verify_security_chain(DB_PATH)
    except sqlite3.Error as exc:
        logger.exception("Could not verify the security audit chain")
        raise HTTPException(status_code=503, detail="The security audit store is not ready.") from exc


@app.get("/api/admin/observability/security-audit/events")
def observability_security_events(request: Request, after: str | None = None, before: str | None = None, event_type: str | None = None, outcome: str | None = None, component: str | None = None, actor_ref: str | None = None, search: str | None = None, cursor: str | None = None, limit: int = 50, _: str = Depends(require_admin)) -> dict[str, Any]:
    _require_observability_sensitive_read(request)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="Security audit page size must be between 1 and 200.")
    cursor_before = None
    if cursor:
        if len(cursor) > 512:
            raise HTTPException(status_code=400, detail="Security audit cursor is invalid.")
        try:
            decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            value = json.loads(decoded.decode("utf-8"))
            if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str) or not str(value[1]).isdigit():
                raise ValueError("invalid cursor payload")
            cursor_before = (value[0], int(value[1]))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise HTTPException(status_code=400, detail="Security audit cursor is invalid.") from exc
    try:
        rows = observability_store.query_security_audit(DB_PATH, limit=limit + 1, after=after, before=before, cursor_before=cursor_before, event_type=event_type, outcome=outcome, component=component, actor_ref=actor_ref, search=search)
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last["timestamp"], last["sequence_id"]], separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more, "updated_at": utc_now()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not query append-only security audit records")
        raise HTTPException(status_code=503, detail="The security audit store is not ready.") from exc


def _require_observability_sensitive_read(request: Request) -> None:
    """Keep event exports behind the existing explicit API sensitive-read opt-in."""
    if not getattr(request.state, "vaos_api_token_hash", None):
        return
    if not get_api_settings()["api_allow_sensitive_reads"] or not getattr(request.state, "vaos_api_sensitive_read", False):
        raise HTTPException(status_code=403, detail="Observability exports include operational or security events. Enable sensitive reads in Settings and use a token explicitly granted sensitive-read access.")


def _assert_report_actor(job: dict[str, Any], actor: str) -> None:
    if actor != "admin" and job.get("requested_by") != actor:
        raise HTTPException(status_code=404, detail="Report job was not found.")


@app.get("/api/admin/observability/reports/types")
def observability_report_types(_: str = Depends(require_admin)) -> dict[str, Any]:
    return {"items": observability_reports.report_types(), "formats": ["csv", "pdf"]}


@app.get("/api/admin/observability/reports")
def observability_report_list(request: Request, limit: int = 50, actor: str = Depends(require_admin)) -> dict[str, Any]:
    _require_observability_sensitive_read(request)
    try:
        items = observability_reports.list_report_jobs(DB_PATH, limit=limit, requested_by=None if actor == "admin" else actor)
        return {"items": items, "count": len(items), "updated_at": utc_now()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not list observability reports")
        raise HTTPException(status_code=503, detail="Report storage is not ready.") from exc


@app.post("/api/admin/observability/reports/jobs", status_code=202)
def observability_report_create(payload: dict[str, Any], request: Request, actor: str = Depends(require_admin)) -> dict[str, Any]:
    _require_observability_sensitive_read(request)
    allowed = {"report_type", "format", "filters"}
    if set(payload) - allowed:
        raise HTTPException(status_code=400, detail="Only report_type, format and filters are accepted.")
    filters = payload.get("filters", {})
    if not isinstance(filters, dict):
        raise HTTPException(status_code=400, detail="filters must be an object.")
    try:
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = observability_reports.create_report_job_in_transaction(
                db, report_type=str(payload.get("report_type", "")), output_format=str(payload.get("format", "")),
                requested_by=actor, parameters=filters,
            )
            _record_observability_security_event_in_transaction(
                db, "observability.report_requested", "queued", component="observability",
                details={"report_type": job["report_type"], "format": job["format"], "job_id": job["job_id"]},
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        logger.exception("Could not queue an observability report")
        raise HTTPException(status_code=503, detail="The report could not be queued.") from exc
    try:
        observability_reports.submit_report_job(DB_PATH, OBSERVABILITY_REPORT_DIR, job["job_id"])
    except (OSError, RuntimeError) as exc:
        logger.exception("Report request was committed, but the local worker queue did not confirm scheduling")
        raise HTTPException(
            status_code=503,
            detail="Your report request was saved, but the worker could not confirm scheduling. Check Reports before retrying.",
        ) from exc
    return observability_reports.public_report_job(job)


@app.get("/api/admin/observability/reports/jobs/{job_id}")
def observability_report_status(job_id: str, request: Request, actor: str = Depends(require_admin)) -> dict[str, Any]:
    _require_observability_sensitive_read(request)
    job = observability_reports.get_report_job(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Report job was not found.")
    _assert_report_actor(job, actor)
    return observability_reports.public_report_job(job)


@app.post("/api/admin/observability/reports/jobs/{job_id}/cancel")
def observability_report_cancel(job_id: str, actor: str = Depends(require_admin)) -> dict[str, Any]:
    job = observability_reports.get_report_job(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Report job was not found.")
    _assert_report_actor(job, actor)
    try:
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cancelled = observability_reports.cancel_report_job_in_transaction(db, job_id, requested_by=job["requested_by"])
            if not cancelled:
                raise HTTPException(status_code=409, detail="Only a queued report can be cancelled.")
            _record_observability_security_event_in_transaction(
                db, "observability.report_cancelled", "cancelled", component="observability",
                details={"job_id": job_id},
            )
        return {"ok": True, "job_id": job_id, "status": "cancelled"}
    except sqlite3.Error as exc:
        logger.exception("Could not cancel an observability report")
        raise HTTPException(status_code=503, detail="The report could not be cancelled.") from exc


@app.get("/api/admin/observability/reports/jobs/{job_id}/download")
def observability_report_download(job_id: str, request: Request, actor: str = Depends(require_admin)) -> FileResponse:
    _require_observability_sensitive_read(request)
    job = observability_reports.get_report_job(DB_PATH, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Report job was not found.")
    _assert_report_actor(job, actor)
    path = observability_reports.report_file_path(OBSERVABILITY_REPORT_DIR, job)
    if not path:
        raise HTTPException(status_code=410 if job.get("status") == "expired" else 409, detail="The report file is expired, incomplete, or failed its integrity check.")
    try:
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not observability_reports.record_download_in_transaction(db, job_id=job_id, requested_by=actor):
                raise HTTPException(status_code=410, detail="The report has expired.")
            _record_observability_security_event_in_transaction(
                db, "observability.report_downloaded", "complete", component="observability",
                details={"job_id": job_id, "sha256": job.get("output_sha256")},
            )
        return FileResponse(path, filename=f"vaos-{job['report_type']}-{job_id}.{job['format']}", media_type="text/csv" if job["format"] == "csv" else "application/pdf")
    except sqlite3.Error as exc:
        logger.exception("Could not record observability report download")
        raise HTTPException(status_code=503, detail="The report download could not be audited.") from exc


@app.get("/api/admin/observability/integrations")
def observability_integrations(request: Request, _: str = Depends(require_admin)) -> dict[str, Any]:
    _require_observability_sensitive_read(request)
    try:
        return observability_siem.list_destinations(DB_PATH)
    except sqlite3.Error as exc:
        logger.exception("Could not read SIEM destination status")
        raise HTTPException(status_code=503, detail="Integration status is unavailable.") from exc


@app.post("/api/admin/observability/integrations", status_code=201)
def observability_integration_create(payload: dict[str, Any], _: str = Depends(require_admin)) -> dict[str, Any]:
    allowed = {"display_name", "transport", "event_format", "endpoint", "secret_ref", "enabled"}
    if set(payload) - allowed or not {"display_name", "endpoint"}.issubset(payload):
        raise HTTPException(status_code=400, detail="Provide display_name and endpoint; transport defaults to signed HTTPS JSON, and secret_ref/enabled are optional where supported.")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean.")
    try:
        with observability_store._connection(DB_PATH) as db:
            db.execute("BEGIN IMMEDIATE")
            destination = observability_siem.configure_destination_in_transaction(db, display_name=str(payload["display_name"]), transport=str(payload.get("transport", "https_json")), event_format=str(payload.get("event_format", "json")), endpoint=str(payload["endpoint"]), secret_ref=str(payload.get("secret_ref", "")), enabled=payload.get("enabled", False) is True)
            _record_observability_security_event_in_transaction(db, "observability.integration_created", "created", component="observability", details={"destination_id": destination["destination_id"], "endpoint_host": destination["endpoint_host"], "transport": destination["transport"], "event_format": destination["event_format"], "enabled": destination["enabled"]})
        return destination
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not create SIEM destination")
        raise HTTPException(status_code=503, detail="The destination could not be saved.") from exc


@app.put("/api/admin/observability/integrations/{destination_id}")
def observability_integration_update(destination_id: str, payload: dict[str, Any], _: str = Depends(require_admin)) -> dict[str, Any]:
    allowed = {"display_name", "transport", "event_format", "endpoint", "secret_ref", "enabled"}
    if set(payload) - allowed or not {"display_name", "endpoint"}.issubset(payload):
        raise HTTPException(status_code=400, detail="Provide display_name and endpoint; transport defaults to signed HTTPS JSON, and secret_ref/enabled are optional where supported.")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean.")
    try:
        with observability_store._connection(DB_PATH) as db:
            db.execute("BEGIN IMMEDIATE")
            destination = observability_siem.configure_destination_in_transaction(db, destination_id=destination_id, display_name=str(payload["display_name"]), transport=str(payload.get("transport", "https_json")), event_format=str(payload.get("event_format", "json")), endpoint=str(payload["endpoint"]), secret_ref=payload.get("secret_ref"), enabled=payload.get("enabled", False) is True)
            _record_observability_security_event_in_transaction(db, "observability.integration_updated", "updated", component="observability", details={"destination_id": destination_id, "endpoint_host": destination["endpoint_host"], "transport": destination["transport"], "event_format": destination["event_format"], "enabled": destination["enabled"]})
        return destination
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not update SIEM destination")
        raise HTTPException(status_code=503, detail="The destination could not be updated.") from exc


@app.delete("/api/admin/observability/integrations/{destination_id}")
def observability_integration_delete(destination_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        with observability_store._connection(DB_PATH) as db:
            db.execute("BEGIN IMMEDIATE")
            destination = observability_siem.archive_destination_in_transaction(db, destination_id)
            if not destination:
                raise HTTPException(status_code=404, detail="Destination was not found or was already removed.")
            _record_observability_security_event_in_transaction(db, "observability.integration_removed", "removed", component="observability", details={"destination_id": destination_id, "endpoint_host": destination["endpoint_host"], "transport": destination["transport"]})
        return {"ok": True, **destination, "message": "Destination removed. Delivery history is retained locally; a send already in progress may still complete."}
    except sqlite3.Error as exc:
        logger.exception("Could not remove SIEM destination")
        raise HTTPException(status_code=503, detail="The destination could not be removed.") from exc


@app.post("/api/admin/observability/integrations/{destination_id}/enabled")
def observability_integration_enable(destination_id: str, payload: dict[str, Any], _: str = Depends(require_admin)) -> dict[str, Any]:
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool) or set(payload) != {"enabled"}:
        raise HTTPException(status_code=400, detail="Provide enabled as a boolean.")
    try:
        with observability_store._connection(DB_PATH) as db:
            db.execute("BEGIN IMMEDIATE")
            if not observability_siem.set_destination_enabled_in_transaction(db, destination_id, enabled):
                raise HTTPException(status_code=404, detail="Destination was not found.")
            _record_observability_security_event_in_transaction(db, "observability.integration_toggled", "enabled" if enabled else "disabled", component="observability", details={"destination_id": destination_id})
        return {"ok": True, "destination_id": destination_id, "enabled": enabled}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not change SIEM destination state")
        raise HTTPException(status_code=503, detail="The destination state could not be changed.") from exc


@app.post("/api/admin/observability/integrations/{destination_id}/test")
def observability_integration_test(destination_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        # Persist intent before a network side effect that cannot be rolled back.
        # The result record below complements this durable request record.
        with observability_store._connection(DB_PATH) as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT 1 FROM observability_siem_destinations WHERE destination_id=? AND archived_at IS NULL",
                (destination_id,),
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="Destination was not found.")
            _record_observability_security_event_in_transaction(
                db, "observability.integration_test_requested", "started", component="observability",
                details={"destination_id": destination_id},
            )
        result = observability_siem.send_test_event(DB_PATH, destination_id)
        with observability_store._connection(DB_PATH) as db:
            db.execute("BEGIN IMMEDIATE")
            _record_observability_security_event_in_transaction(
                db, "observability.integration_tested", "complete" if result.get("ok") else "failed",
                component="observability",
                details={"destination_id": destination_id, "http_status": result.get("http_status"), "error_code": result.get("error_code")},
            )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        logger.exception("Could not test SIEM destination")
        raise HTTPException(status_code=503, detail="The test could not be fully recorded or completed. It may already have been sent; check the receiver before retrying.") from exc


@app.get("/api/admin/engine-settings")
def engine_settings(_: str = Depends(require_admin)):
    return get_engine_settings()


@app.post("/api/admin/engine-settings")
def update_engine_settings(payload: dict[str, Any], _: str = Depends(require_admin)):
    booleans = {"face_matching_enabled", "object_detection_enabled", "object_tracking_enabled"}
    enum_values = {
        "object_image_size": {320, 416, 512, 640, 768},
        "camera_frame_interval_ms": {250, 500, 750, 1000, 1500},
        "camera_max_dimension": {480, 640, 800, 960, 1280},
    }
    updates: dict[str, str] = {}
    try:
        for key in booleans:
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise ValueError(f"{key} must be on or off")
                updates[key] = "1" if payload[key] else "0"
        if "match_threshold" in payload:
            value = float(payload["match_threshold"])
            if not 0.2 <= value <= 0.85:
                raise ValueError("Face match threshold must be between 0.20 and 0.85")
            updates["match_threshold"] = f"{value:.2f}"
        if "object_confidence" in payload:
            value = float(payload["object_confidence"])
            if not 0.05 <= value <= 0.95:
                raise ValueError("Object confidence must be between 0.05 and 0.95")
            updates["object_confidence"] = f"{value:.2f}"
        if "event_cooldown_seconds" in payload:
            value = int(payload["event_cooldown_seconds"])
            if not 15 <= value <= 3600:
                raise ValueError("Event cooldown must be between 15 and 3,600 seconds")
            updates["event_cooldown_seconds"] = str(value)
        if "training_status_refresh_seconds" in payload:
            value = int(payload["training_status_refresh_seconds"])
            if not 1 <= value <= 10:
                raise ValueError("Training status refresh interval must be between 1 and 10 seconds")
            updates["training_status_refresh_seconds"] = str(value)
        for key, allowed in enum_values.items():
            if key in payload:
                value = int(payload[key])
                if value not in allowed:
                    raise ValueError(f"Unsupported value for {key}")
                updates[key] = str(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updates:
        raise HTTPException(status_code=400, detail="No supported engine settings were provided")
    with db_connection() as db:
        db.executemany("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", list(updates.items()))
    return {"settings": get_engine_settings(), "message": "Engine settings saved. Detection settings apply on the next frame; camera capture settings apply when camera pages are opened or refreshed."}


@app.get("/api/admin/model-selection")
def model_selection(_: str = Depends(require_admin)):
    custom_path = ML_DATA_DIR / "active" / "best.pt"
    selected = get_selected_object_model()
    return {"selected": selected, "custom_available": custom_path.is_file(), "custom_path": str(custom_path), "base_available": (MODEL_DIR / "yolo26m.pt").is_file()}


@app.post("/api/admin/model-selection")
def set_model_selection(payload: dict[str, Any], _: str = Depends(require_admin)):
    choice = str(payload.get("model", ""))
    if choice not in {"custom", "yolo26m"}:
        raise HTTPException(status_code=400, detail="Choose your custom best.pt model or base YOLO26m")
    if choice == "custom" and not (ML_DATA_DIR / "active" / "best.pt").is_file():
        raise HTTPException(status_code=409, detail="Your custom best.pt model is not available yet. Train a model first.")
    with db_connection() as db:
        db.execute("INSERT INTO app_settings(key,value) VALUES('object_model_choice',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (choice,))
    return {"selected": choice, "restart_required": True}


@app.post("/api/admin/services/restart")
def request_service_restart(_: str = Depends(require_admin)):
    return request_launcher_command("services_restart")


def request_launcher_command(command: str) -> dict[str, str]:
    if PRODUCTION_MODE:
        raise HTTPException(status_code=409, detail="Service restarts are managed by the production host. Use the deployment platform to restart VAOS.")
    if not LOCAL_WINDOWS_LAUNCHER:
        raise HTTPException(status_code=409, detail="The local restart controller is provided by the Windows launcher. On macOS or Linux, stop and restart both VAOS Uvicorn services with their existing environment settings, or use your process manager.")
    try:
        if LAUNCHER_COMMAND_PATH.exists():
            raise HTTPException(status_code=409, detail="Another launcher action is already in progress.")
        temporary = LAUNCHER_COMMAND_PATH.with_suffix(".json.new")
        temporary.write_text(json.dumps({"command": command, "requested_at": utc_now()}), encoding="utf-8")
        temporary.replace(LAUNCHER_COMMAND_PATH)
    except OSError as exc:
        logger.exception("Could not send a launcher command")
        raise HTTPException(status_code=503, detail="The Windows launcher could not receive this request. Close and restart VAOS from start_windows.bat.") from exc
    return {"status": "queued", "message": "The Windows launcher received the request."}


def read_tunnel_state() -> dict[str, str]:
    if PRODUCTION_MODE and PUBLIC_CAMERA_URL:
        return {"status": "running", "message": "Production camera site is configured through the HTTPS reverse proxy."}
    try:
        state = json.loads(TUNNEL_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return {"status": str(state.get("status", "stopped")), "message": str(state.get("message", ""))}
    except (OSError, json.JSONDecodeError):
        pass
    try:
        if TUNNEL_URL_PATH.read_text(encoding="ascii").strip():
            return {"status": "running", "message": "Camera tunnel is connected."}
    except OSError:
        pass
    return {"status": "stopped", "message": "Camera tunnel is stopped."}


def write_tunnel_state(status: str, message: str) -> None:
    temporary = TUNNEL_STATE_PATH.with_suffix(".json.new")
    temporary.write_text(json.dumps({"status": status, "message": message, "updated_at": utc_now()}), encoding="utf-8")
    temporary.replace(TUNNEL_STATE_PATH)


def public_camera_paused() -> bool:
    return read_tunnel_state()["status"] == "paused"


@app.get("/api/admin/tunnel")
def tunnel_status(_: str = Depends(require_admin)):
    if PRODUCTION_MODE:
        return {"url": PUBLIC_CAMERA_URL, "status": "running", "message": "Production camera site is configured through the HTTPS reverse proxy.", "managed_by_platform": True}
    try:
        url = TUNNEL_URL_PATH.read_text(encoding="ascii").strip()
    except OSError:
        url = ""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not parts.hostname.endswith(".trycloudflare.com"):
        url = ""
    state = read_tunnel_state()
    return {"url": url, **state}


@app.post("/api/admin/tunnel/{action}")
def control_tunnel(action: str, _: str = Depends(require_admin)):
    if PRODUCTION_MODE:
        raise HTTPException(status_code=409, detail="Production camera access is managed by the HTTPS reverse proxy and deployment platform; temporary tunnel controls are unavailable.")
    if action not in {"pause", "start", "stop", "restart"}:
        raise HTTPException(status_code=404, detail="Unknown tunnel action")
    state = read_tunnel_state()
    if state["status"] in {"starting", "stopping", "restarting"}:
        raise HTTPException(status_code=409, detail="Wait for the current tunnel action to finish before choosing another.")
    if action == "pause":
        if state["status"] == "stopped":
            raise HTTPException(status_code=409, detail="Start the tunnel before pausing public camera access.")
        try:
            write_tunnel_state("paused", "Public camera requests are paused. The tunnel connection and URL are retained.")
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Could not update tunnel state on disk.") from exc
        return {"status": "paused", "message": "Public camera access is paused. The tunnel URL is retained."}
    if action == "start" and state["status"] == "paused":
        try:
            write_tunnel_state("running", "Camera tunnel is connected.")
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Could not resume public camera access.") from exc
        return {"status": "running", "message": "Public camera access resumed."}
    if action == "start" and state["status"] == "running":
        return {"status": "running", "message": "Camera tunnel is already running."}
    pending_status = {"start": "starting", "stop": "stopping", "restart": "restarting"}[action]
    try:
        write_tunnel_state(pending_status, f"Camera tunnel {action} requested.")
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Could not update tunnel state on disk.") from exc
    return request_launcher_command(f"tunnel_{action}")


def _admin_tunnel_launcher():
    if os.name != "nt":
        raise HTTPException(status_code=409, detail="The temporary dashboard tunnel is available only through the Windows launcher.")
    try:
        import psutil
        current = psutil.Process(os.getpid())
        for parent in current.parents():
            if parent.name().casefold() not in {"powershell.exe", "pwsh.exe"}:
                continue
            command = " ".join(parent.cmdline()).casefold()
            expected = str(APP_DIR / "start_windows.ps1").casefold()
            if expected in command:
                return parent.pid, parent.create_time()
    except Exception as exc:
        logger.warning("Could not identify the Windows launcher for the dashboard tunnel: %s", exc)
    raise HTTPException(status_code=409, detail="Start VAOS with start_windows.bat first so the dashboard tunnel stops automatically when the launcher exits.")


def _admin_tunnel_manager_alive(state: dict[str, Any]) -> bool:
    try:
        import psutil
        manager_pid = int(state.get("manager_pid", 0))
        manager = psutil.Process(manager_pid)
        if not manager.is_running() or manager.name().casefold() not in {"powershell.exe", "pwsh.exe"}:
            return False
        command = " ".join(manager.cmdline()).casefold()
        return str(ADMIN_TUNNEL_MANAGER_PATH).casefold() in command
    except Exception:
        return False


def _read_admin_tunnel_state() -> dict[str, str]:
    if PRODUCTION_MODE:
        return {"status": "managed", "message": "Production dashboard access is managed by your HTTPS deployment."}
    try:
        state = json.loads(ADMIN_TUNNEL_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    status = str(state.get("status", "stopped"))
    heartbeat = state.get("heartbeat_at")
    if status in {"starting", "running", "stopping", "restarting"} and heartbeat:
        try:
            last_heartbeat = datetime.fromisoformat(str(heartbeat))
            if last_heartbeat.tzinfo is None:
                last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_heartbeat).total_seconds() > 12:
                if _admin_tunnel_manager_alive(state):
                    if status != "running":
                        status = "starting"
                    state["message"] = "Dashboard tunnel manager is active; waiting for its latest connection status."
                else:
                    status = "stopped"
                    state["message"] = "Dashboard tunnel manager is no longer responding. Start the tunnel to reconnect."
        except ValueError:
            status = "stopped"
    try:
        url = ADMIN_TUNNEL_URL_PATH.read_text(encoding="ascii").strip()
    except OSError:
        url = ""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not parts.hostname.endswith(".trycloudflare.com"):
        url = ""
    if status != "running":
        url = ""
    return {"status": status, "message": str(state.get("message", "Dashboard tunnel is stopped.")), "url": url}


def _request_admin_tunnel_action(action: str) -> dict[str, str]:
    if PRODUCTION_MODE:
        raise HTTPException(status_code=409, detail="Production dashboard access is managed by your HTTPS deployment platform.")
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=404, detail="Unknown dashboard tunnel action")
    state = _read_admin_tunnel_state()
    if action == "start" and state["status"] == "running":
        return {**state, "message": "The dashboard tunnel is already running."}
    if state["status"] in {"starting", "stopping", "restarting"}:
        raise HTTPException(status_code=409, detail="Wait for the current dashboard tunnel action to finish.")
    if ADMIN_TUNNEL_COMMAND_PATH.exists():
        raise HTTPException(status_code=409, detail="A dashboard tunnel request is already queued.")
    try:
        launcher_pid, launcher_started = _admin_tunnel_launcher()
        temporary = ADMIN_TUNNEL_COMMAND_PATH.with_suffix(".json.new")
        temporary.write_text(json.dumps({"command": action, "requested_at": utc_now()}), encoding="utf-8")
        temporary.replace(ADMIN_TUNNEL_COMMAND_PATH)
        heartbeat = ""
        try:
            existing = json.loads(ADMIN_TUNNEL_STATE_PATH.read_text(encoding="utf-8"))
            heartbeat = str(existing.get("heartbeat_at", "")) if isinstance(existing, dict) else ""
        except (OSError, json.JSONDecodeError):
            pass
        heartbeat_is_fresh = False
        if heartbeat:
            try:
                last_heartbeat = datetime.fromisoformat(heartbeat)
                if last_heartbeat.tzinfo is None:
                    last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
                heartbeat_is_fresh = (datetime.now(timezone.utc) - last_heartbeat).total_seconds() <= 12
            except ValueError:
                pass
        if not heartbeat_is_fresh:
            if not ADMIN_TUNNEL_MANAGER_PATH.is_file():
                ADMIN_TUNNEL_COMMAND_PATH.unlink(missing_ok=True)
                raise HTTPException(status_code=503, detail="Dashboard tunnel manager file is missing.")
            powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
            args = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ADMIN_TUNNEL_MANAGER_PATH), "-LauncherPid", str(launcher_pid), "-LauncherStartedAt", str(launcher_started), "-DataDir", str(DATA_DIR), "-ToolsDir", str(APP_DIR / "tools")]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(args, cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        logger.exception("Could not request a dashboard tunnel action")
        raise HTTPException(status_code=503, detail="Could not start the dashboard tunnel manager. Check the VAOS launcher and logs.") from exc
    pending = {"start": "starting", "stop": "stopping", "restart": "restarting"}[action]
    return {"status": pending, "message": f"Dashboard tunnel {action} requested. The camera tunnel is unchanged."}


@app.get("/api/admin/admin-tunnel")
def admin_tunnel_status(_: str = Depends(require_admin)):
    return _read_admin_tunnel_state()


@app.post("/api/admin/admin-tunnel/{action}")
def control_admin_tunnel(action: str, _: str = Depends(require_admin)):
    return _request_admin_tunnel_action(action)


@app.get("/api/admin/cameras/{token}/qr")
def camera_qr(token: str, base_url: str, _: str = Depends(require_admin)):
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.netloc:
        raise HTTPException(status_code=400, detail="QR codes require the public HTTPS camera URL")
    with db_connection() as db:
        camera = db.execute("SELECT active FROM cameras WHERE token = ?", (token,)).fetchone()
    if not camera or not camera["active"]:
        raise HTTPException(status_code=404, detail="Camera link not found or revoked")
    camera_url = f"{parts.scheme}://{parts.netloc}/c/{token}"
    qr = qrcode.QRCode(box_size=8, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(camera_url)
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    output = BytesIO()
    image.save(output)
    return Response(
        output.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/admin/cameras/{token}/revoke")
def revoke_camera(token: str, _: str = Depends(require_admin)):
    with db_connection() as db:
        cur = db.execute("UPDATE cameras SET active = 0 WHERE token = ?", (token,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Camera not found")
    return {"ok": True}


@app.post("/api/admin/enrollment-links")
def create_enrollment_link(_: str = Depends(require_admin)):
    token = secrets.token_urlsafe(32)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(hours=24)
    with db_connection() as db:
        db.execute(
            "INSERT INTO enrollment_links(token,created_at,expires_at) VALUES(?,?,?)",
            (token, created.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
        )
    return {"token": token, "created_at": created.isoformat(timespec="seconds"), "expires_at": expires.isoformat(timespec="seconds")}


@app.get("/api/admin/enrollment-links/{token}/qr")
def enrollment_link_qr(token: str, base_url: str, _: str = Depends(require_admin)):
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.netloc or not is_enrollment_link_valid(token):
        raise HTTPException(status_code=400, detail="A valid enrollment invitation and public HTTPS tunnel URL are required")
    url = f"{parts.scheme}://{parts.netloc}/enroll/{token}"
    qr = qrcode.QRCode(box_size=8, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    output = BytesIO()
    image.save(output)
    return Response(content=output.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.delete("/api/admin/enrollment-links/{token}")
def revoke_enrollment_link(token: str, _: str = Depends(require_admin)):
    with db_connection() as db:
        cur = db.execute(
            "UPDATE enrollment_links SET revoked_at=? WHERE token=? AND revoked_at IS NULL AND used_at IS NULL",
            (utc_now(), token),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Enrollment link not found, already used, or already revoked")
    return {"ok": True}


@app.post("/api/admin/participants")
async def create_participant(
    name: str = Form(...),
    photo: UploadFile = File(...),
    _: str = Depends(require_admin),
):
    safe_name = name.strip()
    if not safe_name or len(safe_name) > 80:
        raise HTTPException(status_code=400, detail="Participant name must be 1–80 characters")
    content = await photo.read(MAX_IMAGE_BYTES + 1)
    try:
        embedding = await run_in_threadpool(extract_enrollment_embedding, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (RuntimeError, cv2.error) as exc:
        logger.exception("Participant enrollment model processing failed")
        raise HTTPException(status_code=503, detail="Face recognition models could not be loaded. Check the model files and server log, then try again.") from exc
    except Exception as exc:
        logger.exception("Unexpected participant enrollment failure")
        raise HTTPException(status_code=500, detail="Enrollment failed unexpectedly. Check the server log for details.") from exc
    try:
        encoded_photo = await run_in_threadpool(encode_profile_photo, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        with db_connection() as db:
            cur = db.execute(
                "INSERT INTO participants(name, embedding, profile_photo, created_at) VALUES (?, ?, ?, ?)",
                (safe_name, encode_embedding(embedding), sqlite3.Binary(encoded_photo), utc_now()),
            )
            participant_id = cur.lastrowid
    except Exception as exc:
        logger.exception("Participant enrollment database write failed")
        raise HTTPException(status_code=500, detail="The face template was created but could not be saved. Check the admin service log and available disk space.") from exc
    return {"id": participant_id, "name": safe_name, "created_at": utc_now(), "photo_saved": True}


async def create_public_participant(token: str, name: str, consent: str, photos: list[UploadFile]):
    if not is_enrollment_link_valid(token):
        raise HTTPException(status_code=410, detail="This enrollment link is invalid, expired, or already used.")
    if consent.strip().lower() not in {"yes", "true", "on", "1"}:
        raise HTTPException(status_code=400, detail="Please confirm that you agree to create and store this profile.")
    safe_name = name.strip()
    if not safe_name or len(safe_name) > 80:
        raise HTTPException(status_code=400, detail="Name must be 1–80 characters")
    if not photos or len(photos) > MAX_ENROLLMENT_PHOTOS:
        raise HTTPException(status_code=400, detail=f"Choose between 1 and {MAX_ENROLLMENT_PHOTOS} photos")
    contents, encoded_photos = [], []
    total_size = 0
    for upload in photos:
        content = await upload.read(MAX_IMAGE_BYTES + 1)
        total_size += len(content)
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="Each photo must be a readable image no larger than 6 MB")
        if total_size > MAX_ENROLLMENT_TOTAL_BYTES:
            raise HTTPException(status_code=400, detail="The selected photos are too large together; the total limit is 30 MB")
        contents.append(content)
    try:
        embeddings = [await run_in_threadpool(extract_enrollment_embedding, content) for content in contents]
        encoded_photos = [await run_in_threadpool(encode_profile_photo, content) for content in contents]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Self-enrollment image processing failed")
        raise HTTPException(status_code=503, detail="Face models could not process these photos. Check the admin service and try again.") from exc
    embedding = np.mean(np.stack(embeddings), axis=0).astype(np.float32)
    norm = float(np.linalg.norm(embedding))
    if not np.isfinite(norm) or norm == 0:
        raise HTTPException(status_code=400, detail="Could not make a stable profile from these photos")
    embedding /= norm
    if len(embeddings) > 1 and min(float(np.dot(item, embedding)) for item in embeddings) < 0.25:
        raise HTTPException(status_code=400, detail="The photos do not appear to show the same person. Please choose photos of yourself.")
    created_at = utc_now()
    try:
        with db_connection() as db:
            used = db.execute(
                "UPDATE enrollment_links SET used_at=? WHERE token=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?",
                (created_at, token, created_at),
            )
            if used.rowcount == 0:
                raise HTTPException(status_code=410, detail="This enrollment invitation has already been used or has expired.")
            cur = db.execute(
                "INSERT INTO participants(name,embedding,profile_photo,created_at) VALUES(?,?,?,?)",
                (safe_name, encode_embedding(embedding), sqlite3.Binary(encoded_photos[0]), created_at),
            )
            participant_id = cur.lastrowid
            for photo_bytes in encoded_photos[1:]:
                db.execute(
                    "INSERT INTO participant_photos(person_id,photo,created_at) VALUES(?,?,?)",
                    (participant_id, sqlite3.Binary(photo_bytes), created_at),
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Self-enrollment database write failed")
        raise HTTPException(status_code=500, detail="Profile could not be saved. Please contact the administrator.") from exc
    return {"id": participant_id, "name": safe_name, "created_at": created_at, "photos_saved": len(encoded_photos)}


@app.get("/api/admin/participants")
def list_participants(_: str = Depends(require_admin)):
    with db_connection() as db:
        rows = db.execute(
            "SELECT p.id, p.name, p.created_at, (p.profile_photo IS NOT NULL) AS has_photo, COUNT(s.id) AS sightings "
            "FROM participants p LEFT JOIN sightings s ON s.person_id = p.id "
            "GROUP BY p.id ORDER BY p.created_at DESC"
        ).fetchall()
        people = []
        for row in rows:
            person = dict(row)
            person["photos"] = ([f"/api/admin/participants/{person['id']}/photo"] if person["has_photo"] else [])
            extras = db.execute("SELECT id FROM participant_photos WHERE person_id=? ORDER BY id", (person["id"],)).fetchall()
            person["photos"].extend(f"/api/admin/participants/{person['id']}/photos/{extra['id']}" for extra in extras)
            people.append(person)
    return people


@app.get("/api/admin/participants/{person_id}/photo")
def participant_photo(person_id: int, _: str = Depends(require_admin)):
    with db_connection() as db:
        row = db.execute("SELECT profile_photo FROM participants WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    if not row["profile_photo"]:
        raise HTTPException(status_code=404, detail="Profile photo is not available")
    return Response(content=bytes(row["profile_photo"]), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@app.get("/api/admin/participants/{person_id}/photos/{photo_id}")
def participant_additional_photo(person_id: int, photo_id: int, _: str = Depends(require_admin)):
    with db_connection() as db:
        row = db.execute("SELECT photo FROM participant_photos WHERE id=? AND person_id=?", (photo_id, person_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Profile photo not found")
    return Response(content=bytes(row["photo"]), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@app.delete("/api/admin/participants/{person_id}")
def delete_participant(person_id: int, _: str = Depends(require_admin)):
    with db_connection() as db:
        cur = db.execute("DELETE FROM participants WHERE id = ?", (person_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Participant not found")
    for key in list(last_emitted):
        if key[1] == person_id:
            last_emitted.pop(key, None)
    return {"ok": True}


@app.get("/api/admin/events")
def list_events(_: str = Depends(require_admin)):
    with db_connection() as db:
        rows = db.execute(
            "SELECT s.id, s.timestamp, s.score, s.kind, c.name AS camera_name, p.name AS person_name "
            "FROM sightings s JOIN cameras c ON c.token = s.camera_token "
            "JOIN participants p ON p.id = s.person_id ORDER BY s.id DESC LIMIT 200"
        ).fetchall()
    return [dict(row) for row in rows]


@app.delete("/api/admin/events")
def clear_events(_: str = Depends(require_admin)):
    with db_connection() as db:
        db.execute("DELETE FROM sightings")
        db.execute("DELETE FROM object_sightings")
        db.execute("DELETE FROM object_rule_events")
    last_emitted.clear()
    last_object_emitted.clear()
    return {"ok": True}


def _tracking_rule_response(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw_region = result.pop("region_json", "{}")
    try:
        result["region"] = json.loads(raw_region)
    except (TypeError, ValueError, json.JSONDecodeError):
        result["region"] = {"x": 0, "y": 0, "width": 1, "height": 1}
    result["enabled"] = bool(result.get("enabled"))
    return result


def _validate_rule_camera(db: sqlite3.Connection, camera_token: str | None) -> None:
    if camera_token is None:
        return
    camera = db.execute("SELECT active FROM cameras WHERE token=?", (camera_token,)).fetchone()
    if camera is None:
        raise HTTPException(status_code=404, detail="Selected camera was not found.")
    if not camera["active"]:
        raise HTTPException(status_code=409, detail="Selected camera link is inactive. Reactivate it or use All active cameras.")


@app.get("/api/admin/tracking/rules")
def list_tracking_rules(_: str = Depends(require_admin)) -> dict[str, Any]:
    with db_connection() as db:
        rows = db.execute(
            "SELECT r.*,c.name AS camera_name FROM object_event_rules r "
            "LEFT JOIN cameras c ON c.token=r.camera_token ORDER BY r.id DESC LIMIT 100"
        ).fetchall()
    return {"rules": [_tracking_rule_response(row) for row in rows], "limit": 100,
            "tracking_note": "Track continuity is approximate, camera-local, and reset when the camera service restarts."}


@app.post("/api/admin/tracking/rules")
def create_tracking_rule(payload: TrackingRuleInput, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        normalized = validate_rule(payload.dict())
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = utc_now()
    with db_connection() as db:
        _validate_rule_camera(db, normalized["camera_token"])
        count = int(db.execute("SELECT COUNT(*) FROM object_event_rules").fetchone()[0])
        if count >= 100:
            raise HTTPException(status_code=409, detail="VAOS supports up to 100 saved tracking rules. Remove an unused rule first.")
        cursor = db.execute(
            "INSERT INTO object_event_rules(name,enabled,rule_type,class_name,min_confidence,threshold_count,"
            "min_consecutive_frames,cooldown_seconds,dwell_seconds,camera_token,region_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (normalized["name"], int(normalized["enabled"]), normalized["rule_type"], normalized["class_name"],
             normalized["min_confidence"], normalized["threshold_count"], normalized["min_consecutive_frames"],
             normalized["cooldown_seconds"], normalized["dwell_seconds"], normalized["camera_token"],
             json.dumps(normalized["region"], separators=(",", ":")), now, now),
        )
        row = db.execute(
            "SELECT r.*,c.name AS camera_name FROM object_event_rules r LEFT JOIN cameras c ON c.token=r.camera_token WHERE r.id=?",
            (cursor.lastrowid,),
        ).fetchone()
    invalidate_tracking_rule_cache()
    return {"rule": _tracking_rule_response(row), "message": "Tracking rule saved. Events will be evaluated on future camera frames."}


@app.patch("/api/admin/tracking/rules/{rule_id}")
def update_tracking_rule(rule_id: int, payload: TrackingRulePatch, _: str = Depends(require_admin)) -> dict[str, Any]:
    patch_value = payload.dict(exclude_unset=True)
    if not patch_value:
        raise HTTPException(status_code=422, detail="Provide at least one rule field to update.")
    if any(value is None and key not in {"camera_token", "dwell_seconds", "region"} for key, value in patch_value.items()):
        raise HTTPException(status_code=422, detail="Required rule fields cannot be cleared; provide a replacement value.")
    with db_connection() as db:
        current = db.execute("SELECT * FROM object_event_rules WHERE id=?", (rule_id,)).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="Tracking rule was not found.")
        merged = dict(current)
        try:
            merged["region"] = json.loads(merged.pop("region_json", "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            merged["region"] = {"x": 0, "y": 0, "width": 1, "height": 1}
        merged["enabled"] = bool(merged["enabled"])
        merged.update(patch_value)
        try:
            normalized = validate_rule(merged)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _validate_rule_camera(db, normalized["camera_token"])
        db.execute(
            "UPDATE object_event_rules SET name=?,enabled=?,rule_type=?,class_name=?,min_confidence=?,threshold_count=?,"
            "min_consecutive_frames=?,cooldown_seconds=?,dwell_seconds=?,camera_token=?,region_json=?,updated_at=? WHERE id=?",
            (normalized["name"], int(normalized["enabled"]), normalized["rule_type"], normalized["class_name"],
             normalized["min_confidence"], normalized["threshold_count"], normalized["min_consecutive_frames"],
             normalized["cooldown_seconds"], normalized["dwell_seconds"], normalized["camera_token"],
             json.dumps(normalized["region"], separators=(",", ":")), utc_now(), rule_id),
        )
        row = db.execute(
            "SELECT r.*,c.name AS camera_name FROM object_event_rules r LEFT JOIN cameras c ON c.token=r.camera_token WHERE r.id=?",
            (rule_id,),
        ).fetchone()
    invalidate_tracking_rule_cache(rule_id)
    return {"rule": _tracking_rule_response(row), "message": "Tracking rule updated."}


@app.delete("/api/admin/tracking/rules/{rule_id}")
def delete_tracking_rule(rule_id: int, _: str = Depends(require_admin)) -> dict[str, Any]:
    with db_connection() as db:
        cursor = db.execute("DELETE FROM object_event_rules WHERE id=?", (rule_id,))
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Tracking rule was not found.")
    invalidate_tracking_rule_cache(rule_id)
    return {"ok": True, "message": "Rule removed. Previously recorded rule events are retained."}


@app.get("/api/admin/tracking/events")
def list_tracking_events(limit: int = 100, before_id: int | None = None, camera_token: str | None = None,
                         rule_id: int | None = None, _: str = Depends(require_admin)) -> dict[str, Any]:
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="Limit must be between 1 and 500.")
    clauses: list[str] = []
    values: list[Any] = []
    if before_id is not None:
        clauses.append("e.id < ?")
        values.append(before_id)
    if camera_token:
        clauses.append("e.camera_token = ?")
        values.append(camera_token)
    if rule_id is not None:
        clauses.append("e.rule_id = ?")
        values.append(rule_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db_connection() as db:
        rows = db.execute(
            "SELECT e.id,e.timestamp,e.rule_id,e.rule_name,e.rule_type,e.class_name,e.object_count,e.message,"
            "e.camera_token,c.name AS camera_name FROM object_rule_events e "
            "JOIN cameras c ON c.token=e.camera_token " + where + " ORDER BY e.id DESC LIMIT ?",
            (*values, limit),
        ).fetchall()
    return {"events": [dict(row) for row in rows], "limit": limit,
            "tracking_note": "Events are based on approximate short-term tracks and are not proof of identity."}


def load_public_dataset_imports() -> dict[str, Any]:
    path = ML_DATA_DIR / "public_dataset_imports.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
        # Only datasets with an implemented preparation and validation path are
        # exposed to training. Raw or retired registry records remain unusable.
        return {key: item for key, item in value.items() if key in {"coco2017", "uvh26", "kaggle_auto"} or (key.startswith("local_") and isinstance(item, dict) and item.get("managed_local_dataset") is True)}
    except (OSError, json.JSONDecodeError):
        return {}


def write_public_dataset_imports(value: dict[str, Any]) -> None:
    path = ML_DATA_DIR / "public_dataset_imports.json"
    temporary = path.with_suffix(".json.new")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def _path_is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _dataset_allowed_roots() -> list[Path]:
    return [
        default_datasets_dir().resolve(),
        ML_DATASETS_DIR.resolve(),
    ]


def _training_dataset_roots() -> list[Path]:
    imports = load_public_dataset_imports()
    dataset_root = default_datasets_dir().expanduser()
    active_source = str(ml_training_state.get("source") or "")
    if active_source:
        roots: list[Path] = []
        if active_source in {"reviewed", "combined", "combined_kaggle", "uvh26_combined", "combined_vehicle"}:
            roots.append(ML_DATA_DIR.resolve())
        elif active_source.startswith("dataset:"):
            record = imports.get(active_source.split(":", 1)[1], {})
            if isinstance(record, dict):
                for key in ("source_root", "data_root", "data_yaml"):
                    if record.get(key):
                        path = Path(str(record[key])).expanduser()
                        roots.append((path.parent if path.suffix.lower() in {".yaml", ".yml"} else path).resolve())
        else:
            dataset_ids = {
                "coco2017": {"coco2017"}, "combined": {"coco2017"},
                "combined_kaggle": {"coco2017", "kaggle_auto"},
                "uvh26_combined": {"uvh26"},
                "combined_vehicle": {"coco2017", "uvh26", "kaggle_auto"},
            }.get(active_source, set())
            for dataset_id in dataset_ids:
                record = imports.get(dataset_id, {})
                if dataset_id == "uvh26":
                    roots.append((dataset_root / "UVH-26").resolve())
                elif dataset_id == "kaggle_auto":
                    roots.append((dataset_root / "kaggle_auto").resolve())
                elif isinstance(record, dict) and record.get("data_yaml"):
                    roots.append(Path(str(record["data_yaml"])).expanduser().resolve().parent)
        return roots or [ML_DATA_DIR.resolve()]

    # Older running jobs do not expose their selected source in their status
    # record. Be conservative for those jobs until they finish.
    roots = [ML_DATA_DIR.resolve(), (dataset_root / "UVH-26").resolve(), (dataset_root / "kaggle_auto").resolve()]
    for record in imports.values():
        if not isinstance(record, dict):
            continue
        for key in ("source_root", "data_root", "data_yaml"):
            raw = record.get(key)
            if not raw:
                continue
            path = Path(str(raw)).expanduser()
            roots.append((path.parent if path.suffix.lower() in {".yaml", ".yml"} else path).resolve())
    return roots


def _read_yolo_dataset_config(yaml_path: Path) -> tuple[dict[str, Any], Path, list[str]]:
    try:
        import yaml
        config = yaml.safe_load(yaml_path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Could not read dataset YAML: {exc}") from exc
    except ImportError as exc:
        raise ValueError("PyYAML is not installed in the VAOS admin service environment.") from exc
    if not isinstance(config, dict):
        raise ValueError("Dataset YAML must contain a mapping with path, train, val, and names.")
    names_value = config.get("names")
    if isinstance(names_value, dict):
        try:
            name_keys = sorted(names_value, key=lambda item: int(item))
            if [int(key) for key in name_keys] != list(range(len(name_keys))):
                raise ValueError("The class IDs in dataset YAML must be consecutive integers starting at 0.")
            names = [str(names_value[key]).strip() for key in name_keys]
        except (TypeError, ValueError):
            if all(str(key).lstrip("+-").isdigit() for key in names_value):
                raise
            names = [str(value).strip() for value in names_value.values()]
    elif isinstance(names_value, list):
        names = [str(value).strip() for value in names_value]
    else:
        raise ValueError("The YAML must define object class names as a list or numbered mapping.")
    if not names or any(not name for name in names):
        raise ValueError("The dataset must define at least one non-empty object class name.")
    raw_root = config.get("path") or str(yaml_path.parent)
    root = Path(str(raw_root)).expanduser()
    if not root.is_absolute():
        root = yaml_path.parent / root
    root = root.resolve()
    return config, root, names


def _split_image_paths(value: Any, dataset_root: Path) -> list[Path]:
    entries = value if isinstance(value, list) else [value]
    images: list[Path] = []
    extensions = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
    for entry in entries:
        if not isinstance(entry, (str, Path)):
            raise ValueError("Train/validation entries must be image folders, image-list files, or lists of those paths.")
        path = Path(str(entry)).expanduser()
        if not path.is_absolute():
            path = dataset_root / path
        path = path.resolve()
        if path.is_file() and path.suffix.lower() in {".txt", ".list"}:
            try:
                listing = path.read_text(encoding="utf-8-sig").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"Could not read image list {path.name}: {exc}") from exc
            for line in listing:
                raw = line.strip()
                if not raw:
                    continue
                image = Path(raw).expanduser()
                if not image.is_absolute():
                    image = (path.parent / image).resolve()
                if image.suffix.lower() in extensions:
                    images.append(image.resolve())
        elif path.is_dir():
            images.extend(item.resolve() for item in path.rglob("*") if item.is_file() and item.suffix.lower() in extensions)
        elif path.is_file() and path.suffix.lower() in extensions:
            images.append(path)
        else:
            raise ValueError(f"Train/validation path is missing or unsupported: {entry}")
    return images


def _label_path_for_image(image_path: Path) -> Path:
    parts = list(image_path.parts)
    indexes = [index for index, part in enumerate(parts) if part.casefold() == "images"]
    if indexes:
        parts[indexes[-1]] = "labels"
        return Path(*parts).with_suffix(".txt")
    return image_path.parent.parent / "labels" / image_path.parent.name / f"{image_path.stem}.txt"


def validate_local_yolo_dataset(source_path: Path, progress_callback=None) -> dict[str, Any]:
    """CPU-only structural and annotation validation for a YOLO detection dataset."""
    source_path = source_path.resolve()
    if source_path.is_file() and source_path.suffix.lower() in {".yaml", ".yml"}:
        yaml_path = source_path
        source_root = yaml_path.parent.resolve()
    elif source_path.is_dir():
        source_root = source_path
        candidates = [source_root / "dataset.yaml", source_root / "data.yaml"]
        yaml_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if yaml_path is None:
            raise ValueError("Choose a YOLO detection dataset folder containing dataset.yaml or data.yaml, or select its YAML file directly.")
    else:
        raise ValueError("The selected path must be an existing dataset folder or YAML file.")
    if yaml_path.is_symlink() or source_root.is_symlink():
        raise ValueError("Dataset roots and YAML files cannot be symbolic links.")
    config, dataset_root, names = _read_yolo_dataset_config(yaml_path)
    if not _path_is_within(yaml_path, source_root) or not _path_is_within(dataset_root, source_root):
        raise ValueError("The YAML and its declared dataset root must stay inside the selected source folder. This is required for safe training and purge.")
    split_images = {}
    for split in ("train", "val"):
        if not config.get(split):
            raise ValueError(f"The dataset YAML is missing the required '{split}' split.")
        images = _split_image_paths(config[split], dataset_root)
        if not images:
            raise ValueError(f"The {split} split contains no supported images.")
        if any(not _path_is_within(image, source_root) for image in images):
            raise ValueError(f"At least one {split} image points outside the selected source folder; this dataset cannot be safely purged as a unit.")
        split_images[split] = images
    all_images = split_images["train"] + split_images["val"]
    total = len(all_images)
    missing_labels = invalid_labels = invalid_images = boxes = 0
    for index, image in enumerate(all_images, start=1):
        try:
            if image.stat().st_size <= 0 or cv2.imread(str(image), cv2.IMREAD_UNCHANGED) is None:
                invalid_images += 1
        except OSError:
            invalid_images += 1
        label_path = _label_path_for_image(image)
        if not _path_is_within(label_path, source_root):
            raise ValueError("A derived label path escaped the selected dataset folder.")
        if not label_path.is_file():
            missing_labels += 1
        else:
            try:
                for line in label_path.read_text(encoding="utf-8-sig").splitlines():
                    fields = line.split()
                    if not fields:
                        continue
                    if len(fields) != 5:
                        invalid_labels += 1
                        continue
                    class_id = int(fields[0])
                    coords = [float(value) for value in fields[1:]]
                    if class_id < 0 or class_id >= len(names) or not np.isfinite(coords).all() or any(value < 0 or value > 1 for value in coords) or coords[2] <= 0 or coords[3] <= 0:
                        invalid_labels += 1
                        continue
                    boxes += 1
            except (OSError, UnicodeDecodeError, ValueError):
                invalid_labels += 1
        if progress_callback and (index == total or index % 250 == 0):
            progress_callback(index, total)
    if invalid_images:
        raise ValueError(f"Could not decode {invalid_images:,} listed images with the CPU image reader. Repair those files before adding this dataset.")
    if boxes == 0:
        raise ValueError("No valid YOLO detection boxes were found. This looks like an unlabeled or segmentation dataset, so it cannot train the VAOS detector.")
    if invalid_labels:
        raise ValueError(f"Found {invalid_labels:,} invalid YOLO label rows. Fix them before adding this dataset to training.")
    return {
        "data_yaml": str(yaml_path.resolve()), "source_root": str(source_root), "data_root": str(dataset_root),
        "classes": names, "train_count": len(split_images["train"]), "val_count": len(split_images["val"]),
        "image_count": total, "labeled_images": total - missing_labels, "missing_label_files": missing_labels,
        "invalid_images": invalid_images, "annotations": boxes,
    }


def _dataset_conflicts_with_training(source_root: Path) -> bool:
    if not ml_training_lock.locked():
        return False
    return any(_path_is_within(source_root, active) or _path_is_within(active, source_root) for active in _training_dataset_roots())


def _prepare_local_dataset_worker(name: str, source_path: Path, dataset_id: str) -> None:
    global ml_dataset_prepare_state
    preparation_started = time.perf_counter()
    try:
        ml_dataset_prepare_state.update({"status": "preparing", "progress": 0, "message": "Checking the local YOLO dataset on CPU. No CUDA/GPU calls are made."})
        def progress(current: int, total: int) -> None:
            ml_dataset_prepare_state.update({"status": "preparing", "progress": min(99, round(current * 100 / max(total, 1))), "message": f"CPU validation · checked {current:,} of {total:,} images and labels. No CUDA/GPU calls are made."})
        report = validate_local_yolo_dataset(source_path, progress)
        if _dataset_conflicts_with_training(Path(report["source_root"])):
            raise ValueError("This dataset overlaps files currently used by the active training run. Wait for training to finish before adding it.")
        record = {
            "id": dataset_id, "name": name, "managed_local_dataset": True,
            "data_yaml": report["data_yaml"], "source_root": report["source_root"],
            "data_root": report["data_root"], "classes": report["classes"],
            "train_count": report["train_count"], "val_count": report["val_count"],
            "image_count": report["image_count"], "labeled_images": report["labeled_images"],
            "missing_label_files": report["missing_label_files"], "annotations": report["annotations"],
            "validated_at": utc_now(), "imported_at": utc_now(), "format": "YOLO detection",
        }
        imports = load_public_dataset_imports()
        imports[dataset_id] = record
        write_public_dataset_imports(imports)
        _record_observability_dataset_result(
            "managed_local", succeeded=True, duration_seconds=time.perf_counter() - preparation_started,
            image_count=report["image_count"], train_image_count=report["train_count"],
            validation_image_count=report["val_count"], class_count=len(report["classes"]),
            annotation_count=report["annotations"],
        )
        ml_dataset_prepare_state = {"status": "complete", "progress": 100, "dataset": dataset_id, "message": f"{name} validated and added to the training-data menu · {report['image_count']:,} images · {len(report['classes']):,} classes · {report['annotations']:,} valid boxes. CPU-only; GPU was not used.", "report": record}
    except Exception as exc:
        _record_observability_dataset_result("managed_local", succeeded=False, duration_seconds=time.perf_counter() - preparation_started)
        ml_dataset_prepare_state = {"status": "error", "progress": 0, "message": str(exc)[:700]}
        logger.warning("Local dataset validation failed: %s", exc, exc_info=True)
    finally:
        ml_dataset_prepare_lock.release()


@app.get("/api/admin/ml/datasets/managed")
def list_managed_datasets(_: str = Depends(require_admin)):
    imports = load_public_dataset_imports()
    datasets = [item for key, item in imports.items() if key.startswith("local_") and isinstance(item, dict) and item.get("managed_local_dataset") is True]
    training_active = ml_training_lock.locked()
    return {"datasets": datasets, "preparation": dict(ml_dataset_prepare_state), "training_active": training_active}


@app.post("/api/admin/ml/datasets/prepare")
def prepare_local_dataset(payload: dict[str, Any], _: str = Depends(require_admin)):
    raw_path = str(payload.get("path", "")).strip().strip('"')
    name = str(payload.get("name", "")).strip()[:80]
    allow_during_training = payload.get("cpu_only_during_training") is True
    if not raw_path or not name:
        raise HTTPException(status_code=400, detail="Enter a dataset name and its local folder or YAML file path.")
    source_path = Path(raw_path).expanduser()
    if not source_path.exists() or source_path.is_symlink():
        raise HTTPException(status_code=400, detail="The selected dataset path does not exist or is a symbolic link.")
    source_root = source_path.parent.resolve() if source_path.is_file() else source_path.resolve()
    allowed_roots = _dataset_allowed_roots()
    if not any(_path_is_within(source_root, root) for root in allowed_roots):
        raise HTTPException(status_code=400, detail="For safe registration and purge, place datasets under the configured dataset folder or this app's managed training-data folder.")
    if source_root in allowed_roots:
        raise HTTPException(status_code=400, detail="Select a specific dataset subfolder, not the full datasets root.")
    if any(Path(str(item.get("source_root", ""))).resolve() == source_root for item in load_public_dataset_imports().values() if isinstance(item, dict) and item.get("source_root")):
        raise HTTPException(status_code=409, detail="This folder is already registered as a dataset.")
    if ml_training_lock.locked() and not allow_during_training:
        raise HTTPException(status_code=409, detail="Training is active. Enable the CPU-only preparation option to validate a separate dataset without using the GPU.")
    if ml_dataset_import_lock.locked() or not ml_dataset_prepare_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another dataset download or preparation is running. Wait for it to finish first.")
    if _dataset_conflicts_with_training(source_root):
        ml_dataset_prepare_lock.release()
        raise HTTPException(status_code=409, detail="This folder overlaps a dataset currently used by training. Choose a separate folder or wait for training to finish.")
    dataset_id = f"local_{secrets.token_hex(6)}"
    ml_dataset_prepare_state.update({"status": "preparing", "progress": 0, "dataset": dataset_id, "message": "Starting CPU-only validation. CUDA/GPU will not be queried or used."})
    try:
        _start_context_preserving_thread(_prepare_local_dataset_worker, args=(name, source_path, dataset_id), name="vaos-local-dataset-validation")
    except Exception as exc:
        ml_dataset_prepare_lock.release()
        ml_dataset_prepare_state.update({"status": "error", "message": f"Could not start CPU validation: {exc}"})
        raise HTTPException(status_code=503, detail="Could not start local dataset validation.") from exc
    return {"status": "preparing", "dataset": dataset_id, "message": "CPU-only dataset validation started. The GPU is not used."}


@app.delete("/api/admin/ml/datasets/managed/{dataset_id}")
def purge_managed_dataset(dataset_id: str, payload: dict[str, Any] | None = None, _: str = Depends(require_admin)):
    imports = load_public_dataset_imports()
    record = imports.get(dataset_id)
    public_coco = dataset_id == "coco2017" and isinstance(record, dict)
    if not public_coco and (not dataset_id.startswith("local_") or not isinstance(record, dict) or record.get("managed_local_dataset") is not True):
        raise HTTPException(status_code=404, detail="This managed local dataset was not found.")
    expected_name = str(record.get("name", ""))
    if str((payload or {}).get("confirm_name", "")) != expected_name:
        raise HTTPException(status_code=400, detail="Confirmation did not match the dataset name.")
    if ml_training_lock.locked() or ml_dataset_import_lock.locked() or ml_dataset_prepare_lock.locked():
        raise HTTPException(status_code=409, detail="Stop/wait for training and dataset preparation/downloads to finish before purging a dataset.")
    raw_root = str(ML_DATASETS_DIR / "coco2017") if public_coco else record.get("source_root")
    if not raw_root:
        raise HTTPException(status_code=409, detail="This dataset has no recorded source folder, so VAOS cannot safely purge it.")
    if Path(str(raw_root)).is_symlink():
        raise HTTPException(status_code=403, detail="Linked dataset roots cannot be purged.")
    source_root = Path(str(raw_root)).resolve()
    yaml_value = str(record.get("data_yaml", ""))
    yaml_path = Path(yaml_value).resolve() if yaml_value else Path()
    allowed_roots = _dataset_allowed_roots()
    if source_root.is_symlink() or source_root in allowed_roots or not any(_path_is_within(source_root, root) for root in allowed_roots) or not yaml_value or yaml_path.suffix.lower() not in {".yaml", ".yml"} or not _path_is_within(yaml_path, source_root):
        raise HTTPException(status_code=403, detail="The recorded folder is outside approved dataset roots or is not safe to purge.")
    if public_coco and yaml_path != (source_root / "dataset.yaml"):
        raise HTTPException(status_code=403, detail="The public COCO manifest does not match its managed folder.")
    for other_id, other in imports.items():
        if other_id == dataset_id or not isinstance(other, dict):
            continue
        other_root = other.get("source_root") or other.get("data_root")
        if not other_root and other.get("data_yaml"):
            other_root = str(Path(other["data_yaml"]).parent)
        if other_root and (_path_is_within(Path(other_root).resolve(), source_root) or _path_is_within(source_root, Path(other_root).resolve())):
            raise HTTPException(status_code=409, detail="Another registered dataset shares this folder. Remove the overlapping registration first.")
    if not source_root.is_dir():
        imports.pop(dataset_id, None)
        write_public_dataset_imports(imports)
        return {"ok": True, "removed_from_training": True, "folder_removed": False, "message": "The dataset was removed from the training menu; its source folder was already missing."}
    staged = source_root.with_name(f".vaos-purging-{dataset_id}")
    if staged.exists():
        raise HTTPException(status_code=409, detail="A previous purge staging folder exists. Review it before retrying.")
    try:
        source_root.rename(staged)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"Could not safely stage the original folder for removal: {exc}") from exc
    imports.pop(dataset_id, None)
    try:
        write_public_dataset_imports(imports)
    except OSError as exc:
        staged.rename(source_root)
        raise HTTPException(status_code=500, detail="Could not update the training menu; the dataset folder was restored.") from exc
    try:
        shutil.rmtree(staged)
    except OSError as exc:
        logger.exception("Dataset purge did not remove staged folder %s", staged)
        raise HTTPException(status_code=500, detail=f"Removed from the training menu, but some files remain in {staged}. Check that folder before retrying.") from exc
    return {"ok": True, "removed_from_training": True, "folder_removed": True, "message": f"{expected_name} and its original dataset folder were purged."}


def local_dataset_catalog() -> list[dict[str, Any]]:
    """Describe local datasets that are present but are not yet detector-ready.

    These entries are intentionally informational until their annotations and
    image references have passed conversion/validation. That keeps raw or
    incomplete data from being accidentally used in a model update.
    """
    root = default_datasets_dir()
    idd = root / "idd-lite" / "idd20k_lite"
    india = root / "IndiaScene365-v2" / "IndiaScene365" / "Indiascene365"
    uvh = root / "UVH-26"
    objects365_candidates = (root / "Objects365", root / "objects365", root / "Objects365v2")
    items: list[dict[str, Any]] = []
    if idd.exists():
        items.append({"id": "idd-lite", "name": "IDD Lite · Indian road scenes", "status": "Segmentation only · not detector-ready", "ready": False, "message": "This is the IDD Lite segmentation variant; its masks do not provide object boxes. Converting masks to boxes could merge adjacent traffic objects, so VAOS keeps it out of detection training. The separate IDD Detection download contains bounding boxes."})
    if india.exists():
        items.append({"id": "indiascene365", "name": "IndiaScene365 · Indian scenes", "status": "Downloaded · annotation check failed", "ready": False, "message": "The local XML annotations do not match the image filenames in the downloaded folders, and several labels are corrupted. It is excluded from training until the source files are repaired."})
    if uvh.exists():
        train_json = uvh / "UVH-26-Train" / "UVH-26-MV-Train.json"
        val_json = uvh / "UVH-26-Val" / "UVH-26-MV-Val.json"
        val_images = uvh / "UVH-26-Val" / "data"
        imported = load_public_dataset_imports().get("uvh26")
        ready = bool(imported and Path(str(imported.get("data_yaml", ""))).is_file())
        downloadable = train_json.is_file() and val_json.is_file() and val_images.is_dir()
        items.append({"id": "uvh26", "name": "UVH-26 · Indian road vehicles", "status": "Validated · ready" if ready else "Ready to validate" if downloadable else "Download incomplete", "ready": ready, "can_prepare": True, "message": "Validated majority-vote annotations for Indian road vehicles. Training can use this dataset together with reviewed samples." if ready else "Download official train and validation files, then validate every image reference and convert the boxes to YOLO format." if not downloadable else "Both train and validation files are present. Run validation before using UVH-26 for training."})
    kaggle = root / "kaggle_auto"
    kaggle_annotations = kaggle / "Annotations" / "Annotations"
    kaggle_images = kaggle / "auto"
    kaggle_xml = list(kaggle_annotations.glob("*.xml")) if kaggle_annotations.is_dir() else []
    kaggle_image_count = sum(1 for path in kaggle_images.iterdir() if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}) if kaggle_images.is_dir() else 0
    kaggle_import = load_public_dataset_imports().get("kaggle_auto")
    kaggle_ready = bool(kaggle_import and Path(str(kaggle_import.get("data_yaml", ""))).is_file())
    kaggle_can_prepare = bool(kaggle_xml and kaggle_image_count and len(kaggle_xml) == kaggle_image_count)
    if kaggle.exists():
        items.append({"id": "kaggle_auto", "name": "Kaggle Auto · autorickshaws", "status": "Validated · ready" if kaggle_ready else "Ready to validate" if kaggle_can_prepare else "Image/annotation mismatch", "ready": kaggle_ready, "can_prepare": kaggle_can_prepare, "message": f"Validated {kaggle_image_count:,} Indian autorickshaw images and their Pascal VOC boxes." if kaggle_ready else f"Found {kaggle_image_count:,} images and {len(kaggle_xml):,} Pascal VOC annotation files. Validate filenames, dimensions, and boxes before training." if kaggle_can_prepare else "The local Kaggle Auto folder needs a matching image and Pascal VOC XML annotation for every sample before it can be trained."})
    objects365 = next((path for path in objects365_candidates if path.exists()), None)
    if objects365 is None:
        items.append({"id": "objects365", "name": "Objects365", "status": "Not found locally", "ready": False, "message": "No Objects365 dataset folder is present in the configured dataset directory."})
    else:
        items.append({"id": "objects365", "name": "Objects365", "status": "Downloaded · needs preparation", "ready": False, "message": "Local files were found, but no validated YOLO dataset manifest is registered yet."})
    return items


def uvh26_source_root() -> Path:
    return default_datasets_dir() / "UVH-26"


def prepare_uvh26_dataset() -> None:
    """Validate official UVH-26 MV splits and create a YOLO-ready derivative.

    Source images and annotations are never edited. Image hardlinks keep this
    derivative space-efficient; if the dataset and derivative are on separate
    volumes or hardlinks are unavailable, preparation stops instead of copying
    several gigabytes unexpectedly.
    """
    global ml_dataset_import_state
    preparation_started = time.perf_counter()
    source_root = uvh26_source_root()
    split_specs = {
        "train": (source_root / "UVH-26-Train" / "UVH-26-MV-Train.json", source_root / "UVH-26-Train"),
        "val": (source_root / "UVH-26-Val" / "UVH-26-MV-Val.json", source_root / "UVH-26-Val"),
    }
    staging = source_root / ".vaos-uvh26-yolo-incomplete"
    final_root = source_root / "vaos-uvh26-yolo"
    staging_created = False
    try:
        ml_dataset_import_state.update({"status": "downloading", "dataset": "uvh26", "phase": "connecting", "progress": 0, "downloaded_bytes": 0, "expected_bytes": 90_000_000_000, "speed_bytes_per_second": 0, "message": "Resuming the official UVH-26 download to the configured dataset folder. COCO 2017 + reviewed + Kaggle Auto training can run alongside this download."})
        if any(not annotation.is_file() for annotation, _ in split_specs.values()):
            import truststore
            truststore.inject_into_ssl()
            from huggingface_hub import snapshot_download

            allowed_patterns = [
                "UVH-26-Train/data/**", "UVH-26-Train/UVH-26-MV-Train.json",
                "UVH-26-Val/data/**", "UVH-26-Val/UVH-26-MV-Val.json",
            ]
            local_dir = source_root
            local_dir.mkdir(parents=True, exist_ok=True)
            # snapshot_download resumes its file cache and transfers; progress is
            # polled from the actual destination tree, not guessed from archive size.
            download_done = threading.Event()
            download_error: list[BaseException] = []

            def download_uvh() -> None:
                try:
                    snapshot_download(repo_id="iisc-aim/UVH-26", repo_type="dataset", local_dir=str(local_dir), allow_patterns=allowed_patterns, max_workers=4)
                except BaseException as exc:
                    download_error.append(exc)
                finally:
                    download_done.set()

            _start_context_preserving_thread(download_uvh, name="vaos-uvh26-download-worker")
            previous_bytes = 0
            previous_at = time.monotonic()
            while not download_done.wait(5):
                downloaded = sum(path.stat().st_size for split_root in (source_root / "UVH-26-Train", source_root / "UVH-26-Val") if split_root.exists() for path in split_root.rglob("*") if path.is_file() and ".cache" not in path.parts)
                now = time.monotonic()
                speed = max(0, int((downloaded - previous_bytes) / max(0.1, now - previous_at)))
                previous_bytes, previous_at = downloaded, now
                ml_dataset_import_state.update({"status": "downloading", "dataset": "uvh26", "phase": "download", "progress": min(99, int(downloaded * 100 / 90_000_000_000)), "downloaded_bytes": downloaded, "expected_bytes": 90_000_000_000, "speed_bytes_per_second": speed, "message": f"Downloading/resuming official UVH-26 · {downloaded / 1_000_000_000:.2f} GB saved · {speed / 1_000_000:.2f} MB/s. Interim COCO + reviewed + Kaggle training is independent."})
            if download_error:
                raise RuntimeError(f"UVH-26 download failed and can be retried/resumed: {download_error[0]}")
        ml_dataset_import_state.update({"status": "preparing", "dataset": "uvh26", "phase": "checking", "progress": 1, "message": "UVH-26 files downloaded. Checking train/validation image references and annotations."})
        if any(not annotation.is_file() for annotation, _ in split_specs.values()):
            raise RuntimeError("UVH-26 download finished without both majority-vote train and validation annotations.")
        if final_root.exists():
            imported = load_public_dataset_imports().get("uvh26")
            if imported and Path(str(imported.get("data_yaml", ""))).is_file():
                ml_dataset_import_state = {"status": "complete", "dataset": "uvh26", "message": "UVH-26 is already validated and ready; no source files were changed."}
                return
            raise RuntimeError(f"A previous incomplete derivative exists at {final_root}. Source files were left intact; move that derivative aside before retrying.")
        if staging.exists():
            raise RuntimeError(f"An incomplete preparation folder already exists at {staging}. It was left untouched; inspect it before retrying.")

        manifests: dict[str, dict[str, Any]] = {}
        classes: list[str] = []
        for split, (annotation_path, split_root) in split_specs.items():
            ml_dataset_import_state.update({"status": "preparing", "dataset": "uvh26", "phase": "validating", "progress": 5 if split == "train" else 40, "message": f"Validating UVH-26 {split} split: annotation records, image references, and bounding boxes."})
            if not (split_root / "data").is_dir():
                raise RuntimeError(f"UVH-26 {split} images are missing. Wait for the source download to finish, then retry preparation.")
            payload = json.loads(annotation_path.read_text(encoding="utf-8"))
            if not all(isinstance(payload.get(key), list) for key in ("images", "annotations", "categories")):
                raise RuntimeError(f"{annotation_path.name} is not a COCO detection annotation file.")
            categories = sorted(payload["categories"], key=lambda item: int(item["id"]))
            split_classes = [str(item["name"]).strip() for item in categories]
            if not split_classes or any(not name for name in split_classes):
                raise RuntimeError(f"{annotation_path.name} has missing category names.")
            if classes and split_classes != classes:
                raise RuntimeError("UVH-26 train and validation splits use different class lists; refusing to merge incompatible labels.")
            classes = split_classes
            category_ids = {int(item["id"]): index for index, item in enumerate(categories)}
            image_rows = payload["images"]
            if not image_rows:
                raise RuntimeError(f"{annotation_path.name} contains no image records.")
            image_paths: dict[str, Path] = {}
            duplicate_names: set[str] = set()
            for image_path in (split_root / "data").rglob("*"):
                if image_path.is_file() and image_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
                    key = image_path.name.casefold()
                    if key in image_paths:
                        duplicate_names.add(key)
                    else:
                        image_paths[key] = image_path
            missing = [str(row.get("file_name", "")) for row in image_rows if Path(str(row.get("file_name", ""))).name.casefold() not in image_paths]
            ambiguous = [str(row.get("file_name", "")) for row in image_rows if Path(str(row.get("file_name", ""))).name.casefold() in duplicate_names]
            if missing or ambiguous:
                examples = ", ".join((missing[:3] + ambiguous[:3]))
                raise RuntimeError(f"UVH-26 {split} validation failed: {len(missing):,} referenced images are missing and {len(ambiguous):,} names are ambiguous. Examples: {examples}")
            images_by_id: dict[int, dict[str, Any]] = {}
            excluded_image_ids: set[int] = set()
            excluded_image_names: list[str] = []
            for image_index, row in enumerate(image_rows, start=1):
                try:
                    image_id = int(row["id"])
                    width, height = int(row["width"]), int(row["height"])
                    filename = Path(str(row["file_name"])).name
                except (KeyError, TypeError, ValueError):
                    raise RuntimeError(f"UVH-26 {split} contains an image record with invalid id, dimensions, or filename.")
                if width <= 0 or height <= 0 or image_id in images_by_id:
                    raise RuntimeError(f"UVH-26 {split} contains duplicate image IDs or invalid image dimensions.")
                source_image = image_paths[filename.casefold()]
                if source_image.suffix.lower() == ".png":
                    with source_image.open("rb") as image_file:
                        header = image_file.read(24)
                    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
                        raise RuntimeError(f"UVH-26 image {filename} is not a complete PNG file.")
                    actual_width = int.from_bytes(header[16:20], "big")
                    actual_height = int.from_bytes(header[20:24], "big")
                    if (actual_width, actual_height) != (width, height):
                        excluded_image_ids.add(image_id)
                        excluded_image_names.append(filename)
                        continue
                elif source_image.stat().st_size == 0:
                    raise RuntimeError(f"UVH-26 image {filename} is empty.")
                images_by_id[image_id] = {"width": width, "height": height, "filename": filename, "source": source_image, "labels": []}
                if image_index % 2000 == 0:
                    split_progress = 5 + int(30 * image_index / len(image_rows)) if split == "train" else 40 + int(25 * image_index / len(image_rows))
                    ml_dataset_import_state.update({"progress": split_progress, "message": f"Validated {image_index:,}/{len(image_rows):,} UVH-26 {split} image records."})
            invalid_annotations = 0
            excluded_annotations = 0
            valid_annotations = 0
            for row in payload["annotations"]:
                try:
                    image_id = int(row["image_id"])
                    if image_id in excluded_image_ids:
                        excluded_annotations += 1
                        continue
                    image = images_by_id[image_id]
                    class_id = category_ids[int(row["category_id"])]
                    x, y, width, height = (float(value) for value in row["bbox"])
                    image_width, image_height = image["width"], image["height"]
                    if not all(np.isfinite(value) for value in (x, y, width, height)) or width <= 0 or height <= 0 or x < -1 or y < -1 or x + width > image_width + 1 or y + height > image_height + 1:
                        invalid_annotations += 1
                        continue
                    x = min(max(0.0, x), image_width)
                    y = min(max(0.0, y), image_height)
                    width = min(width, image_width - x)
                    height = min(height, image_height - y)
                    if width <= 0 or height <= 0:
                        invalid_annotations += 1
                        continue
                    normalized = ((x + width / 2) / image_width, (y + height / 2) / image_height, width / image_width, height / image_height)
                    image["labels"].append((class_id, normalized))
                    valid_annotations += 1
                except (KeyError, TypeError, ValueError, OverflowError):
                    invalid_annotations += 1
            if invalid_annotations:
                raise RuntimeError(f"UVH-26 {split} validation found {invalid_annotations:,} annotations with invalid image/category references or bounding boxes. No converted data was published.")
            if len(excluded_image_ids) > max(10, int(len(image_rows) * 0.01)):
                raise RuntimeError(f"UVH-26 {split} has {len(excluded_image_ids):,} image dimension mismatches, above the safe 1% limit. No converted data was published.")
            manifests[split] = {"images": images_by_id, "annotations": valid_annotations, "excluded_images": len(excluded_image_ids), "excluded_annotations": excluded_annotations, "excluded_image_names": excluded_image_names, "annotation_file": str(annotation_path)}

        if staging.exists():
            raise RuntimeError(f"Preparation target already exists: {staging}")
        for split_number, (split, manifest) in enumerate(manifests.items()):
            image_dir, label_dir = staging / "images" / split, staging / "labels" / split
            image_dir.mkdir(parents=True, exist_ok=False)
            staging_created = True
            label_dir.mkdir(parents=True, exist_ok=False)
            image_total = len(manifest["images"])
            ml_dataset_import_state.update({"status": "preparing", "dataset": "uvh26", "phase": "converting", "progress": 68 if split_number == 0 else 83, "message": f"Converting UVH-26 {split} images and COCO boxes into YOLO labels."})
            for image_index, image in enumerate(manifest["images"].values(), start=1):
                image_target = image_dir / image["filename"]
                try:
                    os.link(image["source"], image_target)
                except OSError as exc:
                    raise RuntimeError(f"Could not create a space-saving image link for {image['filename']}. Keep the dataset and VAOS derivative on the same filesystem or volume, and ensure hardlinks are permitted. {exc}") from exc
                lines = [f"{class_id} {cx:.8f} {cy:.8f} {width:.8f} {height:.8f}" for class_id, (cx, cy, width, height) in image["labels"]]
                (label_dir / f"{Path(image['filename']).stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="ascii")
                if image_index % 1000 == 0:
                    progress = 68 + int(14 * image_index / image_total) if split_number == 0 else 83 + int(14 * image_index / image_total)
                    ml_dataset_import_state.update({"progress": progress, "message": f"Converted {image_index:,}/{image_total:,} UVH-26 {split} images."})
        yaml_path = staging / "dataset.yaml"
        yaml_path.write_text("path: " + json.dumps(str(final_root.resolve())) + "\ntrain: images/train\nval: images/val\nnames:\n" + "".join(f"  {index}: {json.dumps(name, ensure_ascii=False)}\n" for index, name in enumerate(classes)), encoding="utf-8")
        # Cheap structural verification before making the prepared copy visible.
        for split, manifest in manifests.items():
            if len(list((staging / "images" / split).iterdir())) != len(manifest["images"]):
                raise RuntimeError(f"UVH-26 {split} conversion produced an unexpected image count.")
            if len(list((staging / "labels" / split).glob("*.txt"))) != len(manifest["images"]):
                raise RuntimeError(f"UVH-26 {split} conversion produced an unexpected label-file count.")
        staging.replace(final_root)
        yaml_path = final_root / "dataset.yaml"
        imported = load_public_dataset_imports()
        train_count = len(manifests["train"]["images"])
        val_count = len(manifests["val"]["images"])
        excluded_images = sum(manifest["excluded_images"] for manifest in manifests.values())
        excluded_annotations = sum(manifest["excluded_annotations"] for manifest in manifests.values())
        imported["uvh26"] = {"id": "uvh26", "name": "UVH-26 · Indian road vehicles", "data_yaml": str(yaml_path), "classes": classes, "train_count": train_count, "val_count": val_count, "train_annotations": manifests["train"]["annotations"], "val_annotations": manifests["val"]["annotations"], "excluded_dimension_mismatch_images": excluded_images, "excluded_annotations": excluded_annotations, "license": "CC BY 4.0", "imported_at": utc_now(), "source": "iisc-aim/UVH-26 · majority-vote annotations"}
        write_public_dataset_imports(imported)
        _record_observability_dataset_result(
            "uvh26", succeeded=True, duration_seconds=time.perf_counter() - preparation_started,
            image_count=max(0, train_count + val_count - excluded_images),
            train_image_count=max(0, train_count - manifests["train"]["excluded_images"]),
            validation_image_count=max(0, val_count - manifests["val"]["excluded_images"]),
            class_count=len(classes),
            annotation_count=sum(manifest["annotations"] for manifest in manifests.values()),
        )
        exclusion_note = f" · excluded {excluded_images} image(s) and {excluded_annotations:,} box(es) with inconsistent image dimensions" if excluded_images else ""
        ml_dataset_import_state = {"status": "complete", "dataset": "uvh26", "message": f"UVH-26 validated · {train_count:,} train and {val_count:,} validation images · {len(classes)} vehicle classes{exclusion_note} · CC BY 4.0."}
        write_training_log("INFO", "UVH-26 validated and prepared", train_images=train_count, validation_images=val_count, classes=len(classes), train_annotations=manifests["train"]["annotations"], validation_annotations=manifests["val"]["annotations"], excluded_dimension_mismatch_images=excluded_images, excluded_annotations=excluded_annotations)
    except Exception as exc:
        _record_observability_dataset_result("uvh26", succeeded=False, duration_seconds=time.perf_counter() - preparation_started)
        if staging_created:
            shutil.rmtree(staging, ignore_errors=True)
        ml_dataset_import_state = {"status": "error", "dataset": "uvh26", "message": str(exc)[:500]}
        logger.exception("VAOS UVH-26 preparation failed")
        write_training_log("ERROR", "UVH-26 preparation failed", error=str(exc)[:400])
    finally:
        if ml_dataset_import_lock.locked():
            ml_dataset_import_lock.release()


def prepare_kaggle_auto_dataset() -> None:
    """Validate the local Kaggle Pascal VOC sample and publish a YOLO derivative."""
    global ml_dataset_import_state
    preparation_started = time.perf_counter()
    source_root = default_datasets_dir() / "kaggle_auto"
    annotation_root = source_root / "Annotations" / "Annotations"
    source_images = source_root / "auto"
    staging = source_root / ".vaos-yolo-incomplete"
    final_root = source_root / "vaos-yolo"
    staging_created = False
    try:
        ml_dataset_import_state.update({"status": "preparing", "dataset": "kaggle_auto", "phase": "validating", "progress": 2, "message": "Validating Kaggle Auto Pascal VOC annotations and matching images."})
        if not annotation_root.is_dir() or not source_images.is_dir():
            raise RuntimeError(f"Expected local Kaggle Auto folders at {annotation_root} and {source_images}.")
        xml_files = sorted(annotation_root.glob("*.xml"), key=lambda item: (item.stem.casefold(), item.name.casefold()))
        images_by_name: dict[str, Path] = {}
        duplicates: set[str] = set()
        for image_path in source_images.iterdir():
            if image_path.is_file() and image_path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                key = image_path.name.casefold()
                if key in images_by_name:
                    duplicates.add(key)
                else:
                    images_by_name[key] = image_path
        if not xml_files or not images_by_name:
            raise RuntimeError("Kaggle Auto has no images or Pascal VOC XML annotations.")
        if len(xml_files) != len(images_by_name):
            raise RuntimeError(f"Kaggle Auto has {len(images_by_name)} images but {len(xml_files)} XML annotations; every image needs exactly one annotation.")
        if duplicates:
            raise RuntimeError("Kaggle Auto has duplicate image filenames, so annotation-to-image matching is ambiguous.")
        if final_root.exists():
            imported = load_public_dataset_imports().get("kaggle_auto")
            if imported and Path(str(imported.get("data_yaml", ""))).is_file():
                ml_dataset_import_state = {"status": "complete", "dataset": "kaggle_auto", "message": "Kaggle Auto is already validated and ready; source files were not changed."}
                return
            raise RuntimeError(f"A previous incomplete Kaggle Auto derivative exists at {final_root}; source files were left intact.")
        if staging.exists():
            raise RuntimeError(f"An incomplete Kaggle Auto preparation folder already exists at {staging}; inspect it before retrying.")

        rows: list[tuple[Path, str, list[tuple[float, float, float, float]]]] = []
        for index, xml_path in enumerate(xml_files, start=1):
            try:
                annotation = ET.parse(xml_path).getroot()
                filename = Path((annotation.findtext("filename") or "").strip()).name
                image_path = images_by_name.get(filename.casefold())
                if not filename or image_path is None:
                    raise RuntimeError(f"Kaggle Auto annotation {xml_path.name} references a missing image.")
                size = annotation.find("size")
                if size is None:
                    raise RuntimeError(f"Kaggle Auto annotation {xml_path.name} is missing image dimensions.")
                width, height = int(size.findtext("width")), int(size.findtext("height"))
            except RuntimeError:
                raise
            except (ET.ParseError, AttributeError, TypeError, ValueError) as exc:
                raise RuntimeError(f"Kaggle Auto annotation {xml_path.name} is malformed: {exc}") from exc
            if width <= 0 or height <= 0:
                raise RuntimeError(f"Kaggle Auto annotation {xml_path.name} has invalid image dimensions.")
            decoded = cv2.imread(str(image_path))
            if decoded is None or decoded.shape[1] != width or decoded.shape[0] != height:
                raise RuntimeError(f"Kaggle Auto image dimensions do not match {xml_path.name}.")
            del decoded
            boxes: list[tuple[float, float, float, float]] = []
            for obj in annotation.findall("object"):
                label = " ".join((obj.findtext("name") or "").strip().casefold().replace("_", " ").split())
                if label not in {"auto rickshaw", "autorickshaw", "auto", "three wheeler", "three-wheeler"}:
                    raise RuntimeError(f"Unexpected Kaggle Auto class '{label}' in {xml_path.name}; this prepared source is intended to contain autorickshaws only.")
                box = obj.find("bndbox")
                try:
                    x1, y1, x2, y2 = (float(box.findtext(key)) for key in ("xmin", "ymin", "xmax", "ymax"))
                except (AttributeError, TypeError, ValueError) as exc:
                    raise RuntimeError(f"Kaggle Auto annotation {xml_path.name} has an invalid bounding box.") from exc
                if not all(np.isfinite(value) for value in (x1, y1, x2, y2)):
                    raise RuntimeError(f"Kaggle Auto annotation {xml_path.name} has a non-finite bounding box.")
                x1, x2 = min(max(x1, 0.0), float(width)), min(max(x2, 0.0), float(width))
                y1, y2 = min(max(y1, 0.0), float(height)), min(max(y2, 0.0), float(height))
                if x2 <= x1 or y2 <= y1:
                    raise RuntimeError(f"Kaggle Auto annotation {xml_path.name} has an empty or reversed bounding box.")
                boxes.append(((x1 + x2) / (2 * width), (y1 + y2) / (2 * height), (x2 - x1) / width, (y2 - y1) / height))
            if not boxes:
                raise RuntimeError(f"Kaggle Auto annotation {xml_path.name} has no object boxes.")
            rows.append((image_path, xml_path.stem, boxes))
            if index % 10 == 0:
                ml_dataset_import_state.update({"progress": 5 + int(35 * index / len(xml_files)), "message": f"Validated {index:,}/{len(xml_files):,} Kaggle Auto images and boxes."})

        (staging / "images" / "train").mkdir(parents=True, exist_ok=False)
        staging_created = True
        (staging / "images" / "val").mkdir(parents=True, exist_ok=False)
        (staging / "labels" / "train").mkdir(parents=True, exist_ok=False)
        (staging / "labels" / "val").mkdir(parents=True, exist_ok=False)
        ml_dataset_import_state.update({"progress": 45, "phase": "converting", "message": "Creating a space-efficient YOLO copy of Kaggle Auto labels."})
        for index, (image_path, stem, boxes) in enumerate(rows):
            split = "val" if (index + 1) % 5 == 0 else "train"
            target_image = staging / "images" / split / image_path.name
            try:
                os.link(image_path, target_image)
            except OSError as exc:
                raise RuntimeError(f"Could not create a space-saving hardlink for {image_path.name}; keep the dataset and derivative on the same filesystem or volume, and ensure hardlinks are permitted. {exc}") from exc
            target_label = staging / "labels" / split / f"{image_path.stem}.txt"
            target_label.write_text("".join(f"0 {cx:.8f} {cy:.8f} {bw:.8f} {bh:.8f}\n" for cx, cy, bw, bh in boxes), encoding="ascii")
            if index % 10 == 0:
                ml_dataset_import_state.update({"progress": 45 + int(50 * (index + 1) / len(rows)), "message": f"Prepared {index + 1:,}/{len(rows):,} Kaggle Auto images."})
        yaml_path = staging / "dataset.yaml"
        yaml_path.write_text("path: " + json.dumps(str(final_root.resolve())) + "\ntrain: images/train\nval: images/val\nnames:\n  0: auto rickshaw\n", encoding="utf-8")
        for split in ("train", "val"):
            if not any((staging / "images" / split).iterdir()) or not any((staging / "labels" / split).iterdir()):
                raise RuntimeError(f"Kaggle Auto {split} split is empty after conversion.")
        staging.replace(final_root)
        imported = load_public_dataset_imports()
        imported["kaggle_auto"] = {"id": "kaggle_auto", "name": "Kaggle Auto · autorickshaws", "data_yaml": str(final_root / "dataset.yaml"), "classes": ["auto rickshaw"], "train_count": len(rows) - len(rows) // 5, "val_count": len(rows) // 5, "annotations": sum(len(row[2]) for row in rows), "imported_at": utc_now(), "source": "Datacluster Labs · local Pascal VOC annotations"}
        write_public_dataset_imports(imported)
        _record_observability_dataset_result(
            "kaggle_auto", succeeded=True, duration_seconds=time.perf_counter() - preparation_started,
            image_count=len(rows), train_image_count=len(rows) - len(rows) // 5,
            validation_image_count=len(rows) // 5, class_count=1,
            annotation_count=sum(len(row[2]) for row in rows),
        )
        ml_dataset_import_state = {"status": "complete", "dataset": "kaggle_auto", "message": f"Kaggle Auto validated · {len(rows):,} images · {sum(len(row[2]) for row in rows):,} autorickshaw boxes · 1 class."}
        write_training_log("INFO", "Kaggle Auto dataset prepared", images=len(rows), annotations=sum(len(row[2]) for row in rows), classes=1)
    except Exception as exc:
        _record_observability_dataset_result("kaggle_auto", succeeded=False, duration_seconds=time.perf_counter() - preparation_started)
        if staging_created:
            shutil.rmtree(staging, ignore_errors=True)
        ml_dataset_import_state = {"status": "error", "dataset": "kaggle_auto", "message": str(exc)[:500]}
        logger.exception("VAOS Kaggle Auto preparation failed")
        write_training_log("ERROR", "Kaggle Auto preparation failed", error=str(exc)[:400])
    finally:
        if ml_dataset_import_lock.locked():
            ml_dataset_import_lock.release()


def build_coco_dataset_yaml() -> tuple[Path, list[str]]:
    import yaml
    import ultralytics

    base = Path(ultralytics.__file__).resolve().parent / "cfg" / "datasets"
    root = ML_DATASETS_DIR / "coco2017"
    dataset_config = yaml.safe_load((base / "coco.yaml").read_text(encoding="utf-8"))
    classes = [str(name) for _, name in sorted(dataset_config["names"].items())]
    root.mkdir(parents=True, exist_ok=True)
    dataset_config["path"] = str(root.resolve())
    yaml_path = root / "dataset.yaml"
    yaml_path.write_text(yaml.safe_dump(dataset_config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return yaml_path, classes


def coco_dataset_layout_ready() -> bool:
    root = ML_DATASETS_DIR / "coco2017"
    required = (
        root / "dataset.yaml", root / "train2017.txt", root / "val2017.txt",
        root / "images" / "train2017", root / "images" / "val2017",
        root / "labels" / "train2017", root / "labels" / "val2017",
    )
    return all(path.exists() for path in required)


def repair_coco_dataset_layout() -> None:
    """Index existing COCO images and reuse extracted label files in the expected layout."""
    root = ML_DATASETS_DIR / "coco2017"
    source_labels = ML_DATASETS_DIR / "coco" / "labels"
    for split in ("train2017", "val2017"):
        images_dir = root / "images" / split
        labels_source = source_labels / split
        if not images_dir.is_dir() or not labels_source.is_dir():
            raise RuntimeError(f"Existing COCO {split} images or labels are missing. The import is incomplete; retry COCO preparation to download only missing files.")
        image_files = sorted(images_dir.glob("*.jpg"))
        label_files = list(labels_source.glob("*.txt"))
        if not image_files or not label_files:
            raise RuntimeError(f"Existing COCO {split} folder is empty. Retry the dataset import to fetch missing files.")
        labels_target = root / "labels" / split
        labels_target.mkdir(parents=True, exist_ok=True)
        for index, label_path in enumerate(label_files, 1):
            target = labels_target / label_path.name
            if not target.exists():
                try:
                    os.link(label_path, target)
                except OSError:
                    shutil.copy2(label_path, target)
            if index % 5000 == 0:
                ml_dataset_import_state.update({"status": "preparing", "phase": "repairing", "progress": min(99, int(index * 100 / len(label_files))), "message": f"Reusing existing COCO {split} labels · {index:,} of {len(label_files):,}. No image download is running."})
        listing = root / f"{split}.txt"
        temporary = listing.with_suffix(".txt.new")
        temporary.write_text("".join(f"./images/{split}/{image.name}\n" for image in image_files), encoding="utf-8")
        temporary.replace(listing)


def import_coco2017_dataset() -> None:
    global ml_dataset_import_state
    preparation_started = time.perf_counter()
    monitor_stop = threading.Event()
    monitor_thread: threading.Thread | None = None
    try:
        dataset_id = "coco2017"
        ml_dataset_import_state = {"status": "preparing", "dataset": dataset_id, "message": "Preparing COCO 2017 configuration."}
        yaml_path, classes = build_coco_dataset_yaml()
        can_repair_without_download = all(
            (ML_DATASETS_DIR / "coco2017" / "images" / split).is_dir()
            and (ML_DATASETS_DIR / "coco" / "labels" / split).is_dir()
            for split in ("train2017", "val2017")
        )
        if can_repair_without_download:
            ml_dataset_import_state = {"status": "preparing", "phase": "repairing", "dataset": dataset_id, "progress": 0, "downloaded_bytes": 0, "expected_bytes": 0, "speed_bytes_per_second": 0, "message": "Reusing COCO images and labels already saved on this PC. No image download is running."}
            repair_coco_dataset_layout()
        layout_ready = coco_dataset_layout_ready()
        ml_dataset_import_state = {"status": "preparing", "phase": "validating" if layout_ready else "connecting", "dataset": dataset_id, "progress": 0, "downloaded_bytes": 0, "expected_bytes": 0 if layout_ready else 20_300_000_000, "speed_bytes_per_second": 0, "message": "Validating COCO files already saved on this PC. No image download is running." if layout_ready else "Connecting to COCO 2017 download sources…"}
        expected_bytes = 20_300_000_000
        archive_names = ("train2017.zip", "val2017.zip", "coco2017labels-segments.zip", "coco2017labels.zip")

        def monitor_download() -> None:
            last_bytes = 0
            last_sample_bytes = 0
            last_sample_time = time.monotonic()
            started_at = last_sample_time
            idle_seconds = 0.0
            while not monitor_stop.wait(1.0):
                downloaded = 0
                partial_names: list[str] = []
                try:
                    scan_dirs = (ML_DATASETS_DIR, ML_DATASETS_DIR / "coco2017" / "images")
                    for scan_dir in scan_dirs:
                        if not scan_dir.is_dir():
                            continue
                        for file_path in scan_dir.iterdir():
                            file_name = file_path.name
                            if file_path.is_file() and (file_name in archive_names or (file_name.endswith(".part") and any(name in file_name for name in archive_names))):
                                try:
                                    downloaded += file_path.stat().st_size
                                    if file_name.endswith(".part"):
                                        partial_names.append(file_name)
                                except OSError:
                                    pass
                except OSError:
                    pass
                now = time.monotonic()
                # Count completed archives and active .part files. A completed archive
                # can be replaced during retry, so use the current on-disk total.
                last_bytes = downloaded
                speed = max(0, int((downloaded - last_sample_bytes) / max(0.1, now - last_sample_time)))
                idle_seconds = idle_seconds + max(0.0, now - last_sample_time) if speed == 0 else 0.0
                last_sample_bytes, last_sample_time = downloaded, now
                percent = min(99, int(downloaded * 100 / expected_bytes))
                extracting = percent >= 97
                elapsed = now - started_at
                active_archive = ""
                if partial_names:
                    # Ultralytics writes active transfers as .<archive>.<id>.part.
                    active_archive = next((archive for archive in archive_names for name in partial_names if archive in name), partial_names[0])
                if extracting:
                    phase, message = "extracting", "Download received · unpacking and validating COCO files."
                elif active_archive and idle_seconds >= 45:
                    phase, message = "stalled", f"No new bytes received for {int(idle_seconds)} seconds from {active_archive}. The transfer may be waiting on the network."
                elif downloaded:
                    phase, message = "downloading", "Downloading COCO 2017 archives."
                elif elapsed < 15:
                    phase, message = "connecting", "Connecting to COCO 2017 download sources…"
                else:
                    phase, message = "waiting", "Waiting for the first download bytes. Check internet access if this message remains."
                ml_dataset_import_state.update({"status": "downloading", "phase": phase, "progress": percent, "downloaded_bytes": downloaded, "expected_bytes": expected_bytes, "speed_bytes_per_second": speed, "active_archive": active_archive, "message": message})

        if not layout_ready:
            ml_dataset_import_state["status"] = "downloading"
            monitor_thread = _start_context_preserving_thread(monitor_download, name="vaos-coco-progress")
        from ultralytics.data.utils import check_det_dataset
        check_det_dataset(str(yaml_path))
        repair_coco_dataset_layout()
        monitor_stop.set()
        if monitor_thread:
            monitor_thread.join(timeout=2)
        imports = load_public_dataset_imports()
        imports[dataset_id] = {"id": dataset_id, "name": "COCO 2017", "data_yaml": str(yaml_path), "classes": classes, "train_limit": 118287, "imported_at": utc_now(), "size_note": "about 20.3 GB; 118,287 training and 5,000 validation images"}
        write_public_dataset_imports(imports)
        _record_observability_dataset_result(
            "coco2017", succeeded=True, duration_seconds=time.perf_counter() - preparation_started,
            image_count=123287, train_image_count=118287, validation_image_count=5000, class_count=len(classes),
        )
        ml_dataset_import_state = {"status": "complete", "dataset": dataset_id, "message": f"{imports[dataset_id]['name']} is ready for training.", "classes": classes}
    except Exception as exc:
        _record_observability_dataset_result("coco2017", succeeded=False, duration_seconds=time.perf_counter() - preparation_started)
        monitor_stop.set()
        if monitor_thread:
            monitor_thread.join(timeout=2)
        logger.exception("VAOS public dataset import failed for %s", dataset_id)
        ml_dataset_import_state = {"status": "error", "dataset": dataset_id, "message": str(exc)[:500]}
    finally:
        ml_dataset_import_lock.release()


@app.get("/api/admin/ml/samples")
def list_ml_samples(label: str = "", limit: int = 250, offset: int = 0, _: str = Depends(require_admin)):
    limit = max(1, min(500, int(limit)))
    offset = max(0, int(offset))
    label = label.strip()
    label_filter = " AND lower(COALESCE(s.confirmed_label,s.suggested_label))=lower(?)" if label else ""
    label_params = (label,) if label else ()
    with db_connection() as db:
        rows = db.execute("SELECT s.id,s.created_at,s.suggested_label,s.bbox,s.confidence,s.review_state,s.confirmed_label,s.source_time_seconds,COALESCE(s.confirmed_label,s.suggested_label) AS review_label,c.name AS camera_name FROM ml_samples s JOIN cameras c ON c.token=s.camera_token WHERE s.review_state IN ('pending','later')" + label_filter + " ORDER BY COALESCE(s.confirmed_label,s.suggested_label) COLLATE NOCASE,s.id DESC LIMIT ? OFFSET ?", (*label_params, limit, offset)).fetchall()
        totals = db.execute("SELECT review_state,COUNT(*) AS n FROM ml_samples GROUP BY review_state").fetchall()
        reviewable_class_counts = db.execute("SELECT COALESCE(confirmed_label,suggested_label) AS label,COUNT(*) AS n FROM ml_samples WHERE review_state IN ('pending','later') AND COALESCE(confirmed_label,suggested_label) IS NOT NULL GROUP BY COALESCE(confirmed_label,suggested_label) ORDER BY label COLLATE NOCASE").fetchall()
        filtered_reviewable_total = int(db.execute("SELECT COUNT(*) FROM ml_samples s WHERE s.review_state IN ('pending','later')" + label_filter, label_params).fetchone()[0])
        collection_stats = db.execute("SELECT COUNT(*) AS total,MAX(created_at) AS latest FROM ml_samples").fetchone()
        setting = db.execute("SELECT value FROM app_settings WHERE key='ml_capture_enabled'").fetchone()
        confirmed_labels = db.execute("SELECT DISTINCT COALESCE(confirmed_label,suggested_label) AS label FROM ml_samples WHERE review_state IN ('confirmed','corrected')").fetchall()
        confirmed_class_counts = db.execute("SELECT COALESCE(confirmed_label,suggested_label) AS label,COUNT(*) AS n FROM ml_samples WHERE review_state IN ('confirmed','corrected') GROUP BY COALESCE(confirmed_label,suggested_label)").fetchall()
        recent_camera_stats = db.execute("SELECT objects_json FROM camera_status WHERE last_frame_at >= ? AND error=''", ((datetime.now(timezone.utc)-timedelta(seconds=45)).isoformat(timespec="seconds"),)).fetchall()
        live_camera_count = len(recent_camera_stats)
        live_object_types = sorted({label for row in recent_camera_stats for label, values in json.loads(row["objects_json"] or "{}").items() if values and int(values[0]) > 0})
    internal_token = os.getenv("VAOS_INTERNAL_STATUS_TOKEN", "")
    live_model: dict[str, Any] = {"available": False, "loaded": False, "custom_model_active": False, "message": "Camera worker status is unavailable until the updated launcher is restarted."}
    if internal_token:
        request = urllib.request.Request(f"{CAMERA_INTERNAL_URL}/internal/model-status", headers={"X-VAOS-Internal-Token": internal_token})
        try:
            with urllib.request.urlopen(request, timeout=1.0) as response:
                worker = json.loads(response.read().decode("utf-8"))
            object_model = next((item for item in worker.get("models", []) if item.get("name") == "yolo26m"), {})
            live_model = {"available": True, "loaded": bool(worker.get("object_model_loaded")), "custom_model_active": bool(worker.get("custom_model_active")), "path": object_model.get("path", ""), "classes": object_model.get("classes", []), "device": worker.get("device", ""), "error": object_model.get("error")}
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            live_model = {"available": False, "loaded": False, "custom_model_active": False, "message": "Camera worker is not responding. Start a live camera, then refresh this page."}
    trained_path = ML_DATA_DIR / "active" / "best.pt"
    metadata_path = ML_DATA_DIR / "active" / "metadata.json"
    try:
        training_info = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        training_info = {}
    class_counts = {str(row["label"]): int(row["n"]) for row in confirmed_class_counts}
    reviewed_total = sum(class_counts.values())
    class_total = len(class_counts)
    class_balance = min(class_counts.values()) / max(class_counts.values()) if class_counts else 0.0
    volume_score = min(1.0, reviewed_total / 500) * 35
    coverage_score = min(1.0, class_total / 8) * 25
    balance_score = min(1.0, class_balance / 0.25) * 20 if class_counts else 0.0
    model_installed = trained_path.is_file()
    model_score = 10.0 if model_installed else 0.0
    model_metrics = training_info.get("metrics", {}) if isinstance(training_info.get("metrics", {}), dict) else {}
    map_score_value = next((float(value) for key, value in model_metrics.items() if "map50-95" in str(key).lower() and isinstance(value, (int, float))), None)
    validation_score = min(1.0, max(0.0, map_score_value / 0.5)) * 10 if map_score_value is not None else 0.0
    maturity_score = round(min(100.0, volume_score + coverage_score + balance_score + model_score + validation_score))
    if reviewed_total < 20 or class_total < 2:
        maturity_level = "Foundation"
    elif reviewed_total < 100 or class_total < 3:
        maturity_level = "Developing"
    elif not model_installed:
        maturity_level = "Training-ready"
    elif map_score_value is None:
        maturity_level = "Trained · no validation metric"
    else:
        maturity_level = "Validation measured"
    maturity = {"score": maturity_score, "level": maturity_level, "reviewed_samples": reviewed_total, "class_count": class_total, "class_balance": round(class_balance, 3), "map50_95": map_score_value, "components": {"sample_volume": round(volume_score), "class_coverage": round(coverage_score), "class_balance": round(balance_score), "trained_model": round(model_score), "validation": round(validation_score)}}
    training_state = dict(ml_training_state)
    launcher_progress = read_launcher_training_progress() if training_state.get("status") == "running" else None
    if launcher_progress:
        current_batch = int(training_state.get("batch") or 0)
        current_total = int(training_state.get("batches") or 0)
        if launcher_progress["batch"] >= current_batch and launcher_progress["batches"] >= current_total:
            training_state.update(launcher_progress)
            training_state["progress_source"] = "launcher_output"
    reviewable_total = sum(int(row["n"]) for row in totals if row["review_state"] in {"pending", "later"})
    return {"samples": [dict(row) for row in rows], "counts": {row["review_state"]: row["n"] for row in totals}, "reviewable_total": reviewable_total, "filtered_reviewable_total": filtered_reviewable_total, "review_offset": offset, "review_limit": limit, "reviewable_class_counts": [{"label": row["label"], "count": int(row["n"])} for row in reviewable_class_counts], "confirmed_class_counts": [{"label": row["label"], "count": int(row["n"])} for row in confirmed_class_counts], "confirmed_classes": [row["label"] for row in confirmed_labels], "training_maturity": maturity, "capture_enabled": bool(setting and setting["value"] == "1"), "sample_total": int(collection_stats["total"] or 0), "sample_latest_at": collection_stats["latest"], "sample_limit": MAX_ML_SAMPLES, "training_epoch_options": list(TRAINING_EPOCH_OPTIONS), "supports_custom_training_epochs": True, "live_camera_count": live_camera_count, "live_object_types": live_object_types, "training": training_state, "training_logs": read_training_log(), "dataset_imports": list(load_public_dataset_imports().values()), "dataset_catalog": local_dataset_catalog(), "dataset_import": dict(ml_dataset_import_state), "dataset_prepare": dict(ml_dataset_prepare_state), "coco_dataset_ready": coco_dataset_layout_ready(), "training_devices": training_device_inventory(), "trained_model": {"installed": model_installed, "path": str(trained_path) if model_installed else "", "metadata": training_info}, "live_model": live_model}


@app.get("/api/admin/ml/logs")
def list_ml_training_logs(_: str = Depends(require_admin)):
    return {"entries": read_training_log(), "training": dict(ml_training_state), "updated_at": utc_now()}



@app.get("/api/admin/ml/history")
def list_ml_training_history(run_id: str | None = None, _: str = Depends(require_admin)):
    """Read epoch metrics from completed Ultralytics results.csv files."""
    runs_dir = ML_DATA_DIR / "runs"
    runs: list[dict[str, Any]] = []
    if runs_dir.is_dir():
        for result_path in runs_dir.glob("*/results.csv"):
            try:
                if result_path.is_symlink() or result_path.stat().st_size > 5 * 1024 * 1024:
                    continue
                run_path = result_path.parent
                if run_path.resolve().parent != runs_dir.resolve():
                    continue
                with result_path.open("r", encoding="utf-8-sig", newline="") as source:
                    rows = list(csv.DictReader(source))
                if not rows:
                    continue
                newest_epoch = max(int(float(row.get("epoch") or 0)) for row in rows)
                run_metadata: dict[str, Any] = {}
                run_metadata_path = run_path / "vaos_run.json"
                if run_metadata_path.is_file() and run_metadata_path.stat().st_size <= 128 * 1024:
                    try:
                        saved_metadata = json.loads(run_metadata_path.read_text(encoding="utf-8"))
                        if isinstance(saved_metadata, dict):
                            run_metadata = saved_metadata
                    except (OSError, json.JSONDecodeError):
                        logger.debug("Could not load metadata for training run %s", run_path.name, exc_info=True)
                runs.append({"id": run_path.name, "epochs": len(rows), "last_epoch": newest_epoch, "updated_at": datetime.fromtimestamp(result_path.stat().st_mtime, timezone.utc).isoformat(), "dataset_name": str(run_metadata.get("dataset_name", "")), "dataset_source": str(run_metadata.get("dataset_source", "")), "initial_model": str(run_metadata.get("initial_model", "")), "samples": run_metadata.get("samples")})
            except (OSError, UnicodeDecodeError, ValueError, csv.Error):
                logger.debug("Skipping unreadable training history file %s", result_path, exc_info=True)
    runs.sort(key=lambda item: item["updated_at"], reverse=True)
    selected = next((item for item in runs if item["id"] == run_id), None) if run_id else (runs[0] if runs else None)
    metric_columns = {
        "box_loss": ("train/box_loss", "Box loss"),
        "class_loss": ("train/cls_loss", "Class loss"),
        "l1_loss": ("train/l1_loss", "L1 regression loss"),
        "dfl_loss": ("train/dfl_loss", "DFL loss"),
        "precision": ("metrics/precision(B)", "Precision"),
        "recall": ("metrics/recall(B)", "Recall"),
        "map50": ("metrics/mAP50(B)", "mAP50"),
        "map50_95": ("metrics/mAP50-95(B)", "mAP50–95"),
        "learning_rate": ("lr/pg0", "Learning rate"),
    }
    metrics: dict[str, dict[str, Any]] = {
        key: {"label": label, "points": []} for key, (_, label) in metric_columns.items()
    }
    if selected:
        results_path = runs_dir / selected["id"] / "results.csv"
        try:
            with results_path.open("r", encoding="utf-8-sig", newline="") as source:
                for row in csv.DictReader(source):
                    try:
                        epoch = int(float(row.get("epoch") or 0))
                    except (TypeError, ValueError):
                        continue
                    if epoch < 1:
                        continue
                    for key, (column, _) in metric_columns.items():
                        raw_value = row.get(column)
                        if raw_value in (None, ""):
                            continue
                        try:
                            value = float(raw_value)
                        except (TypeError, ValueError):
                            continue
                        if value == value and abs(value) != float("inf"):
                            metrics[key]["points"].append({"epoch": epoch, "value": value})
        except (OSError, UnicodeDecodeError, csv.Error):
            logger.exception("Could not load metrics for training run %s", selected["id"])
    # Older YOLO runs in this build record L1 regression instead of DFL.
    # Surface that series in the DFL slot so the UI never appears blank.
    actual_dfl_available = bool(metrics["dfl_loss"]["points"])
    if not actual_dfl_available and metrics["l1_loss"]["points"]:
        metrics["dfl_loss"]["points"] = list(metrics["l1_loss"]["points"])
    return {
        "runs": runs,
        "selected_run": selected["id"] if selected else None,
        "metrics": metrics,
        "dfl_available": actual_dfl_available,
        "regression_note": "This run records L1 regression loss; the DFL chart is showing that same regression series as a compatibility view." if metrics["l1_loss"]["points"] and not actual_dfl_available else "",
    }

@app.post("/api/admin/ml/datasets/import")
def start_public_dataset_import(payload: dict[str, Any], _: str = Depends(require_admin)):
    dataset_id = str(payload.get("dataset", ""))
    if dataset_id == "uvh26":
        imported = load_public_dataset_imports().get("uvh26")
        if imported and Path(str(imported.get("data_yaml", ""))).is_file():
            ml_dataset_import_state.update({"status": "complete", "dataset": "uvh26", "message": "UVH-26 is already validated and ready; no source files were changed."})
            return {"status": "complete", "message": "UVH-26 is already validated and ready."}
        if not ml_dataset_import_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A dataset preparation job is already running.")
        ml_dataset_import_state.update({"status": "downloading", "dataset": "uvh26", "message": "Resuming the official UVH-26 files to the configured dataset folder. This download can run while a COCO + reviewed + Kaggle Auto training job runs."})
        try:
            _start_context_preserving_thread(prepare_uvh26_dataset, name="vaos-uvh26-prepare")
        except Exception as exc:
            ml_dataset_import_lock.release()
            ml_dataset_import_state.update({"status": "error", "dataset": "uvh26", "message": f"Could not start UVH-26 preparation: {exc}"})
            logger.exception("Could not launch UVH-26 preparation")
            raise HTTPException(status_code=503, detail="VAOS could not start UVH-26 preparation. Check the admin service error log.") from exc
        return {"status": "preparing", "message": "UVH-26 validation and conversion started. Source annotations and images will remain unchanged."}
    if dataset_id == "kaggle_auto":
        imported = load_public_dataset_imports().get("kaggle_auto")
        if imported and Path(str(imported.get("data_yaml", ""))).is_file():
            ml_dataset_import_state.update({"status": "complete", "dataset": "kaggle_auto", "message": "Kaggle Auto is already validated and ready; no source files were changed."})
            return {"status": "complete", "message": "Kaggle Auto is already validated and ready."}
        if ml_training_lock.locked():
            raise HTTPException(status_code=409, detail="Wait for the active model training job to finish before preparing Kaggle Auto.")
        if not ml_dataset_import_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A dataset preparation job is already running.")
        ml_dataset_import_state.update({"status": "preparing", "dataset": "kaggle_auto", "phase": "checking", "progress": 1, "message": "Checking local Kaggle Auto images and Pascal VOC annotations."})
        try:
            _start_context_preserving_thread(prepare_kaggle_auto_dataset, name="vaos-kaggle-auto-prepare")
        except Exception as exc:
            ml_dataset_import_lock.release()
            ml_dataset_import_state.update({"status": "error", "dataset": "kaggle_auto", "message": f"Could not start Kaggle Auto preparation: {exc}"})
            logger.exception("Could not launch Kaggle Auto preparation")
            raise HTTPException(status_code=503, detail="VAOS could not start Kaggle Auto preparation. Check the admin service error log.") from exc
        return {"status": "preparing", "message": "Kaggle Auto validation and conversion started. Source images and annotations will remain unchanged."}
    if dataset_id != "coco2017":
        raise HTTPException(status_code=400, detail="Choose UVH-26, Kaggle Auto, or COCO 2017.")
    imported = load_public_dataset_imports().get("coco2017")
    if imported and coco_dataset_layout_ready():
        existing_yaml = Path(str(imported.get("data_yaml", "")))
        if existing_yaml.is_file():
            ml_dataset_import_state.update({"status": "complete", "dataset": "coco2017", "message": "COCO 2017 is already imported on this PC. No download is needed."})
            return {"status": "complete", "message": "COCO 2017 is already imported on this PC. No download is needed."}
    if not ml_dataset_import_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A public dataset import is already running")
    ml_dataset_import_state.update({"status": "preparing", "message": "Starting dataset preparation."})
    try:
        _start_context_preserving_thread(import_coco2017_dataset, name="vaos-coco2017-import")
    except Exception as exc:
        ml_dataset_import_lock.release()
        ml_dataset_import_state.update({"status": "error", "message": f"Could not start dataset import: {exc}"})
        logger.exception("Could not launch the COCO 2017 import worker")
        raise HTTPException(status_code=503, detail="VAOS could not start the dataset import worker. Check the admin service error log.") from exc
    return {"status": "preparing", "message": "Dataset import started."}


def build_combined_coco_reviewed_yaml(coco_yaml: Path, reviewed: list[tuple[int, bytes, str, str]], coco_classes: list[str]) -> tuple[Path, list[str]]:
    """Create a small mixed manifest while reusing the downloaded COCO images in place."""
    root = ML_DATASETS_DIR / "coco2017_combined"
    image_root = root / "reviewed" / "images"
    label_root = root / "reviewed" / "labels"
    (image_root / "train").mkdir(parents=True, exist_ok=True)
    (image_root / "val").mkdir(parents=True, exist_ok=True)
    (label_root / "train").mkdir(parents=True, exist_ok=True)
    (label_root / "val").mkdir(parents=True, exist_ok=True)
    class_names = list(coco_classes)
    class_ids = {name.casefold(): index for index, name in enumerate(class_names)}
    for _, _, label, _ in reviewed:
        if label.casefold() not in class_ids:
            class_ids[label.casefold()] = len(class_names)
            class_names.append(label)
    yaml_path = root / "dataset.yaml"
    coco_config = __import__("yaml").safe_load(coco_yaml.read_text(encoding="utf-8"))
    coco_root = Path(coco_config["path"])

    def resolve_coco_paths(list_name: str) -> list[str]:
        listing = coco_root / list_name
        if not listing.is_file():
            raise RuntimeError(f"COCO image list is missing: {listing}. Re-import COCO 2017.")
        result = []
        for raw in listing.read_text(encoding="utf-8").splitlines():
            entry = raw.strip()
            if not entry:
                continue
            candidate = Path(entry)
            if not candidate.is_absolute():
                candidate = coco_root / candidate
            if not candidate.is_file():
                raise RuntimeError(f"A COCO image listed in {listing.name} is missing: {entry}")
            result.append(str(candidate.resolve()))
        return result

    train_paths = resolve_coco_paths(str(coco_config["train"]))
    val_paths = resolve_coco_paths(str(coco_config["val"]))
    reviewed_train, reviewed_val = [], []
    for index, (sample_id, image_bytes, label, bbox) in enumerate(reviewed):
        split = "val" if index % 5 == 0 else "train"
        stem = f"sample_{sample_id}"
        image_path = image_root / split / f"{stem}.jpg"
        label_path = label_root / split / f"{stem}.txt"
        image_path.write_bytes(image_bytes)
        label_id = class_ids[label.casefold()]
        label_path.write_text(f"{label_id} {bbox.replace(',', ' ')}\n", encoding="ascii")
        (reviewed_val if split == "val" else reviewed_train).append(str(image_path.resolve()))
    (root / "train.txt").write_text("\n".join([*train_paths, *reviewed_train]) + "\n", encoding="utf-8")
    (root / "val.txt").write_text("\n".join([*val_paths, *reviewed_val]) + "\n", encoding="utf-8")
    names = "\n".join(f"  {index}: {json.dumps(name)}" for index, name in enumerate(class_names))
    yaml_path.write_text(f"path: {coco_root.as_posix()}\ntrain: {(root / 'train.txt').as_posix()}\nval: {(root / 'val.txt').as_posix()}\nnames:\n{names}\n", encoding="utf-8")
    return yaml_path, class_names


def build_combined_uvh_reviewed_yaml(uvh_yaml: Path, reviewed: list[tuple[int, bytes, str, str]], uvh_classes: list[str]) -> tuple[Path, list[str], int]:
    """Mix validated UVH-26 images with reviewed crops, preserving existing class IDs."""
    import yaml

    aliases = {"auto rickshaw": "Three-wheeler", "autorickshaw": "Three-wheeler", "rickshaw": "Three-wheeler", "cycle": "Bicycle", "motorcycle": "Two-wheeler", "motorbike": "Two-wheeler", "bike": "Two-wheeler", "mini bus": "Mini-bus", "tempo traveller": "Tempo-traveller"}
    def canonical(label: str) -> str:
        clean = " ".join(str(label).strip().split())
        return aliases.get(clean.casefold(), clean)

    try:
        prior_metadata = json.loads((ML_DATA_DIR / "active" / "metadata.json").read_text(encoding="utf-8"))
        prior_classes = [str(label) for label in prior_metadata.get("classes", []) if str(label).strip()]
    except (OSError, json.JSONDecodeError, TypeError):
        prior_classes = []
    class_names = list(prior_classes)
    known = {label.casefold(): index for index, label in enumerate(class_names)}
    for name in [*uvh_classes, *(canonical(item[2]) for item in reviewed)]:
        if name.casefold() not in known:
            known[name.casefold()] = len(class_names)
            class_names.append(name)
    mapping = {index: known[name.casefold()] for index, name in enumerate(uvh_classes)}
    identity = hashlib.sha256(json.dumps([class_names, [(row[0], row[2], row[3]) for row in reviewed]], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]
    root = uvh_yaml.parent.parent / f"vaos-uvh26-reviewed-{identity}"
    staging = root.with_name(root.name + ".incomplete")
    if root.exists() and (root / "dataset.yaml").is_file():
        return root / "dataset.yaml", class_names, sum(1 for split in ("train", "val") for _ in (root / "images" / split).iterdir())
    if staging.exists():
        raise RuntimeError(f"An incomplete combined UVH-26 dataset exists at {staging}; it was left untouched for inspection.")
    base_root = uvh_yaml.parent
    for split in ("train", "val"):
        image_dir, label_dir = staging / "images" / split, staging / "labels" / split
        image_dir.mkdir(parents=True, exist_ok=False)
        label_dir.mkdir(parents=True, exist_ok=False)
        source_images = base_root / "images" / split
        source_labels = base_root / "labels" / split
        if not source_images.is_dir() or not source_labels.is_dir():
            raise RuntimeError(f"Validated UVH-26 {split} images or labels are missing. Prepare UVH-26 again before training.")
        for image in source_images.iterdir():
            if not image.is_file():
                continue
            os.link(image, image_dir / image.name)
            source_label = source_labels / f"{image.stem}.txt"
            remapped: list[str] = []
            if source_label.is_file():
                for line in source_label.read_text(encoding="ascii").splitlines():
                    fields = line.split()
                    if not fields:
                        continue
                    old_id = int(fields[0])
                    if old_id not in mapping:
                        raise RuntimeError(f"UVH-26 label contains unknown class ID {old_id} in {source_label.name}.")
                    remapped.append(" ".join([str(mapping[old_id]), *fields[1:]]))
            (label_dir / f"{image.stem}.txt").write_text("\n".join(remapped) + ("\n" if remapped else ""), encoding="ascii")

    reviewed_images = 0
    for index, (sample_id, image_bytes, raw_label, bbox) in enumerate(reviewed):
        split = "val" if index % 5 == 0 else "train"
        stem = f"review_sample_{sample_id}"
        fields = [part for part in re.split(r"[\s,]+", str(bbox).strip()) if part]
        try:
            cx, cy, width, height = (float(value) for value in fields)
        except (TypeError, ValueError):
            raise RuntimeError(f"Reviewed sample {sample_id} has an invalid bounding box; it was excluded from combined training.")
        if len(fields) != 4 or not all(np.isfinite(value) for value in (cx, cy, width, height)) or width <= 0 or height <= 0 or width > 1 or height > 1 or cx - width / 2 < -0.01 or cy - height / 2 < -0.01 or cx + width / 2 > 1.01 or cy + height / 2 > 1.01:
            raise RuntimeError(f"Reviewed sample {sample_id} has an invalid normalized bounding box; it was excluded from combined training.")
        class_id = known[canonical(raw_label).casefold()]
        (staging / "images" / split / f"{stem}.jpg").write_bytes(image_bytes)
        (staging / "labels" / split / f"{stem}.txt").write_text(f"{class_id} {cx:.8f} {cy:.8f} {width:.8f} {height:.8f}\n", encoding="ascii")
        reviewed_images += 1
    yaml_path = staging / "dataset.yaml"
    yaml_path.write_text("path: " + json.dumps(str(root.resolve())) + "\ntrain: images/train\nval: images/val\nnames:\n" + "".join(f"  {index}: {json.dumps(name, ensure_ascii=False)}\n" for index, name in enumerate(class_names)), encoding="utf-8")
    if not any((staging / "images" / "train").iterdir()) or not any((staging / "images" / "val").iterdir()):
        raise RuntimeError("The combined UVH-26 dataset needs non-empty train and validation image splits.")
    staging.replace(root)
    return root / "dataset.yaml", class_names, reviewed_images


def build_combined_coco_uvh_kaggle_reviewed_yaml(
    coco_yaml: Path,
    uvh_yaml: Path | None,
    kaggle_yaml: Path,
    reviewed: list[tuple[int, bytes, str, str]],
    coco_classes: list[str],
    uvh_classes: list[str],
    kaggle_classes: list[str],
    expected_images: int,
) -> tuple[Path, list[str], int]:
    """Build one detector dataset from COCO, UVH-26, Kaggle Auto and reviewed crops.

    Source images stay in their original locations. Directory junctions expose
    each validated source under a unique images/ prefix, while only remapped
    YOLO text labels are written into the combined dataset.
    """
    import yaml

    aliases = {
        "auto": "auto rickshaw", "auto rickshaw": "auto rickshaw", "auto-rickshaw": "auto rickshaw",
        "autorickshaw": "auto rickshaw", "rickshaw": "auto rickshaw", "three wheeler": "auto rickshaw",
        "three-wheeler": "auto rickshaw", "three_wheeler": "auto rickshaw", "cycle": "bicycle",
        "two wheeler": "motorcycle", "two-wheeler": "motorcycle", "motorbike": "motorcycle",
        "bike": "motorcycle", "mini bus": "bus", "mini-bus": "bus", "tempo traveller": "van",
        "tempo-traveller": "van", "hatchback": "car", "sedan": "car", "suv": "car", "muv": "car",
        "lcv": "light commercial vehicle", "other": "other vehicle", "mobile phone": "cell phone",
    }

    def canonical(value: str) -> str:
        clean = " ".join(str(value).strip().casefold().replace("_", " ").split())
        return aliases.get(clean, clean)

    source_specs = [("coco2017", coco_yaml, coco_classes)]
    if uvh_yaml is not None:
        source_specs.append(("uvh26", uvh_yaml, uvh_classes))
    source_specs.append(("kaggle_auto", kaggle_yaml, kaggle_classes))
    try:
        prior_metadata = json.loads((ML_DATA_DIR / "active" / "metadata.json").read_text(encoding="utf-8"))
        prior_classes = [str(value) for value in prior_metadata.get("classes", []) if str(value).strip()]
    except (OSError, json.JSONDecodeError, TypeError):
        prior_classes = []

    class_names: list[str] = []
    class_ids: dict[str, int] = {}

    def add_class(label: str) -> int:
        name = canonical(label)
        if not name:
            raise RuntimeError("A training source has an empty class name.")
        if name not in class_ids:
            class_ids[name] = len(class_names)
            class_names.append(name)
        return class_ids[name]

    for label in [*prior_classes, *coco_classes, *uvh_classes, *kaggle_classes, *(row[2] for row in reviewed)]:
        add_class(label)

    def split_images(dataset_yaml: Path, split: str) -> tuple[Path, list[Path]]:
        config = yaml.safe_load(dataset_yaml.read_text(encoding="utf-8")) or {}
        root_value = config.get("path") or str(dataset_yaml.parent)
        dataset_root = Path(str(root_value))
        if not dataset_root.is_absolute():
            dataset_root = dataset_yaml.parent / dataset_root
        images_root = dataset_root / "images"
        spec = config.get(split)
        if isinstance(spec, list):
            entries = spec
        elif isinstance(spec, str):
            target = Path(spec)
            if not target.is_absolute():
                target = dataset_root / target
            if target.is_dir():
                paths = sorted(path for path in target.rglob("*") if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"})
                if not paths:
                    raise RuntimeError(f"The {split} image folder in {dataset_yaml} is empty.")
                return images_root, paths
            if target.is_file() and target.suffix.lower() == ".txt":
                entries = target.read_text(encoding="utf-8").splitlines()
            elif target.is_file():
                entries = [str(target)]
            else:
                raise RuntimeError(f"The {split} image source is missing: {target}")
        else:
            raise RuntimeError(f"The {split} source is not configured in {dataset_yaml}.")
        paths: list[Path] = []
        for entry in entries:
            value = str(entry).strip()
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute():
                path = dataset_root / path
            if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
                raise RuntimeError(f"A {split} image listed by {dataset_yaml.name} is missing or unsupported: {value}")
            paths.append(path)
        if not paths:
            raise RuntimeError(f"The {split} image list in {dataset_yaml} is empty.")
        return images_root, paths

    import_root = default_datasets_dir()
    identity_data = {
        # The COCO YAML is rewritten on every prepare request. Its mtime is
        # therefore not a dataset version and caused a new 120k-image merge
        # on every retry. Hash the stable configuration content instead.
        "sources": [(key, str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest()) for key, path, _ in source_specs],
        "reviewed": [(row[0], canonical(row[2]), row[3]) for row in reviewed],
        "classes": class_names,
    }
    identity = hashlib.sha256(json.dumps(identity_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:14]
    source_name = "coco-uvh26-kaggle" if uvh_yaml is not None else "coco-kaggle"
    root = import_root / f"{source_name}-reviewed-{identity}"
    staging = root.with_name(root.name + ".incomplete")
    if root.exists() and (root / "dataset.yaml").is_file():
        # Ultralytics resolves paths in a text manifest relative to the
        # manifest only when each relative entry starts with "./". Without
        # that prefix it keeps e.g. "images/coco2017/..." relative, and on
        # Windows img2label_paths cannot replace the leading "images" with
        # "labels". It then caches zero labels beside the images.
        for split in ("train", "val"):
            manifest = root / f"{split}.txt"
            if not manifest.is_file():
                raise RuntimeError(f"The existing combined dataset is missing its {split} manifest: {manifest}")
            entries = [line.strip() for line in manifest.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            normalized = [entry if Path(entry).is_absolute() or entry.startswith("./") else f"./{entry.lstrip('./')}" for entry in entries]
            if normalized != entries:
                temporary_manifest = manifest.with_suffix(".txt.new")
                temporary_manifest.write_text("\n".join(normalized) + "\n", encoding="utf-8")
                temporary_manifest.replace(manifest)
        return root / "dataset.yaml", class_names, sum(1 for split in ("train", "val") for _ in (root / f"{split}.txt").read_text(encoding="utf-8").splitlines() if _.strip())
    if root.exists():
        raise RuntimeError(f"An incomplete combined dataset exists at {root}; it was left untouched for inspection.")
    if staging.exists():
        raise RuntimeError(f"An incomplete combined dataset exists at {staging}; it was left untouched for inspection.")

    staging.mkdir(parents=True, exist_ok=False)
    image_base = staging / "images"
    label_base = staging / "labels"
    image_base.mkdir()
    label_base.mkdir()
    split_entries: dict[str, list[str]] = {"train": [], "val": []}
    total_images = 0
    processed_images = 0

    def create_junction(link_path: Path, target_path: Path) -> None:
        if not target_path.is_dir():
            raise RuntimeError(f"A validated dataset image folder is missing: {target_path}")
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            escaped_link = str(link_path).replace("'", "''")
            escaped_target = str(target_path).replace("'", "''")
            script = f"New-Item -ItemType Junction -Path '{escaped_link}' -Target '{escaped_target}' -ErrorAction Stop | Out-Null"
            result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=30)
            if result.returncode or not link_path.is_dir():
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(f"Could not link the source images into the combined training set without copying them: {detail or link_path}")
        else:
            try:
                link_path.symlink_to(target_path.resolve(), target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                raise RuntimeError("Could not create a directory link for the combined dataset. Check that the destination filesystem permits symbolic links.") from exc

    try:
        for source_id, dataset_yaml, source_classes in source_specs:
            config = yaml.safe_load(dataset_yaml.read_text(encoding="utf-8")) or {}
            source_names = config.get("names", source_classes)
            if isinstance(source_names, dict):
                source_names = [str(name) for _, name in sorted(source_names.items(), key=lambda item: int(item[0]))]
            else:
                source_names = [str(name) for name in source_names]
            if source_names != source_classes:
                raise RuntimeError(f"The saved class map for {source_id} no longer matches its validated dataset record.")
            source_image_root = Path(str(config.get("path") or dataset_yaml.parent))
            if not source_image_root.is_absolute():
                source_image_root = dataset_yaml.parent / source_image_root
            source_image_root = source_image_root / "images"
            if not source_image_root.is_dir():
                raise RuntimeError(f"The validated image root for {source_id} is missing: {source_image_root}")
            create_junction(image_base / source_id, source_image_root)
            mapped_ids = {index: add_class(name) for index, name in enumerate(source_classes)}
            for split in ("train", "val"):
                _, image_paths = split_images(dataset_yaml, split)
                target_labels = label_base / source_id
                for image_path in image_paths:
                    relative = image_path.relative_to(source_image_root)
                    source_label = image_path.with_suffix(".txt")
                    image_parts = list(image_path.parts)
                    image_segment = max((i for i, part in enumerate(image_parts) if part.casefold() == "images"), default=-1)
                    if image_segment < 0:
                        raise RuntimeError(f"Image path does not contain an images/ folder: {image_path}")
                    label_parts = image_parts[:]
                    label_parts[image_segment] = "labels"
                    source_label = Path(*label_parts).with_suffix(".txt")
                    relative_label = relative.with_suffix(".txt")
                    target_label = target_labels / relative_label
                    target_label.parent.mkdir(parents=True, exist_ok=True)
                    remapped_lines: list[str] = []
                    if source_label.is_file():
                        for line_number, raw_line in enumerate(source_label.read_text(encoding="ascii").splitlines(), start=1):
                            fields = raw_line.split()
                            if not fields:
                                continue
                            try:
                                class_id = int(fields[0])
                                values = [float(value) for value in fields[1:]]
                            except ValueError as exc:
                                raise RuntimeError(f"Non-numeric YOLO label at {source_label}:{line_number}.") from exc
                            if class_id not in mapped_ids or not all(np.isfinite(value) for value in values):
                                raise RuntimeError(f"Invalid class ID or coordinate at {source_label}:{line_number}.")
                            if len(values) == 4:
                                # Standard YOLO detection boxes are cx, cy, width, height.
                                cx, cy, width, height = values
                                if width <= 0 or height <= 0:
                                    raise RuntimeError(f"Empty normalized box at {source_label}:{line_number}.")
                            elif len(values) >= 6 and len(values) % 2 == 0:
                                # COCO's existing YOLO export stores polygon points. Convert
                                # each polygon to its enclosing detection rectangle.
                                if any(value < -0.01 or value > 1.01 for value in values):
                                    raise RuntimeError(f"Out-of-range polygon coordinate at {source_label}:{line_number}.")
                                points = np.asarray(values, dtype=np.float64).reshape(-1, 2)
                                left, top = points.min(axis=0)
                                right, bottom = points.max(axis=0)
                                width, height = float(right - left), float(bottom - top)
                                cx, cy = float((left + right) / 2), float((top + bottom) / 2)
                                if width <= 0 or height <= 0:
                                    raise RuntimeError(f"Degenerate polygon at {source_label}:{line_number}.")
                            else:
                                raise RuntimeError(f"Malformed detection box or polygon at {source_label}:{line_number}.")
                            values = [cx, cy, width, height]
                            if any(value < -0.01 or value > 1.01 for value in values):
                                raise RuntimeError(f"Out-of-range normalized box at {source_label}:{line_number}.")
                            values = [min(1.0, max(0.0, value)) for value in values]
                            remapped_lines.append(" ".join([str(mapped_ids[class_id]), *(f"{value:.8f}" for value in values)]))
                    target_label.write_text("\n".join(remapped_lines) + ("\n" if remapped_lines else ""), encoding="ascii")
                    split_entries[split].append("./" + (Path("images") / source_id / relative).as_posix())
                    processed_images += 1
                    if processed_images % 1000 == 0:
                        progress = min(88, 5 + int(83 * processed_images / max(1, expected_images)))
                        ml_training_state.update({"status": "preparing", "progress": progress, "message": f"Merging source labels · {processed_images:,} / about {expected_images:,} images · {source_id} {split}."})
                total_images += len(image_paths)

        reviewed_train: list[str] = []
        reviewed_val: list[str] = []
        for index, (sample_id, image_bytes, raw_label, bbox) in enumerate(reviewed):
            split = "val" if (index + 1) % 5 == 0 else "train"
            fields = [part for part in re.split(r"[\s,]+", str(bbox).strip()) if part]
            try:
                left, top, width, height = (float(value) for value in fields)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Reviewed sample {sample_id} has an invalid bounding box.") from exc
            if len(fields) != 4 or not all(np.isfinite(value) for value in (left, top, width, height)) or left < -0.01 or top < -0.01 or width <= 0 or height <= 0 or width > 1 or height > 1 or left + width > 1.01 or top + height > 1.01:
                raise RuntimeError(f"Reviewed sample {sample_id} has an invalid normalized bounding box.")
            cx, cy = left + width / 2, top + height / 2
            class_id = add_class(raw_label)
            stem = f"sample_{sample_id}"
            (image_base / "reviewed" / split).mkdir(parents=True, exist_ok=True)
            (label_base / "reviewed" / split).mkdir(parents=True, exist_ok=True)
            (image_base / "reviewed" / split / f"{stem}.jpg").write_bytes(image_bytes)
            (label_base / "reviewed" / split / f"{stem}.txt").write_text(f"{class_id} {cx:.8f} {cy:.8f} {width:.8f} {height:.8f}\n", encoding="ascii")
            (reviewed_val if split == "val" else reviewed_train).append("./" + (Path("images") / "reviewed" / split / f"{stem}.jpg").as_posix())
        split_entries["train"].extend(reviewed_train)
        split_entries["val"].extend(reviewed_val)
        if not split_entries["train"] or not split_entries["val"]:
            raise RuntimeError("The combined training set needs non-empty train and validation image lists.")
        for split, entries in split_entries.items():
            (staging / f"{split}.txt").write_text("\n".join(entries) + "\n", encoding="utf-8")
        yaml_path = staging / "dataset.yaml"
        names_yaml = "\n".join(f"  {index}: {json.dumps(name, ensure_ascii=False)}" for index, name in enumerate(class_names))
        yaml_path.write_text(f"path: {json.dumps(str(root.resolve()))}\ntrain: train.txt\nval: val.txt\nnames:\n{names_yaml}\n", encoding="utf-8")
        source_names = ["COCO 2017"] + (["UVH-26 majority-vote"] if uvh_yaml is not None else []) + ["Kaggle Auto Pascal VOC", "Human-reviewed camera samples"]
        (staging / "sources.json").write_text(json.dumps({"sources": source_names, "source_images": total_images, "reviewed_samples": len(reviewed), "classes": class_names, "class_aliases": aliases}, indent=2, ensure_ascii=False), encoding="utf-8")
        staging.replace(root)
    except Exception:
        # shutil.rmtree is junction-safe on supported Windows Python versions;
        # it removes the link itself and leaves source dataset folders untouched.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return root / "dataset.yaml", class_names, total_images + len(reviewed)


def count_training_class_instances(data_yaml: Path, class_names: list[str]) -> tuple[dict[str, int], int]:
    """Count labeled objects across the exact train/validation manifests."""
    import yaml

    config = yaml.safe_load(data_yaml.read_text(encoding="utf-8")) or {}
    dataset_root = Path(str(config.get("path") or data_yaml.parent))
    counts = {name: 0 for name in class_names}
    invalid_ids = 0

    def split_images(spec: Any):
        if isinstance(spec, list):
            entries = spec
        elif isinstance(spec, str):
            target = Path(spec)
            if not target.is_absolute():
                target = dataset_root / target
            if target.is_dir():
                yield from (path for path in target.rglob("*") if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"})
                return
            if target.is_file() and target.suffix.lower() == ".txt":
                entries = target.read_text(encoding="utf-8").splitlines()
            elif target.is_file():
                entries = [str(target)]
            else:
                return
        else:
            return
        for entry in entries:
            image_path = Path(str(entry).strip())
            if not str(entry).strip():
                continue
            if not image_path.is_absolute():
                image_path = dataset_root / image_path
            if image_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
                yield image_path

    for split in ("train", "val"):
        for image_path in split_images(config.get(split)):
            parts = list(image_path.parts)
            image_parts = [index for index, part in enumerate(parts) if part.casefold() == "images"]
            if image_parts:
                parts[image_parts[-1]] = "labels"
                label_path = Path(*parts).with_suffix(".txt")
            else:
                label_path = image_path.with_suffix(".txt")
            try:
                with label_path.open("r", encoding="utf-8") as labels_file:
                    for line in labels_file:
                        fields = line.split()
                        if not fields:
                            continue
                        class_id = int(fields[0])
                        if 0 <= class_id < len(class_names):
                            counts[class_names[class_id]] += 1
                        else:
                            invalid_ids += 1
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError, ValueError):
                invalid_ids += 1
    return counts, invalid_ids


@app.post("/api/admin/ml/capture")
def set_ml_capture(payload: dict[str, Any], _: str = Depends(require_admin)):
    enabled = bool(payload.get("enabled"))
    with db_connection() as db:
        db.execute("INSERT INTO app_settings(key,value) VALUES('ml_capture_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if enabled else "0",))
    return {"enabled": enabled}


@app.delete("/api/admin/ml/samples")
def clear_ml_samples(_: str = Depends(require_admin)):
    if ml_training_lock.locked():
        raise HTTPException(status_code=409, detail="Wait for model training to finish before clearing samples")
    with db_connection() as db:
        db.execute("DELETE FROM ml_samples")
    last_ml_capture.clear()
    return {"ok": True}


@app.get("/api/admin/ml/samples/{sample_id}/image")
def ml_sample_image(sample_id: int, _: str = Depends(require_admin)):
    with db_connection() as db:
        row = db.execute("SELECT image FROM ml_samples WHERE id=?", (sample_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Review sample not found")
    return Response(content=row["image"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/admin/ml/samples/{sample_id}/review")
def review_ml_sample(sample_id: int, payload: dict[str, Any], _: str = Depends(require_admin)):
    choice = str(payload.get("choice", ""))
    label = str(payload.get("label", "")).strip().lower()
    state_by_choice = {"correct": "confirmed", "incorrect": "corrected", "dont_know": "unknown", "later": "later"}
    state_by_choice["reject"] = "rejected"
    if choice not in state_by_choice:
        raise HTTPException(status_code=400, detail="Choose correct, incorrect, don't know, or save for later")
    if choice == "incorrect" and (not label or len(label) > 48 or not all(ch.isalnum() or ch in " _-" for ch in label)):
        raise HTTPException(status_code=400, detail="For an incorrect label, enter a short corrected object name")
    with db_connection() as db:
        cur = db.execute("UPDATE ml_samples SET review_state=?,confirmed_label=? WHERE id=?", (state_by_choice[choice], (label if choice == "incorrect" else None), sample_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Review sample not found")
    return {"ok": True}


@app.post("/api/admin/ml/samples/batch-review")
def batch_review_ml_samples(payload: dict[str, Any], _: str = Depends(require_admin)):
    choice = str(payload.get("choice", ""))
    state_by_choice = {"approve": "confirmed", "reject": "rejected", "change_label": "corrected"}
    label = str(payload.get("label", "")).strip().lower()
    sample_ids = payload.get("ids")
    if choice not in state_by_choice or not isinstance(sample_ids, list) or not sample_ids or len(sample_ids) > 500:
        raise HTTPException(status_code=400, detail="Choose approve, reject, or change_label and provide up to 500 sample IDs")
    if choice == "change_label" and (not label or len(label) > 48 or not all(ch.isalnum() or ch in " _-" for ch in label)):
        raise HTTPException(status_code=400, detail="Enter a short corrected object name using letters, numbers, spaces, underscores, or hyphens")
    try:
        ids = sorted({int(sample_id) for sample_id in sample_ids})
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Sample IDs must be whole numbers")
    placeholders = ",".join("?" for _ in ids)
    with db_connection() as db:
        cur = db.execute(f"UPDATE ml_samples SET review_state=?,confirmed_label=? WHERE id IN ({placeholders}) AND review_state IN ('pending','later')", [state_by_choice[choice], label if choice == "change_label" else None, *ids])
    return {"ok": True, "updated": cur.rowcount, "label": label if choice == "change_label" else None}


def run_ml_training(device_choice: str, dataset_source: str, initial_model: str = "auto", resource_mode: bool = False, requested_epochs: int | None = None) -> None:
    global ml_training_state
    job_started = time.monotonic()
    run_name = f"vaos_objects_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
    ml_training_state.update({"run_id": run_name})
    write_training_log("INFO", "Training worker started", run_id=run_name, source=dataset_source, requested_device=device_choice)
    try:
        default_epochs = 20 if dataset_source.startswith("dataset:") or dataset_source in {"uvh26_combined", "combined_vehicle", "combined_kaggle"} else 10 if dataset_source in {"coco2017", "combined"} else 30
        epochs_total = int(requested_epochs) if requested_epochs is not None else default_epochs
        if dataset_source.startswith("dataset:"):
            dataset_id = dataset_source.split(":", 1)[1]
            imported = load_public_dataset_imports().get(dataset_id)
            if not imported or imported.get("managed_local_dataset") is not True or not Path(str(imported.get("data_yaml", ""))).is_file():
                raise RuntimeError("The selected local dataset is no longer registered or its YAML file is missing. Revalidate it on Dataset preparation.")
            yaml_path = Path(str(imported["data_yaml"]))
            labels = list(imported.get("classes", []))
            sample_count = int(imported.get("image_count", int(imported.get("train_count", 0)) + int(imported.get("val_count", 0))))
            dataset_name = str(imported.get("name", "Validated local dataset"))
        elif dataset_source == "coco2017":
            imported = load_public_dataset_imports().get("coco2017")
            if not imported or not Path(imported.get("data_yaml", "")).is_file():
                raise RuntimeError("Import COCO 2017 before selecting it for training.")
            yaml_path = Path(imported["data_yaml"])
            labels = list(imported.get("classes", []))
            sample_count = 118287
            dataset_name = "COCO 2017"
        elif dataset_source == "combined":
            imported = load_public_dataset_imports().get("coco2017")
            if not imported or not Path(imported.get("data_yaml", "")).is_file():
                raise RuntimeError("Import COCO 2017 before choosing combined training.")
            with db_connection() as db:
                rows = db.execute("SELECT id,image,bbox,suggested_label,confirmed_label FROM ml_samples WHERE review_state IN ('confirmed','corrected') ORDER BY id").fetchall()
            labeled = [(row["id"], bytes(row["image"]), row["confirmed_label"] or row["suggested_label"], row["bbox"]) for row in rows]
            reviewed_labels = {item[2] for item in labeled}
            if len(labeled) < 20 or len(reviewed_labels) < 2:
                raise RuntimeError("Combined training needs at least 20 confirmed crops across at least 2 object types.")
            base_yaml, coco_classes = build_coco_dataset_yaml()
            yaml_path, labels = build_combined_coco_reviewed_yaml(base_yaml, labeled, coco_classes)
            sample_count = 118287 + len(labeled)
            dataset_name = "COCO 2017 + human-reviewed samples"
        elif dataset_source == "uvh26_combined":
            imported = load_public_dataset_imports().get("uvh26")
            if not imported or not Path(imported.get("data_yaml", "")).is_file():
                raise RuntimeError("Prepare and validate the local UVH-26 dataset before choosing UVH-26 + reviewed samples.")
            with db_connection() as db:
                rows = db.execute("SELECT id,image,bbox,suggested_label,confirmed_label FROM ml_samples WHERE review_state IN ('confirmed','corrected') ORDER BY id").fetchall()
            labeled = [(row["id"], bytes(row["image"]), row["confirmed_label"] or row["suggested_label"], row["bbox"]) for row in rows]
            reviewed_labels = {item[2] for item in labeled}
            if len(labeled) < 20 or len(reviewed_labels) < 2:
                raise RuntimeError("UVH-26 + reviewed training needs at least 20 confirmed samples across at least 2 object types.")
            yaml_path, labels, reviewed_count = build_combined_uvh_reviewed_yaml(Path(imported["data_yaml"]), labeled, list(imported.get("classes", [])))
            sample_count = int(imported.get("train_count", 0)) + int(imported.get("val_count", 0)) + reviewed_count
            dataset_name = "UVH-26 Indian road vehicles + human-reviewed samples"
        elif dataset_source == "combined_vehicle":
            registry = load_public_dataset_imports()
            coco_import, uvh_import, kaggle_import = (registry.get(key) for key in ("coco2017", "uvh26", "kaggle_auto"))
            if not coco_import or not coco_dataset_layout_ready() or not Path(str(coco_import.get("data_yaml", ""))).is_file():
                raise RuntimeError("Prepare COCO 2017 before selecting the combined Indian vehicle training source.")
            if not uvh_import or not Path(str(uvh_import.get("data_yaml", ""))).is_file():
                raise RuntimeError("Download, validate, and prepare both UVH-26 splits before selecting the combined Indian vehicle training source.")
            if not kaggle_import or not Path(str(kaggle_import.get("data_yaml", ""))).is_file():
                raise RuntimeError("Validate and prepare the local Kaggle Auto dataset before selecting the combined Indian vehicle training source.")
            with db_connection() as db:
                rows = db.execute("SELECT id,image,bbox,suggested_label,confirmed_label FROM ml_samples WHERE review_state IN ('confirmed','corrected') ORDER BY id").fetchall()
            labeled = [(row["id"], bytes(row["image"]), row["confirmed_label"] or row["suggested_label"], row["bbox"]) for row in rows]
            reviewed_labels = {str(item[2]).strip().casefold() for item in labeled if str(item[2]).strip()}
            if len(labeled) < 20 or len(reviewed_labels) < 2:
                raise RuntimeError("The combined source needs at least 20 confirmed samples across at least 2 object types.")
            yaml_path, labels, source_image_count = build_combined_coco_uvh_kaggle_reviewed_yaml(
                Path(coco_import["data_yaml"]),
                Path(uvh_import["data_yaml"]),
                Path(kaggle_import["data_yaml"]),
                labeled,
                list(coco_import.get("classes", [])),
                list(uvh_import.get("classes", [])),
                list(kaggle_import.get("classes", [])),
                int(coco_import.get("train_limit", 118287)) + 5000 + int(uvh_import.get("train_count", 0)) + int(uvh_import.get("val_count", 0)) + int(kaggle_import.get("train_count", 0)) + int(kaggle_import.get("val_count", 0)) + len(labeled),
            )
            sample_count = source_image_count
            dataset_name = "COCO 2017 + UVH-26 + Kaggle Auto + human-reviewed samples"
        elif dataset_source == "combined_kaggle":
            registry = load_public_dataset_imports()
            coco_import, kaggle_import = registry.get("coco2017"), registry.get("kaggle_auto")
            if not coco_import or not coco_dataset_layout_ready() or not Path(str(coco_import.get("data_yaml", ""))).is_file():
                raise RuntimeError("Prepare COCO 2017 before selecting COCO + Kaggle Auto training.")
            if not kaggle_import or not Path(str(kaggle_import.get("data_yaml", ""))).is_file():
                raise RuntimeError("Validate and prepare the local Kaggle Auto dataset before selecting COCO + Kaggle Auto training.")
            with db_connection() as db:
                rows = db.execute("SELECT id,image,bbox,suggested_label,confirmed_label FROM ml_samples WHERE review_state IN ('confirmed','corrected') ORDER BY id").fetchall()
            labeled = [(row["id"], bytes(row["image"]), row["confirmed_label"] or row["suggested_label"], row["bbox"]) for row in rows]
            reviewed_labels = {str(item[2]).strip().casefold() for item in labeled if str(item[2]).strip()}
            if len(labeled) < 20 or len(reviewed_labels) < 2:
                raise RuntimeError("The interim combined source needs at least 20 confirmed samples across at least 2 object types.")
            base_yaml, coco_classes = build_coco_dataset_yaml()
            yaml_path, labels, source_image_count = build_combined_coco_uvh_kaggle_reviewed_yaml(
                Path(coco_import["data_yaml"]), None, Path(kaggle_import["data_yaml"]), labeled,
                list(coco_import.get("classes", coco_classes)), [], list(kaggle_import.get("classes", [])),
                int(coco_import.get("train_limit", 118287)) + 5000 + int(kaggle_import.get("train_count", 0)) + int(kaggle_import.get("val_count", 0)) + len(labeled),
            )
            sample_count = source_image_count
            dataset_name = "COCO 2017 + Kaggle Auto + human-reviewed samples (UVH-26 downloading separately)"
        elif dataset_source == "reviewed":
            with db_connection() as db:
                rows = db.execute("SELECT id,image,bbox,review_state,suggested_label,confirmed_label FROM ml_samples WHERE review_state IN ('confirmed','corrected') ORDER BY id").fetchall()
            labeled = [(row["id"], bytes(row["image"]), row["confirmed_label"] or row["suggested_label"], row["bbox"]) for row in rows]
            labels = sorted({item[2] for item in labeled})
            if len(labeled) < 20 or len(labels) < 2:
                raise RuntimeError("Training needs at least 20 confirmed samples across at least 2 object types.")
            job_dir = ML_DATA_DIR / "dataset"
            for split in ("train", "val"):
                (job_dir / "images" / split).mkdir(parents=True, exist_ok=True)
                (job_dir / "labels" / split).mkdir(parents=True, exist_ok=True)
            label_ids = {label: index for index, label in enumerate(labels)}
            for index, (sample_id, image_bytes, label, bbox) in enumerate(labeled):
                split = "val" if index % 5 == 0 else "train"
                stem = f"sample_{sample_id}"
                (job_dir / "images" / split / f"{stem}.jpg").write_bytes(image_bytes)
                (job_dir / "labels" / split / f"{stem}.txt").write_text(f"{label_ids[label]} {bbox.replace(',', ' ')}\n", encoding="ascii")
            names_yaml = "\n".join(f"  {index}: {json.dumps(label)}" for index, label in enumerate(labels))
            data_yaml = f"path: {job_dir.as_posix()}\ntrain: images/train\nval: images/val\nnames:\n{names_yaml}\n"
            yaml_path = job_dir / "data.yaml"
            yaml_path.write_text(data_yaml, encoding="utf-8")
            sample_count = len(labeled)
            dataset_name = "Human-reviewed camera samples"
        else:
            raise RuntimeError("Choose reviewed samples, prepared COCO 2017, UVH-26 + reviewed samples, or the combined COCO + UVH-26 + Kaggle Auto + reviewed source.")
        write_training_log("INFO", "Training data prepared", dataset=dataset_name, images=sample_count, classes=len(labels))
        if ml_training_state.get("stop_requested"):
            ml_training_state = {"status": "stopped", "progress": 0, "epoch": 0, "epochs": epochs_total, "stop_requested": False, "message": "Training was stopped safely before the first epoch; no new model was installed."}
            write_training_log("WARNING", "Training was stopped before the first epoch")
            return
        try:
            import torch
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError("Optional object-detection training packages are missing. Install a PyTorch build for this OS and accelerator, then install requirements-object-detection.txt and the supported Ultralytics version from the platform setup guide.") from exc
        custom_checkpoint = ML_DATA_DIR / "active" / "best.pt"
        base_checkpoint = MODEL_DIR / "yolo26m.pt"
        if initial_model == "yolo26m":
            model_path = base_checkpoint if base_checkpoint.is_file() else Path("yolo26m.pt")
        elif initial_model == "best.pt":
            if not custom_checkpoint.is_file():
                raise RuntimeError("Your custom best.pt is not available. Train a custom model first.")
            model_path = custom_checkpoint
        elif initial_model.startswith("source:"):
            source_name = Path(initial_model.split(":", 1)[1]).name
            model_path = (ML_MODEL_SOURCES_DIR / source_name).resolve()
            if model_path.parent != ML_MODEL_SOURCES_DIR.resolve() or not model_path.is_file() or model_path.suffix.lower() != ".pt":
                raise RuntimeError("The selected imported model is missing or invalid. Refresh model sources and try again.")
        else:
            model_path = custom_checkpoint if custom_checkpoint.is_file() else base_checkpoint if base_checkpoint.is_file() else Path("yolo26m.pt")
        model_file = str(model_path)
        process_threads = available_cpu_threads()
        training_target = resolve_training_device(torch, device_choice, process_threads, resource_mode=resource_mode)
        device_kind = device_choice.partition(":")[0]
        train_device = training_target["device"]
        device_label = training_target["label"]
        gpu_device_indices: list[int] = []
        if device_kind == "gpu":
            selected_devices = train_device if isinstance(train_device, (list, tuple)) else [train_device]
            try:
                gpu_device_indices = [int(index) for index in selected_devices]
            except (TypeError, ValueError):
                gpu_device_indices = [0]
        if training_target["cpu_threads"] is not None:
            torch.set_num_threads(int(training_target["cpu_threads"]))
        job_kind = "enrichment" if resource_mode else "training"
        ml_training_state = {"status": "running", "job_kind": job_kind, "source": dataset_source, "dataset_name": dataset_name, "dataset_id": dataset_source.split(":", 1)[1] if dataset_source.startswith("dataset:") else "", "data_yaml": str(yaml_path), "message": f"Training on {device_label} with {sample_count:,} {dataset_name} images and {len(labels)} object types.", "samples": sample_count, "classes": labels, "epoch": 0, "epochs": epochs_total, "progress": 0, "batch": 0, "batches": 0, "stats": {}, "elapsed_seconds": 0, "eta_seconds": None, "device": device_label}
        write_training_log("INFO", "Training dataset resolved", source=dataset_source, dataset_name=dataset_name, images=sample_count, classes=len(labels), epochs=epochs_total, device=device_label)
        model = YOLO(model_file)
        def scalar(value: Any) -> float | None:
            try:
                if hasattr(value, "detach"):
                    value = value.detach().float().mean().cpu().item()
                result_value = float(value)
                return round(result_value, 5) if np.isfinite(result_value) else None
            except (TypeError, ValueError, RuntimeError):
                return None

        def current_stats(trainer: Any) -> dict[str, Any]:
            metrics = getattr(trainer, "metrics", {}) or {}
            stats = {}
            metric_keys = {
                "precision": "metrics/precision(B)",
                "recall": "metrics/recall(B)",
                "map50": "metrics/mAP50(B)",
                "map50_95": "metrics/mAP50-95(B)",
            }
            for name, key in metric_keys.items():
                parsed = scalar(metrics.get(key))
                if parsed is not None:
                    stats[name] = parsed
            losses = getattr(trainer, "tloss", None)
            if losses is not None:
                try:
                    if hasattr(losses, "detach"):
                        losses = losses.detach().float().flatten().cpu().tolist()
                    elif not isinstance(losses, (list, tuple)):
                        losses = [losses]
                    for name, value in zip(("box_loss", "class_loss", "dfl_loss"), losses):
                        parsed = scalar(value)
                        if parsed is not None:
                            stats[name] = parsed
                except (TypeError, ValueError, RuntimeError):
                    pass
            learning_rates = getattr(trainer, "lr", {}) or {}
            if isinstance(learning_rates, dict) and learning_rates:
                parsed = scalar(max(learning_rates.values()))
                if parsed is not None:
                    stats["learning_rate"] = parsed
            if gpu_device_indices:
                try:
                    reserved_bytes = sum(torch.cuda.memory_reserved(index) for index in gpu_device_indices)
                    stats["gpu_memory_gb"] = round(reserved_bytes / (1024 ** 3), 2)
                except (RuntimeError, AttributeError):
                    pass
            try:
                import psutil
                stats["system_memory_gb"] = round(psutil.Process().memory_info().rss / (1024 ** 3), 2)
                stats["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
            except Exception:
                pass
            host_metrics = get_system_metrics()
            if host_metrics.get("gpu_utilization_percent") is not None:
                stats["gpu_utilization_percent"] = host_metrics["gpu_utilization_percent"]
            if host_metrics.get("gpu_memory_used_mb") is not None:
                stats["gpu_memory_used_mb"] = host_metrics["gpu_memory_used_mb"]
                stats["gpu_memory_total_mb"] = host_metrics.get("gpu_memory_total_mb")
            if host_metrics.get("memory_percent") is not None:
                stats["system_memory_percent"] = host_metrics["memory_percent"]
            return stats

        batch_speed_ema = 0.0
        image_speed_ema = 0.0
        last_batch_time: float | None = None
        # Seed the persistence clock before the first batch callback; keeping it
        # as None makes ``now - last_persisted_progress`` fail on batch one.
        last_persisted_progress = job_started

        def update_batch(trainer):
            nonlocal batch_speed_ema, image_speed_ema, last_batch_time, last_persisted_progress
            epoch_index = int(getattr(trainer, "epoch", 0))
            batch_index = int(getattr(trainer, "batch_i", -1)) + 1
            train_loader = getattr(trainer, "train_loader", None)
            total_batches = max(1, len(train_loader) if train_loader is not None else 1)
            fraction = min(1.0, max(0.0, batch_index / total_batches))
            elapsed = time.monotonic() - job_started
            epoch = min(epochs_total, epoch_index + 1)
            overall_progress = min(99, max(0, int(((epoch_index + fraction) / epochs_total) * 100)))
            completed_epochs = min(float(epochs_total), max(0.0, float(epoch_index)) + fraction)
            stats = current_stats(trainer)
            now = time.monotonic()
            sample = getattr(trainer, "batch", None)
            image_count = 0
            try:
                image_tensor = sample.get("img") if isinstance(sample, dict) else sample[0]
                image_count = int(image_tensor.shape[0])
            except (TypeError, IndexError, AttributeError, KeyError):
                image_count = int(getattr(getattr(trainer, "args", None), "batch", 1) or 1)
            delta = max(0.001, now - last_batch_time) if last_batch_time is not None else 0.0
            instant_batches = 1.0 / delta if delta else 0.0
            instant_images = image_count / delta if delta else 0.0
            if delta:
                batch_speed_ema = instant_batches if batch_speed_ema <= 0 else batch_speed_ema * 0.8 + instant_batches * 0.2
                image_speed_ema = instant_images if image_speed_ema <= 0 else image_speed_ema * 0.8 + instant_images * 0.2
            last_batch_time = now
            epoch_eta = int(max(0, total_batches - batch_index) / max(0.001, batch_speed_ema))
            stats.update({"batches_per_second": round(batch_speed_ema, 3), "images_per_second": round(image_speed_ema, 2), "processed_images": max(0, epoch_index) * total_batches * image_count + batch_index * image_count, "batch_size": image_count})
            ml_training_state.update({"status": "running", "epoch": epoch, "epochs": epochs_total, "batch": min(batch_index, total_batches), "batches": total_batches, "progress": overall_progress, "epoch_progress": round(fraction * 100, 2), "elapsed_seconds": int(elapsed), "epoch_eta_seconds": epoch_eta, "eta_seconds": estimate_training_eta(elapsed, completed_epochs, epochs_total), "stats": stats})
            if now - last_persisted_progress >= 10:
                write_training_log("INFO", "Training progress update", epoch=epoch, epochs=epochs_total, batch=batch_index, batches=total_batches, epoch_progress=round(fraction * 100, 2), progress=overall_progress, epoch_eta_seconds=epoch_eta, eta_seconds=ml_training_state.get("eta_seconds"), images_per_second=round(image_speed_ema, 2), batches_per_second=round(batch_speed_ema, 3), stats=stats, elapsed_seconds=int(elapsed))
                last_persisted_progress = now

        def update_epoch(trainer):
            epoch = min(epochs_total, int(getattr(trainer, "epoch", -1)) + 1)
            stats = current_stats(trainer)
            summaries = [f"{name} {value:.3f}" for name, value in (("mAP50", stats.get("map50")), ("precision", stats.get("precision")), ("recall", stats.get("recall"))) if value is not None]
            stop_requested = bool(ml_training_state.get("stop_requested"))
            elapsed = time.monotonic() - job_started
            ml_training_state.update({"status": "running", "epoch": epoch, "epochs": epochs_total, "batch": ml_training_state.get("batches", 0), "progress": int(epoch / epochs_total * 100), "elapsed_seconds": int(elapsed), "eta_seconds": estimate_training_eta(elapsed, epoch, epochs_total), "stats": stats, "message": "Stop requested · finishing this epoch and saving the checkpoint." if stop_requested else f"Epoch {epoch}/{epochs_total} · {', '.join(summaries) if summaries else 'model is updating'}"})
            write_training_log("INFO", "Training epoch completed", epoch=epoch, epochs=epochs_total, stats=stats, elapsed_seconds=int(elapsed))
            if stop_requested:
                trainer.stop = True
        model.add_callback("on_train_batch_end", update_batch)
        model.add_callback("on_train_epoch_end", update_epoch)
        write_training_log("INFO", "Model fit started", model=Path(model_file).name, device=ml_training_state["device"], images=sample_count, epochs=epochs_total)
        run_dir = ML_DATA_DIR / "runs" / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        run_metadata = {"run_id": run_name, "dataset_source": dataset_source, "dataset_name": dataset_name, "epochs_requested": epochs_total, "initial_model": Path(model_file).name, "device": device_label, "samples": sample_count, "started_at": utc_now()}
        (run_dir / "vaos_run.json").write_text(json.dumps(run_metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        ml_training_state.update({"run_id": run_name, "initial_model": Path(model_file).name})
        write_training_log("INFO", "Training run initialized", run_id=run_name, source=dataset_source, dataset_name=dataset_name, epochs=epochs_total, initial_model=Path(model_file).name, images=sample_count, classes=len(labels))
        available_ram_gb = None
        try:
            import psutil
            available_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        except Exception:
            logger.debug("Available RAM could not be measured; using the conservative training profile", exc_info=True)
        cuda_device_count = torch.cuda.device_count() if torch.cuda.is_available() else 0
        profile = training_resource_profile(
            cpu_threads=process_threads,
            available_ram_gb=available_ram_gb,
            accelerator={"gpu": "cuda", "mps": "mps"}.get(device_kind, "cpu"),
            gpu_count=cuda_device_count if device_kind == "gpu" else 0,
            resource_mode=resource_mode,
        )
        training_workers = int(profile["workers"])
        training_batch = int(profile["batch"])
        if device_kind == "gpu" and resource_mode and cuda_device_count > 1:
            training_batch = max(cuda_device_count, training_batch * cuda_device_count)
        training_image_size = int(profile["image_size"])
        write_training_log(
            "INFO", "Training resources configured", device=device_label,
            accelerator=device_kind, gpu_count=cuda_device_count,
            batch="auto" if training_batch == -1 else training_batch,
            workers=training_workers, cpu_threads=process_threads,
            memory_available_gb=round(available_ram_gb, 2) if available_ram_gb is not None else None,
            image_size=training_image_size, profile=profile["profile"],
        )
        result = model.train(data=str(yaml_path), epochs=epochs_total, imgsz=training_image_size, batch=training_batch, device=train_device, workers=training_workers, cache=False, project=str(ML_DATA_DIR / "runs"), name=run_name, exist_ok=True, verbose=False, plots=False)
        best = Path(result.save_dir) / "weights" / "best.pt"
        stopped_early = bool(ml_training_state.get("stop_requested"))
        if stopped_early and not best.is_file():
            best = Path(result.save_dir) / "weights" / "last.pt"
        if not best.is_file():
            raise RuntimeError("Training ended without producing a best.pt model file.")
        active = ML_DATA_DIR / "active"
        active.mkdir(parents=True, exist_ok=True)
        import shutil
        if (active / "best.pt").is_file():
            backup_dir = active / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"best_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.pt"
            shutil.copy2(active / "best.pt", backup_path)
            write_training_log("INFO", "Previous custom model backed up", backup=str(backup_path))
        temp_model = active / "best.pt.new"
        shutil.copy2(best, temp_model)
        temp_model.replace(active / "best.pt")
        result_metrics = getattr(result, "results_dict", {}) or {}
        final_metrics = {str(key): round(float(value), 4) for key, value in result_metrics.items() if isinstance(value, (int, float))}
        ml_training_state.update({"status": "running", "progress": 99, "message": "Training finished · saving per-class object counts and final model details."})
        write_training_log("INFO", "Summarizing final training labels", dataset=dataset_name, classes=len(labels))
        try:
            class_counts, invalid_class_ids = count_training_class_instances(yaml_path, labels)
        except Exception:
            logger.exception("Could not summarize per-class training labels")
            class_counts, invalid_class_ids = {}, None
        metadata = {"trained_at": utc_now(), "samples": sample_count, "dataset_source": dataset_name, "dataset_id": dataset_source, "run_id": run_name, "classes": labels, "class_counts": class_counts, "invalid_class_ids": invalid_class_ids, "device": device_label, "epochs": epochs_total, "metrics": final_metrics, "base_model": Path(model_file).name, "base_model_source": Path(model_file).name, "resource_mode": bool(resource_mode), "workers": training_workers, "batch": training_batch}
        metadata_tmp = active / "metadata.json.new"
        metadata_tmp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        metadata_tmp.replace(active / "metadata.json")
        ml_training_state = {"status": "stopped" if stopped_early else "complete", "job_kind": job_kind, "source": dataset_source, "dataset_name": dataset_name, "dataset_id": dataset_source.split(":", 1)[1] if dataset_source.startswith("dataset:") else "", "run_id": run_name, "initial_model": Path(model_file).name, "progress": int(ml_training_state.get("progress", 100)) if stopped_early else 100, "epoch": int(ml_training_state.get("epoch", epochs_total)) if stopped_early else epochs_total, "epochs": epochs_total, "batch": ml_training_state.get("batch", 0), "batches": ml_training_state.get("batches", 0), "elapsed_seconds": int(time.monotonic() - job_started), "eta_seconds": 0, "stats": ml_training_state.get("stats", {}), "device": device_label, "message": "Training stopped safely at the epoch boundary. The saved checkpoint is now your custom model; restart VAOS to load it." if stopped_early else "Training finished. Restart VAOS to load your custom detector.", "samples": sample_count, "classes": labels, "model": str(active / "best.pt"), "metrics": final_metrics}
        elapsed_seconds = int(time.monotonic() - job_started)
        annotation_count = sum(class_counts.values()) if invalid_class_ids == 0 else None
        write_training_log("INFO", "Training finished", run_id=run_name, source=dataset_source, dataset_name=dataset_name, epochs=epochs_total, status=ml_training_state["status"], model=str(active / "best.pt"), metrics=final_metrics, elapsed_seconds=elapsed_seconds, annotations=annotation_count)
    except Exception as exc:
        logger.exception("VAOS object model training failed")
        error_message = str(exc)[:400] or exc.__class__.__name__
        write_training_log("ERROR", "Training failed", run_id=run_name, source=dataset_source, dataset_name=ml_training_state.get("dataset_name", ""), initial_model=ml_training_state.get("initial_model", initial_model), error=error_message, error_type=exc.__class__.__name__, elapsed_seconds=int(time.monotonic() - job_started))
        ml_training_state = {"status": "error", "job_kind": "enrichment" if resource_mode else "training", "source": dataset_source, "dataset_name": ml_training_state.get("dataset_name", ""), "dataset_id": ml_training_state.get("dataset_id", ""), "run_id": ml_training_state.get("run_id", ""), "epochs": ml_training_state.get("epochs", requested_epochs or 0), "message": error_message, "progress": int(ml_training_state.get("progress", 0)), "epoch": ml_training_state.get("epoch", 0), "stats": ml_training_state.get("stats", {}), "device": ml_training_state.get("device", device_choice.upper())}
    finally:
        ml_training_lock.release()


@app.post("/api/admin/ml/train")
def start_ml_training(payload: dict[str, Any], _: str = Depends(require_admin)):
    device_choice = str(payload.get("device", "cpu:1"))
    resource_mode = bool(payload.get("resource_mode", False))
    try:
        requested_epochs = int(payload.get("epochs", 20))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Choose an epoch count from 10 to 50 in steps of five.")
    if requested_epochs not in TRAINING_EPOCH_OPTIONS:
        raise HTTPException(status_code=400, detail="Choose an epoch count from 10, 15, 20, 25, 30, 35, 40, 45, or 50.")
    if not (device_choice.startswith("cpu:") and (device_choice.split(":", 1)[1].isdigit() or (resource_mode and device_choice.endswith(":all")))) and not (device_choice.startswith("gpu:") and (device_choice.split(":", 1)[1].isdigit() or (resource_mode and device_choice.endswith(":all")))) and device_choice != "mps":
        raise HTTPException(status_code=400, detail="Choose an available CPU thread count, CUDA GPU, or Apple MPS device from the device list")
    inventory = training_device_inventory()
    if device_choice == "mps" and not inventory.get("mps_available"):
        raise HTTPException(status_code=400, detail="Apple MPS is not available in this installation. Choose CPU or install PyTorch with MPS support on Apple Silicon.")
    if device_choice.startswith("cpu:") and device_choice.split(":", 1)[1].isdigit() and int(device_choice.split(":", 1)[1]) > inventory["cpu_threads"]:
        raise HTTPException(status_code=400, detail=f"This PC has {inventory['cpu_threads']} logical CPU threads available")
    if device_choice == "gpu:all" and not inventory["gpus"]:
        raise HTTPException(status_code=400, detail="No NVIDIA GPU is available to the training runtime. Choose an all-CPU option instead.")
    if device_choice.startswith("gpu:") and device_choice.split(":", 1)[1].isdigit() and int(device_choice.split(":", 1)[1]) not in {item["index"] for item in inventory["gpus"]}:
        raise HTTPException(status_code=400, detail="That GPU is not available. Refresh the page and choose one listed in the device menu.")
    dataset_source = str(payload.get("source", "reviewed"))
    is_registered_dataset = dataset_source.startswith("dataset:")
    if dataset_source not in {"reviewed", "coco2017", "combined", "uvh26_combined", "combined_kaggle", "combined_vehicle"} and not is_registered_dataset:
        write_training_log("WARNING", "Training request rejected", reason="Unsupported dataset source", source=dataset_source)
        raise HTTPException(status_code=400, detail="Choose a validated dataset from Dataset preparation or one of the built-in training sources.")
    if is_registered_dataset:
        dataset_id = dataset_source.split(":", 1)[1]
        imported_local = load_public_dataset_imports().get(dataset_id)
        if not re.fullmatch(r"local_[a-f0-9]{12}", dataset_id) or not isinstance(imported_local, dict) or imported_local.get("managed_local_dataset") is not True or not Path(str(imported_local.get("data_yaml", ""))).is_file():
            raise HTTPException(status_code=409, detail="That local dataset is no longer available. Revalidate it on Dataset preparation.")
    if dataset_source in {"coco2017", "combined", "combined_kaggle", "combined_vehicle"} and ("coco2017" not in load_public_dataset_imports() or not coco_dataset_layout_ready()):
        write_training_log("WARNING", "Training request rejected", reason="COCO 2017 is not imported", source=dataset_source)
        raise HTTPException(status_code=409, detail="Prepare COCO 2017 first. Existing image and label files will be reused; missing files are identified before a download starts.")
    if dataset_source in {"uvh26_combined", "combined_vehicle"} and ("uvh26" not in load_public_dataset_imports() or not Path(str(load_public_dataset_imports().get("uvh26", {}).get("data_yaml", ""))).is_file()):
        write_training_log("WARNING", "Training request rejected", reason="UVH-26 has not passed validation", source=dataset_source)
        raise HTTPException(status_code=409, detail="Validate and prepare both UVH-26 splits first. The dataset is not selectable until image references and annotations pass validation.")
    if dataset_source in {"combined_kaggle", "combined_vehicle"} and ("kaggle_auto" not in load_public_dataset_imports() or not Path(str(load_public_dataset_imports().get("kaggle_auto", {}).get("data_yaml", ""))).is_file()):
        write_training_log("WARNING", "Training request rejected", reason="Kaggle Auto is not validated", source=dataset_source)
        raise HTTPException(status_code=409, detail="Validate and prepare the local Kaggle Auto images and Pascal VOC annotations first.")
    if dataset_source in {"coco2017", "combined", "combined_kaggle", "combined_vehicle"} and ml_dataset_import_state.get("dataset") == "coco2017" and ml_dataset_import_state.get("status") in {"preparing", "downloading"}:
        reason = "COCO 2017 is still downloading. Wait for the import to finish before using COCO-based training. You can switch to Reviewed camera samples to train from confirmed samples now."
        write_training_log("WARNING", "Training request rejected", reason=reason, source=dataset_source)
        raise HTTPException(status_code=409, detail=reason)
    if not ml_training_lock.acquire(blocking=False):
        reason = "A model training job is already active. Wait for it to finish before starting another."
        write_training_log("WARNING", "Training request rejected", reason=reason, source=dataset_source)
        raise HTTPException(status_code=409, detail=reason)
    if ml_dataset_prepare_lock.locked():
        ml_training_lock.release()
        raise HTTPException(status_code=409, detail="Local dataset validation is still running. Wait until it completes before starting training.")
    initial_model = str(payload.get("initial_model", "auto"))
    if initial_model not in {"auto", "best.pt", "yolo26m"}:
        raise HTTPException(status_code=400, detail="Choose best.pt or YOLO26m as the starting model.")
    active_record = load_public_dataset_imports().get(dataset_source.split(":", 1)[1], {}) if is_registered_dataset else {}
    source_labels = {"reviewed": "Human-reviewed camera samples", "coco2017": "COCO 2017", "combined": "COCO 2017 + human-reviewed camera samples", "uvh26_combined": "UVH-26 + human-reviewed camera samples", "combined_kaggle": "COCO 2017 + Kaggle Auto + human-reviewed samples", "combined_vehicle": "COCO 2017 + UVH-26 + Kaggle Auto + human-reviewed samples"}
    dataset_name = str(active_record.get("name") or source_labels.get(dataset_source, dataset_source))
    ml_training_state.update({"status": "preparing", "source": dataset_source, "dataset_id": dataset_source.split(":", 1)[1] if is_registered_dataset else "", "dataset_name": dataset_name, "epochs": requested_epochs, "data_yaml": str(active_record.get("data_yaml", "")), "message": f"Training request accepted · {dataset_name} · {requested_epochs} epochs. Preparing the selected data and model; the first progress update can take a few minutes.", "stop_requested": False, "progress": 0, "epoch": 0, "batch": 0, "batches": 0, "stats": {}, "device": device_choice.upper().replace(":", " · "), "elapsed_seconds": 0, "eta_seconds": None})
    write_training_log("INFO", "Training requested", source=dataset_source, dataset_name=dataset_name, epochs=requested_epochs, device=device_choice, checkpoint=initial_model, resource_mode=resource_mode)
    try:
        _start_context_preserving_thread(run_ml_training, args=(device_choice, dataset_source, initial_model, resource_mode, requested_epochs), name="vaos-ml-training")
    except Exception as exc:
        ml_training_lock.release()
        ml_training_state.update({"status": "error", "message": f"Could not launch the training worker: {exc}"})
        logger.exception("Could not launch the VAOS training worker")
        write_training_log("ERROR", "Training worker could not be launched", error=str(exc)[:400])
        raise HTTPException(status_code=503, detail="VAOS could not start its background training worker. See the Training activity log and admin service error log.") from exc
    return {"status": "preparing", "message": f"Training setup started · {dataset_name} · {requested_epochs} epochs.", "dataset_name": dataset_name, "epochs": requested_epochs}


def _bundle_project_summary(project: dict[str, Any]) -> dict[str, Any]:
    datasets = {str(item.get("id")) for item in load_public_dataset_imports().values() if isinstance(item, dict)}
    models_response = list_ml_model_sources("admin")
    models = {str(item.get("id")) for item in models_response.get("sources", []) if isinstance(item, dict)}
    return {
        "name": project["name"],
        "description": project["description"],
        "engine_settings": project["engine_settings"],
        "dataset_references": project["dataset_refs"],
        "model_references": project["model_refs"],
        "available_dataset_ids": sorted(datasets),
        "missing_dataset_ids": sorted({item["id"] for item in project["dataset_refs"]} - datasets),
        "available_model_ids": sorted(models),
        "missing_model_ids": sorted({item["id"] for item in project["model_refs"]} - models),
        "apply_note": "Applying a bundle changes only the listed engine settings. Dataset and model references are informational; VAOS does not download, select, train, or replace models when a bundle is applied.",
    }


@app.get("/api/admin/projects")
def list_project_bundles(_: str = Depends(require_admin)) -> dict[str, Any]:
    bundles = []
    PROJECT_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(PROJECT_BUNDLE_DIR.glob("*.vaosproject"), key=lambda entry: entry.stat().st_mtime, reverse=True)[:200]:
        try:
            if path.is_symlink() or not _path_is_within(path, PROJECT_BUNDLE_DIR) or path.stat().st_size > 2 * 1024 * 1024:
                continue
            inspected = inspect_bundle(path.read_bytes())
            bundles.append({"id": inspected["archive_sha256"], "name": inspected["project"]["name"], "description": inspected["project"]["description"], "created_at": inspected["manifest"]["created_at"], "producer_version": inspected["manifest"]["producer_version"], "bytes": path.stat().st_size})
        except (OSError, BundleError):
            logger.warning("Skipping invalid saved VAOS project bundle %s", path.name)
    return {"bundles": bundles, "count": len(bundles), "storage_note": "Bundles store settings and local resource names only; private records, images, credentials, model weights, and executable files are excluded."}


@app.post("/api/admin/projects/export")
def export_project_bundle(payload: dict[str, Any], _: str = Depends(require_admin)) -> Response:
    name = payload.get("name", "VAOS project")
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="Bundle name must be text.")
    imports = load_public_dataset_imports()
    dataset_refs = []
    for dataset_id, item in imports.items():
        if not isinstance(item, dict):
            continue
        dataset_refs.append({
            "id": str(item.get("id", dataset_id)), "name": str(item.get("name", dataset_id)),
            "format": str(item.get("format", "YOLO detection")),
            "train_count": max(0, int(item.get("train_count", 0) or 0)),
            "val_count": max(0, int(item.get("val_count", 0) or 0)),
            "class_count": len(item.get("classes", [])) if isinstance(item.get("classes"), list) else 0,
        })
    model_refs = [{"id": str(item["id"]), "name": str(item["name"]), "kind": str(item["kind"])} for item in list_ml_model_sources("admin").get("sources", [])]
    try:
        content, manifest = build_bundle({"name": name.strip() or "VAOS project", "description": "VAOS engine configuration and local dataset/model references.", "engine_settings": get_engine_settings(), "dataset_refs": dataset_refs[:100], "model_refs": model_refs[:100]}, app_version=APP_VERSION, created_at=utc_now())
        content = encrypt_bundle(content, payload.get("passphrase", ""))
    except (BundleError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    safe_name = "".join(char for char in name.casefold() if char.isalnum() or char in "-_ ").strip().replace(" ", "-")[:48] or "vaos-project"
    return Response(content=content, media_type="application/vnd.vaos.project+encrypted", headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="{safe_name}.vaosproject"', "X-VAOS-Project-SHA256": manifest["project_sha256"]})


@app.get("/api/admin/projects/{bundle_id}")
def get_project_bundle(bundle_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", bundle_id):
        raise HTTPException(status_code=400, detail="The project bundle ID is invalid.")
    path = PROJECT_BUNDLE_DIR / f"{bundle_id}.vaosproject"
    try:
        if path.is_symlink() or not _path_is_within(path, PROJECT_BUNDLE_DIR) or path.stat().st_size > 2 * 1024 * 1024:
            raise HTTPException(status_code=404, detail="Saved project bundle was not found.")
        inspected = inspect_bundle(path.read_bytes())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Saved project bundle was not found.") from exc
    except BundleError as exc:
        raise HTTPException(status_code=400, detail=f"Saved bundle failed validation: {exc}") from exc
    if inspected["archive_sha256"] != bundle_id:
        raise HTTPException(status_code=409, detail="The saved bundle changed after it was listed. Import it again to review the current file.")
    return {"bundle_id": bundle_id, "manifest": inspected["manifest"], "preview": _bundle_project_summary(inspected["project"])}


@app.post("/api/admin/projects/import")
async def import_project_bundle(file: UploadFile = File(...), passphrase: str = Form(...), dry_run: bool = True, _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        content = await file.read(MAX_ENCRYPTED_BYTES + 1)
        content = await run_in_threadpool(decrypt_bundle, content, passphrase)
        inspected = inspect_bundle(content)
    except BundleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="VAOS could not read this project bundle.") from exc
    finally:
        await file.close()
    bundle_id = inspected["archive_sha256"]
    saved_path = PROJECT_BUNDLE_DIR / f"{bundle_id}.vaosproject"
    saved = False
    reused = False
    if not dry_run:
        PROJECT_BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
        if saved_path.exists():
            if saved_path.is_symlink() or not _path_is_within(saved_path, PROJECT_BUNDLE_DIR):
                raise HTTPException(status_code=409, detail="The saved bundle path is unsafe; contact the administrator.")
            saved = True
            reused = True
        else:
            temporary = PROJECT_BUNDLE_DIR / f".{bundle_id}.uploading"
            try:
                with temporary.open("xb") as target:
                    target.write(content)
                    target.flush()
                    os.fsync(target.fileno())
                temporary.replace(saved_path)
                saved = True
            except FileExistsError:
                saved = saved_path.is_file()
            except OSError as exc:
                temporary.unlink(missing_ok=True)
                logger.exception("Could not save an imported VAOS project bundle")
                raise HTTPException(status_code=503, detail="VAOS could not save the project bundle. Check local disk space and permissions.") from exc
    summary = _bundle_project_summary(inspected["project"])
    return {"dry_run": dry_run, "saved": saved, "reused": reused, "bundle_id": bundle_id, "manifest": inspected["manifest"], "preview": summary, "message": "Preview only; no files or settings changed." if dry_run else "Project bundle saved locally. Settings remain unchanged until you choose Apply settings."}


@app.post("/api/admin/projects/{bundle_id}/apply")
def apply_project_bundle(bundle_id: str, payload: dict[str, Any], _: str = Depends(require_admin)) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", bundle_id) or payload != {"confirm": True}:
        raise HTTPException(status_code=400, detail="Confirm the reviewed project settings before applying this bundle.")
    path = PROJECT_BUNDLE_DIR / f"{bundle_id}.vaosproject"
    try:
        if path.is_symlink() or not _path_is_within(path, PROJECT_BUNDLE_DIR):
            raise HTTPException(status_code=404, detail="Saved project bundle was not found.")
        inspected = inspect_bundle(path.read_bytes())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Saved project bundle was not found.") from exc
    except BundleError as exc:
        raise HTTPException(status_code=400, detail=f"Saved bundle failed validation: {exc}") from exc
    if inspected["archive_sha256"] != bundle_id:
        raise HTTPException(status_code=409, detail="The saved bundle changed after it was listed. Import it again to review the current file.")
    project = inspected["project"]
    update = update_engine_settings(project["engine_settings"], "admin")
    summary = _bundle_project_summary(project)
    return {"ok": True, "settings": update["settings"], "available_dataset_ids": summary["available_dataset_ids"], "missing_dataset_ids": summary["missing_dataset_ids"], "available_model_ids": summary["available_model_ids"], "missing_model_ids": summary["missing_model_ids"], "message": "Engine settings applied. Dataset and model references were not imported, selected, downloaded, or trained.", "restart_required": False}


@app.delete("/api/admin/projects/{bundle_id}")
def delete_project_bundle(bundle_id: str, _: str = Depends(require_admin)) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", bundle_id):
        raise HTTPException(status_code=400, detail="The project bundle ID is invalid.")
    path = PROJECT_BUNDLE_DIR / f"{bundle_id}.vaosproject"
    if path.is_symlink() or not _path_is_within(path, PROJECT_BUNDLE_DIR):
        raise HTTPException(status_code=404, detail="Saved project bundle was not found.")
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Saved project bundle was not found.") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="VAOS could not remove the saved bundle file.") from exc
    return {"ok": True, "message": "The saved configuration bundle was removed. Referenced datasets and models were not deleted."}


@app.get("/api/admin/ml/model-sources")
def list_ml_model_sources(_: str = Depends(require_admin)):
    sources = []
    base = MODEL_DIR / "yolo26m.pt"
    if base.is_file():
        sources.append({"id": "yolo26m", "name": "YOLO26m base model", "path": str(base), "kind": "built-in"})
    custom = ML_DATA_DIR / "active" / "best.pt"
    if custom.is_file():
        sources.append({"id": "best.pt", "name": "Current best.pt", "path": str(custom), "kind": "custom"})
    for path in sorted(ML_MODEL_SOURCES_DIR.glob("*.pt"), key=lambda item: item.name.casefold()):
        sources.append({"id": f"source:{path.name}", "name": path.stem, "path": str(path), "kind": "imported"})
    with db_connection() as db:
        reviewed_count = int(db.execute("SELECT COUNT(*) FROM ml_samples WHERE review_state IN ('confirmed','corrected')").fetchone()[0])
        reviewed_types = int(db.execute("SELECT COUNT(DISTINCT COALESCE(confirmed_label,suggested_label)) FROM ml_samples WHERE review_state IN ('confirmed','corrected') AND COALESCE(confirmed_label,suggested_label) IS NOT NULL").fetchone()[0])
    return {"sources": sources, "dataset": {"coco2017_ready": coco_dataset_layout_ready(), "reviewed_count": reviewed_count, "reviewed_types": reviewed_types}, "dataset_catalog": local_dataset_catalog(), "training": {"status": ml_training_state.get("status", "idle")}, "devices": training_device_inventory()}


def get_huggingface_yolo_models() -> list[dict[str, Any]]:
    """Return only official YOLO26 detection checkpoints from the Ultralytics Hub repo."""
    api_url = f"https://huggingface.co/api/models/{HF_YOLO_REPO_ID}"
    request = urllib.request.Request(api_url, headers={"User-Agent": "VAOS-ModelPicker/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            repository = json.loads(response.read(2_000_000).decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Could not reach Hugging Face to load the official YOLO26 model list. Check internet access and retry.") from exc
    available = {item.get("rfilename") for item in repository.get("siblings", []) if isinstance(item, dict)}
    return [{"id": filename, "name": f"YOLO26{Path(filename).stem[-1].upper()} · {filename}", "repo": HF_YOLO_REPO_ID, "filename": filename} for filename in sorted(HF_YOLO_FILES & available, key=lambda name: "nsmlx".index(name[6]))]


@app.get("/api/admin/ml/huggingface-models")
def list_huggingface_yolo_models(_: str = Depends(require_admin)):
    return {"models": get_huggingface_yolo_models(), "repository": HF_YOLO_REPO_ID}


@app.post("/api/admin/ml/huggingface-models")
def download_huggingface_yolo_model(payload: dict[str, Any], _: str = Depends(require_admin)):
    filename = str(payload.get("filename", ""))
    available = {model["filename"] for model in get_huggingface_yolo_models()}
    if filename not in available or filename not in HF_YOLO_FILES:
        raise HTTPException(status_code=400, detail="Choose an official YOLO26 detection checkpoint from the available Hugging Face list.")
    if ml_training_lock.locked():
        raise HTTPException(status_code=409, detail="Wait for the current training run to finish before downloading another model.")
    destination = ML_MODEL_SOURCES_DIR / f"hf_{filename}"
    if destination.is_file() and destination.stat().st_size >= 1_000_000:
        return {"id": f"source:{destination.name}", "name": destination.stem, "bytes": destination.stat().st_size, "reused": True}
    temporary = ML_MODEL_SOURCES_DIR / f"{destination.name}.part"
    model_url = f"https://huggingface.co/{HF_YOLO_REPO_ID}/resolve/main/{filename}?download=true"
    request = urllib.request.Request(model_url, headers={"User-Agent": "VAOS-ModelPicker/1.0", "Accept": "application/octet-stream"})
    total = 0
    prefix = bytearray()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise RuntimeError("Hugging Face returned a web page instead of model weights.")
            while chunk := response.read(8 * 1024 * 1024):
                if len(prefix) < 512:
                    prefix.extend(chunk[:512 - len(prefix)])
                total += len(chunk)
                if total > 2 * 1024 * 1024 * 1024:
                    raise RuntimeError("This model is larger than VAOS's 2 GB download limit.")
                output.write(chunk)
        if total < 1_000_000 or bytes(prefix).lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise RuntimeError("The download was too small or was not a model file.")
        if not bytes(prefix).startswith((b"PK\x03\x04", b"\x80")):
            raise RuntimeError("The downloaded file does not have a recognized PyTorch checkpoint signature.")
        temporary.replace(destination)
        write_training_log("INFO", "Official Hugging Face model downloaded", repository=HF_YOLO_REPO_ID, filename=filename, bytes=total)
        return {"id": f"source:{destination.name}", "name": destination.stem, "bytes": total, "reused": False}
    except HTTPException:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        temporary.unlink(missing_ok=True)
        logger.exception("Could not download official Hugging Face model %s", filename)
        raise HTTPException(status_code=503, detail=f"Could not download {filename} from Hugging Face: {str(exc)[:240]}") from exc


@app.post("/api/admin/ml/model-sources")
async def upload_ml_model_source(file: UploadFile = File(...), _: str = Depends(require_admin)):
    original_name = Path(file.filename or "").name
    if Path(original_name).suffix.lower() != ".pt" or not original_name:
        raise HTTPException(status_code=400, detail="Choose a PyTorch YOLO model file with a .pt extension.")
    safe_stem = "".join(character for character in Path(original_name).stem if character.isalnum() or character in "._-").strip("._-")[:80]
    if not safe_stem:
        raise HTTPException(status_code=400, detail="The model filename needs letters or numbers.")
    if ml_training_lock.locked():
        raise HTTPException(status_code=409, detail="Wait for the current training run to finish before importing another model.")
    temporary = ML_MODEL_SOURCES_DIR / f"{safe_stem}.pt.uploading"
    destination = ML_MODEL_SOURCES_DIR / f"{safe_stem}.pt"
    total = 0
    try:
        with temporary.open("wb") as target:
            while chunk := await file.read(8 * 1024 * 1024):
                total += len(chunk)
                if total > 2 * 1024 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Model files must be 2 GB or smaller.")
                target.write(chunk)
        if total < 1_000_000:
            raise HTTPException(status_code=400, detail="That file is too small to be a usable model checkpoint.")
        if destination.exists():
            digest = hashlib.sha256()
            with temporary.open("rb") as model_file:
                for chunk in iter(lambda: model_file.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            suffix = digest.hexdigest()[:8]
            destination = ML_MODEL_SOURCES_DIR / f"{safe_stem}_{suffix}.pt"
        temporary.replace(destination)
        write_training_log("INFO", "Model source imported", name=destination.name, bytes=total)
        return {"id": f"source:{destination.name}", "name": destination.stem, "bytes": total}
    except HTTPException:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        logger.exception("Could not save an imported model source")
        raise HTTPException(status_code=503, detail="Could not save this model. Check available disk space and the admin log.") from exc
    finally:
        await file.close()


@app.post("/api/admin/ml/enrichment")
def start_ml_enrichment(payload: dict[str, Any], _: str = Depends(require_admin)):
    lock_acquired = False
    try:
        checkpoint = str(payload.get("checkpoint", "yolo26m"))
        dataset_source = str(payload.get("dataset_source", "combined"))
        if dataset_source not in {"reviewed", "combined"}:
            raise HTTPException(status_code=400, detail="Choose reviewed samples or COCO 2017 + reviewed samples.")
        source_list = list_ml_model_sources(_).get("sources", [])
        source_ids = {item["id"] for item in source_list if isinstance(item, dict) and item.get("id")}
        if checkpoint not in source_ids:
            raise HTTPException(status_code=400, detail="Choose a model source that appears in the available model list.")
        if dataset_source == "combined" and (not coco_dataset_layout_ready() or "coco2017" not in load_public_dataset_imports()):
            raise HTTPException(status_code=409, detail="Prepare COCO 2017 before choosing it with reviewed samples. Your local COCO files will be reused.")
        if dataset_source in {"reviewed", "combined"}:
            with db_connection() as db:
                reviewed_count = int(db.execute("SELECT COUNT(*) FROM ml_samples WHERE review_state IN ('confirmed','corrected')").fetchone()[0])
                reviewed_types = int(db.execute("SELECT COUNT(DISTINCT COALESCE(confirmed_label,suggested_label)) FROM ml_samples WHERE review_state IN ('confirmed','corrected') AND COALESCE(confirmed_label,suggested_label) IS NOT NULL").fetchone()[0])
            if reviewed_count < 20 or reviewed_types < 2:
                raise HTTPException(status_code=409, detail="Confirm at least 20 reviewed samples across two object types before starting an update.")
        if not ml_training_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A training run is already active. Wait for it to finish before starting enrichment.")
        lock_acquired = True
        inventory = training_device_inventory()
        device_choice = "gpu:all" if inventory["gpus"] else f"cpu:{inventory['cpu_threads']}"
        device_summary = "all available CUDA GPUs" if inventory["gpus"] else f"CPU · {inventory['cpu_threads']} threads (no NVIDIA GPU detected)"
        dataset_name = "Human-reviewed camera samples" if dataset_source == "reviewed" else "COCO 2017 + human-reviewed camera samples"
        ml_training_state.update({"status": "preparing", "message": f"Model update queued · {dataset_name} · {device_summary}.", "stop_requested": False, "progress": 0, "epoch": 0, "batch": 0, "batches": 0, "stats": {}, "device": device_summary, "elapsed_seconds": 0, "eta_seconds": None, "job_kind": "enrichment"})
        write_training_log("INFO", "Model enrichment requested", checkpoint=checkpoint, dataset=dataset_name, resources="all CUDA GPUs, bounded CPU data workers, managed system memory")
        _start_context_preserving_thread(run_ml_training, args=(device_choice, dataset_source, checkpoint, True), name="vaos-ml-enrichment")
        lock_acquired = False  # The worker releases the lock in its finally block.
    except HTTPException:
        if lock_acquired:
            ml_training_lock.release()
        raise
    except Exception as exc:
        if lock_acquired:
            ml_training_lock.release()
        logger.exception("Could not prepare the VAOS model enrichment request")
        message = "Could not prepare model enrichment. Check Training activity and the admin service error log for details."
        ml_training_state.update({"status": "error", "job_kind": "enrichment", "message": message})
        write_training_log("ERROR", "Model enrichment request failed before the worker started", error=str(exc)[:400])
        raise HTTPException(status_code=503, detail=message) from exc
    return {"status": "preparing", "message": f"Model update started with {dataset_name}. The existing best.pt will be backed up before replacement."}


@app.post("/api/admin/ml/train/stop")
def stop_ml_training(_: str = Depends(require_admin)):
    if ml_training_state.get("status") not in {"preparing", "running"}:
        write_training_log("WARNING", "Stop request rejected", reason="No active training job")
        raise HTTPException(status_code=409, detail="There is no active model training run to stop.")
    ml_training_state["stop_requested"] = True
    ml_training_state["message"] = "Stop requested · training will finish the current epoch and save its checkpoint."
    write_training_log("WARNING", "Safe stop requested", epoch=ml_training_state.get("epoch", 0))
    return {"status": "stopping", "message": ml_training_state["message"]}


def video_training_blocks(device: str) -> bool:
    active = ml_training_lock.locked() or ml_training_state.get("status") in {"preparing", "running", "stopping"}
    accelerator = any(word in str(ml_training_state.get("device", "")).lower() for word in ("gpu", "cuda", "mps"))
    return bool(active and not (device == "cpu" and accelerator and ml_training_state.get("status") == "running"))


@app.get("/api/admin/video-review/availability", include_in_schema=False)
def video_review_availability(_: str = Depends(require_admin)):
    return {"cpu_available": not video_training_blocks("cpu")}


@app.post("/api/admin/video-review/sessions")
def create_video_review_session(payload: dict[str, Any], _: str = Depends(require_admin)):
    if video_training_blocks(str(payload.get("analysis_device", "auto"))):
        raise HTTPException(status_code=409, detail="Video analysis is paused while model training is active so it does not compete for GPU or CPU resources.")
    if str(payload.get("source_type", "")) != "local_video":
        raise HTTPException(status_code=400, detail="For automated analysis, choose a local video file that you have permission to use.")
    filename = str(payload.get("file_name", "")).replace("\\", "/").split("/")[-1].strip()
    filename = "".join(character for character in filename if character.isprintable())[:100]
    if not filename or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail="Choose a local video file first.")
    name = f"Local video · {filename}"
    with db_connection() as db:
        token = secrets.token_urlsafe(24)
        db.execute("INSERT INTO cameras(token,name,created_at,active,source_type,source_ref) VALUES(?,?,?,0,'local_video',?)", (token, name, utc_now(), filename))
    return {"session_id": token, "name": name, "message": "Local video session ready. The original video stays in your browser; only cropped detections are saved if sample collection is enabled."}


@app.post("/api/admin/video-review/sessions/{session_id}/frame")
async def analyze_video_review_frame(session_id: str, request: Request, _: str = Depends(require_admin)):
    if video_training_blocks(request.headers.get("x-analysis-device", "auto")):
        raise HTTPException(status_code=409, detail="Video analysis paused because model training is active.")
    with db_connection() as db:
        session = db.execute("SELECT token FROM cameras WHERE token=? AND source_type='local_video'", (session_id,)).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail="This video review session was not found. Select the local video file again to start a session.")
    if "image/" not in request.headers.get("content-type", ""):
        raise HTTPException(status_code=415, detail="Send a JPEG video frame.")
    try:
        source_time = float(request.headers.get("x-video-time", "0"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="The video timestamp is invalid.") from exc
    if not 0 <= source_time <= 86_400:
        raise HTTPException(status_code=400, detail="The video timestamp is outside the supported range.")
    content = await request.body()
    frame_processing_started = time.perf_counter()
    try:
        save_samples = request.headers.get("x-save-samples", "1") == "1"
        analysis_device = request.headers.get("x-analysis-device", "auto")
        if analysis_device not in {"auto", "cpu"}:
            raise ValueError("Choose automatic or CPU video analysis.")
        result = await run_in_threadpool(process_video_review_frame, session_id, content, source_time, save_samples, analysis_device)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except cv2.error as exc:
        logger.exception("Video frame analysis failed for session %s", session_id[:6])
        raise HTTPException(status_code=422, detail="VAOS could not decode this video frame. Confirm the selected local file plays in the browser.") from exc
    record_observability_video_review_latency((time.perf_counter() - frame_processing_started) * 1000.0)
    queue_observability_video_review_frame()
    return result


@app.post("/api/enroll/{token}", summary="Create a volunteer profile from a one-time enrollment invitation")
async def public_enrollment_api(
    token: str,
    name: str = Form(...),
    consent: str = Form(...),
    photos: list[UploadFile] = File(...),
):
    """The invitation token is a limited capability; explicit consent is required."""
    return await create_public_participant(token, name, consent, photos)


@app.post("/api/camera/{token}/frame")
async def receive_frame(token: str, request: Request):
    with db_connection() as db:
        camera = db.execute("SELECT active FROM cameras WHERE token = ?", (token,)).fetchone()
    if not camera or not camera["active"]:
        raise HTTPException(status_code=404, detail="Camera link invalid or revoked")
    content_type = request.headers.get("content-type", "")
    if "image/" not in content_type:
        raise HTTPException(status_code=415, detail="Send a JPEG image frame")
    content = await request.body()
    frame_processing_started = time.perf_counter()
    try:
        result = await run_in_threadpool(process_frame, token, content)
    except (ValueError, RuntimeError, cv2.error) as exc:
        logger.warning("Frame processing failed for camera %s: %s", token[:6], exc)
        with db_connection() as db:
            db.execute("INSERT INTO camera_status(camera_token,last_frame_at,error) VALUES(?,?,?) ON CONFLICT(camera_token) DO UPDATE SET last_frame_at=excluded.last_frame_at,error=excluded.error", (token, utc_now(), str(exc)[:400]))
        status = 400 if isinstance(exc, ValueError) else 503
        raise HTTPException(status_code=status, detail=(str(exc) if status == 400 else "Local models could not process this frame. Check Model status and diagnostics in the private dashboard.")) from exc
    record_observability_inference_latency((time.perf_counter() - frame_processing_started) * 1000.0)
    await run_in_threadpool(queue_observability_inference_frame, token)
    return JSONResponse(result)


def _record_observability_host_sample(session_id: str) -> None:
    """Persist a low-frequency sample from VAOS's existing cached telemetry collector."""
    # Keep one host sample cycle at a time so a failed-sample summary can be
    # retried with a stable event ID and cleared without racing a later cycle.
    with observability_host_sample_lock:
        _record_observability_host_sample_locked(session_id)


def _record_observability_host_sample_locked(session_id: str) -> None:
    global observability_host_sample_loss_count, observability_host_sample_loss_event_id
    sample_time = utc_now()
    metrics = get_system_metrics()
    host_id = observability_installation_id or "local-installation"
    host_values = (
        ("hardware.cpu_utilization_percent", metrics.get("cpu_percent"), {"host_id": host_id}),
        ("hardware.ram_used_bytes", metrics.get("memory_used_bytes"), {"host_id": host_id}),
        ("hardware.disk_used_bytes", metrics.get("disk_used_bytes"), {"host_id": host_id, "volume_id": "vaos-data"}),
        ("hardware.disk_used_percent", metrics.get("disk_used_percent"), {"host_id": host_id, "volume_id": "vaos-data"}),
    )
    samples: list[tuple[str, float, dict[str, Any], str]] = []
    for metric_key, value, dimensions in host_values:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            samples.append((metric_key, float(value), dimensions, metric_key))
    gpu_power_samples: list[dict[str, Any]] = []
    for device in metrics.get("gpu_devices", []):
        value = device.get("utilization_percent")
        index = device.get("index")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(index, int):
            dimensions = {"device_id": str(device.get("device_id") or index), "model_id": str(device.get("name", "NVIDIA GPU"))[:120]}
            samples.append(("hardware.gpu_utilization_percent", float(value), dimensions, f"gpu:{index}"))
        power = device.get("power_draw_w")
        device_id = str(device.get("device_id", ""))
        if isinstance(power, (int, float)) and not isinstance(power, bool) and math.isfinite(float(power)) and 0 <= float(power) <= 10_000 and re.fullmatch(r"[a-fA-F0-9]{24,64}", device_id):
            gpu_power_samples.append({"device_id": device_id.lower(), "model_id": str(device.get("name", "NVIDIA GPU"))[:120], "power_draw_w": float(power)})
    failed_metric_keys: list[str] = []
    for metric_key, value, dimensions, source in samples:
        event_id = hashlib.sha256(f"{session_id}|{sample_time}|{metric_key}|{source}".encode()).hexdigest()
        try:
            observability_store.record_event(
                DB_PATH, event_id=event_id, timestamp=sample_time, event_type="hardware.metric_sample",
                component="hardware", message="A host or GPU metric sample was recorded.", session_id=session_id,
                details={"metric_key": metric_key, "source": "existing VAOS telemetry collector"},
                metric_observations=[{
                    "metric_key": metric_key,
                    "value": value,
                    "dimensions": dimensions,
                    "provenance": "Existing VAOS system telemetry collector",
                    "measurement_scope": "Cached host or device sample from the existing VAOS telemetry collector",
                    "estimated": False,
                    "timestamp": sample_time,
                }],
            )
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            failed_metric_keys.append(metric_key)
    if gpu_power_samples:
        try:
            observability_store.record_gpu_power_samples(
                DB_PATH,
                samples=gpu_power_samples,
                timestamp=sample_time,
                session_id=session_id,
            )
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            failed_metric_keys.append("hardware.gpu_power_draw_w")
    if failed_metric_keys:
        logger.warning(
            "VAOS host telemetry could not persist %s metric sample(s); other samples were attempted; failed metric keys: %s",
            len(failed_metric_keys), ", ".join(failed_metric_keys),
        )
        if observability_host_sample_loss_event_id is None:
            observability_host_sample_loss_event_id = f"host-sample-gap-{secrets.token_hex(16)}"
        observability_host_sample_loss_count += len(failed_metric_keys)
        for metric_key in failed_metric_keys:
            observability_host_sample_loss_metric_counts[metric_key] = (
                observability_host_sample_loss_metric_counts.get(metric_key, 0) + 1
            )

    if observability_host_sample_loss_count and observability_host_sample_loss_event_id:
        try:
            observability_store.record_event(
                DB_PATH,
                event_id=observability_host_sample_loss_event_id,
                timestamp=sample_time,
                event_type="hardware.metric_sample_gap",
                component="hardware",
                level="WARNING",
                message="One or more host or GPU metric samples could not be persisted.",
                session_id=session_id,
                details={
                    "dropped_sample_count": observability_host_sample_loss_count,
                    "failed_metric_counts": dict(sorted(observability_host_sample_loss_metric_counts.items())),
                    "retry_note": "The summary is retained in memory and retried on a later telemetry cycle if this write fails.",
                },
            )
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.warning("Could not persist the pending VAOS host telemetry loss summary", exc_info=True)
        else:
            observability_host_sample_loss_count = 0
            observability_host_sample_loss_metric_counts.clear()
            observability_host_sample_loss_event_id = None


def _record_observability_api_latency_sample(session_id: str) -> None:
    """Persist p50/p95 for the bounded recent API sample every telemetry cycle."""
    global observability_api_latency_dropped
    with observability_api_latency_lock:
        samples = list(observability_api_latency_samples)
        dropped = observability_api_latency_dropped
        observability_api_latency_samples.clear()
        observability_api_latency_dropped = 0
    if not samples:
        return
    ordered = sorted(samples)

    def quantile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return float(ordered[lower])
        weight = position - lower
        return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)

    sample_time = utc_now()
    event_id = f"api-latency-{secrets.token_hex(16)}"
    observations = [
        {
            "metric_key": metric_key,
            "value": quantile(fraction),
            "dimensions": {},
            "provenance": "Monotonic middleware timing; bounded recent-request sample",
            "measurement_scope": f"{len(samples)} instrumented REST responses; {dropped} older samples dropped before this window was recorded",
            "estimated": False,
            "timestamp": sample_time,
        }
        for metric_key, fraction in (("api.request_latency_p50_ms", 0.50), ("api.request_latency_p95_ms", 0.95))
    ]
    try:
        observability_store.record_event(
            DB_PATH,
            event_id=event_id,
            timestamp=sample_time,
            event_type="api.latency_sample",
            component="api",
            message="A bounded API response-latency sample was recorded.",
            details={"sample_count": len(samples), "dropped_sample_count": dropped},
            session_id=session_id,
            metric_observations=observations,
        )
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        with observability_api_latency_lock:
            combined = samples + list(observability_api_latency_samples)
            overflow = max(0, len(combined) - OBSERVABILITY_API_LATENCY_SAMPLE_LIMIT)
            observability_api_latency_samples.clear()
            observability_api_latency_samples.extend(combined[overflow:])
            observability_api_latency_dropped += dropped + overflow
        logger.debug("Could not persist the sampled VAOS API latency quantiles; retained samples for retry", exc_info=True)


def _record_observability_inference_latency_sample(session_id: str) -> None:
    """Persist p50/p95 for bounded successful camera-frame processing samples."""
    global observability_inference_latency_dropped
    with observability_inference_latency_lock:
        samples = list(observability_inference_latency_samples)
        dropped = observability_inference_latency_dropped
        observability_inference_latency_samples.clear()
        observability_inference_latency_dropped = 0
    if not samples:
        return
    ordered = sorted(samples)

    def quantile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return float(ordered[lower])
        weight = position - lower
        return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)

    sample_time = utc_now()
    event_id = f"camera-frame-latency-{secrets.token_hex(16)}"
    observations = [
        {
            "metric_key": metric_key,
            "value": quantile(fraction),
            "dimensions": {},
            "provenance": "Monotonic successful camera frame-processing timing; bounded recent sample",
            "measurement_scope": f"{len(samples)} successful camera frame-processing calls; {dropped} older samples dropped before this window was recorded",
            "estimated": False,
            "timestamp": sample_time,
        }
        for metric_key, fraction in (
            ("inference.camera_frame_processing_latency_p50_ms", 0.50),
            ("inference.camera_frame_processing_latency_p95_ms", 0.95),
        )
    ]
    try:
        observability_store.record_event(
            DB_PATH,
            event_id=event_id,
            timestamp=sample_time,
            event_type="inference.camera_frame_latency_sample",
            component="inference",
            message="A bounded successful camera frame-processing latency sample was recorded.",
            details={"sample_count": len(samples), "dropped_sample_count": dropped},
            session_id=session_id,
            metric_observations=observations,
        )
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        with observability_inference_latency_lock:
            combined = samples + list(observability_inference_latency_samples)
            overflow = max(0, len(combined) - OBSERVABILITY_INFERENCE_LATENCY_SAMPLE_LIMIT)
            observability_inference_latency_samples.clear()
            observability_inference_latency_samples.extend(combined[overflow:])
            observability_inference_latency_dropped += dropped + overflow
        logger.debug("Could not persist sampled camera frame-processing latency quantiles; retained samples for retry", exc_info=True)


def _record_observability_video_review_latency_sample(session_id: str) -> None:
    """Persist p50/p95 for bounded successful local-video frame samples."""
    global observability_video_review_latency_dropped
    with observability_video_review_latency_lock:
        samples = list(observability_video_review_latency_samples)
        dropped = observability_video_review_latency_dropped
        observability_video_review_latency_samples.clear()
        observability_video_review_latency_dropped = 0
    if not samples:
        return
    ordered = sorted(samples)

    def quantile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return float(ordered[lower])
        weight = position - lower
        return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)

    sample_time = utc_now()
    event_id = f"local-video-frame-latency-{secrets.token_hex(16)}"
    observations = [
        {
            "metric_key": metric_key,
            "value": quantile(fraction),
            "dimensions": {},
            "provenance": "Monotonic successful local-video frame-processing timing; bounded recent sample",
            "measurement_scope": f"{len(samples)} successful local-video frame-processing calls; {dropped} older samples dropped before this window was recorded",
            "estimated": False,
            "timestamp": sample_time,
        }
        for metric_key, fraction in (
            ("inference.local_video_frame_processing_latency_p50_ms", 0.50),
            ("inference.local_video_frame_processing_latency_p95_ms", 0.95),
        )
    ]
    try:
        observability_store.record_event(
            DB_PATH,
            event_id=event_id,
            timestamp=sample_time,
            event_type="inference.local_video_frame_latency_sample",
            component="inference",
            message="A bounded successful local-video frame-processing latency sample was recorded.",
            details={"sample_count": len(samples), "dropped_sample_count": dropped},
            session_id=session_id,
            metric_observations=observations,
        )
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        with observability_video_review_latency_lock:
            combined = samples + list(observability_video_review_latency_samples)
            overflow = max(0, len(combined) - OBSERVABILITY_INFERENCE_LATENCY_SAMPLE_LIMIT)
            observability_video_review_latency_samples.clear()
            observability_video_review_latency_samples.extend(combined[overflow:])
            observability_video_review_latency_dropped += dropped + overflow
        logger.debug("Could not persist sampled local-video frame-processing latency quantiles; retained samples for retry", exc_info=True)


def _observability_heartbeat_worker(session_id: str, stop_event: threading.Event) -> None:
    last_retention = 0.0
    while not stop_event.wait(30):
        try:
            observability_store.heartbeat_session(DB_PATH, session_id)
        except (OSError, sqlite3.Error, ValueError):
            logger.debug("Could not update the VAOS runtime heartbeat", exc_info=True)
        try:
            flush_observability_api_requests()
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.debug("Could not flush aggregated VAOS REST request counters", exc_info=True)
        try:
            flush_observability_training_events()
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.debug("Could not flush queued training telemetry", exc_info=True)
        try:
            recovered_security_events = observability_siem.flush_security_event_fallback(DB_PATH, maximum=100)
            if recovered_security_events:
                logger.info("Replayed %s queued authentication security event(s)", recovered_security_events)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.warning("Could not replay queued authentication security events", exc_info=True)
        try:
            delivery_result = observability_siem.dispatch_pending(DB_PATH, maximum=2)
            if delivery_result["attempted"]:
                logger.info("VAOS SIEM delivery attempt: %s", delivery_result)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.warning("VAOS SIEM delivery worker could not update its queue", exc_info=True)
        try:
            _record_observability_host_sample(session_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.warning("Could not collect or persist a VAOS host telemetry sample", exc_info=True)
        try:
            _record_observability_api_latency_sample(session_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.debug("Could not persist a VAOS API latency sample", exc_info=True)
        try:
            _record_observability_inference_latency_sample(session_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.debug("Could not persist VAOS camera frame-processing latency samples", exc_info=True)
        try:
            _record_observability_video_review_latency_sample(session_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.debug("Could not persist VAOS local-video frame-processing latency samples", exc_info=True)
        if time.monotonic() - last_retention >= 24 * 60 * 60:
            try:
                result = observability_store.apply_retention(DB_PATH)
                expired_reports = observability_reports.expire_reports(DB_PATH, OBSERVABILITY_REPORT_DIR)
                if result["metric_observations_removed"] or result["event_details_redacted"] or result["delivery_events_removed"] or expired_reports:
                    logger.info("VAOS observability retention applied: %s; %s report(s) expired", result, expired_reports)
                last_retention = time.monotonic()
            except (OSError, sqlite3.Error, ValueError):
                logger.warning("VAOS observability retention could not run; it will retry later", exc_info=True)


def _start_observability_lifecycle() -> None:
    global observability_session_id, observability_installation_id, observability_heartbeat_stop, observability_heartbeat_thread
    try:
        session_id, installation_id = observability_store.start_session(DB_PATH, component="admin", app_version=APP_VERSION)
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.exception("Could not record the VAOS admin runtime session")
        return
    with observability_lifecycle_lock:
        observability_installation_id = installation_id
        if observability_heartbeat_thread and observability_heartbeat_thread.is_alive() and not (observability_heartbeat_stop and observability_heartbeat_stop.is_set()):
            observability_session_id = session_id
            return
        stop_event = threading.Event()
        thread = threading.Thread(target=_observability_heartbeat_worker, args=(session_id, stop_event), name="vaos-observability-heartbeat", daemon=True)
        observability_session_id = session_id
        observability_heartbeat_stop = stop_event
        observability_heartbeat_thread = thread
        thread.start()


def _stop_observability_lifecycle() -> None:
    global observability_session_id, observability_heartbeat_stop, observability_heartbeat_thread
    with observability_lifecycle_lock:
        session_id = observability_session_id
        stop_event = observability_heartbeat_stop
        thread = observability_heartbeat_thread
        observability_session_id = None
        observability_heartbeat_stop = None
        observability_heartbeat_thread = None
        if stop_event:
            stop_event.set()
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2)
    if session_id:
        try:
            _record_observability_api_latency_sample(session_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.warning("A final VAOS API latency sample could not be recorded", exc_info=True)
        try:
            _record_observability_inference_latency_sample(session_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.warning("A final VAOS camera frame-processing latency sample could not be recorded", exc_info=True)
        try:
            _record_observability_video_review_latency_sample(session_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.warning("A final VAOS local-video frame-processing latency sample could not be recorded", exc_info=True)
    try:
        for _ in range(100):
            persisted = flush_observability_api_requests(max_events=5000)
            with observability_api_lock:
                memory_fallback_pending = bool(observability_api_aggregates or observability_frame_aggregates)
            durable_fallback_pending = observability_counter_fallback_pending()
            if not persisted and not memory_fallback_pending and not durable_fallback_pending:
                    break
            if not persisted:
                if durable_fallback_pending:
                    logger.warning("Some VAOS request/frame counters remain in the durable secondary queue and will be retried on the next start")
                elif memory_fallback_pending:
                    logger.warning("Some VAOS request/frame counters remain only in bounded memory and may be lost on process exit")
                break
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.warning("Some aggregated VAOS REST request counters could not be flushed at shutdown", exc_info=True)
    try:
        for _ in range(20):
            persisted = flush_observability_training_events(max_events=25)
            with observability_training_lock:
                if not observability_training_queue and not observability_training_dropped:
                    break
            if persisted == 0:
                break
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.warning("Some queued training telemetry could not be flushed at shutdown", exc_info=True)
    if session_id:
        try:
            observability_store.end_session(DB_PATH, session_id)
        except (OSError, sqlite3.Error, ValueError):
            logger.exception("Could not mark the VAOS runtime session as cleanly stopped")


@app.on_event("startup")
def announce_startup():
    threading.Thread(target=check_release, args=(APP_VERSION,), daemon=True, name="release-check").start()
    global observability_training_recovery_started
    _start_observability_lifecycle()
    try:
        for _ in range(20):
            if not flush_observability_api_requests(max_events=5000):
                break
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.warning("Could not reconcile durable VAOS request/frame counters at startup", exc_info=True)
    try:
        recovered_security_events = observability_siem.flush_security_event_fallback(DB_PATH, maximum=1000)
        if recovered_security_events:
            logger.info("Replayed %s queued authentication security event(s) at startup", recovered_security_events)
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.warning("Could not reconcile queued authentication security events at startup", exc_info=True)
    with observability_training_recovery_lock:
        if not observability_training_recovery_started:
            observability_training_recovery_started = True
            threading.Thread(target=reconcile_training_log_observability, name="vaos-training-observability-recovery", daemon=True).start()
    try:
        queued_report_ids = observability_reports.recover_report_jobs(DB_PATH)
        observability_reports.expire_reports(DB_PATH, OBSERVABILITY_REPORT_DIR)
        removed_report_artifacts = observability_reports.cleanup_orphan_report_artifacts(DB_PATH, OBSERVABILITY_REPORT_DIR)
        if removed_report_artifacts:
            logger.info("Removed %s orphaned observability report artifact(s)", removed_report_artifacts)
        observability_reports.submit_queued_report_jobs(DB_PATH, OBSERVABILITY_REPORT_DIR, queued_report_ids)
    except (OSError, sqlite3.Error, RuntimeError):
        logger.exception("Could not recover pending observability report jobs")
    logger.info("VAOS %s admin service started", APP_VERSION)
    if PRODUCTION_MODE:
        logger.info("Production administrator site is configured behind the deployment HTTPS proxy")
        logger.info("Production camera site: %s", PUBLIC_CAMERA_URL)
    else:
        logger.info("Dashboard: http://127.0.0.1:8000")
        logger.info("Admin username: admin")
        logger.info("Local password file: %s", PASSWORD_PATH)
    logger.info("Models download on first enrollment or incoming camera frame.")


@app.on_event("shutdown")
def record_clean_shutdown():
    _stop_observability_lifecycle()
