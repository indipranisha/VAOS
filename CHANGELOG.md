# Release notes

## 1.1.0

- Add a stable `/api/v1` API alias, authenticated OpenAPI document, and administrator-only interactive API reference.
- Add expiring, revocable read/control API tokens stored as hashes, per-token rate limits, and a bounded audit trail that omits request bodies and secrets.
- Add Settings controls for API access and optional MCP agent access, including read-only mode, capability groups, sensitive-action and deletion gates.
- Add an optional stdio MCP bridge with guarded JSON, multipart, and binary API tools plus OpenAPI and live-policy resources; keep the MCP SDK out of core dependencies.
- Document the API, authentication, endpoint groups, MCP setup, security controls, and operating limits.
- Keep VAOS services and active model training untouched during implementation; changes take effect after the next planned service start.

## 1.0.4

- Add a Video Review page with an official YouTube viewer and fast playback at each video's supported rate.
- Analyze authorized local video files in the browser, draw object boxes on sampled frames, and add cropped detections to the existing review queue without uploading or retaining the full video.
- Record video timestamps on queued samples and keep video review sources out of the camera list and live-camera dashboard.
- Pause video analysis while model training is active.

## 1.0.3

- Route the legacy Live System address into the unified Control Room overview so the dashboard and live system no longer appear as separate views.

## 1.0.2

- Replace legacy short local admin passwords with a random 32-byte credential before startup.
- Print the current admin credential after an in-app service reload so it remains recoverable from the launcher.

## 1.0.1

- Add separate Settings controls for a public HTTPS link to the main admin dashboard, with an independently managed URL and tunnel process from the camera tunnel.
- Keep the dashboard tunnel connected through in-app service reloads and stop it when the Windows launcher exits.
- Throttle repeated failed admin sign-in attempts.
- Separate dashboard and Settings navigation, tighten training and activity layouts, and improve compact training/model summaries.

## 1.0.0

- Adopt VAOS as the product identity across the app, launcher and setup guides.
- Add a Docker Compose deployment baseline with separated administrator and camera services, persistent storage and automatic HTTPS via Caddy.
- Require a strong administrator secret, internal service token and HTTPS camera URL in production mode.
- Add the configurable data directory and production camera-service address for container networking.
- Document Windows startup, production operations, privacy obligations and model licensing.
