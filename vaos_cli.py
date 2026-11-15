"""Small standard-library CLI client for the VAOS authenticated API."""
from __future__ import annotations

import argparse
import base64
import csv
import getpass
import io
import json
import os
import sys
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_SERVICE = 4
TAIL_SEEN_EVENT_LIMIT = 5_000
TAIL_POLL_EVENT_LIMIT = 10_000


class VaosCliError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_SERVICE):
        super().__init__(message)
        self.exit_code = exit_code


def _iso_boundary(value: str, *, end: bool = False, timezone_name: str = "UTC") -> str:
    """Convert a date/time boundary from the selected zone to canonical UTC."""
    try:
        zone = ZoneInfo(timezone_name)
        if len(value) == 10:
            parsed_date = date.fromisoformat(value)
            if end:
                parsed_date += timedelta(days=1)
            parsed = datetime.combine(parsed_date, datetime_time.min, zone).astimezone(timezone.utc)
        else:
            text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=zone)
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError as exc:
        raise VaosCliError(f"Invalid date/time {value!r}: {exc}", EXIT_USAGE) from exc
    except ZoneInfoNotFoundError as exc:
        raise VaosCliError(f"Unknown timezone {timezone_name!r}.", EXIT_USAGE) from exc


class Client:
    def __init__(self, url: str | None = None):
        self.base = (url or os.getenv("VAOS_URL") or "http://127.0.0.1:8000").rstrip("/")
        parts = urlsplit(self.base)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise VaosCliError("VAOS_URL must be an http(s) origin without credentials, query or fragment.", EXIT_USAGE)
        if parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise VaosCliError("Remote VAOS connections require HTTPS.", EXIT_USAGE)
        self.token = os.getenv("VAOS_API_TOKEN", "").strip()
        self.auth = None
        if not self.token:
            user = os.getenv("VAOS_USERNAME", "admin")
            password = os.getenv("VAOS_PASSWORD")
            if password is None:
                password = getpass.getpass("VAOS admin password (not saved): ")
            self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")

    def request(self, method: str, path: str, *, query: dict[str, Any] | None = None, payload: dict[str, Any] | None = None, binary: bool = False) -> Any:
        url = f"{self.base}/api/v1{path}"
        if query:
            url += "?" + urlencode({key: value for key, value in query.items() if value is not None and value != ""})
        headers = {"Accept": "application/json, application/pdf, text/csv", "User-Agent": "vaos-cli/1.1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.auth:
            headers["Authorization"] = self.auth
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=20) as response:
                data = response.read()
                if binary:
                    return data, response.headers
                if not data:
                    return {}
                return json.loads(data.decode("utf-8"))
        except HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                message = error_payload.get("detail", error_payload.get("message", str(exc)))
            except Exception:
                message = str(exc)
            exit_code = EXIT_AUTH if exc.code in {401, 403, 428} else EXIT_SERVICE
            raise VaosCliError(f"VAOS returned HTTP {exc.code}: {message}", exit_code) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise VaosCliError(f"Could not reach VAOS at {self.base}: {exc}") from exc


def _range_args(args: argparse.Namespace) -> dict[str, str]:
    result: dict[str, str] = {}
    zone = getattr(args, "timezone", "UTC")
    if getattr(args, "from_time", None):
        result["after"] = _iso_boundary(args.from_time, timezone_name=zone)
    if getattr(args, "to_time", None):
        result["before"] = _iso_boundary(args.to_time, end=len(args.to_time) == 10, timezone_name=zone)
    if result.get("after") and result.get("before") and result["after"] >= result["before"]:
        raise VaosCliError("--from must be earlier than --to.", EXIT_USAGE)
    return result


def _localize_times(data: Any, timezone_name: str) -> Any:
    if timezone_name.upper() == "UTC":
        return data
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise VaosCliError(f"Unknown timezone {timezone_name!r}.", EXIT_USAGE) from exc
    timestamp_names = {"timestamp", "started_at", "last_heartbeat_at", "clean_shutdown_at", "requested_at", "expires_at", "updated_at", "first_started_at", "last_success_at"}
    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            converted = {}
            for key, item in value.items():
                output_key = key
                if isinstance(item, str) and (key in timestamp_names or key.endswith("_at_utc")):
                    try:
                        parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
                        if parsed.tzinfo:
                            item = parsed.astimezone(zone).isoformat(timespec="seconds")
                            if key.endswith("_utc"):
                                output_key = key[:-4] + "_local"
                    except ValueError:
                        pass
                converted[output_key] = visit(item)
            return converted
        return value
    return visit(data)


def _write_output(data: Any, fmt: str = "text", output: str | None = None, timezone_name: str = "UTC") -> None:
    data = _localize_times(data, timezone_name)
    if fmt == "json":
        text = json.dumps(data, indent=2, ensure_ascii=False)
    elif fmt == "ndjson":
        rows = data if isinstance(data, list) else [data]
        text = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    elif fmt == "csv":
        rows = data if isinstance(data, list) else [data]
        stream = io.StringIO(newline="")
        if rows and all(isinstance(row, dict) for row in rows):
            fields = list(dict.fromkeys(key for row in rows for key in row))
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        else:
            writer = csv.writer(stream)
            for row in rows:
                writer.writerow([row])
        text = stream.getvalue()
    elif fmt == "text":
        if isinstance(data, (dict, list)):
            text = json.dumps(data, indent=2, ensure_ascii=False)
        else:
            text = str(data)
    else:
        raise VaosCliError(f"Unsupported output format: {fmt}", EXIT_USAGE)
    if output:
        Path(output).expanduser().write_text(text, encoding="utf-8", newline="")
    else:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")


def _write_text_lines(lines: list[str], output: str | None = None, *, append: bool = False) -> None:
    text = "\n".join(lines)
    if text:
        text += "\n"
    if output:
        path = Path(output).expanduser()
        if append:
            with path.open("a", encoding="utf-8", newline="") as stream:
                stream.write(text)
        else:
            path.write_text(text, encoding="utf-8", newline="")
    else:
        sys.stdout.write(text)


def _fetch_events(client: Client, args: argparse.Namespace, *, limit: int, all_pages: bool = False) -> list[dict[str, Any]]:
    query: dict[str, Any] = {**_range_args(args), "limit": min(200, limit)}
    for arg, key in (("component", "component"), ("level", "level"), ("event_type", "event_type"), ("session_id", "session_id"), ("correlation_id", "correlation_id"), ("training_job_id", "training_job_id"), ("dataset_id", "dataset_id"), ("model_id", "model_id"), ("search", "search")):
        value = getattr(args, arg, None)
        if value:
            query[key] = value
    results: list[dict[str, Any]] = []
    while len(results) < limit:
        query["limit"] = min(200, limit - len(results))
        page = client.request("GET", "/admin/observability/events", query=query)
        items = page.get("items", [])
        results.extend(items)
        if not all_pages or not page.get("has_more") or not page.get("next_cursor"):
            break
        query["cursor"] = page["next_cursor"]
    return results[:limit]


def _event_time(item: dict[str, Any]) -> datetime:
    value = item.get("timestamp")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise VaosCliError("The event API returned an invalid timestamp.") from exc
    if parsed.tzinfo is None:
        raise VaosCliError("The event API returned a timestamp without a timezone.")
    return parsed.astimezone(timezone.utc)


def _tail_filters(args: argparse.Namespace) -> dict[str, Any]:
    query: dict[str, Any] = _range_args(args)
    for arg, key in (
        ("component", "component"), ("level", "level"), ("event_type", "event_type"),
        ("session_id", "session_id"), ("correlation_id", "correlation_id"),
        ("training_job_id", "training_job_id"), ("dataset_id", "dataset_id"),
        ("model_id", "model_id"), ("search", "search"),
    ):
        value = getattr(args, arg, None)
        if value:
            query[key] = value
    return query


def _fetch_tail_updates(
    client: Client,
    args: argparse.Namespace,
    seen: dict[str, dict[str, Any]],
    *,
    empty_start: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read the bounded event window newer than the oldest retained tail ID."""
    query = _tail_filters(args)
    if seen:
        oldest_seen = min((_event_time(item) for item in seen.values()))
        watermark = oldest_seen.isoformat(timespec="microseconds").replace("+00:00", "Z")
        supplied_after = query.get("after")
        if supplied_after:
            supplied_dt = datetime.fromisoformat(str(supplied_after).replace("Z", "+00:00")).astimezone(timezone.utc)
            watermark_dt = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
            if supplied_dt > watermark_dt:
                watermark = str(supplied_after)
        query["after"] = watermark
    elif empty_start is not None:
        watermark = empty_start.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        supplied_after = query.get("after")
        if supplied_after:
            supplied_dt = datetime.fromisoformat(str(supplied_after).replace("Z", "+00:00")).astimezone(timezone.utc)
            watermark_dt = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
            if supplied_dt > watermark_dt:
                watermark = str(supplied_after)
        query["after"] = watermark

    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page_query = {**query, "limit": min(200, TAIL_POLL_EVENT_LIMIT - len(rows))}
        if cursor:
            page_query["cursor"] = cursor
        page = client.request("GET", "/admin/observability/events", query=page_query)
        items = page.get("items", [])
        if not isinstance(items, list):
            raise VaosCliError("The event API returned an invalid page.")
        rows.extend(item for item in items if isinstance(item, dict))
        has_more = bool(page.get("has_more"))
        next_cursor = page.get("next_cursor")
        if not has_more:
            break
        if not next_cursor:
            raise VaosCliError("The event API indicated more tail records without a continuation cursor.")
        if len(rows) >= TAIL_POLL_EVENT_LIMIT:
            raise VaosCliError(
                "The log tail is more than 10,000 events behind in one poll. Add a component or time filter, "
                "or restart the tail from a narrower range; VAOS stopped instead of silently skipping records."
            )
        cursor = str(next_cursor)
    return rows


def _bounded_tail_seen(
    seen: dict[str, dict[str, Any]], additions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    combined = dict(seen)
    for item in additions:
        event_id = str(item.get("event_id") or "")
        if not event_id:
            raise VaosCliError("The event API returned a record without an event ID.")
        combined[event_id] = item
    ordered = sorted(combined.values(), key=lambda item: (_event_time(item), str(item.get("event_id", ""))))
    if len(ordered) <= TAIL_SEEN_EVENT_LIMIT:
        return {str(item["event_id"]): item for item in ordered}

    # Keep the newest bounded window without splitting a timestamp group. The
    # next poll uses an inclusive timestamp filter, so retaining every ID at
    # its oldest timestamp prevents duplicates at that boundary.
    retained = ordered[-TAIL_SEEN_EVENT_LIMIT:]
    cutoff = _event_time(retained[0])
    retained = [item for item in ordered if _event_time(item) >= cutoff]
    while len(retained) > TAIL_SEEN_EVENT_LIMIT:
        later_times = [_event_time(item) for item in retained if _event_time(item) > cutoff]
        if not later_times:
            raise VaosCliError(
                "More than 5,000 tail events share the same timestamp; VAOS stopped to avoid unbounded memory or duplicate output."
            )
        cutoff = min(later_times)
        retained = [item for item in retained if _event_time(item) >= cutoff]
    return {str(item["event_id"]): item for item in retained}


def _write_tail_batch(items: list[dict[str, Any]], args: argparse.Namespace, output_stream: Any = None) -> None:
    if not items:
        return
    ordered = sorted(items, key=lambda item: (_event_time(item), str(item.get("event_id", ""))))
    if args.format != "text":
        _write_output(ordered, args.format, timezone_name=args.timezone)
        return
    for item in ordered:
        localized = _localize_times(item, args.timezone)
        line = f"{localized.get('timestamp', '')} {item.get('level', '')} {item.get('component', '')} {item.get('event_type', '')} {item.get('message', '')}\n"
        if output_stream:
            output_stream.write(line)
            output_stream.flush()
        else:
            sys.stdout.write(line)


def _command_status(client: Client, args: argparse.Namespace) -> None:
    data = client.request("GET", "/health")
    _write_output(data, args.format, args.output, args.timezone)


def _command_stats(client: Client, args: argparse.Namespace) -> None:
    action = getattr(args, "stats_action", None)
    if action == "metrics":
        definitions = client.request("GET", "/admin/observability/metrics", query={"limit": 500}).get("items", [])
        definitions = [item for item in definitions if item.get("available") and item.get("metric_type") in {"gauge", "histogram", "counter"}]
        if args.metric:
            definitions = [item for item in definitions if item.get("metric_key") == args.metric]
            if not definitions:
                raise VaosCliError(f"No recorded metric named {args.metric!r}.", EXIT_USAGE)
        date_range = _range_args(args)
        if not date_range:
            raise VaosCliError("stats metrics requires --from and --to.", EXIT_USAGE)
        group = args.group_by or "none"
        dimensions = {}
        for raw in getattr(args, "dimension", []):
            if "=" not in raw:
                raise VaosCliError("--dimension must use key=value syntax.", EXIT_USAGE)
            key, value = raw.split("=", 1)
            if not key or not value:
                raise VaosCliError("--dimension must use non-empty key=value values.", EXIT_USAGE)
            dimensions[key] = value
        rows: list[dict[str, Any]] = []
        for metric in definitions:
            query = {"metric": metric["metric_key"], **date_range, "limit": 1000}
            if dimensions:
                query["dimensions"] = json.dumps(dimensions, separators=(",", ":"))
            response = client.request("GET", "/admin/observability/series", query=query)
            series = response.get("items", [])
            if response.get("truncated") or int(response.get("total", len(series))) > len(series):
                raise VaosCliError(
                    f"Metric {metric['metric_key']!r} has more than 1,000 matching points. Narrow the time range or add --dimension filters.",
                    EXIT_USAGE,
                )
            if group == "none":
                rows.extend({"timestamp": item["timestamp"], "metric_key": metric["metric_key"], "value": item["value"], "unit": metric["unit"]} for item in series)
                continue
            grouped: dict[str, list[float]] = {}
            for item in series:
                stamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
                key = stamp.strftime("%Y-%m-%d") if group == "day" else stamp.strftime("%Y-%m-%dT%H:00Z")
                grouped.setdefault(key, []).append(float(item["value"]))
            for key, values in sorted(grouped.items()):
                if metric.get("metric_type") == "counter":
                    rows.append({"period_utc": key, "metric_key": metric["metric_key"], "total": sum(values), "samples": len(values), "unit": metric["unit"], "dimensions": dimensions})
                else:
                    rows.append({"period_utc": key, "metric_key": metric["metric_key"], "mean": sum(values) / len(values), "samples": len(values), "unit": metric["unit"], "dimensions": dimensions})
        _write_output(rows, args.format, args.output, args.timezone)
        return
    date_range = _range_args(args)
    if bool(date_range.get("after")) != bool(date_range.get("before")):
        raise VaosCliError("Provide both --from and --to for a custom overview range.", EXIT_USAGE)
    period = "custom" if date_range else args.period
    data = client.request("GET", "/admin/observability/overview", query={"period": period, **date_range})
    _write_output(data, args.format, args.output, args.timezone)


def _command_logs(client: Client, args: argparse.Namespace) -> None:
    action = args.logs_action
    if action == "tail":
        if args.follow and args.format not in {"text", "ndjson"}:
            raise VaosCliError("A live log stream supports text or NDJSON. Use --no-follow for a JSON or CSV snapshot.", EXIT_USAGE)
        if args.follow and args.output and args.format != "text":
            raise VaosCliError("logs tail --output currently supports text only; remove --output to stream JSON/NDJSON to stdout.", EXIT_USAGE)
        tail_started_at = datetime.now(timezone.utc)
        initial = _fetch_events(client, args, limit=args.limit, all_pages=True)
        if not args.follow:
            ordered = sorted(initial, key=lambda item: (_event_time(item), str(item.get("event_id", ""))))
            _write_output(ordered, args.format, args.output, args.timezone)
            return
        output_stream = Path(args.output).expanduser().open("a", encoding="utf-8", newline="") if args.output else None
        try:
            _write_tail_batch(initial, args, output_stream)
            if not args.follow:
                return
            seen = _bounded_tail_seen({}, initial)
            while True:
                updates = _fetch_tail_updates(client, args, seen, empty_start=tail_started_at)
                new = []
                cycle_ids = set(seen)
                for item in updates:
                    event_id = str(item.get("event_id") or "")
                    if event_id and event_id not in cycle_ids:
                        new.append(item)
                        cycle_ids.add(event_id)
                _write_tail_batch(new, args, output_stream)
                seen = _bounded_tail_seen(seen, new)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return
        finally:
            if output_stream:
                output_stream.close()
    elif action == "cat":
        rows = _fetch_events(client, args, limit=args.limit, all_pages=True)
        if args.format == "text":
            lines = []
            for item in rows:
                item = _localize_times(item, args.timezone)
                lines.append(f"{item.get('timestamp', '')} {item.get('level', '')} {item.get('component', '')} {item.get('event_type', '')} {item.get('message', '')}")
            _write_text_lines(lines, args.output)
        else:
            _write_output(rows, args.format, args.output, args.timezone)
    else:
        rows = _fetch_events(client, args, limit=args.limit, all_pages=True)
        if args.format == "text":
            lines = []
            for item in rows:
                item = _localize_times(item, args.timezone)
                lines.append(f"{item.get('timestamp', '')} {item.get('level', '')} {item.get('component', '')} {item.get('event_type', '')} {item.get('message', '')}")
            _write_text_lines(lines, args.output)
        else:
            _write_output(rows, args.format, args.output, args.timezone)


def _command_reports(client: Client, args: argparse.Namespace) -> None:
    if args.reports_action == "list":
        data = client.request("GET", "/admin/observability/reports", query={"limit": args.limit})
        _write_output(data.get("items", []), args.format, args.output, args.timezone)
        return
    report_type = {"statistics": "lifetime-statistics", "security-events": "security-events", "logs": "events", "lifecycle": "lifecycle", "metric-series": "metric-series"}.get(args.report_name, args.report_name)
    detail_filters = (args.session_id, args.correlation_id, args.training_job_id, args.dataset_id, args.model_id)
    if any(detail_filters) and report_type != "events":
        raise VaosCliError("Session, correlation, training-job, dataset and model filters apply only to a logs/events report.", EXIT_USAGE)
    if args.actor_ref and report_type != "security-events":
        raise VaosCliError("Actor reference filters apply only to a security-events report.", EXIT_USAGE)
    if args.actor_ref and len(str(args.actor_ref).strip()) > 120:
        raise VaosCliError("actor_ref exceeds its 120-character limit.", EXIT_USAGE)
    filters = {} if report_type == "lifetime-statistics" else {**_range_args(args)}
    if report_type == "metric-series":
        if not args.metric:
            raise VaosCliError("metric-series requires --metric.", EXIT_USAGE)
        filters["metric"] = args.metric
    if report_type in {"events", "security-events"}:
        if args.component: filters["component"] = args.component
        if args.event_type: filters["event_type"] = args.event_type
        if args.session_id: filters["session_id"] = args.session_id
        if args.correlation_id: filters["correlation_id"] = args.correlation_id
        if report_type == "events":
            if args.training_job_id: filters["training_job_id"] = args.training_job_id
            if args.dataset_id: filters["dataset_id"] = args.dataset_id
            if args.model_id: filters["model_id"] = args.model_id
        if args.search: filters["search"] = args.search
        if report_type == "events" and args.level: filters["level"] = args.level
        if report_type == "security-events" and args.outcome: filters["outcome"] = args.outcome
        if report_type == "security-events" and args.actor_ref: filters["actor_ref"] = args.actor_ref
    elif report_type in {"training", "datasets", "models", "cameras", "tracking-events", "mcp-activity"}:
        if args.search: filters["search"] = args.search
        if args.event_type and report_type in {"training", "models", "mcp-activity"}: filters["event_type"] = args.event_type
    payload = {"report_type": report_type, "format": args.report_format, "filters": filters}
    job = client.request("POST", "/admin/observability/reports/jobs", payload=payload)
    deadline = time.monotonic() + args.wait_seconds
    while job.get("status") in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.5)
        job = client.request("GET", f"/admin/observability/reports/jobs/{job['job_id']}")
    if job.get("status") != "complete":
        _write_output(job, "json")
        raise VaosCliError("The report did not complete before the wait limit. Check it later with 'vaos reports list'.")
    content, _headers = client.request("GET", f"/admin/observability/reports/jobs/{job['job_id']}/download", binary=True)
    destination = Path(args.output).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    temporary.write_bytes(content)
    temporary.replace(destination)
    print(f"Saved {report_type} report to {destination}")


def _command_integrations(client: Client, args: argparse.Namespace) -> None:
    data = client.request("GET", "/admin/observability/integrations")
    _write_output(data, args.format, args.output, args.timezone)


def _format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json", "ndjson", "csv"), default="text")
    parser.add_argument("--output", help="Write formatted output to this local file instead of stdout.")
    parser.add_argument("--timezone", default="UTC", help="Output and date-input timezone (IANA name; default UTC).")


def _range_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="from_time", help="Start date/time; a date includes that full UTC day.")
    parser.add_argument("--to", dest="to_time", help="End date/time; a date includes that full UTC day.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaos", description="Use the authenticated VAOS API from a Linux or Windows terminal.")
    parser.add_argument("--url", help="VAOS origin; defaults to VAOS_URL or http://127.0.0.1:8000.")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Read service health."); _format_arg(status)
    stats = commands.add_parser("stats", help="Read installation statistics.")
    stats.add_argument("stats_action", nargs="?", choices=("overview", "metrics"), default="overview")
    stats.add_argument("--period", choices=("lifetime", "24h", "7d", "30d"), default="lifetime")
    stats.add_argument("--group-by", choices=("none", "hour", "day"), default="none")
    stats.add_argument("--metric", help="Limit metric-series queries to one metric key."); stats.add_argument("--dimension", action="append", default=[], help="Filter one exact metric dimension as key=value; may be repeated."); _range_arg(stats); _format_arg(stats)
    logs = commands.add_parser("logs", help="Search locally recorded redacted operational events.")
    log_commands = logs.add_subparsers(dest="logs_action", required=True)
    for name, help_text in (("list", "List the newest events"), ("search", "Filter events"), ("cat", "Read paginated events for shell pipelines"), ("tail", "Poll for new events until interrupted")):
        item = log_commands.add_parser(name, help=help_text); _range_arg(item)
        item.add_argument("--component"); item.add_argument("--level", type=str.upper, choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")); item.add_argument("--event-type", dest="event_type"); item.add_argument("--session-id", dest="session_id", help="Match one exact VAOS runtime session ID."); item.add_argument("--correlation-id", dest="correlation_id", help="Match one exact request or operation correlation ID."); item.add_argument("--training-job-id", dest="training_job_id", help="Match an exact job ID recorded in safe event details."); item.add_argument("--dataset-id", dest="dataset_id", help="Match an exact dataset ID recorded in safe event details."); item.add_argument("--model-id", dest="model_id", help="Match an exact model ID recorded in safe event details."); item.add_argument("--search")
        item.add_argument("--limit", type=int, default=200 if name == "cat" else 50)
        item.add_argument("--interval", type=float, default=2.0, help=argparse.SUPPRESS)
        if name == "tail":
            item.add_argument("--follow", action=argparse.BooleanOptionalAction, default=True, help="Continue polling until interrupted (default). Use --no-follow for one poll.")
        _format_arg(item)
    reports = commands.add_parser("reports", help="List or generate PDF/CSV reports.")
    report_commands = reports.add_subparsers(dest="reports_action", required=True)
    report_list = report_commands.add_parser("list"); report_list.add_argument("--limit", type=int, default=50); _format_arg(report_list)
    export = report_commands.add_parser("export")
    export.add_argument("report_name", choices=("statistics", "security-events", "logs", "lifecycle", "metric-series", "training", "datasets", "models", "inference", "cameras", "tracking-events", "hardware-energy", "api-activity", "mcp-activity"))
    export.add_argument("--format", dest="report_format", choices=("pdf", "csv"), default="pdf")
    export.add_argument("--output", required=True); export.add_argument("--metric"); export.add_argument("--component"); export.add_argument("--event-type"); export.add_argument("--session-id", dest="session_id", help="Filter a logs export by one exact VAOS runtime session ID."); export.add_argument("--correlation-id", dest="correlation_id", help="Filter a logs export by one exact correlation ID."); export.add_argument("--training-job-id", dest="training_job_id", help="Filter a logs export by an exact recorded job ID."); export.add_argument("--dataset-id", dest="dataset_id", help="Filter a logs export by an exact recorded dataset ID."); export.add_argument("--model-id", dest="model_id", help="Filter a logs export by an exact recorded model ID."); export.add_argument("--actor-ref", dest="actor_ref", help="Filter a security-events export by one exact recorded actor reference."); export.add_argument("--level", type=str.upper, choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")); export.add_argument("--outcome"); export.add_argument("--search"); export.add_argument("--wait-seconds", type=int, default=300); _range_arg(export)
    integrations = commands.add_parser("integrations", help="Show configured outbound delivery support."); integration_status = integrations.add_subparsers(dest="integrations_action", required=True).add_parser("status"); _format_arg(integration_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "limit", 1) < 1 or getattr(args, "limit", 1) > 5000:
            raise VaosCliError("--limit must be between 1 and 5,000.", EXIT_USAGE)
        if getattr(args, "interval", 1) < 0.5 or getattr(args, "interval", 1) > 60:
            raise VaosCliError("--interval must be between 0.5 and 60 seconds.", EXIT_USAGE)
        if getattr(args, "wait_seconds", 1) < 1 or getattr(args, "wait_seconds", 1) > 3600:
            raise VaosCliError("--wait-seconds must be between 1 and 3,600.", EXIT_USAGE)
        client = Client(args.url)
        if args.command == "status": _command_status(client, args)
        elif args.command == "stats": _command_stats(client, args)
        elif args.command == "logs": _command_logs(client, args)
        elif args.command == "reports": _command_reports(client, args)
        elif args.command == "integrations": _command_integrations(client, args)
        return 0
    except VaosCliError as exc:
        print(f"vaos: {exc}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:
        return 0
    except OSError as exc:
        print(f"vaos: local file operation failed: {exc}", file=sys.stderr)
        return EXIT_SERVICE


if __name__ == "__main__":
    raise SystemExit(main())
