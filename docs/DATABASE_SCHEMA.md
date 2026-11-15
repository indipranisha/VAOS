# VAOS database schema overview

VAOS uses SQLite. The application schema is created and upgraded by `init_db()` in `main.py`; it is not maintained as a set of ORM models or a separate migration directory. Feature 8 observability tables use an additive schema ledger and verified backup path. Check the active source and `PRAGMA user_version`/schema tables before writing migration tooling against a live database.

## Core tables

| Table | Purpose and key relationships |
| --- | --- |
| `cameras` | Camera token, label, active state, map/heading and source metadata. Token is the primary key. |
| `participants` | Enrolled participant name, face embedding, optional profile photo and creation time. |
| `participant_photos` | Additional photos keyed to a participant; deleting the participant cascades to these records. |
| `enrollment_links` | Expiry, use and revocation state for public enrollment invitations. |
| `sightings` | Timestamped face-match result referencing a camera and participant. |
| `camera_status` | Latest frame timestamp, face/match counts, object summary and error state for a camera. |
| `object_sightings` | Timestamped detected object class/count/confidence per camera. |
| `object_event_rules` | Camera-scoped or global object count/dwell rule configuration. |
| `object_rule_events` | Recorded rule trigger, retaining a safe rule name/type/class/count and message. Rule deletion sets the historical rule reference to null. |
| `app_settings` | Application settings as key/value text. Values may include sensitive configuration; protect the database. |
| `api_tokens` | Token hash, prefix, label, scopes, expiry and revocation metadata. Raw token secrets are not stored. |
| `api_access_log` | Bounded API activity metadata (timestamp, token prefix, method, path); it does not store request bodies. |
| `mcp_approvals` | Exact-request approval fingerprint, safe preview, status, expiry and decision/usage metadata bound to a token hash. |
| `ml_samples` | Captured image bytes, suggested/confirmed label, box, confidence and review state; references a camera. This is sensitive image data. |

Additional dataset/training state and observability schemas may be created by their respective modules or application migrations. Consult `observability/database-schema.md` for the observability tables, retention and backup/rollback procedure. Do not assume this compact overview lists every column in every schema version.

## Change and backup rules

- Add migrations rather than editing live database files by hand.
- Preserve existing rows and verify foreign-key behavior with a disposable copy.
- Before a schema change, make a consistent SQLite backup and verify its integrity.
- Test both upgrade and restore using representative supported prior schemas.
- Do not copy production biometric records into unit-test fixtures. Tests should use temporary databases and synthetic data.
- Database backups, WAL/SHM sidecars, exports and copied `data` directories can all contain sensitive records; store and delete them accordingly.

Relevant implementation: `main.py:init_db`, `observability_store.py`, `observability_reports.py`, `mcp_server.py`. For architecture and release gates, see [architecture](ARCHITECTURE.md) and [testing](TESTING.md).
