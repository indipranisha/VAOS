"""Public VAOS camera-site ASGI service."""
from __future__ import annotations

import logging
import os
import hmac
import sqlite3
import threading

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from request_limits import RequestBodyLimitMiddleware
from starlette.concurrency import run_in_threadpool

# Prevent importing the private app from creating or logging its admin secret.
os.environ["CAMERA_SERVICE_IMPORT"] = "1"
import main as core

logger = logging.getLogger("vaos.public")
app = FastAPI(title="VAOS Camera Station", version=core.APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(RequestBodyLimitMiddleware)
app.mount("/static", StaticFiles(directory=str(core.APP_DIR / "static")), name="static")

_camera_observability_lock = threading.Lock()
_camera_observability_session: str | None = None
_camera_observability_stop: threading.Event | None = None
_camera_observability_thread: threading.Thread | None = None


def _camera_observability_heartbeat(session_id: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(30):
        try:
            core.observability_store.heartbeat_session(core.DB_PATH, session_id)
        except (OSError, sqlite3.Error, ValueError):
            logger.debug("Could not update the VAOS camera runtime heartbeat", exc_info=True)
        try:
            core.flush_observability_api_requests()
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.debug("Could not flush public-service request counters", exc_info=True)
        try:
            core._record_observability_inference_latency_sample(session_id)
        except (OSError, sqlite3.Error, ValueError, RuntimeError):
            logger.debug("Could not flush public-service frame-processing latency samples", exc_info=True)


@app.on_event("startup")
def record_camera_startup():
    global _camera_observability_session, _camera_observability_stop, _camera_observability_thread
    try:
        session_id, _installation_id = core.observability_store.start_session(core.DB_PATH, component="camera", app_version=core.APP_VERSION)
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.exception("Could not record the VAOS camera runtime session")
        return
    with _camera_observability_lock:
        if _camera_observability_thread and _camera_observability_thread.is_alive() and not (_camera_observability_stop and _camera_observability_stop.is_set()):
            _camera_observability_session = session_id
            return
        stop_event = threading.Event()
        thread = threading.Thread(target=_camera_observability_heartbeat, args=(session_id, stop_event), name="vaos-camera-observability-heartbeat", daemon=True)
        _camera_observability_session = session_id
        _camera_observability_stop = stop_event
        _camera_observability_thread = thread
        thread.start()


@app.on_event("shutdown")
def record_camera_shutdown():
    global _camera_observability_session, _camera_observability_stop, _camera_observability_thread
    with _camera_observability_lock:
        session_id = _camera_observability_session
        stop_event = _camera_observability_stop
        thread = _camera_observability_thread
        _camera_observability_session = None
        _camera_observability_stop = None
        _camera_observability_thread = None
        if stop_event:
            stop_event.set()
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2)
    try:
        for _ in range(100):
            persisted = core.flush_observability_api_requests(max_events=5000)
            with core.observability_api_lock:
                memory_fallback_pending = bool(core.observability_api_aggregates or core.observability_frame_aggregates)
            durable_fallback_pending = core.observability_counter_fallback_pending()
            if not persisted and not memory_fallback_pending and not durable_fallback_pending:
                break
            if not persisted:
                if durable_fallback_pending:
                    logger.warning("Some camera-service request/frame counters remain in the durable secondary queue and will be retried on the next start")
                elif memory_fallback_pending:
                    logger.warning("Some camera-service request/frame counters remain only in bounded memory and may be lost on process exit")
                break
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        logger.warning("Some camera-service request counters could not be flushed at shutdown", exc_info=True)
    if session_id:
        try:
            core._record_observability_inference_latency_sample(session_id)
            core.observability_store.end_session(core.DB_PATH, session_id)
        except (OSError, sqlite3.Error, ValueError):
            logger.exception("Could not mark the VAOS camera runtime session as cleanly stopped")


@app.middleware("http")
async def pause_public_camera_access(request: Request, call_next):
    # Keep cloudflared connected so the public URL remains stable while paused;
    # block camera and enrollment endpoints until the admin resumes access.
    with core.observability_request_context(request) as correlation_id:
        if core.public_camera_paused() and not request.url.path.startswith("/internal/") and request.url.path != "/health":
            if request.url.path.startswith("/api/"):
                core.queue_observability_api_request("unmatched", request.method, 503)
            response = JSONResponse({"detail": "Public camera access is temporarily paused by the administrator."}, status_code=503)
        else:
            response = await call_next(request)
            if request.url.path.startswith("/api/"):
                route = getattr(request.scope.get("route"), "path", None)
                core.queue_observability_api_request(route or "unmatched", request.method, response.status_code)
                if response.status_code == 200 and route == "/api/camera/{token}/frame":
                    parts = request.url.path.split("/")
                    if len(parts) >= 5 and parts[3]:
                        core.queue_observability_inference_frame(parts[3])
        response.headers[core.OBSERVABILITY_CORRELATION_HEADER] = correlation_id
        return response


@app.get("/health")
def health():
    return {"status": "ok", "app": "vaos-camera", "instance_id": core.INSTANCE_ID, "version": core.APP_VERSION}


@app.get("/internal/model-status")
def internal_model_status(request: Request):
    expected = os.getenv("VAOS_INTERNAL_STATUS_TOKEN", "")
    supplied = request.headers.get("x-vaos-internal-token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=404, detail="Not found")
    return core.model_runtime_status()


@app.get("/c/{token}", response_class=HTMLResponse)
def camera_page(token: str, request: Request):
    return core.camera_page(token, request)


@app.get("/enroll/{token}", response_class=HTMLResponse)
def enrollment_page(token: str, request: Request):
    return core.public_enrollment_page(token, request)


@app.post("/api/enroll/{token}")
async def enroll_participant(
    token: str,
    name: str = Form(...),
    consent: str = Form(...),
    photos: list[UploadFile] = File(...),
):
    return await core.create_public_participant(token, name, consent, photos)


@app.post("/api/camera/{token}/frame")
async def receive_frame(token: str, request: Request):
    # Delegate to the shared implementation while exposing no admin routes.
    return await core.receive_frame(token, request)
