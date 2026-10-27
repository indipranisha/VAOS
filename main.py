from __future__ import annotations

import hmac
import csv
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import hashlib
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import cv2
import numpy as np
import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

APP_DIR = Path(__file__).resolve().parent
INSTANCE_ID = hashlib.sha256(str(APP_DIR).casefold().encode("utf-8")).hexdigest()[:16]
DATA_DIR = APP_DIR / "data"
MODEL_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "app.db"
PASSWORD_PATH = DATA_DIR / "admin_password.txt"
TUNNEL_URL_PATH = DATA_DIR / "public_camera_url.txt"
TUNNEL_STATE_PATH = DATA_DIR / "tunnel_state.json"
LAUNCHER_COMMAND_PATH = DATA_DIR / "launcher_command.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("camera-poc")

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

APP_VERSION = "0.10.2"
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
    "camera_max_dimension": "640",
}
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_ENROLLMENT_PHOTOS = 10
MAX_ENROLLMENT_TOTAL_BYTES = 30 * 1024 * 1024

app = FastAPI(title="VAOS (Video Analytics - Operating System)", version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
security = HTTPBasic(auto_error=False)

model_lock = threading.RLock()
recognition_lock = threading.Lock()
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
last_ml_capture: dict[tuple[str, str], float] = {}
OBJECT_CLASSES = ("person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush")
OBJECT_EVENTS_COOLDOWN = 60
ML_DATA_DIR = DATA_DIR / "ml_training"
ML_DATA_DIR.mkdir(parents=True, exist_ok=True)
ML_MODEL_SOURCES_DIR = ML_DATA_DIR / "model_sources"
ML_MODEL_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
HF_YOLO_REPO_ID = "Ultralytics/YOLO26"
HF_YOLO_FILES = {f"yolo26{size}.pt" for size in "nsmlx"}
MAX_ML_SAMPLES = 5000
ML_DATASETS_DIR = ML_DATA_DIR / "public_datasets"
ML_DATASETS_DIR.mkdir(parents=True, exist_ok=True)
ML_TRAINING_LOG_PATH = ML_DATA_DIR / "training.log"
ml_training_state: dict[str, Any] = {"status": "idle", "message": "No training job has been started."}
ml_dataset_import_state: dict[str, Any] = {"status": "idle", "message": "No public dataset import is running."}
ml_sama_import_state: dict[str, Any] = {"status": "idle", "dataset": "sama-coco", "message": "SAMA-COCO annotations are not prepared yet."}
ml_training_lock = threading.Lock()
ml_dataset_import_lock = threading.Lock()
ml_training_log_lock = threading.Lock()
system_metrics_lock = threading.Lock()
system_metrics_cache: dict[str, Any] = {"sampled_at": 0.0, "data": {}}
training_devices_cache: dict[str, Any] = {"sampled_at": 0.0, "data": {}}


def training_device_inventory() -> dict[str, Any]:
    """List usable CPU thread counts and NVIDIA devices without importing torch."""
    if time.monotonic() - float(training_devices_cache["sampled_at"]) < 30:
        return training_devices_cache["data"]
    cpu_threads = max(1, os.cpu_count() or 1)
    gpus: list[dict[str, Any]] = []
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
    training_devices_cache.update({"sampled_at": time.monotonic(), "data": {"cpu_threads": cpu_threads, "gpus": gpus}})
    return training_devices_cache["data"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_training_log(level: str, message: str, **details: Any) -> None:
    """Append concise, private training events to the durable local log."""
    record = {"timestamp": utc_now(), "level": level.upper(), "message": message, **details}
    try:
        with ml_training_log_lock:
            if ML_TRAINING_LOG_PATH.exists() and ML_TRAINING_LOG_PATH.stat().st_size > 1_000_000:
                backup = ML_TRAINING_LOG_PATH.with_suffix(".log.1")
                backup.unlink(missing_ok=True)
                ML_TRAINING_LOG_PATH.replace(backup)
            with ML_TRAINING_LOG_PATH.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.exception("Could not write a VAOS training event to the local training log")


def read_training_log() -> list[dict[str, Any]]:
    try:
        with ml_training_log_lock:
            if not ML_TRAINING_LOG_PATH.is_file():
                return []
            lines = ML_TRAINING_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        return [json.loads(line) for line in lines if line.strip()]
    except (OSError, json.JSONDecodeError):
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
            "progress": min(99, max(0, int(percent))),
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
    return {"opencv": cv2.__version__, "device": compute_device, "cuda_available": bool(object_detector and object_detector.use_cuda), "custom_model_active": trained_active, "object_model_loaded": object_detector is not None, "models": model_status}


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
    if PASSWORD_PATH.exists():
        saved = PASSWORD_PATH.read_text(encoding="utf-8").strip()
        if len(saved) == 5 and saved.isascii() and saved.isdigit():
            return saved
    password = "".join(secrets.choice("0123456789") for _ in range(5))
    PASSWORD_PATH.write_text(password + "\n", encoding="utf-8")
    try:
        os.chmod(PASSWORD_PATH, 0o600)
    except OSError:
        pass
    logger.warning("New dashboard password created in %s", PASSWORD_PATH)
    logger.warning("Dashboard username: admin | password: %s", password)
    return password


# The public camera process imports this module only to share recognition and
# database helpers; it must never initialize or print admin credentials.
ADMIN_PASSWORD = "" if os.getenv("CAMERA_SERVICE_IMPORT") == "1" else get_or_create_admin_password()


def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Sign in to the local dashboard",
            headers={"WWW-Authenticate": "Basic realm=\"Local Camera POC 5-digit PIN\""},
        )
    username_ok = hmac.compare_digest(credentials.username.encode(), b"admin")
    password_ok = hmac.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Incorrect dashboard credentials",
            headers={"WWW-Authenticate": "Basic realm=\"Local Camera POC 5-digit PIN\""},
        )
    return credentials.username


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
            "gpu_available": False,
            "gpu_name": "Unavailable",
            "gpu_utilization_percent": None,
            "gpu_memory_used_mb": None,
            "gpu_memory_total_mb": None,
            "gpu_temperature_c": None,
        }
        try:
            import psutil
            memory = psutil.virtual_memory()
            metrics.update({"cpu_percent": psutil.cpu_percent(interval=None), "memory_percent": memory.percent, "memory_used_gb": round((memory.total - memory.available) / (1024 ** 3), 2), "memory_total_gb": round(memory.total / (1024 ** 3), 2)})
        except Exception:
            logger.debug("Host CPU/RAM metrics are unavailable", exc_info=True)
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            try:
                result = subprocess.run([nvidia_smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=1.25, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                row = next(csv.reader([result.stdout.splitlines()[0]]))
                if len(row) >= 5:
                    as_int = lambda value: int(value.strip()) if value.strip().isdigit() else None
                    metrics.update({"gpu_available": True, "gpu_name": row[0].strip(), "gpu_utilization_percent": as_int(row[1]), "gpu_memory_used_mb": as_int(row[2]), "gpu_memory_total_mb": as_int(row[3]), "gpu_temperature_c": as_int(row[4])})
            except (OSError, subprocess.SubprocessError, IndexError, csv.Error):
                logger.debug("NVIDIA system metrics are unavailable", exc_info=True)
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
                heading REAL NOT NULL DEFAULT 0
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
            CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ml_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                camera_token TEXT NOT NULL, suggested_label TEXT NOT NULL,
                image BLOB NOT NULL, bbox TEXT NOT NULL DEFAULT '0.5,0.5,0.9,0.9', confidence REAL NOT NULL DEFAULT 0,
                review_state TEXT NOT NULL DEFAULT 'pending',
                confirmed_label TEXT, FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_ml_samples_review ON ml_samples(review_state, id DESC);
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(cameras)").fetchall()}
        for name, declaration in (("map_x", "REAL NOT NULL DEFAULT 0.5"), ("map_y", "REAL NOT NULL DEFAULT 0.5"), ("heading", "REAL NOT NULL DEFAULT 0")):
            if name not in columns:
                db.execute(f"ALTER TABLE cameras ADD COLUMN {name} {declaration}")
        participant_columns = {row["name"] for row in db.execute("PRAGMA table_info(participants)").fetchall()}
        sample_columns = {row["name"] for row in db.execute("PRAGMA table_info(ml_samples)").fetchall()}
        if "bbox" not in sample_columns:
            db.execute("ALTER TABLE ml_samples ADD COLUMN bbox TEXT NOT NULL DEFAULT '0.5,0.5,0.9,0.9'")
        if "confidence" not in sample_columns:
            db.execute("ALTER TABLE ml_samples ADD COLUMN confidence REAL NOT NULL DEFAULT 0")
        if "profile_photo" not in participant_columns:
            db.execute("ALTER TABLE participants ADD COLUMN profile_photo BLOB")


init_db()


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
            request = urllib.request.Request(url, headers={"User-Agent": "LocalCameraRecognitionPOC/0.2", "Accept": "application/octet-stream"})
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
    """YOLO26m COCO detector on CUDA device 0 when PyTorch supports it."""
    def __init__(self, model_path: str):
        import torch
        from ultralytics import YOLO

        self.use_cuda = os.getenv("OBJECT_DETECTOR_DEVICE", "auto").lower() != "cpu" and torch.cuda.is_available()
        self.device = 0 if self.use_cuda else "cpu"
        self.device_label = f"YOLO26m · NVIDIA GPU 0 ({torch.cuda.get_device_name(0)})" if self.use_cuda else "YOLO26m · CPU (CUDA unavailable)"
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
            if not self.use_cuda:
                raise
            logger.exception("YOLO26m GPU inference failed; switching to CPU")
            self.use_cuda = False
            self.device = "cpu"
            self.device_label = "YOLO26m · CPU (GPU inference failed)"
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
            sample_count = db.execute("SELECT COUNT(*) AS n FROM ml_samples").fetchone()["n"]
        if capture_enabled and capture_enabled["value"] == "1" and sample_count < MAX_ML_SAMPLES:
            for obj in objects:
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
                        db.execute("INSERT INTO ml_samples(created_at,camera_token,suggested_label,image,bbox,confidence) SELECT ?,?,?,?,?,? WHERE (SELECT COUNT(*) FROM ml_samples)<?", (utc_now(), token, obj["label"], encoded_crop.tobytes(), normalized_box, float(obj["score"]), MAX_ML_SAMPLES))
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
                    track["box"], track["seen"], track["used"] = obj["box"], now_track, True
                else:
                    track = {"id": next_track_id, "label": obj["label"], "box": obj["box"], "seen": now_track, "used": True}
                    existing_tracks.append(track)
                    next_track_id += 1
                obj["track_id"] = track["id"]
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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_admin)):
    settings = get_engine_settings()
    return templates.TemplateResponse(request, "index.html", {
        "threshold": settings["match_threshold"],
        "cooldown": settings["event_cooldown_seconds"],
        "admin_password": ADMIN_PASSWORD,
        "version": APP_VERSION,
    })


@app.get("/training", response_class=HTMLResponse)
def ml_training_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "training.html", {"version": APP_VERSION, "admin_password": ADMIN_PASSWORD})


@app.get("/training/activity", response_class=HTMLResponse)
def ml_training_activity_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "training_activity.html", {"version": APP_VERSION, "admin_password": ADMIN_PASSWORD})


@app.get("/live-system", response_class=HTMLResponse)
def live_system_page(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "live_system.html", {"version": APP_VERSION, "admin_password": ADMIN_PASSWORD})


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
    return templates.TemplateResponse(request, "camera.html", {"camera_name": camera["name"], "token": token, "frame_interval_ms": settings["camera_frame_interval_ms"], "max_dimension": settings["camera_max_dimension"]})


def public_enrollment_page(token: str, request: Request):
    if not is_enrollment_link_valid(token):
        raise HTTPException(status_code=404, detail="This enrollment link is invalid, expired, or already used.")
    return templates.TemplateResponse(request, "enroll.html", {"token": token})


@app.post("/api/admin/cameras")
def create_camera(payload: dict[str, Any], _: str = Depends(require_admin)):
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Enter a camera name")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="Camera name must be 80 characters or fewer")
    token = secrets.token_urlsafe(24)
    with db_connection() as db:
        index = db.execute("SELECT COUNT(*) AS n FROM cameras").fetchone()["n"]
        map_x = 0.18 + (index % 4) * 0.21
        map_y = 0.25 + ((index // 4) % 3) * 0.28
        db.execute("INSERT INTO cameras(token, name, created_at, active,map_x,map_y) VALUES (?, ?, ?, 1,?,?)", (token, name, utc_now(), map_x, map_y))
    return {"token": token, "name": name, "active": True, "created_at": utc_now(), "map_x": map_x, "map_y": map_y, "heading": 0}


@app.get("/api/admin/cameras")
def list_cameras(_: str = Depends(require_admin)):
    with db_connection() as db:
        rows = db.execute("SELECT token, name, created_at, active FROM cameras ORDER BY created_at DESC").fetchall()
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
        rows = db.execute("SELECT c.token,c.name,c.active,c.map_x,c.map_y,c.heading,s.last_frame_at,s.faces_detected,s.matched_count,s.objects_json,s.error FROM cameras c LEFT JOIN camera_status s ON c.token=s.camera_token ORDER BY c.created_at DESC").fetchall()
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
            "http://127.0.0.1:8001/internal/model-status",
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
    last_emitted.clear()
    last_object_emitted.clear()
    return {"ok": True}


def load_public_dataset_imports() -> dict[str, Any]:
    path = ML_DATA_DIR / "public_dataset_imports.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_public_dataset_imports(value: dict[str, Any]) -> None:
    path = ML_DATA_DIR / "public_dataset_imports.json"
    temporary = path.with_suffix(".json.new")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


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


def sama_dataset_root() -> Path:
    return ML_DATASETS_DIR / "sama-coco" / "dataset"


def sama_dataset_layout_ready() -> bool:
    root = sama_dataset_root()
    required = (root / "dataset.yaml", root / "train2017.txt", root / "val2017.txt", root / "images" / "train2017", root / "images" / "val2017", root / "labels" / "train2017", root / "labels" / "val2017")
    if not all(path.exists() for path in required):
        return False
    try:
        return any((root / "labels" / "train2017").glob("*.txt")) and any((root / "labels" / "val2017").glob("*.txt"))
    except OSError:
        return False


def _ensure_image_junction(source: Path, target: Path) -> None:
    source = source.resolve(strict=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target):
        if target.resolve(strict=True) == source:
            return
        if target.is_dir() and sum(1 for _ in target.glob("*.jpg")) == sum(1 for _ in source.glob("*.jpg")):
            return
        raise RuntimeError(f"SAMA image path already exists with different contents: {target}")
    try:
        os.symlink(source, target, target_is_directory=True)
    except OSError:
        # Windows junctions reuse the existing 20 GB COCO image folders without copying them.
        # Use paths relative to the app root and set cwd explicitly. Passing an
        # absolute command string as a single cmd.exe argument double-quotes it
        # on Windows when it contains spaces, so mklink fails despite valid paths.
        target_relative = target.relative_to(BASE_DIR).as_posix()
        source_relative = source.relative_to(BASE_DIR).as_posix()
        command = f'mklink /J "{target_relative}" "{source_relative}"'
        result = subprocess.run(command, shell=True, cwd=BASE_DIR, capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0 or not target.is_dir():
            reason = (result.stderr or result.stdout or "unknown Windows junction error").strip()
            raise RuntimeError(f"Could not link SAMA-COCO to the existing COCO images ({reason[:240]}); no image copy or download was started.")


def import_sama_coco_dataset() -> None:
    """Convert official SAMA-COCO COCO-format annotations to YOLO labels, reusing local COCO images."""
    global ml_sama_import_state
    source_root = ML_DATASETS_DIR / "sama-coco"
    downloads = source_root / "downloads"
    root = sama_dataset_root()
    image_source = ML_DATASETS_DIR / "coco2017" / "images"
    archive_specs = (
        ("sama-coco-train.zip", "https://sama-documentation-assets.s3.amazonaws.com/sama-coco/sama-coco-train.zip", 154_437_504, "train"),
        ("sama-coco-val.zip", "https://sama-documentation-assets.s3.amazonaws.com/sama-coco/sama-coco-val.zip", 5_744_782, "val"),
    )
    try:
        if not coco_dataset_layout_ready():
            raise RuntimeError("Prepare the already-downloaded COCO 2017 image files first. SAMA-COCO reuses those images and does not download them again.")
        imports = load_public_dataset_imports()
        if imports.get("sama-coco") and sama_dataset_layout_ready():
            ml_sama_import_state = {"status": "complete", "dataset": "sama-coco", "progress": 100, "message": "SAMA-COCO annotations are already prepared and ready. COCO images are reused locally."}
            return
        downloads.mkdir(parents=True, exist_ok=True)
        archives: list[tuple[Path, str]] = []
        expected_total = sum(spec[2] for spec in archive_specs)
        downloaded_total = 0
        for filename, url, expected_size, _split in archive_specs:
            archive_path = downloads / filename
            if not archive_path.is_file() or archive_path.stat().st_size != expected_size:
                temporary = archive_path.with_suffix(archive_path.suffix + ".part")
                temporary.unlink(missing_ok=True)
                ml_sama_import_state = {"status": "downloading", "dataset": "sama-coco", "phase": "downloading", "progress": int(downloaded_total * 100 / expected_total), "downloaded_bytes": downloaded_total, "expected_bytes": expected_total, "message": f"Downloading official annotation archive {filename}. COCO images remain local."}
                request = urllib.request.Request(url, headers={"User-Agent": "VAOS-Dataset-Importer/1.0"})
                started = time.monotonic()
                received = 0
                with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as target_file:
                    while chunk := response.read(1024 * 1024):
                        target_file.write(chunk)
                        received += len(chunk)
                        ml_sama_import_state.update({"status": "downloading", "phase": "downloading", "progress": min(99, int((downloaded_total + received) * 100 / expected_total)), "downloaded_bytes": downloaded_total + received, "expected_bytes": expected_total, "speed_bytes_per_second": int(received / max(0.1, time.monotonic() - started)), "message": f"Downloading {filename} · {received:,} bytes. Existing COCO images will be reused."})
                if received != expected_size:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(f"SAMA-COCO archive size check failed for {filename}; expected {expected_size:,} bytes, got {received:,}.")
                temporary.replace(archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                bad_member = archive.testzip()
                if bad_member:
                    raise RuntimeError(f"SAMA-COCO archive failed integrity validation: {filename} · {bad_member}")
                if not any(name.lower().endswith(".json") for name in archive.namelist()):
                    raise RuntimeError(f"SAMA-COCO archive contains no annotation JSON files: {filename}")
            downloaded_total += expected_size
            archives.append((archive_path, _split))
        for split in ("train", "val"):
            source_images = image_source / ("train2017" if split == "train" else "val2017")
            target_images = root / "images" / ("train2017" if split == "train" else "val2017")
            _ensure_image_junction(source_images, target_images)
            (root / "labels" / ("train2017" if split == "train" else "val2017")).mkdir(parents=True, exist_ok=True)
        import yaml
        import ultralytics
        ultralytics_config = Path(ultralytics.__file__).resolve().parent / "cfg" / "datasets" / "coco.yaml"
        config = yaml.safe_load(ultralytics_config.read_text(encoding="utf-8"))
        classes = [str(name) for _, name in sorted(config["names"].items())]
        class_ids = {name.casefold(): index for index, name in enumerate(classes)}
        json_shards: list[tuple[Path, str, str]] = []
        for archive_path, split in archives:
            with zipfile.ZipFile(archive_path) as archive:
                json_shards.extend((archive_path, split, member) for member in archive.namelist() if member.lower().endswith(".json"))
        total_shards = len(json_shards)
        processed_images = 0
        ml_sama_import_state = {"status": "preparing", "dataset": "sama-coco", "phase": "converting", "progress": 0, "processed_images": 0, "total_images": 123_287, "message": f"Converting official SAMA-COCO annotations · 0 of {total_shards} shards. Reusing local COCO images."}
        for shard_index, (archive_path, split, member) in enumerate(json_shards, 1):
            image_split = "train2017" if split == "train" else "val2017"
            labels_dir = root / "labels" / image_split
            with zipfile.ZipFile(archive_path) as archive, archive.open(member) as raw:
                document = json.loads(raw.read().decode("utf-8"))
            categories = {int(item["id"]): class_ids.get(str(item.get("name", "")).casefold()) for item in document.get("categories", []) if isinstance(item, dict) and str(item.get("id", "")).isdigit()}
            annotations_by_image: dict[int, list[str]] = {}
            images_by_id = {int(item["id"]): item for item in document.get("images", []) if isinstance(item, dict) and str(item.get("id", "")).isdigit()}
            for annotation in document.get("annotations", []):
                if not isinstance(annotation, dict):
                    continue
                image_id = int(annotation.get("image_id", -1))
                category_id = int(annotation.get("category_id", -1))
                label_id = categories.get(category_id)
                image = images_by_id.get(image_id)
                bbox = annotation.get("bbox")
                if label_id is None or image is None or not isinstance(bbox, list) or len(bbox) < 4:
                    continue
                try:
                    width, height = float(image["width"]), float(image["height"])
                    x, y, box_width, box_height = map(float, bbox[:4])
                except (KeyError, TypeError, ValueError):
                    continue
                if width <= 0 or height <= 0 or box_width <= 0 or box_height <= 0:
                    continue
                x1, y1 = max(0.0, x), max(0.0, y)
                x2, y2 = min(width, x + box_width), min(height, y + box_height)
                if x2 <= x1 or y2 <= y1:
                    continue
                cx, cy, bw, bh = ((x1 + x2) / (2 * width), (y1 + y2) / (2 * height), (x2 - x1) / width, (y2 - y1) / height)
                annotations_by_image.setdefault(image_id, []).append(f"{label_id} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
            for image_id, image in images_by_id.items():
                image_name = Path(str(image.get("file_name", ""))).name
                if not image_name.lower().endswith(".jpg") or not image_name:
                    continue
                label_path = labels_dir / f"{Path(image_name).stem}.txt"
                lines = annotations_by_image.get(image_id, [])
                if lines:
                    label_path.write_text("\n".join(lines) + "\n", encoding="ascii")
                else:
                    label_path.unlink(missing_ok=True)
                processed_images += 1
            ml_sama_import_state.update({"status": "preparing", "dataset": "sama-coco", "phase": "converting", "progress": min(99, int(shard_index * 100 / total_shards)), "processed_images": processed_images, "total_images": 123_287, "message": f"Converting SAMA-COCO labels · shard {shard_index} of {total_shards} · {processed_images:,} images. No image files are being copied."})
        for split, image_split in (("train", "train2017"), ("val", "val2017")):
            image_dir = root / "images" / image_split
            listing = root / f"{split}2017.txt"
            images = sorted(image_dir.glob("*.jpg"))
            if not images:
                raise RuntimeError(f"No existing COCO {image_split} images were found for SAMA-COCO.")
            listing.write_text("".join(f"./images/{image_split}/{path.name}\n" for path in images), encoding="utf-8")
        dataset_config = {"path": str(root.resolve()), "train": "train2017.txt", "val": "val2017.txt", "names": {index: name for index, name in enumerate(classes)}}
        yaml_path = root / "dataset.yaml"
        yaml_path.write_text(yaml.safe_dump(dataset_config, sort_keys=False, allow_unicode=True), encoding="utf-8")
        from ultralytics.data.utils import check_det_dataset
        check_det_dataset(str(yaml_path))
        metadata = {"id": "sama-coco", "name": "SAMA-COCO", "data_yaml": str(yaml_path), "classes": classes, "train_limit": 118_287, "validation_images": 5_000, "imported_at": utc_now(), "license": "CC BY 4.0", "source": "https://www.sama.com/sama-coco-dataset", "annotation_archives": [str(path) for path, _ in archives], "image_source": "Existing local COCO 2017 images; reused via directory junctions"}
        imports = load_public_dataset_imports()
        imports["sama-coco"] = metadata
        write_public_dataset_imports(imports)
        ml_sama_import_state = {"status": "complete", "dataset": "sama-coco", "phase": "complete", "progress": 100, "processed_images": processed_images, "total_images": processed_images, "message": f"SAMA-COCO ready · {processed_images:,} images indexed across {len(classes)} classes. Existing COCO images are reused; no image copy or download was done."}
        write_training_log("INFO", "SAMA-COCO prepared", images=processed_images, classes=len(classes), license="CC BY 4.0", reused_coco_images=True)
    except Exception as exc:
        logger.exception("VAOS SAMA-COCO preparation failed")
        ml_sama_import_state = {"status": "error", "dataset": "sama-coco", "message": str(exc)[:500]}
        write_training_log("ERROR", "SAMA-COCO preparation failed", error=str(exc)[:400])
    finally:
        ml_dataset_import_lock.release()


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
            monitor_thread = threading.Thread(target=monitor_download, name="vaos-coco-progress", daemon=True)
            monitor_thread.start()
        from ultralytics.data.utils import check_det_dataset
        check_det_dataset(str(yaml_path))
        repair_coco_dataset_layout()
        monitor_stop.set()
        if monitor_thread:
            monitor_thread.join(timeout=2)
        imports = load_public_dataset_imports()
        imports[dataset_id] = {"id": dataset_id, "name": "COCO 2017", "data_yaml": str(yaml_path), "classes": classes, "train_limit": 118287, "imported_at": utc_now(), "size_note": "about 20.3 GB; 118,287 training and 5,000 validation images"}
        write_public_dataset_imports(imports)
        ml_dataset_import_state = {"status": "complete", "dataset": dataset_id, "message": f"{imports[dataset_id]['name']} is ready for training.", "classes": classes}
    except Exception as exc:
        monitor_stop.set()
        if monitor_thread:
            monitor_thread.join(timeout=2)
        logger.exception("VAOS public dataset import failed for %s", dataset_id)
        ml_dataset_import_state = {"status": "error", "dataset": dataset_id, "message": str(exc)[:500]}
    finally:
        ml_dataset_import_lock.release()


@app.get("/api/admin/ml/samples")
def list_ml_samples(_: str = Depends(require_admin)):
    with db_connection() as db:
        rows = db.execute("SELECT s.id,s.created_at,s.suggested_label,s.confidence,s.review_state,s.confirmed_label,c.name AS camera_name FROM ml_samples s JOIN cameras c ON c.token=s.camera_token WHERE s.review_state IN ('pending','later') ORDER BY s.id DESC LIMIT 100").fetchall()
        totals = db.execute("SELECT review_state,COUNT(*) AS n FROM ml_samples GROUP BY review_state").fetchall()
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
        request = urllib.request.Request("http://127.0.0.1:8001/internal/model-status", headers={"X-VAOS-Internal-Token": internal_token})
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
    return {"samples": [dict(row) for row in rows], "counts": {row["review_state"]: row["n"] for row in totals}, "confirmed_classes": [row["label"] for row in confirmed_labels], "training_maturity": maturity, "capture_enabled": bool(setting and setting["value"] == "1"), "sample_total": int(collection_stats["total"] or 0), "sample_latest_at": collection_stats["latest"], "sample_limit": MAX_ML_SAMPLES, "live_camera_count": live_camera_count, "live_object_types": live_object_types, "training": training_state, "training_logs": read_training_log(), "dataset_imports": list(load_public_dataset_imports().values()), "dataset_import": dict(ml_dataset_import_state), "sama_dataset_import": dict(ml_sama_import_state), "coco_dataset_ready": coco_dataset_layout_ready(), "sama_dataset_ready": sama_dataset_layout_ready(), "training_devices": training_device_inventory(), "trained_model": {"installed": model_installed, "path": str(trained_path) if model_installed else "", "metadata": training_info}, "live_model": live_model}


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
                runs.append({"id": run_path.name, "epochs": len(rows), "last_epoch": newest_epoch, "updated_at": datetime.fromtimestamp(result_path.stat().st_mtime, timezone.utc).isoformat()})
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
    return {
        "runs": runs,
        "selected_run": selected["id"] if selected else None,
        "metrics": metrics,
        "dfl_available": bool(metrics["dfl_loss"]["points"]),
        "regression_note": "This run records L1 regression loss rather than DFL loss." if metrics["l1_loss"]["points"] and not metrics["dfl_loss"]["points"] else "",
    }

@app.post("/api/admin/ml/datasets/import")
def start_public_dataset_import(payload: dict[str, Any], _: str = Depends(require_admin)):
    dataset_id = str(payload.get("dataset", ""))
    if dataset_id == "sama-coco":
        imported = load_public_dataset_imports().get("sama-coco")
        if imported and sama_dataset_layout_ready():
            ml_sama_import_state.update({"status": "complete", "dataset": "sama-coco", "progress": 100, "message": "SAMA-COCO is already prepared. Existing COCO images are reused."})
            return {"status": "complete", "message": "SAMA-COCO is already prepared. Existing COCO images are reused."}
        if not ml_dataset_import_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Another public dataset preparation is already running")
        ml_sama_import_state.update({"status": "preparing", "dataset": "sama-coco", "phase": "starting", "progress": 0, "message": "Starting SAMA-COCO annotation preparation. Existing COCO images will be reused."})
        try:
            threading.Thread(target=import_sama_coco_dataset, name="vaos-sama-coco-import", daemon=True).start()
        except Exception as exc:
            ml_dataset_import_lock.release()
            logger.exception("Could not launch the SAMA-COCO preparation worker")
            ml_sama_import_state.update({"status": "error", "dataset": "sama-coco", "message": "Could not start SAMA-COCO preparation. Check the admin service log."})
            raise HTTPException(status_code=503, detail="Could not start SAMA-COCO preparation. Check the admin service error log.") from exc
        return {"status": "preparing", "message": "SAMA-COCO preparation started. COCO images are reused locally."}
    if dataset_id != "coco2017":
        raise HTTPException(status_code=400, detail="Choose COCO 2017 or SAMA-COCO")
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
        threading.Thread(target=import_coco2017_dataset, name="vaos-coco2017-import", daemon=True).start()
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
    if choice not in state_by_choice or not isinstance(sample_ids, list) or not sample_ids or len(sample_ids) > 100:
        raise HTTPException(status_code=400, detail="Choose approve, reject, or change_label and provide up to 100 sample IDs")
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


def run_ml_training(device_choice: str, dataset_source: str, initial_model: str = "auto", resource_mode: bool = False) -> None:
    global ml_training_state
    job_started = time.monotonic()
    write_training_log("INFO", "Training worker started", source=dataset_source, requested_device=device_choice)
    try:
        epochs_total = 10 if dataset_source in {"coco2017", "combined", "sama_coco", "sama_combined"} else 30
        if dataset_source == "coco2017":
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
        elif dataset_source in {"sama_coco", "sama_combined"}:
            imported = load_public_dataset_imports().get("sama-coco")
            if not imported or not Path(imported.get("data_yaml", "")).is_file() or not sama_dataset_layout_ready():
                raise RuntimeError("Prepare SAMA-COCO annotations before selecting this dataset. Existing COCO images will be reused.")
            base_yaml = Path(imported["data_yaml"])
            labels = list(imported.get("classes", []))
            if dataset_source == "sama_coco":
                yaml_path = base_yaml
                sample_count = 118287
                dataset_name = "SAMA-COCO"
            else:
                with db_connection() as db:
                    rows = db.execute("SELECT id,image,bbox,suggested_label,confirmed_label FROM ml_samples WHERE review_state IN ('confirmed','corrected') ORDER BY id").fetchall()
                labeled = [(row["id"], bytes(row["image"]), row["confirmed_label"] or row["suggested_label"], row["bbox"]) for row in rows]
                reviewed_labels = {item[2] for item in labeled}
                if len(labeled) < 20 or len(reviewed_labels) < 2:
                    raise RuntimeError("SAMA-COCO + reviewed training needs at least 20 confirmed crops across at least 2 object types.")
                yaml_path, labels = build_combined_coco_reviewed_yaml(base_yaml, labeled, labels)
                sample_count = 118287 + len(labeled)
                dataset_name = "SAMA-COCO + human-reviewed samples"
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
            raise RuntimeError("Choose reviewed camera samples, COCO 2017, or prepared SAMA-COCO.")
        write_training_log("INFO", "Training data prepared", dataset=dataset_name, images=sample_count, classes=len(labels))
        if ml_training_state.get("stop_requested"):
            ml_training_state = {"status": "stopped", "progress": 0, "epoch": 0, "epochs": epochs_total, "stop_requested": False, "message": "Training was stopped safely before the first epoch; no new model was installed."}
            write_training_log("WARNING", "Training was stopped before the first epoch")
            return
        try:
            import torch
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError("Training packages are not installed. Enable optional YOLO/GPU support in the Windows launcher first.") from exc
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
        device_kind, _, device_value = device_choice.partition(":")
        device_index = int(device_value or 0) if device_value.isdigit() else 0
        if device_kind == "gpu" and device_value == "all":
            if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
                raise RuntimeError("CUDA GPUs are not available to the training runtime. Select a CPU option or install CUDA-enabled PyTorch support.")
            gpu_ids = list(range(torch.cuda.device_count()))
            train_device = gpu_ids
            device_label = "All NVIDIA GPUs · " + ", ".join(f"GPU {index} {torch.cuda.get_device_name(index)}" for index in gpu_ids)
            torch.set_num_threads(max(1, (os.cpu_count() or 1) - 1))
        elif device_kind == "gpu":
            if not torch.cuda.is_available():
                raise RuntimeError("The selected NVIDIA GPU is visible to Windows but CUDA is unavailable in this Python environment. Choose a CPU thread option or install the CUDA-enabled PyTorch support from the launcher.")
            if device_index >= torch.cuda.device_count():
                raise RuntimeError(f"GPU {device_index} is not available to the training runtime. Refresh the device list and choose an available device.")
            train_device = str(device_index)
            device_label = f"GPU {device_index} · {torch.cuda.get_device_name(device_index)}"
        else:
            cpu_threads = max(1, (os.cpu_count() or 1) - 1) if device_value == "all" and resource_mode else max(1, min(device_index, os.cpu_count() or 1))
            torch.set_num_threads(cpu_threads)
            train_device = "cpu"
            device_label = f"CPU · {cpu_threads} thread{'s' if cpu_threads != 1 else ''}"
        job_kind = "enrichment" if resource_mode else "training"
        ml_training_state = {"status": "running", "job_kind": job_kind, "message": f"Training on {device_label} with {sample_count:,} {dataset_name} images and {len(labels)} object types.", "samples": sample_count, "classes": labels, "epoch": 0, "epochs": epochs_total, "progress": 0, "batch": 0, "batches": 0, "stats": {}, "elapsed_seconds": 0, "eta_seconds": None, "device": device_label}
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
            if device_kind == "gpu":
                try:
                    stats["gpu_memory_gb"] = round(torch.cuda.memory_reserved(device_index) / (1024 ** 3), 2)
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
        last_persisted_progress = last_batch_time

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
        run_name = f"vaos_objects_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cpu_worker_budget = max(0, (os.cpu_count() or 1) - 2)
        try:
            import psutil
            memory_worker_budget = max(1, int(max(0.0, psutil.virtual_memory().available / (1024 ** 3) - 8) / 1.5))
        except Exception:
            memory_worker_budget = 4
        training_workers = min(8, cpu_worker_budget, memory_worker_budget) if resource_mode else min(4, cpu_worker_budget)
        if resource_mode and device_kind == "gpu" and torch.cuda.is_available():
            training_batch = -1 if torch.cuda.device_count() == 1 else max(8, 8 * len(train_device))
        else:
            training_batch = 16 if resource_mode and device_kind == "cpu" else 4
        write_training_log("INFO", "Training resources configured", device=device_label, gpu_count=torch.cuda.device_count() if torch.cuda.is_available() else 0, batch="auto" if training_batch == -1 else training_batch, workers=training_workers, cpu_threads=os.cpu_count(), memory_available_gb=round((__import__("psutil").virtual_memory().available / (1024 ** 3)), 2) if resource_mode else None)
        result = model.train(data=str(yaml_path), epochs=epochs_total, imgsz=640, batch=training_batch, device=train_device, workers=training_workers, cache=False, project=str(ML_DATA_DIR / "runs"), name=run_name, exist_ok=True, verbose=False)
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
        metadata = {"trained_at": utc_now(), "samples": sample_count, "dataset_source": dataset_name, "classes": labels, "device": device_label, "epochs": epochs_total, "metrics": final_metrics, "base_model": Path(model_file).name, "base_model_source": Path(model_file).name, "resource_mode": bool(resource_mode), "workers": training_workers, "batch": training_batch}
        metadata_tmp = active / "metadata.json.new"
        metadata_tmp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        metadata_tmp.replace(active / "metadata.json")
        ml_training_state = {"status": "stopped" if stopped_early else "complete", "job_kind": job_kind, "progress": int(ml_training_state.get("progress", 100)) if stopped_early else 100, "epoch": int(ml_training_state.get("epoch", epochs_total)) if stopped_early else epochs_total, "epochs": epochs_total, "batch": ml_training_state.get("batch", 0), "batches": ml_training_state.get("batches", 0), "elapsed_seconds": int(time.monotonic() - job_started), "eta_seconds": 0, "stats": ml_training_state.get("stats", {}), "device": device_label, "message": "Training stopped safely at the epoch boundary. The saved checkpoint is now your custom model; restart VAOS to load it." if stopped_early else "Training finished. Restart VAOS to load your custom detector.", "samples": sample_count, "classes": labels, "model": str(active / "best.pt"), "metrics": final_metrics}
        write_training_log("INFO", "Training finished", status=ml_training_state["status"], model=str(active / "best.pt"), metrics=final_metrics, elapsed_seconds=int(time.monotonic() - job_started))
    except Exception as exc:
        logger.exception("VAOS object model training failed")
        error_message = str(exc)[:400] or exc.__class__.__name__
        write_training_log("ERROR", "Training failed", error=error_message, elapsed_seconds=int(time.monotonic() - job_started))
        ml_training_state = {"status": "error", "job_kind": "enrichment" if resource_mode else "training", "message": error_message, "progress": int(ml_training_state.get("progress", 0)), "epoch": ml_training_state.get("epoch", 0), "epochs": ml_training_state.get("epochs", 0), "stats": ml_training_state.get("stats", {}), "device": ml_training_state.get("device", device_choice.upper())}
    finally:
        ml_training_lock.release()


@app.post("/api/admin/ml/train")
def start_ml_training(payload: dict[str, Any], _: str = Depends(require_admin)):
    device_choice = str(payload.get("device", "cpu:1"))
    resource_mode = bool(payload.get("resource_mode", False))
    if not (device_choice.startswith("cpu:") and (device_choice.split(":", 1)[1].isdigit() or (resource_mode and device_choice.endswith(":all")))) and not (device_choice.startswith("gpu:") and (device_choice.split(":", 1)[1].isdigit() or (resource_mode and device_choice.endswith(":all")))):
        raise HTTPException(status_code=400, detail="Choose an available CPU thread count or GPU from the device list")
    inventory = training_device_inventory()
    if device_choice.startswith("cpu:") and device_choice.split(":", 1)[1].isdigit() and int(device_choice.split(":", 1)[1]) > inventory["cpu_threads"]:
        raise HTTPException(status_code=400, detail=f"This PC has {inventory['cpu_threads']} logical CPU threads available")
    if device_choice == "gpu:all" and not inventory["gpus"]:
        raise HTTPException(status_code=400, detail="No NVIDIA GPU is available to the training runtime. Choose an all-CPU option instead.")
    if device_choice.startswith("gpu:") and device_choice.split(":", 1)[1].isdigit() and int(device_choice.split(":", 1)[1]) not in {item["index"] for item in inventory["gpus"]}:
        raise HTTPException(status_code=400, detail="That GPU is not available. Refresh the page and choose one listed in the device menu.")
    dataset_source = str(payload.get("source", "reviewed"))
    if dataset_source not in {"reviewed", "coco2017", "combined", "sama_coco", "sama_combined"}:
        write_training_log("WARNING", "Training request rejected", reason="Unsupported dataset source", source=dataset_source)
        raise HTTPException(status_code=400, detail="Choose reviewed samples, COCO 2017, or SAMA-COCO training")
    if dataset_source in {"coco2017", "combined"} and ("coco2017" not in load_public_dataset_imports() or not coco_dataset_layout_ready()):
        write_training_log("WARNING", "Training request rejected", reason="COCO 2017 is not imported", source=dataset_source)
        raise HTTPException(status_code=409, detail="Prepare COCO 2017 first. Existing image and label files will be reused; missing files are identified before a download starts.")
    if dataset_source in {"coco2017", "combined"} and ml_dataset_import_state.get("status") in {"preparing", "downloading"}:
        reason = "COCO 2017 is still downloading. Wait for the import to finish before using COCO-based training. You can switch to Reviewed camera samples to train from confirmed samples now."
        write_training_log("WARNING", "Training request rejected", reason=reason, source=dataset_source)
        raise HTTPException(status_code=409, detail=reason)
    if dataset_source in {"sama_coco", "sama_combined"} and ("sama-coco" not in load_public_dataset_imports() or not sama_dataset_layout_ready()):
        write_training_log("WARNING", "Training request rejected", reason="SAMA-COCO annotations are not prepared", source=dataset_source)
        raise HTTPException(status_code=409, detail="Prepare SAMA-COCO first. Its annotations will reuse your local COCO images without downloading them again.")
    if dataset_source in {"sama_coco", "sama_combined"} and ml_sama_import_state.get("status") in {"preparing", "downloading"}:
        raise HTTPException(status_code=409, detail="SAMA-COCO preparation is still running. Wait for it to finish before starting training.")
    if not ml_training_lock.acquire(blocking=False):
        reason = "A model training job is already active. Wait for it to finish before starting another."
        write_training_log("WARNING", "Training request rejected", reason=reason, source=dataset_source)
        raise HTTPException(status_code=409, detail=reason)
    initial_model = str(payload.get("initial_model", "auto"))
    if initial_model not in {"auto", "best.pt", "yolo26m"}:
        raise HTTPException(status_code=400, detail="Choose best.pt or YOLO26m as the starting model.")
    ml_training_state.update({"status": "preparing", "message": "Training request accepted. Preparing the selected data and model; the first progress update can take a few minutes.", "stop_requested": False, "progress": 0, "epoch": 0, "batch": 0, "batches": 0, "stats": {}, "device": device_choice.upper().replace(":", " · "), "elapsed_seconds": 0, "eta_seconds": None})
    write_training_log("INFO", "Training requested", source=dataset_source, device=device_choice, checkpoint=initial_model, resource_mode=resource_mode)
    try:
        threading.Thread(target=run_ml_training, args=(device_choice, dataset_source, initial_model, resource_mode), name="vaos-ml-training", daemon=True).start()
    except Exception as exc:
        ml_training_lock.release()
        ml_training_state.update({"status": "error", "message": f"Could not launch the training worker: {exc}"})
        logger.exception("Could not launch the VAOS training worker")
        write_training_log("ERROR", "Training worker could not be launched", error=str(exc)[:400])
        raise HTTPException(status_code=503, detail="VAOS could not start its background training worker. See the Training activity log and admin service error log.") from exc
    return {"status": "preparing", "message": "Training setup started."}


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
    return {"sources": sources, "dataset": {"coco2017_ready": coco_dataset_layout_ready(), "sama_coco_ready": sama_dataset_layout_ready(), "reviewed_count": reviewed_count, "reviewed_types": reviewed_types}, "training": {"status": ml_training_state.get("status", "idle")}, "devices": training_device_inventory()}


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
        dataset_source = str(payload.get("dataset_source", "sama_combined"))
        if dataset_source not in {"combined", "sama_combined"}:
            raise HTTPException(status_code=400, detail="Choose COCO 2017 + reviewed samples or SAMA-COCO + reviewed samples.")
        source_list = list_ml_model_sources(_).get("sources", [])
        source_ids = {item["id"] for item in source_list if isinstance(item, dict) and item.get("id")}
        if checkpoint not in source_ids:
            raise HTTPException(status_code=400, detail="Choose a model source that appears in the available model list.")
        if dataset_source == "sama_combined" and (not sama_dataset_layout_ready() or "sama-coco" not in load_public_dataset_imports()):
            raise HTTPException(status_code=409, detail="Prepare SAMA-COCO first. It reuses the COCO images already saved on this PC.")
        if dataset_source == "combined" and (not coco_dataset_layout_ready() or "coco2017" not in load_public_dataset_imports()):
            raise HTTPException(status_code=409, detail="Prepare COCO 2017 before enrichment. Your local COCO files will be reused.")
        if dataset_source == "sama_combined" and ml_sama_import_state.get("status") in {"preparing", "downloading"}:
            raise HTTPException(status_code=409, detail="Wait for SAMA-COCO preparation to finish before enriching the model.")
        if not ml_training_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="A training run is already active. Wait for it to finish before starting enrichment.")
        lock_acquired = True
        inventory = training_device_inventory()
        device_choice = "gpu:all" if inventory["gpus"] else f"cpu:{inventory['cpu_threads']}"
        device_summary = "all available CUDA GPUs" if inventory["gpus"] else f"CPU · {inventory['cpu_threads']} threads (no NVIDIA GPU detected)"
        dataset_name = "SAMA-COCO + human-reviewed camera samples" if dataset_source == "sama_combined" else "COCO 2017 + human-reviewed camera samples"
        ml_training_state.update({"status": "preparing", "message": f"Model update queued · {dataset_name} · {device_summary}.", "stop_requested": False, "progress": 0, "epoch": 0, "batch": 0, "batches": 0, "stats": {}, "device": device_summary, "elapsed_seconds": 0, "eta_seconds": None, "job_kind": "enrichment"})
        write_training_log("INFO", "Model enrichment requested", checkpoint=checkpoint, dataset=dataset_name, resources="all CUDA GPUs, bounded CPU data workers, managed system memory")
        threading.Thread(target=run_ml_training, args=(device_choice, dataset_source, checkpoint, True), name="vaos-ml-enrichment", daemon=True).start()
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
    try:
        result = await run_in_threadpool(process_frame, token, content)
    except (ValueError, RuntimeError, cv2.error) as exc:
        logger.warning("Frame processing failed for camera %s: %s", token[:6], exc)
        with db_connection() as db:
            db.execute("INSERT INTO camera_status(camera_token,last_frame_at,error) VALUES(?,?,?) ON CONFLICT(camera_token) DO UPDATE SET last_frame_at=excluded.last_frame_at,error=excluded.error", (token, utc_now(), str(exc)[:400]))
        status = 400 if isinstance(exc, ValueError) else 503
        raise HTTPException(status_code=status, detail=(str(exc) if status == 400 else "Local models could not process this frame. Check Model status and diagnostics in the private dashboard.")) from exc
    return JSONResponse(result)


@app.on_event("startup")
def announce_startup():
    logger.info("Dashboard: http://127.0.0.1:8000")
    logger.info("Admin username: admin")
    logger.info("Admin password is stored locally in %s", PASSWORD_PATH)
    logger.info("Models download on first enrollment or incoming camera frame.")
