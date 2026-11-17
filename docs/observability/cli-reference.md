# `vaos` command-line client

The optional standard-library Python CLI calls the same authenticated VAOS REST API. It does not open the VAOS SQLite database directly and does not bypass API scopes. Its entry point is defined in `pyproject.toml`.

## Install

From a checkout, with Python 3.10 or later:

```sh
python -m pip install .
```

For a development checkout without installing:

```sh
python vaos_cli.py --help
```

Set `VAOS_URL` if VAOS is not at `http://127.0.0.1:8000`. Set `VAOS_API_TOKEN` in the current process environment or a protected secret store. Alternatively, `VAOS_USERNAME` and `VAOS_PASSWORD` can provide dashboard Basic authentication; if no API token or password is set, the CLI prompts with hidden input. It never accepts a token as a command-line option. Remote non-loopback API origins must use HTTPS; plain HTTP is accepted only for local loopback.

## Commands

```text
vaos status [--format text|json|ndjson|csv] [--output FILE] [--timezone ZONE]
vaos stats [overview|metrics] [--period lifetime|24h|7d|30d] [--from TIME --to TIME]
          [--metric KEY] [--group-by none|hour|day] [output options]
vaos logs list|search|cat|tail [filters] [--limit N] [output options]
vaos reports list [--limit N] [output options]
vaos reports export statistics|security-events|logs|lifecycle|metric-series|training|datasets|models|inference|cameras|tracking-events|hardware-energy|api-activity|mcp-activity
          [--format pdf|csv] --output FILE [--metric KEY] [--actor-ref REF] [--from TIME --to TIME]
vaos integrations status [output options]
```

### Examples

```sh
vaos status --format json
vaos stats --period lifetime
vaos stats --period 7d --format csv --output weekly.csv
vaos stats metrics --metric hardware.cpu_utilization_percent --from 2026-11-01 --to 2026-11-12 --group-by day
vaos stats metrics --metric api.request_latency_p95_ms --from 2026-11-11 --to 2026-11-12 --group-by hour
vaos stats metrics --metric inference.camera_frame_processing_latency_p95_ms --from 2026-11-11 --to 2026-11-12 --group-by hour
vaos stats metrics --metric training.job_duration_seconds --from 2026-11-11 --to 2026-11-12 --dimension training_job_id=vaos_objects_20261111_120000_a1b2c3
vaos stats metrics --metric api.requests --from 2026-11-11 --to 2026-11-12 --group-by hour --dimension method=GET
vaos logs search --component training --level error --limit 100 --format ndjson
vaos logs search --correlation-id run-abc123 --format ndjson
vaos logs list
vaos logs search --training-job-id vaos_objects_20261111_120000_a1b2c3 --dataset-id uvh26 --model-id best.pt --format ndjson
vaos logs search --session-id 2bb84936-bc96-40a3-9cca-c32ec886a523 --format ndjson
vaos logs tail --follow --component training --format ndjson
vaos logs tail --component camera --output camera-events.txt
vaos reports export security-events --format csv --from 2026-11-01 --to 2026-11-12 --output security.csv
vaos reports export statistics --format pdf --output statistics.pdf
vaos reports export security-events --format csv --from 2026-11-01 --to 2026-11-12 --actor-ref api-token:abc123 --output actor-audit.csv
vaos reports export logs --format csv --from 2026-11-01 --to 2026-11-12 --session-id 2bb84936-bc96-40a3-9cca-c32ec886a523 --correlation-id run-abc123 --output run-events.csv
vaos reports export logs --format csv --from 2026-11-01 --to 2026-11-12 --training-job-id vaos_objects_20261111_120000_a1b2c3 --dataset-id uvh26 --model-id best.pt --output training-run.csv
vaos reports export training --format csv --from 2026-11-01 --to 2026-11-12 --output training.csv
vaos reports export api-activity --format pdf --from 2026-11-01 --to 2026-11-12 --output api-activity.pdf
vaos reports export tracking-events --format csv --from 2026-11-01 --to 2026-11-12 --search truck --output truck-events.csv
vaos integrations status --format json
```

`--from` and `--to` accept ISO timestamps or local date/time values. A date-only boundary covers that whole calendar day in `--timezone`; it is converted to UTC before the API request. Returned timestamps remain UTC unless a timezone is specified, in which case UTC keys are renamed to `_local`.

`logs cat` follows all cursor pages up to the 5,000 row safety limit. `logs list` and `logs search` return one bounded page. `logs tail` prints the newest `--limit` events first (50 by default), then polls for newer events until interrupted with Ctrl+C. It uses cursor pagination and an inclusive timestamp watermark, and suppresses event IDs already printed so bursts larger than one API page are not silently skipped or repeated. Its in-memory deduplication window is capped at 5,000 event IDs, and one catch-up poll is capped at 10,000 API rows; if a poll exceeds that bound, the command stops with an error rather than silently dropping records. Text can be appended to a file. A live stream supports text or NDJSON; JSON and CSV are available with `--no-follow` for a one-time snapshot. Metrics require both range boundaries. Report export waits up to 300 seconds by default and saves the completed file atomically; use `reports list` to inspect a job that did not finish before the wait bound.

## Shell pipelines

These examples use grep on the CLI's bounded event-store output. The CLI command remains permission-checked; the pipe does not read raw service logs:

```sh
vaos logs cat --format text | grep -Ei 'cuda|error|failed'
vaos logs cat --format ndjson | grep 'training'
```

`logs tail` follows by default; `--follow` makes that behavior explicit, and `--no-follow` performs one bounded snapshot and exits. The tail uses event timestamps for its overlap window; an event inserted later with a timestamp older than the retained 5,000-ID window may not appear in the live stream. Use `logs search` or `logs cat` for a historical reconciliation.

`--session-id`, `--correlation-id`, `--training-job-id`, `--dataset-id` and `--model-id` are exact filters for `logs` commands and `reports export logs`; the CLI rejects those identifier filters for unrelated report types. Job, dataset and model filters only match identifiers recorded in safe event details. Admin and camera requests now receive a validated request-correlation ID (or a generated one) in the response, and request-scoped events use it. Older records and unrelated background events can still lack a correlation ID; this does not provide distributed tracing.

Severity names for `--level` are case-insensitive; VAOS records and returns the canonical uppercase level.

`reports export security-events --actor-ref REF` matches the exact recorded audit actor reference (up to 120 characters); it is rejected for other report types. The value may represent a local service label, API-token prefix or hash and is not necessarily a verified person or tenant.

Metric queries support gauges, histograms and counters. Repeat `--dimension key=value` to constrain a series to exact registered dimensions. The CLI refuses to aggregate a query when the API reports more than the newest 1,000 points, avoiding partial totals; narrow the time range or add a dimension filter and retry.

## Exit codes

When `--output` is not used, successful formatted results go to standard output, so they can be piped into tools such as `grep`. Usage, authentication, authorization and service errors are written to standard error. Closing a downstream pipe is treated as a normal exit.

| Code | Meaning |
| --- | --- |
| `0` | Success, including clean pipe closure. |
| `2` | Command or argument usage problem. |
| `3` | Authentication, authorization or request failure. |
| `4` | Service unavailable or transport failure. |

Use `vaos --help`, `vaos stats --help` and `vaos logs --help` for generated details. This initial CLI has no command for changing settings, starting training, deleting data or executing agent actions.

## Linux service logs

The CLI reads VAOS's authorized, persisted event API. `journalctl` reads the operating-system service output; these are different sources, and plain-text service output is not automatically imported into the VAOS event store. VAOS does not install a systemd unit, so substitute the unit name configured by the administrator:

```sh
VAOS_UNIT='<installed-vaos-unit-name>'
systemctl status "$VAOS_UNIT"
journalctl -u "$VAOS_UNIT" --since '1 hour ago' --output cat
journalctl -fu "$VAOS_UNIT"
```

For remote CLI use, set `VAOS_URL` to an HTTPS origin and obtain credentials from a protected secret store or the CLI's hidden password prompt. Do not put API tokens or passwords in command arguments, shell history, or an unprotected systemd unit file. The CLI does not manage service units or provision credentials.
