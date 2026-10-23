"""Public camera-only ASGI service. Keep the admin app on localhost:8000."""
from __future__ import annotations

import logging
import os
import hmac

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

# Prevent importing the private app from creating or logging its admin secret.
os.environ["CAMERA_SERVICE_IMPORT"] = "1"
import main as core

logger = logging.getLogger("camera-poc.public")
app = FastAPI(title="Camera Station", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(core.APP_DIR / "static")), name="static")


@app.middleware("http")
async def pause_public_camera_access(request: Request, call_next):
    # Keep cloudflared connected so the public URL remains stable while paused;
    # block camera and enrollment endpoints until the admin resumes access.
    if core.public_camera_paused() and not request.url.path.startswith("/internal/") and request.url.path != "/health":
        return JSONResponse({"detail": "Public camera access is temporarily paused by the administrator."}, status_code=503)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "app": "camera-station", "instance_id": core.INSTANCE_ID}


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
