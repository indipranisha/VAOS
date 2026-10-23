# VAOS (Video Analytics - Operating System)

Video Analytics Operating System

A beginner-oriented proof of concept for receiving sample frames from mobile browser cameras, comparing faces against **explicitly enrolled participants**, and recording repeat sightings. The web app and inference run on the PC that starts the server.

> **Use only with informed permission and appropriate authorisation.** Face templates are biometric data. This is an experiment, not a production surveillance or access-control system. Do not use results as the sole basis for consequential decisions.

## Features in this starter

- Admin dashboard protected by a generated username/password.
- Generate a separate, revocable link for each mobile camera.
- Mobile camera page with a visible notice, permission prompt, and explicit start/stop controls.
- Local face detection (YuNet) and face matching (SFace) through OpenCV.
- Enrol a consenting participant from a clear image or live capture inside the private dashboard, or create a separate single-use QR invitation for phone-based self-enrollment.
- Detect 80 common COCO object categories in sampled frames with Ultralytics YOLO26m; draw live boxes in the camera preview.
- Build a local, human-reviewed custom object detector from the separate **ML training** page. Collection is opt-in and stores object crops only.
- Assign temporary, per-camera object track IDs and show a count-based occupancy estimate.
- Track camera heartbeat, face/match/object counts, and rate-limited recognition/object events in the private dashboard.
- Show model readiness and compute-device diagnostics; run object detection on NVIDIA GPU 0 through PyTorch CUDA when available, with CPU fallback. Face detection and matching use CPU.
- Keep each enrolled person's selected profile photos and numerical face template in the local SQLite database; show their photo gallery in the private dashboard.
- Ignore unknown faces instead of saving them or automatically building profiles for them.
- Save timestamps and match scores for enrolled people, with a 120-second per-camera/person event cooldown.
- Revoke links, delete participant templates, and clear the event log.

## What the POC does not do

- It does not record video or retain source camera frames.
- It does not score people as dangerous, infer emotion, identify license plates, or follow a face between cameras.
- It does not identify people unless they were enrolled by an administrator.
- It does not use a paid API or send images to a hosted AI model.
- It does not implement liveness/anti-spoofing, high availability, multi-user administration, robust audit controls, or production-grade security.

## 1. Requirements

- Windows 10/11 (the included starter script is for Windows).
- Python 3.12 recommended. During setup, enable **Add Python to PATH**.
- Internet access for initial package installation and first-time model download, unless you already have valid local model files.
- A modern mobile browser that supports `getUserMedia`.

## 2. Start the app on Windows

1. Extract this folder to a normal location such as `C:\CameraRecognitionPOC`.
2. Double-click `start_windows.bat`. On first launch it installs `cloudflared` from Cloudflare's official Windows release if needed, starts the camera-only HTTPS Quick Tunnel, then opens the local dashboard. Keep the launcher window open while using the app. Internet access is required for package/model setup and the tunnel.
3. Wait for Python packages to install on the first run. The required camera and face-enrollment packages install first. On NVIDIA systems, the launcher then attempts CUDA-enabled PyTorch and YOLO26m support; if optional object-detection setup fails, the camera app and face enrollment still start with YuNet/SFace.
4. Open `http://127.0.0.1:8000` on the PC. The private dashboard listens on localhost only.
5. The terminal prints a generated five-digit numeric password the first time. Sign in with username `admin`. After signing in, the PIN is also displayed at the top of the private dashboard and saved in `data\admin_password.txt`.
6. Enrol a consenting participant using a clear front-facing image or capture a photo with the dashboard's enrollment camera. Existing valid face models are reused; YOLO26m weights download automatically on first use.
7. Create a camera label, then copy the individual camera link.
8. To let someone create their own profile, use **Phone profile invitation → Create enrollment QR** in the private dashboard. Show or download the QR for them to scan on their phone. The invitation can be used once, expires in 24 hours, and accepts 1–10 photos from the phone camera, photo library, or both. The participant must agree before submitting. Your existing admin enrollment form remains available.

The launcher starts two local services: the admin dashboard on `127.0.0.1:8000` and the camera-only service on port `8001`. It creates a temporary Cloudflare Quick Tunnel only to the camera service, detects the HTTPS URL, and fills it into the dashboard automatically. Each run writes to a separate log folder under `data\logs`; this avoids overwriting logs held by a previous process. Starting the launcher again from the same folder opens its existing dashboard. If an older copy of this app owns either port, the launcher stops only verified Uvicorn camera-app services before starting the selected folder; it reports other programs without stopping them. Press Enter in the active launcher window to stop its services and tunnel. The dashboard and `/api/admin/` endpoints are absent from the public service. The public service exposes only camera stations and the separate QR enrollment page/API; the QR invitation token is single-use and expires after 24 hours.

## 3. Make the camera link work on a phone

Phone browsers generally require the page to be delivered over HTTPS before granting camera access. A URL like `http://127.0.0.1:8000` on a phone points to the phone itself, not your PC. A plain local-network HTTP URL may also be rejected by the browser's camera security rules.

### Temporary HTTPS camera tunnel

The Windows launcher creates the Quick Tunnel and detects its temporary `https://...trycloudflare.com` URL. Once the dashboard opens, create a camera link; its QR code will use that address. Scan it with the phone, tick the authorization notice, choose **Start camera**, and allow camera permission. Use **Choose camera** to select a listed device; stop the stream before switching cameras. Camera labels may appear after permission is granted.

Quick Tunnels are for testing. Anyone with the temporary URL can reach the camera-only service; keep camera-link tokens private. The public hostname changes each time the launcher starts and becomes unavailable when it exits. For a stable or production setup, configure a named Cloudflare Tunnel and appropriate access controls.

**Tunnel privacy warning:** an HTTPS tunnel relays requests between the phone and your PC. Recognition and storage still happen on the PC, but video frames pass through the tunnel provider's infrastructure in transit. Do not use a third-party tunnel for sensitive feeds unless its handling is acceptable to you. For a strict local-only trial, configure a trusted HTTPS endpoint/VPN or use a dedicated isolated network with an appropriate trusted certificate.

When the tunnel URL changes on restart, create a fresh camera link and QR code for the new hostname. Old links contain the previous hostname.

## 4. Test the workflow

1. Enrol yourself or a consenting volunteer from a clean portrait photo.
2. Create a link named `Test phone`.
3. Open it on your phone and start the camera.
4. Keep the face reasonably close, front-facing, and well lit.
5. The mobile page should show a match when it finds an enrolled face above the threshold.
6. The dashboard should show a `FIRST SEEN` event the first time, and `REPEAT` for later logged events. To prevent a large log of identical rows, repeat events are rate-limited to once every two minutes per person per camera.

Try testing across varied light, camera angle, distance, glasses, and partial face occlusion. Face similarity is not a probability and may produce false matches or misses. Tune the threshold only against a small, consented test set. The default threshold is `0.42` cosine similarity and is a starting value, not a validated operating point.

## 5. Local files and data

- `data/app.db`: local SQLite database holding camera tokens, participant names, profile photos, face-template vectors, and event records. Profile photos are only served through the authenticated private dashboard and are removed when the participant is deleted.
- `data/logs/<run-id>/`: separate startup logs for each launcher run.
- Place `face_detection_yunet_2023mar.onnx` and `face_recognition_sface_2021dec.onnx` beside `main.py` to use local files. YOLO26m weights download automatically on first use. Alternatively, set `MODEL_DIR` for the face models.
- `data/models/`: fallback cache for downloaded models.
- `data/admin_password.txt`: generated local dashboard password.
- Each enrolled profile photo is JPEG-encoded and stored as a BLOB inside `data/app.db` so the enrolled participant profile and gallery are visible in the dashboard. Public self-enrollment is optional: a participant may take or upload 1–10 photos, within 6 MB per photo and 30 MB total. All selected images and the merged face template are stored locally after the person checks the consent box. Ongoing camera frames are processed in memory and are not written to disk. Matched event data is persisted until deleted. Protect the database as sensitive biometric data and remove profiles when they are no longer needed.

Back up the database only if necessary and protect it like other sensitive data. Deleting a participant removes their stored template and their associated sighting events. Deleting `data/app.db` removes app data. Models are downloaded again only when no valid local or cached copy is available. YuNet downloads use GitHub LFS media (the ordinary raw URL can return a small pointer); SFace is size- and checksum-checked. Both models are parsed by OpenCV. Enrollment shows a safe useful message while detailed diagnostics go to the admin service console.

## 6. Useful settings

You can set environment variables before starting Uvicorn:

- `MATCH_THRESHOLD` — cosine similarity cutoff, default `0.42`.
- `EVENT_COOLDOWN_SECONDS` — minimum seconds between logged sightings of one person on one camera, default `120`.
- `OBJECT_DETECTOR_DEVICE` — `auto` (default) or `cpu`. Auto uses NVIDIA GPU 0 when PyTorch CUDA is available. Face models currently run on CPU.

The browser camera preview plays at the camera's native frame rate. Face and object analysis sends frames sequentially at up to three samples per second; actual analysis rate depends on camera resolution, network speed, and processing time. The object detector is YOLO26m, a current 80-class COCO model. Ultralytics reports 53.1 COCO validation mAP at 640 pixels for YOLO26m compared with 48.6 for YOLO26s; this benchmark does not guarantee better results in every camera scene, and small or obscured objects may still be missed. On systems with an NVIDIA GPU and a compatible driver, the Windows launcher installs PyTorch CUDA and selects GPU 0. Otherwise, inference falls back to CPU. YOLO26m uses more compute than YOLO26s; the first model load downloads its weights and may take several minutes. Track IDs are temporary, local to one camera, and reset after an object leaves view; they do not link identities across cameras. The occupancy labels use only the current visible person count and are not a danger or behavioral risk score. The face recognizer remains OpenCV Zoo SFace; do not interpret the model choice or similarity score as validated biometric accuracy. Ultralytics software and YOLO26 weights are distributed under the AGPL-3.0 license; review its terms before redistributing or using this package commercially.

### Human-reviewed object training

Open **ML training** from the private dashboard. Turn on collection while a camera is live to save up to 500 object crops locally; the app does not save whole frames. Review each suggestion as **Correct**, **Incorrect** (enter the corrected object name), **Don't know** (excluded), or **Save for later**. The page offers COCO 2017 as its only public dataset. Import is manual and downloads about 20.3 GB (118,287 training and 5,000 validation images) to `data/ml_training/public_datasets/coco2017`; keep about that much free disk space. Choose COCO alone, reviewed samples alone, or a combined COCO + reviewed run. Combined mode requires at least 20 confirmed camera crops across 2 object types. Training starts from the current custom `best.pt` when present (otherwise YOLO26m) and saves the new result as `data/ml_training/active/best.pt`; choose the live detector under dashboard **Settings → Model and services**. COCO runs use 10 epochs; reviewed-only runs use 30. **Stop training** requests a safe stop at the end of the current epoch, after validation and checkpoint save. COCO 2017 is already the source dataset for the shipped YOLO26m detector, so training on it again can take a long time and may not improve results; combining with carefully reviewed samples is the personalization path. More diverse, accurately reviewed views generally produce a more useful model; the resulting model is not automatically validated for safety-critical use.

From **Settings → Model and services**, choose base YOLO26m or your custom `best.pt`. Applying a choice restarts the private admin and camera services and the temporary HTTPS tunnel, then reloads the dashboard. The camera URL may change. The in-app restart control works while VAOS was started using `start_windows.bat` and its launcher window remains open; otherwise, restart VAOS from that batch file.

Example PowerShell command:

```powershell
$env:MATCH_THRESHOLD = "0.45"
$env:EVENT_COOLDOWN_SECONDS = "120"
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Use the default starter script if you do not need to change these values. Set `MODEL_DIR` to the folder containing the named ONNX files before starting to override the project-root location.

## 7. Model licence notes

- YuNet face-detection model: OpenCV Zoo model directory and its model-specific licence: https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
- SFace face-recognition model: OpenCV Zoo's SFace directory specifies Apache 2.0: https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/README.md
- YOLO26m and Ultralytics software: AGPL-3.0. Review the license terms before redistribution or commercial use: https://www.ultralytics.com/license
- OpenCV, FastAPI, and the other dependencies have their own licences. Re-check the relevant terms before any distribution or commercial use.

## 8. Next improvements after the POC

Prioritise: authenticated camera registration, user/consent audit records, encrypted database backups, HTTPS/VPN deployment, configurable retention, face-quality checks, liveness testing, measured false-match/false-non-match rates, and a documented procedure for correcting and deleting data.

