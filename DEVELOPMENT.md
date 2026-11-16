# VAOS development guide

Keep changes focused, preserve existing data and launcher behavior, and update the user and production documentation when routes, configuration, deployment or data handling change.

## Local checks

- Start the Windows build with `start_windows.bat` and confirm `/health` reports the expected VAOS version.
- Confirm the private dashboard requires administrator authentication.
- Use a consenting test participant and a test camera link; verify camera start/stop, event recording and revocation.
- For model, dataset or training changes, check diagnostics, retained data paths and safe-stop behavior.
- Never commit `data`, secrets, downloaded weights, datasets or logs.

## Privacy and operations

Face templates and selected profile photos are sensitive. Keep enrollment consent explicit, unknown faces unpersisted, and source frames transient. Preserve revocation and deletion controls. Do not send camera frames or face templates to hosted AI services by default. Before deployment, protect the administrator hostname, secure backups, define retention procedures, review dependency/model licenses and validate in staging.
