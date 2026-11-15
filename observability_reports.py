"""Bounded, asynchronous CSV/PDF reports for local VAOS observability data."""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import getpass
import subprocess
import textwrap
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import observability_store as store

MAX_REPORT_ROWS = 5000
MAX_REPORT_BYTES = 12 * 1024 * 1024
REPORT_TTL_HOURS = 24
REPORT_DEFINITIONS = (
    {"report_type": "lifetime-statistics", "label": "Installation lifetime statistics", "coverage": "Durable counters and availability notes.", "source": "observability lifetime aggregates"},
    {"report_type": "events", "label": "Operational events and logs", "coverage": "Recorded operational events; bounded by retention and the selected range.", "source": "observability event store"},
    {"report_type": "security-events", "label": "Append-only security audit events", "coverage": "Recorded security audit events; sensitive-read permission required.", "source": "append-only security audit"},
    {"report_type": "lifecycle", "label": "Application lifecycle sessions", "coverage": "Known runtime sessions; unknown exits are not treated as confirmed crashes.", "source": "runtime session store"},
    {"report_type": "metric-series", "label": "One metric history", "coverage": "One registered metric and only the observations still retained.", "source": "metric observation store"},
    {"report_type": "training", "label": "Training activity and epoch metrics", "coverage": "Recorded training events and training.* metric observations. Legacy files are not reconstructed.", "source": "training events and metric observations"},
    {"report_type": "datasets", "label": "Dataset preparation and reviewed sample inventory", "coverage": "Forward dataset validation/import summaries plus counts and labels from retained review samples; source paths and image bytes are excluded. Imports before dataset telemetry was added are not reconstructed.", "source": "dataset preparation events and ml_samples review table"},
    {"report_type": "models", "label": "Model activity", "coverage": "Recorded model events only; model files and weights are never included.", "source": "model event records"},
    {"report_type": "inference", "label": "Inference activity", "coverage": "Recorded camera and local-video frame batches; raw frames, video names and local-video session tokens are never included.", "source": "inference event records"},
    {"report_type": "cameras", "label": "Camera and stream inventory", "coverage": "Current camera inventory and latest status only; camera tokens and stream URLs are excluded.", "source": "cameras and camera_status tables"},
    {"report_type": "tracking-events", "label": "Object tracking and rule events", "coverage": "Recorded object-rule events; camera tokens and stream URLs are excluded.", "source": "object_rule_events table"},
    {"report_type": "hardware-energy", "label": "Hardware and energy observations", "coverage": "Recorded hardware.* and energy metric observations only. No power sensor means energy remains unavailable.", "source": "metric observation store"},
    {"report_type": "api-activity", "label": "API activity", "coverage": "Aggregated API request events; credentials and raw request bodies are excluded.", "source": "API event records"},
    {"report_type": "mcp-activity", "label": "MCP activity and approvals", "coverage": "Recorded MCP security audit events and local bridge tool invocation name/outcome/duration. Tool arguments and returned content are not collected by this report.", "source": "MCP security audit events and invocation telemetry"},
)
REPORT_TYPES = {item["report_type"] for item in REPORT_DEFINITIONS}
REPORT_FORMATS = {"csv", "pdf"}
LIFETIME_REPORT_LABELS = {
    "application_starts": "Application starts",
    "clean_stops": "Clean application stops",
    "uptime": "Installation uptime",
    "training_jobs_started": "Training jobs started",
    "training_jobs_completed": "Training jobs completed",
    "training_jobs_failed": "Training jobs failed",
    "training_epochs_completed": "Training epochs completed",
    "api_requests": "API requests",
    "inference_frames": "Inference frames processed",
    "energy_wh": "Estimated GPU board energy",
}
MAX_ACTIVE_JOBS_PER_ACTOR = 5
MAX_ACTIVE_REPORT_JOBS = 100
MAX_LIST_JOBS = 100
MAX_METRIC_REPORT_ROWS = 1000
_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _validate_range(parameters: dict[str, Any]) -> dict[str, Any]:
    allowed = {"after", "before", "metric", "dimensions", "group_by", "aggregation", "interval", "level", "component", "event_type", "session_id", "correlation_id", "training_job_id", "dataset_id", "model_id", "actor_ref", "search", "outcome"}
    if set(parameters) - allowed:
        raise ValueError("Report filters contain unsupported fields.")
    after = str(parameters.get("after", ""))
    before = str(parameters.get("before", ""))
    start, end = store._normalize_time(after), store._normalize_time(before)
    if store._parse_time(start) >= store._parse_time(end) or store._parse_time(end) - store._parse_time(start) > timedelta(days=366):
        raise ValueError("Report range must be ordered and no longer than 366 days.")
    result: dict[str, Any] = {"after": start, "before": end}
    if "metric" in parameters:
        metric = str(parameters["metric"])
        if metric not in store.METRIC_CATALOGUE:
            raise ValueError("Unknown metric key.")
        result["metric"] = metric
    if "dimensions" in parameters:
        if not result.get("metric") or not isinstance(parameters["dimensions"], dict):
            raise ValueError("Metric dimension filters require a metric key and a JSON object.")
        store._metric_dimension_filters(result["metric"], parameters["dimensions"])
        result["dimensions"] = store.redact_details(parameters["dimensions"])
    for field in ("group_by", "aggregation", "interval"):
        if field in parameters and str(parameters[field]).strip():
            result[field] = str(parameters[field]).strip()
    if any(field in result for field in ("group_by", "aggregation", "interval")) and not result.get("metric"):
        raise ValueError("Metric grouping and aggregation options require a metric key.")
    if result.get("metric"):
        result.setdefault("aggregation", "raw")
        result.setdefault("interval", "hour")
        if result["aggregation"] != "raw" and result["aggregation"] not in store.metric_series_aggregations(result["metric"]):
            raise ValueError("This aggregation is not supported for the selected metric.")
        if result["interval"] not in store.METRIC_SERIES_INTERVALS:
            raise ValueError("Choose a supported metric time interval.")
        if result.get("group_by") and result["group_by"] not in store.METRIC_CATALOGUE[result["metric"]]["dimensions"]:
            raise ValueError(f"Unsupported grouping dimension for {result['metric']}: {result['group_by']}.")
    for field, limit in (("level", 16), ("component", 80), ("event_type", 96), ("session_id", 120), ("correlation_id", 120), ("training_job_id", 120), ("dataset_id", 120), ("model_id", 120), ("actor_ref", 120), ("search", 200), ("outcome", 64)):
        raw_value = str(parameters.get(field, "")).strip()
        if len(raw_value) > limit:
            raise ValueError(f"{field} exceeds its length limit.")
        value = store._safe_text(raw_value, limit)
        if value:
            result[field] = value
    if result.get("level") and result["level"].upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Unsupported report severity.")
    if result.get("event_type") and not re.fullmatch(r"[a-z][a-z0-9_.-]{1,95}", result["event_type"]):
        raise ValueError("Event type must be a lowercase event key.")
    if result.get("outcome") and not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", result["outcome"]):
        raise ValueError("Outcome must be a lowercase key.")
    return result


def create_report_job(
    db_path: str | Path, *, report_type: str, output_format: str,
    requested_by: str, parameters: dict[str, Any], now: str | None = None,
) -> dict[str, Any]:
    with store._connection(Path(db_path).expanduser().resolve()) as db:
        db.execute("BEGIN IMMEDIATE")
        return create_report_job_in_transaction(
            db, report_type=report_type, output_format=output_format,
            requested_by=requested_by, parameters=parameters, now=now,
        )


def create_report_job_in_transaction(
    db: Any, *, report_type: str, output_format: str,
    requested_by: str, parameters: dict[str, Any], now: str | None = None,
) -> dict[str, Any]:
    """Insert a queued job inside a transaction owned by the calling route."""
    if not db.in_transaction:
        raise RuntimeError("Report creation requires an active SQLite transaction.")
    if report_type not in REPORT_TYPES:
        raise ValueError("Unsupported report type.")
    if output_format not in REPORT_FORMATS:
        raise ValueError("Report format must be CSV or PDF.")
    allowed_filters = {
        "events": {"after", "before", "level", "component", "event_type", "session_id", "correlation_id", "training_job_id", "dataset_id", "model_id", "search"},
        "security-events": {"after", "before", "component", "event_type", "search", "outcome", "actor_ref"},
        "lifecycle": {"after", "before"},
        "lifetime-statistics": set(),
        "metric-series": {"after", "before", "metric", "dimensions", "group_by", "aggregation", "interval"},
        "training": {"after", "before", "event_type", "search"},
        "datasets": {"after", "before", "search"},
        "models": {"after", "before", "event_type", "search"},
        "inference": {"after", "before"},
        "cameras": {"after", "before", "search"},
        "tracking-events": {"after", "before", "search"},
        "hardware-energy": {"after", "before"},
        "api-activity": {"after", "before"},
        "mcp-activity": {"after", "before", "event_type", "search"},
    }[report_type]
    if set(parameters) - allowed_filters:
        raise ValueError("One or more filters do not apply to this report type.")
    params = {} if report_type == "lifetime-statistics" else _validate_range(parameters)
    if report_type == "metric-series" and not params.get("metric"):
        raise ValueError("Metric-series reports require a metric key.")
    if report_type != "metric-series" and "metric" in params:
        params.pop("metric")
    requested = store._normalize_time(now or store.utc_now())
    expires = (store._parse_time(requested) + timedelta(hours=REPORT_TTL_HOURS)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    job_id = uuid.uuid4().hex
    active = int(db.execute(
        "SELECT COUNT(*) FROM observability_report_jobs WHERE requested_by=? AND status IN ('queued','running')",
        (str(requested_by)[:120],),
    ).fetchone()[0])
    if active >= MAX_ACTIVE_JOBS_PER_ACTOR:
        raise ValueError("This account already has five reports queued or running. Wait for one to finish before requesting another.")
    active_total = int(db.execute("SELECT COUNT(*) FROM observability_report_jobs WHERE status IN ('queued','running')").fetchone()[0])
    if active_total >= MAX_ACTIVE_REPORT_JOBS:
        raise ValueError("The local report queue is full. Wait for existing reports to finish before requesting another.")
    db.execute(
        "INSERT INTO observability_report_jobs(job_id,report_type,requested_at,requested_by,status,parameters_json,progress_percent,expires_at) VALUES(?,?,?,?, 'queued', ?,0,?)",
        (job_id, report_type, requested, str(requested_by)[:120], store._json({"format": output_format, **params}), expires),
    )
    return get_report_job_in_transaction(db, job_id) or {"job_id": job_id, "status": "queued"}


def get_report_job(db_path: str | Path, job_id: str) -> dict[str, Any] | None:
    with store._connection(Path(db_path).expanduser().resolve()) as db:
        return get_report_job_in_transaction(db, job_id)


def get_report_job_in_transaction(db: Any, job_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return None
    row = db.execute("SELECT job_id,report_type,requested_at,requested_by,status,progress_percent,output_name,output_sha256,output_size_bytes,expires_at,error_code,parameters_json FROM observability_report_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    parameters = json.loads(result.pop("parameters_json"))
    result["format"] = parameters.get("format", "csv")
    result["filters"] = {key: value for key, value in parameters.items() if key != "format"}
    return result


def public_report_job(job: dict[str, Any]) -> dict[str, Any]:
    """Return a report status without the internal requester identity."""
    return {key: value for key, value in job.items() if key != "requested_by"}


def list_report_jobs(db_path: str | Path, *, limit: int = 50, requested_by: str | None = None) -> list[dict[str, Any]]:
    if not 1 <= int(limit) <= MAX_LIST_JOBS:
        raise ValueError(f"Report list page size must be between 1 and {MAX_LIST_JOBS}.")
    query = "SELECT job_id,report_type,requested_at,requested_by,status,progress_percent,output_name,output_sha256,output_size_bytes,expires_at,error_code,parameters_json FROM observability_report_jobs"
    params: list[Any] = []
    if requested_by is not None:
        query += " WHERE requested_by=?"
        params.append(str(requested_by)[:120])
    query += " ORDER BY requested_at DESC,job_id DESC LIMIT ?"
    params.append(int(limit))
    with store._connection(Path(db_path).expanduser().resolve()) as db:
        rows = db.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        parameters = json.loads(item.pop("parameters_json"))
        item["format"] = parameters.get("format", "csv")
        item["filters"] = {key: value for key, value in parameters.items() if key != "format"}
        result.append(public_report_job(item))
    return result


def report_types() -> list[dict[str, Any]]:
    result = []
    for definition in REPORT_DEFINITIONS:
        item = {**definition, "formats": ["csv", "pdf"], "max_rows": MAX_REPORT_ROWS, "max_range_days": 366}
        if item["report_type"] == "lifetime-statistics":
            item.pop("max_range_days")
            item["max_rows"] = 100
        if item["report_type"] == "metric-series":
            item["requires_metric"] = True
            item["max_rows"] = 1000
        result.append(item)
    return result


def _optional_table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _event_report_rows(
    db_path: Path, *, component: str, parameters: dict[str, Any], event_type: str | None = None,
) -> tuple[list[str], list[list[Any]]]:
    header = ["timestamp_utc", "record_type", "source", "event_type", "level", "session_id", "correlation_id", "message", "value", "unit", "dimensions_json", "details_json", "quality"]
    rows: list[list[Any]] = []
    cursor = None
    fixed_type = event_type
    requested_type = parameters.get("event_type")
    if requested_type and fixed_type and requested_type != fixed_type:
        return header, rows
    selected_type = requested_type or fixed_type
    while len(rows) <= MAX_REPORT_ROWS:
        page_size = min(200, MAX_REPORT_ROWS + 1 - len(rows))
        items = store.query_events(
            db_path, limit=page_size, after=parameters["after"], before=parameters["before"],
            cursor_before=cursor, component=component, event_type=selected_type,
            session_id=parameters.get("session_id"), correlation_id=parameters.get("correlation_id"),
            training_job_id=parameters.get("training_job_id"), dataset_id=parameters.get("dataset_id"),
            model_id=parameters.get("model_id"), search=parameters.get("search"),
        )
        if not items:
            break
        rows.extend([[item["timestamp"], "event", component, item["event_type"], item["level"], item.get("session_id"), item.get("correlation_id"), item["message"], "", "", "{}", store._json(item["details"]), "recorded event"] for item in items])
        if len(items) < page_size:
            break
        cursor = (items[-1]["timestamp"], items[-1]["event_id"])
    return header, rows


def _metric_prefix_rows(db_path: Path, *, parameters: dict[str, Any], prefixes: tuple[str, ...]) -> tuple[list[str], list[list[Any]]]:
    header = ["timestamp_utc", "record_type", "source", "event_type", "level", "message", "value", "unit", "dimensions_json", "details_json", "quality"]
    clauses = ["timestamp >= ?", "timestamp < ?"]
    args: list[Any] = [parameters["after"], parameters["before"]]
    clauses.append("(" + " OR ".join("metric_key LIKE ?" for _ in prefixes) + ")")
    args.extend(f"{prefix}%" for prefix in prefixes)
    args.append(MAX_REPORT_ROWS + 1)
    with store._connection(db_path) as db:
        rows = db.execute(
            "SELECT metric_key,timestamp,value,dimensions_json,provenance,measurement_scope,estimated FROM observability_metric_observations WHERE "
            + " AND ".join(clauses) + " ORDER BY timestamp DESC,observation_id DESC LIMIT ?", args,
        ).fetchall()
    result = []
    for row in rows:
        metric = store.METRIC_CATALOGUE.get(str(row["metric_key"]), {})
        detail = {"provenance": row["provenance"], "measurement_scope": row["measurement_scope"], "estimated": bool(row["estimated"])}
        result.append([row["timestamp"], "metric", row["metric_key"], "", "", metric.get("name", row["metric_key"]), row["value"], metric.get("unit", ""), row["dimensions_json"], store._json(detail), "estimated" if row["estimated"] else "measured"])
    return header, result


def _counter_report_rows(db_path: Path, *, parameters: dict[str, Any], metric_key: str) -> tuple[list[str], list[list[Any]]]:
    header = ["timestamp_utc", "record_type", "source", "event_type", "level", "message", "value", "unit", "dimensions_json", "details_json", "quality"]
    with store._connection(db_path) as db:
        items = db.execute(
            "SELECT ce.timestamp,ce.delta,ce.dimensions_json,e.message FROM observability_counter_events ce "
            "JOIN observability_events e ON e.event_id=ce.event_id WHERE ce.metric_key=? AND ce.timestamp>=? AND ce.timestamp<? "
            "ORDER BY ce.timestamp DESC,ce.event_id DESC LIMIT ?",
            (metric_key, parameters["after"], parameters["before"], MAX_REPORT_ROWS + 1),
        ).fetchall()
    metric = store.METRIC_CATALOGUE.get(metric_key, {})
    return header, [[item["timestamp"], "counter", metric_key, metric_key, "", item["message"], item["delta"], metric.get("unit", ""), item["dimensions_json"], "{}", "durable source event"] for item in items]


def _with_empty_status(report_type: str, header: list[str], rows: list[list[Any]]) -> tuple[list[str], list[list[Any]]]:
    if rows or report_type not in REPORT_TYPES - {"lifetime-statistics", "events", "security-events", "lifecycle", "metric-series"}:
        return header, rows
    definition = next(item for item in REPORT_DEFINITIONS if item["report_type"] == report_type)
    status = [""] * len(header)
    status[1] = "status"
    status[2] = str(definition["source"])
    status[5] = "No matching source records were recorded in this time range."
    status[10] = "No data; this is not a zero measurement. " + str(definition["coverage"])
    return header, [status]


def _secure_report_directory(path: Path, *, recursive: bool = False) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "nt":
        domain = os.environ.get("USERDOMAIN", "")
        account = getpass.getuser()
        principal = f"{domain}\\{account}" if domain and "\\" not in account else account
        targets = [path]
        if recursive:
            targets.extend(child for child in path.iterdir() if child.is_file() or child.is_symlink())
        for target in targets:
            is_directory = target.is_dir()
            inheritance = "(OI)(CI)" if is_directory else ""
            command = ["icacls", str(target), "/inheritance:r", "/grant:r", f"{principal}:{inheritance}F", f"*S-1-5-18:{inheritance}F", f"*S-1-5-32-544:{inheritance}F"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0:
                raise OSError("VAOS could not restrict access to the observability report directory.")
    else:
        os.chmod(path, 0o700)


def cleanup_orphan_report_artifacts(db_path: str | Path, report_dir: str | Path, *, now: str | None = None) -> int:
    """Remove only deterministic report-job artifacts not owned by a live result.

    Call during startup after interrupted jobs are reconciled and before the
    background worker resumes. User-named files are never touched.
    """
    database = Path(db_path).expanduser().resolve()
    root = Path(report_dir).expanduser().resolve()
    _secure_report_directory(root, recursive=True)
    current = store._normalize_time(now or store.utc_now())
    with store._connection(database) as db:
        rows = db.execute("SELECT job_id,output_name,parameters_json,expires_at FROM observability_report_jobs WHERE status='complete'").fetchall()
    valid = {
        str(row["output_name"])
        for row in rows
        if row["output_name"] == f"{row['job_id']}.{json.loads(row['parameters_json']).get('format', 'csv')}"
        and json.loads(row["parameters_json"]).get("format", "csv") in REPORT_FORMATS
        and row["expires_at"]
        and store._parse_time(str(row["expires_at"])) > store._parse_time(current)
    }
    removed = 0
    for child in root.iterdir():
        if not re.fullmatch(r"[0-9a-f]{32}\.(?:csv|pdf)(?:\.part)?", child.name):
            continue
        if (child.name.endswith(".part") or child.name not in valid) and (child.is_file() or child.is_symlink()):
            child.unlink(missing_ok=True)
            removed += 1
    return removed


def recover_report_jobs(db_path: str | Path) -> list[str]:
    """Mark interrupted jobs honestly and return still-queued work for resubmission."""
    with store._connection(Path(db_path).expanduser().resolve()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE observability_report_jobs SET status='failed',error_code='process_interrupted' WHERE status='running'")
        queued = [str(row[0]) for row in db.execute("SELECT job_id FROM observability_report_jobs WHERE status='queued' ORDER BY requested_at LIMIT 100").fetchall()]
        db.commit()
    return queued


def _report_rows(db_path: Path, report_type: str, parameters: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    if report_type == "lifetime-statistics":
        overview = store.overview(db_path, period="lifetime")
        header = ["metric_key", "display_name", "value", "unit", "available", "quality", "reason"]
        rows = [[key, item.get("label") or LIFETIME_REPORT_LABELS.get(key, key), item.get("value"), item.get("unit"), item.get("available"), item.get("quality"), item.get("reason")] for key, item in overview.get("kpis", {}).items()]
        return header, rows
    if report_type == "metric-series":
        metric_key = str(parameters["metric"])
        definition = store.METRIC_CATALOGUE[metric_key]
        dimensions = parameters.get("dimensions") or {}
        aggregation = str(parameters.get("aggregation", "raw"))
        interval = str(parameters.get("interval", "hour"))
        group_by = parameters.get("group_by") or None
        if aggregation == "raw":
            items = store.query_metric_series(db_path, metric_key=metric_key, after=parameters["after"], before=parameters["before"], limit=MAX_METRIC_REPORT_ROWS, dimensions=dimensions)
            total = store.count_metric_series(db_path, metric_key=metric_key, after=parameters["after"], before=parameters["before"], dimensions=dimensions)
        else:
            series = store.query_metric_series_aggregated(
                db_path, metric_key=metric_key, after=parameters["after"], before=parameters["before"],
                aggregation=aggregation, interval=interval, limit=MAX_METRIC_REPORT_ROWS,
                dimensions=dimensions, group_by=group_by,
            )
            items, total = series["items"], series["total"]
        if total > MAX_METRIC_REPORT_ROWS:
            raise ValueError("Report would exceed the 1,000-row metric history limit. Narrow the time range before exporting.")
        header = ["timestamp_utc", "metric_key", "value", "unit", "dimensions_json", "aggregation", "interval", "record_count", "provenance", "measurement_scope", "estimated"]
        rows = [[item["timestamp"], metric_key, item["value"], definition["unit"], store._json(item["dimensions"]), aggregation, interval if aggregation != "raw" else "", item.get("record_count", 1), item["provenance"], item["measurement_scope"], item["estimated"]] for item in items]
        return header, rows
    if report_type == "lifecycle":
        header = ["session_id", "component", "app_version", "started_at_utc", "last_heartbeat_at_utc", "clean_shutdown_at_utc", "exit_reason"]
        rows: list[list[Any]] = []
        cursor = None
        while len(rows) <= MAX_REPORT_ROWS:
            page_size = min(200, MAX_REPORT_ROWS + 1 - len(rows))
            items = store.list_sessions(db_path, limit=page_size, after=parameters["after"], before=parameters["before"], cursor_before=cursor)
            if not items:
                break
            rows.extend([[item.get("session_id"), item.get("component"), item.get("app_version"), item.get("started_at"), item.get("last_heartbeat_at"), item.get("clean_shutdown_at"), item.get("exit_reason")] for item in items])
            if len(items) < page_size:
                break
            cursor = (items[-1]["started_at"], items[-1]["session_id"])
        return header, rows
    if report_type == "security-events":
        header = ["timestamp_utc", "event_type", "outcome", "actor_ref", "component", "correlation_id", "details_json", "integrity_hash"]
        rows: list[list[Any]] = []
        cursor = None
        while len(rows) <= MAX_REPORT_ROWS:
            page_size = min(200, MAX_REPORT_ROWS + 1 - len(rows))
            items = store.query_security_audit(db_path, limit=page_size, after=parameters["after"], before=parameters["before"], cursor_before=cursor, event_type=parameters.get("event_type"), outcome=parameters.get("outcome"), component=parameters.get("component"), actor_ref=parameters.get("actor_ref"), search=parameters.get("search"))
            if not items:
                break
            rows.extend([[item["timestamp"], item["event_type"], item["outcome"], item["actor_ref"], item["component"], item["correlation_id"], store._json(item["details"]), item["integrity_hash"]] for item in items])
            if len(items) < page_size:
                break
            cursor = (items[-1]["timestamp"], items[-1]["sequence_id"])
        return header, rows
    if report_type == "training":
        header, events = _event_report_rows(db_path, component="training", parameters=parameters)
        _, metrics = _metric_prefix_rows(db_path, parameters=parameters, prefixes=("training.",))
        return header, events + metrics
    if report_type in {"models", "inference", "api-activity"}:
        if report_type == "api-activity":
            return _counter_report_rows(db_path, parameters=parameters, metric_key="api.requests")
        if report_type == "inference":
            return _counter_report_rows(db_path, parameters=parameters, metric_key="inference.frames_processed")
        component = {
            "models": "model",
            "inference": "camera",
            "api-activity": "api",
        }[report_type]
        fixed_type = "inference.frame_batch" if report_type == "inference" else None
        return _event_report_rows(db_path, component=component, parameters=parameters, event_type=fixed_type)
    if report_type == "mcp-activity":
        header = ["timestamp_utc", "record_type", "source", "event_type", "level", "session_id", "correlation_id", "message", "value", "unit", "dimensions_json", "details_json", "quality"]
        rows: list[list[Any]] = []
        cursor = None
        while len(rows) <= MAX_REPORT_ROWS:
            page_size = min(200, MAX_REPORT_ROWS + 1 - len(rows))
            items = store.query_security_audit(
                db_path, limit=page_size, after=parameters["after"], before=parameters["before"],
                cursor_before=cursor, component="mcp", event_type=parameters.get("event_type"), search=parameters.get("search"),
            )
            if not items:
                break
            rows.extend([[item["timestamp"], "audit", "mcp", item["event_type"], item["outcome"], "", item["correlation_id"], "Recorded MCP security/approval event", "", "", "{}", store._json({"actor_ref": item["actor_ref"], "correlation_id": item["correlation_id"], "details": item["details"]}), "append-only audit"] for item in items])
            if len(items) < page_size:
                break
            cursor = (items[-1]["timestamp"], items[-1]["sequence_id"])
        selected_type = parameters.get("event_type")
        if selected_type in (None, "mcp.tool_invoked"):
            event_parameters = {"after": parameters["after"], "before": parameters["before"], "event_type": "mcp.tool_invoked"}
            if parameters.get("search"):
                event_parameters["search"] = parameters["search"]
            _, invocation_rows = _event_report_rows(
                db_path, component="mcp", parameters=event_parameters, event_type="mcp.tool_invoked",
            )
            rows.extend(invocation_rows)
        rows.sort(key=lambda row: str(row[0]), reverse=True)
        return header, rows[:MAX_REPORT_ROWS]
    if report_type == "hardware-energy":
        return _metric_prefix_rows(db_path, parameters=parameters, prefixes=("hardware.", "energy."))
    if report_type == "datasets":
        header = ["timestamp_utc", "record_type", "source", "event_type", "level", "message", "value", "unit", "dimensions_json", "details_json", "quality"]
        rows: list[list[Any]] = []
        clauses = ["created_at >= ?", "created_at < ?"]
        args: list[Any] = [parameters["after"], parameters["before"]]
        if parameters.get("search"):
            clauses.append("instr(lower(COALESCE(NULLIF(confirmed_label,''),suggested_label)),?)>0")
            args.append(str(parameters["search"]).casefold())
        with store._connection(db_path) as db:
            if _optional_table_exists(db, "ml_samples"):
                samples = db.execute(
                    "SELECT COALESCE(NULLIF(confirmed_label,''),suggested_label) AS label,review_state,COUNT(*) AS sample_count,MIN(created_at) AS first_seen,MAX(created_at) AS last_seen FROM ml_samples WHERE "
                    + " AND ".join(clauses) + " GROUP BY label,review_state ORDER BY label,review_state LIMIT ?", [*args, MAX_REPORT_ROWS + 1],
                ).fetchall()
            else:
                samples = []
        for item in samples:
            rows.append([item["last_seen"], "sample-summary", "ml_samples", "", "", "Reviewed sample count by label and review state", item["sample_count"], "samples", store._json({"label": item["label"], "review_state": item["review_state"]}), store._json({"first_seen": item["first_seen"], "last_seen": item["last_seen"]}), "image data excluded"])
        for event_type in ("dataset.validation_succeeded", "dataset.validation_failed"):
            events = store.query_events(
                db_path, limit=MAX_REPORT_ROWS + 1, after=parameters["after"], before=parameters["before"],
                component="datasets", event_type=event_type, search=parameters.get("search"),
            )
            for event in events:
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                dataset_kind = str(details.get("dataset_kind", "unknown"))
                detail_values = {
                    key: details[key]
                    for key in ("dataset_kind", "prepared_count", "train_count", "validation_count", "class_count", "box_count")
                    if key in details
                }
                rows.append([
                    event["timestamp"], "dataset-preparation", "observability_events", event["event_type"],
                    event["level"], event["message"], details.get("prepared_count", ""), "images",
                    store._json({"dataset_kind": dataset_kind}), store._json(detail_values),
                    "source paths, dataset names and image data excluded; annotation count appears only when verified",
                ])
        return header, rows
    if report_type == "cameras":
        header = ["timestamp_utc", "record_type", "source", "event_type", "level", "message", "value", "unit", "dimensions_json", "details_json", "quality"]
        clauses = ["c.created_at >= ?", "c.created_at < ?"]
        args: list[Any] = [parameters["after"], parameters["before"]]
        if parameters.get("search"):
            clauses.append("instr(lower(c.name),?)>0")
            args.append(str(parameters["search"]).casefold())
        with store._connection(db_path) as db:
            if _optional_table_exists(db, "cameras"):
                status_available = _optional_table_exists(db, "camera_status")
                status_join = " LEFT JOIN camera_status cs ON cs.camera_token=c.token" if status_available else ""
                status_columns = ",cs.last_frame_at,cs.faces_detected,cs.matched_count" if status_available else ",NULL AS last_frame_at,NULL AS faces_detected,NULL AS matched_count"
                cameras = db.execute(
                    "SELECT c.name,c.source_type,c.active,c.created_at" + status_columns + " FROM cameras c" + status_join
                    + " WHERE " + " AND ".join(clauses) + " ORDER BY c.created_at DESC LIMIT ?", [*args, MAX_REPORT_ROWS + 1],
                ).fetchall()
            else:
                cameras = []
        rows = [[item["created_at"], "camera-inventory", "cameras", "", "", "Current camera and latest stream status; private token omitted", item["active"], "active (0/1)", store._json({"name": item["name"], "source_type": item["source_type"]}), store._json({"last_frame_at": item["last_frame_at"], "faces_detected": item["faces_detected"], "matched_count": item["matched_count"]}), "current snapshot"] for item in cameras]
        return header, rows
    if report_type == "tracking-events":
        header = ["timestamp_utc", "record_type", "source", "event_type", "level", "message", "value", "unit", "dimensions_json", "details_json", "quality"]
        clauses = ["e.timestamp >= ?", "e.timestamp < ?"]
        args: list[Any] = [parameters["after"], parameters["before"]]
        if parameters.get("search"):
            clauses.append("(instr(lower(e.rule_name),?)>0 OR instr(lower(e.class_name),?)>0 OR instr(lower(e.message),?)>0)")
            args.extend([str(parameters["search"]).casefold()] * 3)
        with store._connection(db_path) as db:
            if _optional_table_exists(db, "object_rule_events"):
                camera_join = " LEFT JOIN cameras c ON c.token=e.camera_token" if _optional_table_exists(db, "cameras") else ""
                camera_select = ",c.name AS camera_name" if camera_join else ",NULL AS camera_name"
                items = db.execute(
                    "SELECT e.timestamp,e.rule_name,e.rule_type,e.class_name,e.object_count,e.message" + camera_select
                    + " FROM object_rule_events e" + camera_join + " WHERE " + " AND ".join(clauses)
                    + " ORDER BY e.timestamp DESC,e.id DESC LIMIT ?", [*args, MAX_REPORT_ROWS + 1],
                ).fetchall()
            else:
                items = []
        rows = [[item["timestamp"], "tracking-rule-event", "object_rule_events", item["rule_type"], "", item["message"], item["object_count"], "objects", store._json({"rule_name": item["rule_name"], "class_name": item["class_name"], "camera_name": item["camera_name"]}), "{}", "recorded event"] for item in items]
        return header, rows
    header = ["timestamp_utc", "level", "component", "event_type", "session_id", "correlation_id", "message", "details_json"]
    rows: list[list[Any]] = []
    cursor = None
    while len(rows) <= MAX_REPORT_ROWS:
        page_size = min(200, MAX_REPORT_ROWS + 1 - len(rows))
        items = store.query_events(
            db_path, limit=page_size, after=parameters["after"], before=parameters["before"],
            cursor_before=cursor, level=parameters.get("level"), component=parameters.get("component"),
            event_type=parameters.get("event_type"), session_id=parameters.get("session_id"), correlation_id=parameters.get("correlation_id"),
            training_job_id=parameters.get("training_job_id"), dataset_id=parameters.get("dataset_id"), model_id=parameters.get("model_id"),
            search=parameters.get("search"),
        )
        if not items:
            break
        for item in items:
            rows.append([item["timestamp"], item["level"], item["component"], item["event_type"], item.get("session_id"), item.get("correlation_id"), item["message"], store._json(item["details"])])
        if len(items) < page_size:
            break
        last = items[-1]
        cursor = (last["timestamp"], last["event_id"])
    return header, rows


def _csv_safe(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value if value is not None else "")
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _render_csv(header: list[str], rows: list[list[Any]], *, metadata: dict[str, str] | None = None) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    if metadata is not None:
        header = ["record_kind", *header, "report_id", "generated_at_utc", "filters_json", "data_note"]
        if rows:
            rows = [["data", *row, metadata["report_id"], metadata["generated_at_utc"], metadata["filters_json"], metadata["data_note"]] for row in rows]
        else:
            rows = [["status", *("" for _ in range(len(header) - 5)), metadata["report_id"], metadata["generated_at_utc"], metadata["filters_json"], "No matching records in the selected range; this is not a zero measurement."]]
    writer.writerow(header)
    byte_count = len(output.getvalue().encode("utf-8-sig"))
    for row in rows:
        part = io.StringIO(newline="")
        csv.writer(part, lineterminator="\r\n").writerow([_csv_safe(value) for value in row])
        encoded = part.getvalue().encode("utf-8")
        byte_count += len(encoded)
        if byte_count > MAX_REPORT_BYTES:
            raise ValueError("Report exceeds the 12 MB limit. Narrow the time range or filters.")
        output.write(part.getvalue())
    return output.getvalue().encode("utf-8-sig")


def _pdf_escape_hex(text: str) -> bytes:
    return text.encode("cp1252", errors="replace").hex().upper().encode("ascii")


def _pdf_header_label(value: str) -> str:
    aliases = {
        "timestamp_utc": "Timestamp (UTC)",
        "metric_key": "Metric",
        "display_name": "Metric name",
        "dimensions_json": "Dimensions (JSON)",
        "details_json": "Details (JSON)",
        "actor_ref": "Actor reference",
        "correlation_id": "Correlation ID",
        "session_id": "Session ID",
        "training_job_id": "Training job ID",
        "dataset_id": "Dataset ID",
        "model_id": "Model ID",
        "integrity_hash": "Integrity hash",
        "record_count": "Records",
        "quality": "Data quality",
        "reason": "Availability note",
        "measurement_scope": "Measurement scope",
    }
    if value in aliases:
        return aliases[value]
    return " ".join(part.capitalize() for part in value.split("_") if part)


def _pdf_format_row(header: list[str], row: list[Any]) -> list[str]:
    availability_index = header.index("available") if "available" in header else None
    availability = row[availability_index] if availability_index is not None and availability_index < len(row) else None
    formatted: list[str] = []
    for index, column in enumerate(header):
        value = row[index] if index < len(row) else None
        if column == "available" and isinstance(value, bool):
            formatted.append("Available" if value else "Unavailable")
        elif column == "estimated" and isinstance(value, bool):
            formatted.append("Estimated" if value else "Measured")
        elif value is None and availability is False and column == "value":
            formatted.append("Not recorded")
        elif value is None:
            formatted.append("")
        elif column == "quality":
            formatted.append(str(value).replace("_", " ").capitalize())
        else:
            formatted.append(str(value))
    return formatted


def _pdf_column_width(column: str) -> float:
    widths = {
        "timestamp_utc": 92, "created_at": 92, "started_at_utc": 92,
        "last_heartbeat_at_utc": 96, "clean_shutdown_at_utc": 96,
        "metric_key": 104, "display_name": 104, "event_type": 92,
        "session_id": 108, "training_job_id": 108, "dataset_id": 96,
        "model_id": 96, "correlation_id": 104, "actor_ref": 88,
        "integrity_hash": 125, "details_json": 150, "dimensions_json": 138,
        "reason": 142, "message": 150, "provenance": 104,
        "measurement_scope": 105, "coverage": 130, "data_note": 150,
        "value": 52, "record_count": 48, "unit": 48, "available": 62,
        "quality": 82, "estimated": 62, "level": 56, "outcome": 60,
        "component": 72, "source": 82, "record_type": 82,
        "app_version": 64, "epoch": 48, "interval": 56,
        "aggregation": 74, "exit_reason": 84,
    }
    return float(widths.get(column, 78))


def _pdf_column_groups(header: list[str], available_width: float, *, max_columns: int = 7) -> list[list[int]]:
    """Group wide reports into readable horizontal table sections."""
    groups: list[list[int]] = []
    current: list[int] = []
    used_width = 36.0  # Repeated row number keeps split sections aligned.
    for index, column in enumerate(header):
        width = _pdf_column_width(column)
        if current and (len(current) >= max_columns or used_width + width > available_width):
            groups.append(current)
            current = []
            used_width = 36.0
        current.append(index)
        used_width += width
    if current:
        groups.append(current)
    return groups or [[]]


def _pdf_limit_group_text(cells: list[str], group_columns: list[int], limit: int = 1200) -> list[str]:
    """Bound text independently for each horizontal section and disclose omissions."""
    result = list(cells)
    remaining = limit
    truncated = False
    for index in group_columns:
        value = result[index] if index < len(result) else ""
        if not value:
            continue
        if truncated:
            result[index] = "Omitted (section text limit)"
            continue
        if len(value) > remaining:
            keep = max(0, remaining - 3)
            result[index] = value[:keep] + "..." if keep else "..."
            remaining = 0
            truncated = True
        else:
            remaining = max(0, remaining - len(value) - 3)
            if remaining == 0:
                truncated = True
    return result


def _lifetime_pdf_cards(header: list[str], rows: list[list[Any]]) -> list[tuple[str, str]]:
    index = {name: position for position, name in enumerate(header)}
    metrics = {str(row[index["metric_key"]]): row for row in rows if "metric_key" in index and len(row) > index["metric_key"]}

    def metric_value(key: str) -> str:
        row = metrics.get(key)
        if not row:
            return "Not recorded"
        item = {name: row[position] for name, position in index.items() if position < len(row)}
        if not item.get("available") or item.get("value") is None:
            return "Not recorded"
        value = item["value"]
        if key == "uptime":
            seconds = max(0, int(value))
            days, remainder = divmod(seconds, 86_400)
            hours, remainder = divmod(remainder, 3_600)
            minutes, seconds = divmod(remainder, 60)
            parts = ([f"{days}d"] if days else []) + ([f"{hours}h"] if hours else []) + ([f"{minutes}m"] if minutes else [])
            if not parts or (not days and not hours):
                parts.append(f"{seconds}s")
            return " ".join(parts)
        if key == "energy_wh":
            return f"{float(value):.3f} Wh (estimated)"
        unit = str(item.get("unit") or "").strip()
        return f"{value} {unit}".strip()

    return [
        ("Application starts", metric_value("application_starts")),
        ("Installation uptime", metric_value("uptime")),
        ("API requests", metric_value("api_requests")),
        ("GPU board energy", metric_value("energy_wh")),
    ]


def _render_pdf(
    title: str,
    header: list[str],
    rows: list[list[Any]],
    *,
    metadata: list[str] | None = None,
    summary_cards: list[tuple[str, str]] | None = None,
) -> bytes:
    page_width, page_height = 792, 612  # Letter landscape keeps wide observability records legible.
    margin = 36
    content_width = page_width - margin * 2
    body_size = 7.7
    line_height = 10.1
    meta = {}
    for line in metadata or []:
        key, separator, value = line.partition(":")
        if separator:
            meta[key.strip().lower()] = value.strip()
    report_id = meta.get("report id", "")
    generated = meta.get("generated utc", "")
    cards = summary_cards or [
        ("Rows included", meta.get("rows included", str(len(rows)))),
        ("Requested period", meta.get("applied filters", "Not specified")),
        ("Data source", meta.get("source", "VAOS observability records")),
        ("Coverage", meta.get("coverage", "See report notes")),
    ]
    cards = cards[:4]
    card_meta_aliases = {
        "rows included": "rows included",
        "requested period": "applied filters",
        "data source": "source",
        "coverage": "coverage",
    }
    summarized_meta = {card_meta_aliases[label.casefold()] for label, _ in cards if label.casefold() in card_meta_aliases}
    details = []
    for line in metadata or []:
        key, separator, _ = line.partition(":")
        normalized_key = key.strip().casefold() if separator else ""
        if normalized_key not in {"report id", "generated utc"} and normalized_key not in summarized_meta:
            details.append(line)
    column_groups = _pdf_column_groups(header, content_width)
    pages: list[list[bytes]] = []
    page_ops: list[bytes] = []

    def text_op(x: float, top: float, text: str, size: float, *, bold: bool = False, color: tuple[float, float, float] = (0.12, 0.17, 0.24)) -> bytes:
        font = b"F2" if bold else b"F1"
        baseline = page_height - top - size
        return (
            b"BT /" + font + b" " + f"{size:g}".encode("ascii") + b" Tf "
            + (" ".join(f"{part:g}" for part in color)).encode("ascii")
            + b" rg 1 0 0 1 " + f"{x:g} {baseline:g}".encode("ascii")
            + b" Tm <" + _pdf_escape_hex(text) + b"> Tj ET"
        )

    def rect_op(x: float, top: float, width: float, height: float, color: tuple[float, float, float]) -> bytes:
        bottom = page_height - top - height
        return (
            b"q " + (" ".join(f"{part:g}" for part in color)).encode("ascii")
            + b" rg " + f"{x:g} {bottom:g} {width:g} {height:g}".encode("ascii") + b" re f Q"
        )

    def wrap_pdf(text: str, width: int) -> list[str]:
        return textwrap.wrap(text, width=width, replace_whitespace=True, drop_whitespace=True, break_long_words=True) or [""]

    def start_page(*, first: bool, group_index: int) -> float:
        nonlocal page_ops
        page_ops = []
        group_columns = column_groups[group_index]
        section_text = ""
        if len(column_groups) > 1:
            section_text = (
                f"Table section {group_index + 1} of {len(column_groups)} - columns "
                f"{group_columns[0] + 1}-{group_columns[-1] + 1} of {len(header)}"
                if group_columns else f"Table section {group_index + 1} of {len(column_groups)}"
            )
        if first:
            page_ops.append(text_op(margin, 28, title, 16, bold=True, color=(0.08, 0.20, 0.35)))
            page_ops.append(text_op(margin, 50, "VAOS local observability report - generated from recorded data", 8.5, color=(0.28, 0.34, 0.40)))
            page_ops.append(text_op(margin, 64, f"Report ID: {report_id}    Generated UTC: {generated}", 7.5, color=(0.34, 0.39, 0.45)))
            gap = 8
            card_width = (content_width - gap * (len(cards) - 1)) / max(1, len(cards))
            card_top, card_height = 82, 60
            for card_index, (label, value) in enumerate(cards):
                x = margin + card_index * (card_width + gap)
                page_ops.extend([
                    rect_op(x, card_top, card_width, card_height, (0.94, 0.96, 0.98)),
                    rect_op(x, card_top, 3, card_height, (0.10, 0.43, 0.68)),
                    text_op(x + 10, card_top + 8, label, 7.5, bold=True, color=(0.30, 0.36, 0.43)),
                ])
                card_lines = wrap_pdf(str(value), 36)[:3]
                for line_index, card_line in enumerate(card_lines):
                    page_ops.append(text_op(x + 10, card_top + 24 + line_index * 10, card_line, 8.2, bold=line_index == 0))
            y = card_top + card_height + 12
            for detail in details:
                for detail_line in wrap_pdf(detail, 150):
                    page_ops.append(text_op(margin, y, detail_line, 7.3, color=(0.29, 0.34, 0.40)))
                    y += 9.2
            if section_text:
                page_ops.append(text_op(margin, y, section_text, 7.3, bold=True, color=(0.29, 0.34, 0.40)))
                y += 9.2
            return y + 9
        page_ops.append(text_op(margin, 24, title, 10, bold=True, color=(0.08, 0.20, 0.35)))
        page_ops.append(text_op(margin, 39, f"Report ID: {report_id} - continued", 7.5, color=(0.34, 0.39, 0.45)))
        if section_text:
            page_ops.append(text_op(margin, 51, section_text, 7.3, bold=True, color=(0.29, 0.34, 0.40)))
            return 68
        return 58

    def wrap_cell(text: str, width: float) -> list[str]:
        characters = max(5, int((width - 10) / (body_size * 0.51)))
        return textwrap.wrap(text, width=characters, replace_whitespace=True, drop_whitespace=True, break_long_words=True) or [""]

    def add_table_header(y: float, group_columns: list[int], widths: list[float]) -> float:
        nonlocal page_ops
        labels = ["Record", *(_pdf_header_label(header[index]) for index in group_columns)]
        lines_by_cell = [wrap_cell(label, width) for label, width in zip(labels, widths)]
        row_height = max(19, max(len(lines) for lines in lines_by_cell) * line_height + 7)
        page_ops.append(rect_op(margin, y, content_width, row_height, (0.12, 0.27, 0.40)))
        x = margin
        for cell_index, (lines, width) in enumerate(zip(lines_by_cell, widths)):
            for line_index, line in enumerate(lines):
                page_ops.append(text_op(x + 5, y + 3 + line_index * line_height, line, 7.1, bold=True, color=(1, 1, 1)))
            x += width
            if cell_index < len(widths) - 1:
                xline = x
                page_ops.append(
                    b"q 0.55 0.65 0.73 RG 0.35 w "
                    + f"{xline:g} {page_height - y:g} m {xline:g} {page_height - y - row_height:g} l S Q".encode("ascii")
                )
        return y + row_height

    def fitted_widths(group_columns: list[int]) -> list[float]:
        raw_widths = [36.0, *(_pdf_column_width(header[index]) for index in group_columns)]
        scale = content_width / sum(raw_widths)
        return [width * scale for width in raw_widths]

    empty_state = "No matching records in the selected range; this is not a zero measurement."
    for group_index, group_columns in enumerate(column_groups):
        widths = fitted_widths(group_columns)
        y = add_table_header(start_page(first=group_index == 0, group_index=group_index), group_columns, widths)
        if not rows:
            empty_height = max(18, len(wrap_pdf(empty_state, 132)) * line_height + 6)
            page_ops.append(rect_op(margin, y, content_width, empty_height, (0.97, 0.98, 0.99)))
            page_ops.append(text_op(margin + 7, y + 3, empty_state, body_size))
            page_ops.append(
                b"q 0.80 0.84 0.88 RG 0.35 w "
                + f"{margin:g} {page_height - y - empty_height:g} m {page_width - margin:g} {page_height - y - empty_height:g} l S Q".encode("ascii")
            )
            pages.append(page_ops)
            continue

        for row_index, row in enumerate(rows):
            cells = _pdf_limit_group_text(_pdf_format_row(header, row), group_columns)
            visible_cells = [str(row_index + 1), *(cells[index] if index < len(cells) else "" for index in group_columns)]
            lines_by_cell = [wrap_cell(text, width) for text, width in zip(visible_cells, widths)]
            line_offset = 0
            longest_cell = max(len(lines) for lines in lines_by_cell)
            continued = False

            while line_offset < longest_cell:
                available_height = page_height - 36 - y
                capacity = 0 if available_height < 18 else max(1, int((available_height - 6) // line_height))
                next_page_y = 68 if len(column_groups) > 1 else 58
                next_page_height = page_height - 36 - next_page_y
                next_page_capacity = max(1, int((next_page_height - 6) // line_height))
                move_whole_row = line_offset == 0 and capacity < longest_cell and longest_cell <= next_page_capacity
                if capacity < 1 or move_whole_row:
                    pages.append(page_ops)
                    y = add_table_header(start_page(first=False, group_index=group_index), group_columns, widths)
                    available_height = page_height - 36 - y
                    capacity = 0 if available_height < 18 else max(1, int((available_height - 6) // line_height))
                if capacity < 1:
                    # The header itself should always leave room for at least one row line.
                    raise ValueError("PDF table header leaves no room for a report row.")

                line_count = min(capacity, longest_cell - line_offset)
                fragment_lines: list[list[str]] = []
                for cell_index, cell_lines in enumerate(lines_by_cell):
                    if cell_index == 0 and continued:
                        fragment_lines.append(wrap_cell(f"{row_index + 1} cont", widths[0])[:line_count])
                    else:
                        fragment_lines.append(cell_lines[line_offset:line_offset + line_count])
                row_height = max(18, max((len(lines) for lines in fragment_lines), default=1) * line_height + 6)
                if y + row_height > page_height - 36 + 0.01:
                    raise ValueError("PDF row fragment exceeds the printable page area.")

                page_ops.append(rect_op(margin, y, content_width, row_height, (0.97, 0.98, 0.99) if row_index % 2 == 0 else (1, 1, 1)))
                x = margin
                for cell_index, (lines, width) in enumerate(zip(fragment_lines, widths)):
                    for line_index, line in enumerate(lines):
                        page_ops.append(text_op(x + 5, y + 3 + line_index * line_height, line, body_size))
                    x += width
                    if cell_index < len(widths) - 1:
                        xline = x
                        page_ops.append(
                            b"q 0.82 0.86 0.89 RG 0.3 w "
                            + f"{xline:g} {page_height - y:g} m {xline:g} {page_height - y - row_height:g} l S Q".encode("ascii")
                        )
                page_ops.append(
                    b"q 0.80 0.84 0.88 RG 0.35 w "
                    + f"{margin:g} {page_height - y - row_height:g} m {page_width - margin:g} {page_height - y - row_height:g} l S Q".encode("ascii")
                )
                y += row_height
                line_offset += line_count
                continued = line_offset < longest_cell
                if continued:
                    pages.append(page_ops)
                    y = add_table_header(start_page(first=False, group_index=group_index), group_columns, widths)
        pages.append(page_ops)

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{5 + i * 2} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    for page_index, operations in enumerate(pages):
        page_ops = list(operations)
        page_ops.append(text_op(page_width - margin - 54, page_height - 25, f"Page {page_index + 1} of {len(pages)}", 7.5, color=(0.34, 0.39, 0.45)))
        stream = b"\n".join(page_ops)
        page_obj = 5 + page_index * 2
        content_obj = page_obj + 1
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_obj} 0 R >>".encode("ascii"))
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream")
    output = bytearray(b"%PDF-1.4\n%VAOS\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
        if len(output) > MAX_REPORT_BYTES:
            raise ValueError("Report exceeds the 12 MB limit. Narrow the time range or filters.")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(output)


def process_report_job(db_path: str | Path, report_dir: str | Path, job_id: str) -> dict[str, Any] | None:
    database = Path(db_path).expanduser().resolve()
    output_dir = Path(report_dir).expanduser().resolve()
    _secure_report_directory(output_dir)
    with store._connection(database) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT report_type,parameters_json,status FROM observability_report_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row or row["status"] != "queued":
            db.commit()
            return get_report_job(database, job_id)
        db.execute("UPDATE observability_report_jobs SET status='running',progress_percent=5 WHERE job_id=? AND status='queued'", (job_id,))
        db.commit()
    format_name = "csv"
    destination: Path | None = None
    temporary: Path | None = None
    try:
        report_type = str(row["report_type"])
        parameters = json.loads(str(row["parameters_json"]))
        format_name = str(parameters.pop("format", "csv"))
        header, rows = _report_rows(database, report_type, parameters)
        matching_row_count = len(rows)
        row_limit = MAX_METRIC_REPORT_ROWS if report_type == "metric-series" else MAX_REPORT_ROWS
        if len(rows) > row_limit:
            raise ValueError(f"Report would exceed its {row_limit:,}-row limit. Narrow the time range or filters before exporting.")
        if not rows:
            header, rows = _with_empty_status(report_type, header, rows)
        with store._connection(database) as db:
            db.execute("UPDATE observability_report_jobs SET progress_percent=65 WHERE job_id=? AND status='running'", (job_id,))
        if format_name == "csv":
            definition = next(item for item in REPORT_DEFINITIONS if item["report_type"] == report_type)
            content = _render_csv(header, rows, metadata={
                "report_id": job_id,
                "generated_at_utc": store.utc_now(),
                "filters_json": store._json(parameters),
                "data_note": str(definition["coverage"]),
            })
        elif format_name == "pdf":
            filter_summary = ", ".join(f"{key}={value}" for key, value in sorted(parameters.items()) if key != "format") or "installation lifetime"
            definition = next(item for item in REPORT_DEFINITIONS if item["report_type"] == report_type)
            pdf_metadata = [f"Report ID: {job_id}", f"Generated UTC: {store.utc_now()}", f"Applied filters: {filter_summary}", f"Rows included: {matching_row_count}", f"Source: {definition['source']}", f"Coverage: {definition['coverage']}", "Data note: missing or expired source history is not reconstructed.", "Each table section limits row text to 1,200 characters; omitted values are marked.", "Rows marked 'cont' continue on the next page."]
            if report_type == "lifetime-statistics":
                summary_cards = _lifetime_pdf_cards(header, rows)
            else:
                summary_cards = [
                    ("Rows included", f"{matching_row_count:,}"),
                    ("Requested period", filter_summary),
                    ("Data source", definition["source"]),
                    ("Coverage", definition["coverage"]),
                ]
            content = _render_pdf(f"VAOS Observability - {definition['label']}", header, rows, metadata=pdf_metadata, summary_cards=summary_cards)
        else:
            raise ValueError("Unsupported report format.")
        filename = f"{job_id}.{format_name}"
        destination = output_dir / filename
        temporary = output_dir / f".{job_id}.{format_name}.part"
        temporary.unlink(missing_ok=True)
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        digest = hashlib.sha256(content).hexdigest()
        with store._connection(database) as db:
            db.execute("UPDATE observability_report_jobs SET status='complete',progress_percent=100,output_name=?,output_sha256=?,output_size_bytes=?,error_code=NULL WHERE job_id=? AND status='running'", (filename, digest, len(content), job_id))
        return get_report_job(database, job_id)
    except Exception as exc:
        if temporary:
            temporary.unlink(missing_ok=True)
        if destination:
            destination.unlink(missing_ok=True)
        with store._connection(database) as db:
            error_text = str(exc).casefold()
            error_code = "report_limit_exceeded" if isinstance(exc, ValueError) and "row" in error_text and "limit" in error_text else "report_generation_failed"
            db.execute("UPDATE observability_report_jobs SET status='failed',progress_percent=0,error_code=? WHERE job_id=? AND status='running'", (error_code, job_id))
        return get_report_job(database, job_id)


def record_download(db_path: str | Path, *, job_id: str, requested_by: str) -> bool:
    with store._connection(Path(db_path).expanduser().resolve()) as db:
        db.execute("BEGIN IMMEDIATE")
        return record_download_in_transaction(db, job_id=job_id, requested_by=requested_by)


def record_download_in_transaction(db: Any, *, job_id: str, requested_by: str) -> bool:
    """Record an authorized export in the caller's audit transaction."""
    if not db.in_transaction:
        raise RuntimeError("Report download auditing requires an active SQLite transaction.")
    job = get_report_job_in_transaction(db, job_id)
    now = store.utc_now()
    if not job or job["status"] != "complete" or store._parse_time(job["expires_at"]) <= store._parse_time(now):
        return False
    export_id = uuid.uuid4().hex
    db.execute("INSERT INTO observability_export_audit(export_id,timestamp,report_job_id,actor_ref,report_type,format,filters_json) VALUES(?,?,?,?,?,?,?)", (export_id, now, job_id, str(requested_by)[:120], job["report_type"], job["format"], store._json(job["filters"])))
    return True


def report_file_path(report_dir: str | Path, job: dict[str, Any]) -> Path | None:
    name = job.get("output_name")
    if not isinstance(name, str) or name != f"{job.get('job_id')}.{job.get('format')}":
        return None
    root = Path(report_dir).expanduser().resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root or not candidate.is_file() or candidate.stat().st_size != job.get("output_size_bytes"):
        return None
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return candidate if hmac_compare(digest, str(job.get("output_sha256") or "")) else None


def hmac_compare(first: str, second: str) -> bool:
    # Avoid an extra dependency while keeping the artifact digest comparison constant-time.
    return hmac.compare_digest(first, second)


def cancel_report_job(db_path: str | Path, job_id: str, *, requested_by: str) -> bool:
    with store._connection(Path(db_path).expanduser().resolve()) as db:
        db.execute("BEGIN IMMEDIATE")
        return cancel_report_job_in_transaction(db, job_id, requested_by=requested_by)


def cancel_report_job_in_transaction(db: Any, job_id: str, *, requested_by: str) -> bool:
    """Cancel a queued job inside the caller's audit transaction."""
    if not db.in_transaction:
        raise RuntimeError("Report cancellation requires an active SQLite transaction.")
    result = db.execute(
        "UPDATE observability_report_jobs SET status='cancelled',progress_percent=0,error_code='cancelled_by_user' WHERE job_id=? AND requested_by=? AND status='queued'",
        (job_id, str(requested_by)[:120]),
    )
    return result.rowcount == 1


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vaos-observability-report")
        return _executor


def submit_report_job(db_path: str | Path, report_dir: str | Path, job_id: str) -> None:
    _get_executor().submit(process_report_job, db_path, report_dir, job_id)


def submit_queued_report_jobs(db_path: str | Path, report_dir: str | Path, job_ids: list[str]) -> None:
    executor = _get_executor()
    for job_id in job_ids[:MAX_LIST_JOBS]:
        executor.submit(process_report_job, db_path, report_dir, job_id)


def expire_reports(db_path: str | Path, report_dir: str | Path, *, now: str | None = None) -> int:
    current = store._normalize_time(now or store.utc_now())
    with store._connection(Path(db_path).expanduser().resolve()) as db:
        rows = db.execute("SELECT job_id,output_name FROM observability_report_jobs WHERE status='complete' AND expires_at<=?", (current,)).fetchall()
    root = Path(report_dir).expanduser().resolve()
    expired = 0
    for row in rows:
        name = str(row["output_name"] or "")
        if name == f"{row['job_id']}.csv" or name == f"{row['job_id']}.pdf":
            candidate = (root / name).resolve()
            if candidate.parent == root:
                candidate.unlink(missing_ok=True)
        with store._connection(Path(db_path).expanduser().resolve()) as db:
            db.execute("UPDATE observability_report_jobs SET status='expired',output_name=NULL WHERE job_id=? AND status='complete' AND expires_at<=?", (row["job_id"], current))
        expired += 1
    return expired
