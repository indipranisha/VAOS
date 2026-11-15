# Application lifecycle and uptime

VAOS records installation identity and runtime sessions in its existing SQLite database. The first successful observability initialization creates one installation UUID and stores the first-start timestamp. Each admin or camera process has its own session with component name, process ID, an opaque process-incarnation ID, application version, start time, heartbeat time and shutdown state. The incarnation ID is generated once per Python process and distinguishes a fast restart even if Windows reuses the same PID.

## Session states

- **Running:** startup was recorded and heartbeats are still current.
- **Clean:** VAOS recorded its shutdown hook and end time.
- **Unknown:** a session stopped heartbeating for at least 120 seconds before a later startup reconciled it. This is not proof of a crash; the machine may have powered off or the process may have been terminated without a clean hook.

The heartbeat normally runs about every 30 seconds. A missing heartbeat is not changed directly into a confirmed crash. VAOS records a reconciliation event and preserves the last known heartbeat. A fresh session with the same component and PID but a different incarnation ID is also reconciled as **Unknown** immediately; that identifies a new process using a reused PID, without claiming why the old process ended. Repeated startup recording by the same process incarnation remains idempotent.

## Uptime interpretation

The lifetime uptime total is the union of observed admin and camera process intervals, so overlap is not counted twice. A clean session uses its recorded stop time. An unknown session contributes only through its last heartbeat. Therefore uptime can be a lower bound when shutdown was not observed; it is not wall-clock availability or proof that a user could reach the application.

Installation start counts and clean-stop counts are durable counters. Session history is separately retained and cursor-paginated. First-start time, session start, heartbeat, clean stop, application version and component are stored in UTC. Schema v11 adds a nullable `process_instance_id` to preserve existing session rows; on the first post-upgrade start, a matching legacy row with no incarnation ID is classified as Unknown, and the verified pre-change backup migration path is used for non-empty databases. Incarnation IDs are internal deduplication values and are not returned by the lifecycle API.

## API and terminal access

- `GET /api/admin/observability/lifecycle` and `/api/v1/admin/observability/lifecycle` return up to 200 sessions, with `after`, `before` and opaque `cursor` filters. The `after` boundary is inclusive; `before` is exclusive.
- `GET /api/admin/observability/overview` returns lifetime or bounded-period KPIs. It identifies unavailable metrics instead of treating missing observations as zero.
- `vaos status` shows current service health; `vaos stats --period lifetime` returns the durable totals. Use `vaos --help` for connection and output-format options.

The lifecycle panel and report use the same stored sessions. Historical records are not fabricated for time before the observability store was initialized. See [the validation record](validation.md) for migration and restore evidence and remaining release gates.
