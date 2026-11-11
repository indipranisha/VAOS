# Release notes

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
