# VAOS 1.2.18 release notes

This release consolidates changes since 1.2.6.

- Organized Settings into clear sections while preserving existing options.
- Fixed Projects scrolling and added encrypted project export/import using a versioned VAOS envelope, AES-256-GCM and scrypt. Export is now POST with name/passphrase JSON. Import requires a matching eight-character alphanumeric passphrase. Saved local configurations remain unencrypted.
- Added CPU video analysis and 8x, 16x, 32x and 64x review speeds; high speeds may skip brief detections.
- Added review preview navigation, arrow keys, detection-box overlays, sample metadata and five review actions.
- Added a configurable 1–10 second training status refresh interval (default 5), recent-speed ETA calculations and approximate per-epoch image counts.
- Added an isolated COCO person preparation/training helper. It runs outside the application's training lock; do not run it alongside another GPU training job. It is not automatically promoted to the active detector.

## Upgrade

Install requirements.txt into the existing virtual environment. Restart services after active training finishes, then refresh browser pages. New templates may load before backend restart; encrypted export and box metadata require the updated backend. Existing saved bundles remain readable; plaintext bundle uploads are no longer accepted.

## Validation

API security, project bundle and UI shell checks cover the changes. Browser checks covered Settings, Projects scrolling and review navigation/details. No guarantee is made that increasing RAM allocation improves throughput. Active training checkpoints and machine-specific model metadata are excluded from this release commit.
