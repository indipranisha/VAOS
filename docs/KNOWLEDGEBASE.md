# VAOS knowledgebase — 1.2.18

See [release notes](RELEASE_NOTES_1.2.18.md) for current behavior and upgrade requirements.

## Training

Settings → Detection → Processing and performance → Training status refresh controls polling from 1 to 10 seconds. Save controls and refresh Training. Older services use a browser preference fallback. ETAs use recent batch speed over up to two minutes; validation/checkpoint time is additional. Image counts are approximate from batch size.

Review detections opens a zoom dialog with Previous/Next, arrow keys, metadata and Approve, Reject, Correct label, Don’t know and Save for later. Boxes require updated backend metadata. Browsing follows the current filtered page.

Only one GPU training job should run at a time. Standalone tools do not share the application's lock. Resume from a checkpoint after verifying no other GPU training run is active.

## Project bundles

See [bundle guide](PROJECT_BUNDLES.md). Export/import uses an eight-character case-sensitive alphanumeric passphrase. Forgotten passphrases cannot be recovered. Imported configuration is stored unencrypted locally. A Method Not Allowed export response means the backend needs restarting after jobs finish.
