# Camera, streaming and enrollment

VAOS uses a separate camera-site service for browser camera pages, frame submission and public enrollment. Local development runs the admin service on port 8000 and camera service on port 8001. Production puts both behind HTTPS reverse-proxy routes; the administrator hostname must have its own network access policy.

## Camera link lifecycle

1. An administrator creates a camera entry and a camera URL/QR in the admin UI.
2. Open the camera page on the intended device and grant browser camera permission.
3. Keep the link private. Revoke it when the device is retired, exposed or no longer authorized.
4. The camera page submits frames to the camera service. The service delegates inference and persistence to shared application logic and reports service health separately from detection outcomes.
5. Confirm camera health and recent events in the admin UI; a reachable page alone does not prove frames are being processed.

An enrollment invitation is a separate one-time/expiring link. The person must be informed and explicitly consent before a profile is created. Do not collect face data from people who have not consented or where you lack legal authority.

## Networks and HTTPS

Local services bind to loopback in the documented development setup. Do not change them to a public interface without an authenticated, HTTPS reverse proxy and firewall/access policy. The Windows launcher can create a temporary HTTPS tunnel for camera access; its URL may change and is not a production identity or access-control layer. Do not use a public tunnel to expose the private admin service or MCP stdio process.

For production, configure the separate admin and camera hostnames, TLS termination and protected administrator network using the [production setup](../README.md). Remote API requests must use HTTPS. Configure trusted proxy CIDRs narrowly if forwarded scheme headers are needed.

## Data handling

Camera frames may be transiently processed, while reviewed samples, participant photos, face embeddings, sightings and related records may be persisted under `VAOS_DATA_DIR`. Confirm configured behavior in the active source build and its settings before deployment. Keep data access restricted, define retention and deletion rules, and test consent withdrawal/deletion behavior. Backups contain sensitive information and require equivalent protection.

## Troubleshooting

- **Camera permission denied:** grant permission for the exact HTTPS origin in the browser/device settings, then reload the camera page.
- **Phone cannot load the page:** confirm the phone can reach the camera hostname/tunnel, check TLS and the configured public camera URL, and verify the camera service health.
- **Page loads but no detections appear:** inspect service health, model readiness and frame errors. Check supported model dependencies and available hardware.
- **Enrollment fails:** verify that the invitation is valid, unused and unexpired, that required consent was submitted, and that uploaded images meet the current validation rules.

For endpoint details, see [camera APIs](API.md). For protection and privacy obligations, see [Security and privacy](SECURITY_PRIVACY.md).
