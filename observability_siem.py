"""Opt-in external delivery of redacted security events.

Supported transports are signed HTTPS JSON, RFC 5424 syslog over TLS, and
OTLP/HTTP JSON logs. Destinations remain disabled until explicitly enabled.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import socket
import sqlite3
import ssl
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import observability_store as store

MAX_DESTINATIONS = 10
MAX_ATTEMPTS = 8
MAX_DELIVERY_ROWS = 50_000
MAX_PENDING_PER_DESTINATION = 5_000
DELIVERY_HISTORY_DAYS = 90
DISPATCH_LEASE_SECONDS = 30
DNS_LOOKUP_TIMEOUT_SECONDS = 3.0
DNS_RESOLVER_WORKERS = 2
DNS_RESOLVER_QUEUE_SIZE = 8
MIN_HMAC_SECRET_BYTES = 32
SIEM_CA_BUNDLE_ENV = "VAOS_SIEM_CA_BUNDLE"
MAX_SIEM_CA_BUNDLE_BYTES = 4 * 1024 * 1024
SECURITY_EVENT_FALLBACK_MAX_ROWS = 10_000
SECURITY_EVENT_FALLBACK_MAX_BYTES = 32 * 1024 * 1024
_SECURITY_EVENT_FALLBACK_INIT_LOCK = threading.Lock()
_SECURITY_EVENT_FALLBACK_INITIALIZED: set[str] = set()
_SECURITY_EVENT_FALLBACK_TYPES = frozenset({"auth.login_failed", "auth.login_succeeded", "authz.request_denied"})
_SECURITY_EVENT_FALLBACK_ACTOR = re.compile(r"\A(?:local-admin|unauthenticated-client|api-token:vaos_[A-Za-z0-9_-]{9})\Z")
_DESTINATION_ID = re.compile(r"^[a-f0-9]{32}$")
_OPAQUE_EVENT_ID = re.compile(r"^[A-Fa-f0-9]{32}$")
_SECRET_REF = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_EVENT_FORMATS = {"json", "ndjson", "cef", "ecs"}
logger = logging.getLogger(__name__)
_dns_requests: queue.Queue[
    tuple[str, int, queue.Queue[tuple[bool, Any]]]
] = queue.Queue(maxsize=DNS_RESOLVER_QUEUE_SIZE)
_dns_workers: list[threading.Thread] = []
_dns_workers_lock = threading.Lock()


def _configured_secret(secret_ref: str) -> str:
    """Read an environment-referenced signing key and enforce HMAC key length."""
    secret = os.getenv(secret_ref, "")
    if len(secret.encode("utf-8")) < MIN_HMAC_SECRET_BYTES:
        return ""
    return secret


def _configured_value(secret_ref: str) -> str:
    return os.getenv(secret_ref, "") if _SECRET_REF.fullmatch(str(secret_ref or "")) else ""


def _create_tls_context() -> ssl.SSLContext:
    """Create a verifying client context, optionally adding an operator CA bundle.

    The configured bundle augments platform roots; it never disables certificate
    or hostname verification. Invalid configuration fails closed without
    including a local file path in the returned error.
    """
    try:
        context = ssl.create_default_context()
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError("tls_context_unavailable") from exc

    configured_bundle = os.getenv(SIEM_CA_BUNDLE_ENV, "").strip()
    if configured_bundle:
        try:
            bundle_path = Path(configured_bundle).expanduser()
            if not bundle_path.is_absolute() or not bundle_path.is_file():
                raise ValueError("invalid_ca_bundle_path")
            with bundle_path.open("rb") as bundle_file:
                bundle_bytes = bundle_file.read(MAX_SIEM_CA_BUNDLE_BYTES + 1)
            if len(bundle_bytes) > MAX_SIEM_CA_BUNDLE_BYTES:
                raise ValueError("ca_bundle_too_large")
            context.load_verify_locations(cadata=bundle_bytes.decode("ascii"))
        except (OSError, ssl.SSLError, ValueError, RuntimeError) as exc:
            raise RuntimeError("tls_ca_bundle_invalid") from exc

    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("tls_verification_unavailable")
    return context


def _validate_https_endpoint(endpoint: str, *, label: str) -> tuple[str, str]:
    value = str(endpoint).strip()
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
        raise ValueError(f"{label} endpoint must be an HTTPS URL on port 443, without credentials, query parameters or fragment.")
    host = parsed.hostname.rstrip(".").casefold()
    if host in {"localhost", "localhost.localdomain"} or "." not in host:
        raise ValueError(f"{label} endpoint must use a public DNS hostname.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Use a DNS hostname instead of an IP address for outbound delivery.")
    path = parsed.path or "/"
    if len(path) > 1024 or "\\" in path or any(ord(char) < 32 for char in path):
        raise ValueError("Outbound endpoint path is invalid or too long.")
    return host, path


def validate_endpoint(endpoint: str, transport: str = "https_json") -> tuple[str, str, int]:
    """Validate a transport URL and return its DNS host, path and fixed port."""
    if transport in {"https_json", "otlp"}:
        host, path = _validate_https_endpoint(
            endpoint, label="OTLP" if transport == "otlp" else "Webhook"
        )
        return host, path, 443
    if transport != "syslog_tls":
        raise ValueError("Choose signed HTTPS JSON, Syslog TLS, or OTLP/HTTP JSON.")
    parsed = urlsplit(str(endpoint).strip())
    if (parsed.scheme != "syslog-tls" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, 6514)):
        raise ValueError("Syslog TLS must use syslog-tls://public-hostname[:6514] with no path, credentials, query or fragment.")
    host = parsed.hostname.rstrip(".").casefold()
    if host in {"localhost", "localhost.localdomain"} or "." not in host:
        raise ValueError("Syslog TLS must use a public DNS hostname.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Use a DNS hostname instead of an IP address for outbound delivery.")
    return host, "", 6514


def configure_destination(
    db_path: str | Path, *, display_name: str, endpoint: str, secret_ref: str | None,
    transport: str = "https_json", event_format: str = "json", enabled: bool = False,
    destination_id: str | None = None,
) -> dict[str, Any]:
    database = Path(db_path).expanduser().resolve()
    with store._connection(database) as db:
        db.execute("BEGIN IMMEDIATE")
        return configure_destination_in_transaction(
            db, display_name=display_name, endpoint=endpoint, secret_ref=secret_ref,
            transport=transport, event_format=event_format, enabled=enabled,
            destination_id=destination_id,
        )


def configure_destination_in_transaction(
    db: Any, *, display_name: str, endpoint: str, secret_ref: str | None,
    transport: str = "https_json", event_format: str = "json", enabled: bool = False,
    destination_id: str | None = None,
) -> dict[str, Any]:
    """Create or update a destination inside the caller's SQLite transaction."""
    if not db.in_transaction:
        raise RuntimeError("SIEM destination changes require an active SQLite transaction.")
    name = store._safe_text(display_name.strip(), 80)
    if not name:
        raise ValueError("Destination name is required.")
    if transport not in {"https_json", "syslog_tls", "otlp"}:
        raise ValueError("Choose signed HTTPS JSON, Syslog TLS, or OTLP/HTTP JSON.")
    if event_format not in _EVENT_FORMATS:
        raise ValueError("Choose JSON, NDJSON, CEF, or ECS event mapping.")
    if transport == "otlp" and event_format != "json":
        raise ValueError("OTLP uses its standard OpenTelemetry log mapping; choose JSON as the event format.")
    host, _path, port = validate_endpoint(endpoint, transport)
    if destination_id and not _DESTINATION_ID.fullmatch(destination_id):
        raise ValueError("Destination ID is invalid.")
    if destination_id and secret_ref is None:
        # The edit UI intentionally never receives the stored endpoint path or
        # credential reference. Preserve the reference only for the same
        # transport; changing transports starts with no credential configured.
        existing = db.execute(
            "SELECT transport,secret_ref FROM observability_siem_destinations WHERE destination_id=? AND archived_at IS NULL",
            (destination_id,),
        ).fetchone()
        if not existing:
            raise ValueError("SIEM destination was not found.")
        secret_ref = str(existing["secret_ref"] or "") if str(existing["transport"]) == transport else ""
    secret_ref = str(secret_ref or "").strip()
    if secret_ref and not _SECRET_REF.fullmatch(secret_ref):
        raise ValueError("Credential reference must be an environment variable name, for example VAOS_SIEM_TOKEN.")
    if transport == "https_json" and not secret_ref:
        raise ValueError("Signed HTTPS JSON requires an HMAC key environment variable name.")
    if transport == "syslog_tls" and secret_ref:
        raise ValueError("Syslog TLS uses certificate-validated TLS; it does not use an HMAC key field.")
    if enabled and transport == "https_json" and not _configured_secret(secret_ref):
        raise ValueError(f"The HMAC key must be configured in the service environment and contain at least {MIN_HMAC_SECRET_BYTES} UTF-8 bytes before the destination can be enabled.")
    if enabled and transport == "otlp" and secret_ref and not os.getenv(secret_ref):
        raise ValueError("The referenced OTLP bearer-token environment variable is not configured.")
    identifier = destination_id or secrets.token_hex(16)
    if destination_id:
        existing = db.execute("SELECT destination_id FROM observability_siem_destinations WHERE destination_id=? AND archived_at IS NULL", (identifier,)).fetchone()
        if not existing:
            raise ValueError("SIEM destination was not found.")
        db.execute("UPDATE observability_siem_destinations SET display_name=?,transport=?,endpoint=?,enabled=?,secret_ref=?,redaction_profile='default',event_format=?,updated_at=? WHERE destination_id=?", (name, transport, endpoint.strip(), int(enabled), secret_ref, event_format, store.utc_now(), identifier))
    else:
        count = int(db.execute("SELECT COUNT(*) FROM observability_siem_destinations WHERE archived_at IS NULL").fetchone()[0])
        if count >= MAX_DESTINATIONS:
            raise ValueError(f"VAOS supports at most {MAX_DESTINATIONS} SIEM destinations in this installation.")
        timestamp = store.utc_now()
        db.execute("INSERT INTO observability_siem_destinations(destination_id,display_name,transport,endpoint,enabled,secret_ref,redaction_profile,event_format,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (identifier, name, transport, endpoint.strip(), int(enabled), secret_ref, "default", event_format, timestamp, timestamp))
    return {"destination_id": identifier, "display_name": name, "transport": transport, "event_format": event_format, "endpoint_host": host, "endpoint_port": port, "enabled": bool(enabled), "secret_configured": bool(secret_ref and os.getenv(secret_ref)), "updated_at": store.utc_now()}


def list_destinations(db_path: str | Path) -> dict[str, Any]:
    database = Path(db_path).expanduser().resolve()
    with store._connection(database) as db:
        rows = db.execute("SELECT d.destination_id,d.display_name,d.transport,d.event_format,d.endpoint,d.enabled,d.secret_ref,d.dropped_events,d.created_at,d.updated_at,(SELECT COUNT(*) FROM observability_delivery_events e WHERE e.destination_id=d.destination_id AND e.status IN ('queued','retrying')) AS pending,(SELECT COUNT(*) FROM observability_delivery_events e WHERE e.destination_id=d.destination_id AND e.status='dead_letter') AS dead_letter,(SELECT MAX(updated_at) FROM observability_delivery_events e WHERE e.destination_id=d.destination_id AND e.status='sent') AS last_success_at FROM observability_siem_destinations d WHERE d.archived_at IS NULL ORDER BY d.created_at DESC LIMIT ?", (MAX_DESTINATIONS,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            endpoint = item.pop("endpoint")
            item["endpoint_host"] = urlsplit(endpoint).hostname
            item["endpoint_port"] = validate_endpoint(endpoint, str(item["transport"]))[2]
        except ValueError:
            item["endpoint_host"] = "invalid endpoint"
            item.pop("endpoint", None)
        secret_ref = str(item.pop("secret_ref") or "")
        item["secret_configured"] = bool(secret_ref and (_configured_secret(secret_ref) if item["transport"] == "https_json" else _configured_value(secret_ref)))
        item["credential_present"] = bool(secret_ref)
        item["credential_required"] = item["transport"] == "https_json"
        item["enabled"] = bool(item["enabled"])
        items.append(item)
    return {"items": items, "supported_transports": ["https_json", "syslog_tls", "otlp"], "unsupported_transports": {}, "outbound_enabled_by_default": False, "delivery_limits": {"maximum_rows": MAX_DELIVERY_ROWS, "maximum_pending_per_destination": MAX_PENDING_PER_DESTINATION, "terminal_history_days": DELIVERY_HISTORY_DAYS}}


def set_destination_enabled(db_path: str | Path, destination_id: str, enabled: bool) -> bool:
    if not _DESTINATION_ID.fullmatch(destination_id):
        return False
    database = Path(db_path).expanduser().resolve()
    with store._connection(database) as db:
        db.execute("BEGIN IMMEDIATE")
        return set_destination_enabled_in_transaction(db, destination_id, enabled)


def set_destination_enabled_in_transaction(db: Any, destination_id: str, enabled: bool) -> bool:
    """Toggle delivery inside the caller's SQLite transaction."""
    if not db.in_transaction:
        raise RuntimeError("SIEM destination toggles require an active SQLite transaction.")
    if not _DESTINATION_ID.fullmatch(destination_id):
        return False
    row = db.execute("SELECT transport,secret_ref,archived_at FROM observability_siem_destinations WHERE destination_id=?", (destination_id,)).fetchone()
    if not row:
        return False
    if row["archived_at"]:
        return False
    transport, secret_ref = str(row["transport"]), str(row["secret_ref"] or "")
    if enabled and transport == "https_json" and (not _SECRET_REF.fullmatch(secret_ref) or not _configured_secret(secret_ref)):
        raise ValueError(f"Configure the referenced HMAC key with at least {MIN_HMAC_SECRET_BYTES} UTF-8 bytes in the service environment before enabling this destination.")
    if enabled and transport == "otlp" and secret_ref and (not _SECRET_REF.fullmatch(secret_ref) or not os.getenv(secret_ref)):
        raise ValueError("Configure the referenced OTLP bearer-token environment variable before enabling this destination.")
    db.execute("UPDATE observability_siem_destinations SET enabled=?,updated_at=? WHERE destination_id=?", (int(enabled), store.utc_now(), destination_id))
    if not enabled:
        db.execute("UPDATE observability_delivery_events SET status='disabled',updated_at=? WHERE destination_id=? AND status IN ('queued','retrying')", (store.utc_now(), destination_id))
    else:
        db.execute("UPDATE observability_delivery_events SET status='queued',next_attempt_at=NULL,updated_at=? WHERE destination_id=? AND status='disabled'", (store.utc_now(), destination_id))
    return True


def archive_destination(db_path: str | Path, destination_id: str) -> dict[str, str] | None:
    """Remove a destination from active use while preserving its delivery/audit history."""
    if not _DESTINATION_ID.fullmatch(destination_id):
        return None
    database = Path(db_path).expanduser().resolve()
    now = store.utc_now()
    with store._connection(database) as db:
        db.execute("BEGIN IMMEDIATE")
        return archive_destination_in_transaction(db, destination_id)


def archive_destination_in_transaction(db: Any, destination_id: str) -> dict[str, str] | None:
    """Archive a destination inside the caller's SQLite transaction."""
    if not db.in_transaction:
        raise RuntimeError("SIEM destination removal requires an active SQLite transaction.")
    if not _DESTINATION_ID.fullmatch(destination_id):
        return None
    row = db.execute("SELECT display_name,endpoint,transport,archived_at FROM observability_siem_destinations WHERE destination_id=?", (destination_id,)).fetchone()
    if not row or row["archived_at"]:
        return None
    now = store.utc_now()
    host = urlsplit(str(row["endpoint"])).hostname or "unknown host"
    db.execute("UPDATE observability_siem_destinations SET enabled=0,endpoint='https://removed.invalid/',secret_ref='',archived_at=?,updated_at=? WHERE destination_id=?", (now, now, destination_id))
    db.execute("UPDATE observability_delivery_events SET status='disabled',next_attempt_at=NULL,updated_at=? WHERE destination_id=? AND status IN ('queued','retrying')", (now, destination_id))
    return {"destination_id": destination_id, "display_name": str(row["display_name"]), "transport": str(row["transport"]), "endpoint_host": host, "archived_at": now}


def _enqueue_security_event_in_transaction(db: Any, event_id: str, *, now: str) -> int:
    """Add delivery rows using a caller-owned SQLite write transaction."""
    retention = db.execute(
        "SELECT delivery_history_days FROM observability_retention_policy WHERE singleton_id=1"
    ).fetchone()
    retention_days = int(retention["delivery_history_days"]) if retention else DELIVERY_HISTORY_DAYS
    history_before = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    db.execute("DELETE FROM observability_delivery_events WHERE status IN ('sent','dead_letter','disabled') AND updated_at<?", (history_before,))
    destinations = db.execute("SELECT destination_id FROM observability_siem_destinations WHERE enabled=1 AND archived_at IS NULL").fetchall()
    total_rows = int(db.execute("SELECT COUNT(*) FROM observability_delivery_events").fetchone()[0])
    inserted = 0
    for row in destinations:
        destination_id = str(row["destination_id"])
        if db.execute("SELECT 1 FROM observability_delivery_events WHERE destination_id=? AND event_id=?", (destination_id, event_id)).fetchone():
            continue
        pending = int(db.execute("SELECT COUNT(*) FROM observability_delivery_events WHERE destination_id=? AND status IN ('queued','retrying')", (destination_id,)).fetchone()[0])
        if total_rows >= MAX_DELIVERY_ROWS:
            evicted = db.execute("DELETE FROM observability_delivery_events WHERE delivery_id=(SELECT delivery_id FROM observability_delivery_events WHERE status IN ('sent','dead_letter','disabled') ORDER BY updated_at,delivery_id LIMIT 1)")
            if evicted.rowcount:
                total_rows -= 1
        if pending >= MAX_PENDING_PER_DESTINATION or total_rows >= MAX_DELIVERY_ROWS:
            db.execute("UPDATE observability_siem_destinations SET dropped_events=dropped_events+1,updated_at=? WHERE destination_id=?", (now, destination_id))
            dropped = int(db.execute("SELECT dropped_events FROM observability_siem_destinations WHERE destination_id=?", (destination_id,)).fetchone()[0])
            if dropped & (dropped - 1) == 0:
                logger.warning("SIEM queue capacity reached; external delivery skipped destination=%s dropped_total=%s", destination_id, dropped)
            continue
        cursor = db.execute("INSERT OR IGNORE INTO observability_delivery_events(delivery_id,destination_id,event_id,status,attempt_count,next_attempt_at,updated_at) VALUES(?,?,?,'queued',0,?,?)", (secrets.token_hex(16), destination_id, event_id, now, now))
        inserted += max(0, cursor.rowcount)
        total_rows += max(0, cursor.rowcount)
    return inserted


def enqueue_security_event(db_path: str | Path, event_id: str) -> int:
    database = Path(db_path).expanduser().resolve()
    with store._connection(database) as db:
        db.execute("BEGIN IMMEDIATE")
        inserted = _enqueue_security_event_in_transaction(db, event_id, now=store.utc_now())
        db.commit()
        return inserted


def record_security_event_and_enqueue_in_transaction(
    db: Any, *, event_type: str, outcome: str, component: str,
    actor_ref: str = "", details: dict[str, Any] | None = None,
    correlation_id: str | None = None, event_id: str | None = None,
    timestamp: str | None = None,
) -> bool:
    """Append an audit, event and outbox row in the caller's active transaction."""
    if not db.in_transaction:
        raise RuntimeError("Security event persistence requires an active SQLite transaction.")
    identifier = event_id or secrets.token_hex(16)
    now = store._normalize_time(timestamp or store.utc_now())
    safe_details = {"outcome": outcome, **(details or {})}
    inserted = store.record_security_event_in_transaction(
        db, event_type=event_type, outcome=outcome, component=component,
        actor_ref=actor_ref, details=safe_details, correlation_id=correlation_id,
        event_id=identifier, timestamp=now,
    )
    store._insert_event(
        db, event_id=identifier, timestamp=now, event_type="security." + event_type[:86],
        component=component, level="WARNING" if outcome in {"denied", "failed"} else "INFO",
        message="A structured security audit event was recorded.", session_id=None,
        correlation_id=correlation_id, details=safe_details,
    )
    # Event timestamps describe when the source says the event happened;
    # they may be delayed or clock-skewed. Delivery scheduling must use
    # the local enqueue time so future-dated events are not held until
    # their claimed occurrence time.
    _enqueue_security_event_in_transaction(db, identifier, now=store.utc_now())
    return inserted


def record_security_event_and_enqueue(
    db_path: str | Path, *, event_type: str, outcome: str, component: str,
    actor_ref: str = "", details: dict[str, Any] | None = None,
    correlation_id: str | None = None, event_id: str | None = None,
    timestamp: str | None = None,
) -> bool:
    """Atomically persist a security audit, its operational event and outbox rows."""
    with store._connection(Path(db_path).expanduser().resolve()) as db:
        db.execute("BEGIN IMMEDIATE")
        inserted = record_security_event_and_enqueue_in_transaction(
            db, event_type=event_type, outcome=outcome, component=component,
            actor_ref=actor_ref, details=details, correlation_id=correlation_id,
            event_id=event_id, timestamp=timestamp,
        )
        db.commit()
        return inserted


def security_event_fallback_database_path(db_path: str | Path) -> Path:
    """Return the private sidecar path for authentication/authorization audit recovery."""
    primary = Path(db_path).expanduser().resolve()
    return primary.parent / ".vaos-observability-fallback" / "security-events.sqlite3"


def initialize_security_event_fallback_store(db_path: str | Path) -> Path:
    """Create a bounded private sidecar used only if auth-event writes fail."""
    fallback_path = security_event_fallback_database_path(db_path)
    key = str(fallback_path)
    with _SECURITY_EVENT_FALLBACK_INIT_LOCK:
        if key in _SECURITY_EVENT_FALLBACK_INITIALIZED and fallback_path.is_file() and not fallback_path.is_symlink() and not fallback_path.parent.is_symlink():
            return fallback_path
        if fallback_path.parent.is_symlink() or fallback_path.is_symlink():
            raise RuntimeError("Refusing to use a linked VAOS security-event fallback path.")
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        store._restrict_private_path(fallback_path.parent, directory=True)
        with store._connection(fallback_path, timeout_seconds=2.0) as db:
            db.execute("PRAGMA page_size=4096")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute(f"PRAGMA max_page_count={SECURITY_EVENT_FALLBACK_MAX_BYTES // 4096}")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS security_event_fallback_metadata (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                    schema_version INTEGER NOT NULL,
                    dropped_events INTEGER NOT NULL DEFAULT 0 CHECK(dropped_events >= 0)
                );
                INSERT OR IGNORE INTO security_event_fallback_metadata(singleton_id,schema_version,dropped_events) VALUES(1,1,0);
                CREATE TABLE IF NOT EXISTS security_event_fallback_queue (
                    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    component TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    correlation_id TEXT,
                    timestamp TEXT NOT NULL,
                    queued_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_security_event_fallback_queue_order
                    ON security_event_fallback_queue(sequence_id,event_id);
                """
            )
            metadata = db.execute("SELECT schema_version FROM security_event_fallback_metadata WHERE singleton_id=1").fetchone()
            if not metadata or int(metadata["schema_version"]) != 1:
                raise RuntimeError("The VAOS security-event fallback queue has an unsupported schema version.")
        store._restrict_private_path(fallback_path, directory=False)
        _SECURITY_EVENT_FALLBACK_INITIALIZED.add(key)
    return fallback_path


@contextmanager
def _security_event_fallback_connection(path: Path, *, timeout_seconds: float = 0.25):
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise RuntimeError("The VAOS security-event fallback queue is not initialized.")
    with store._connection(path, timeout_seconds=timeout_seconds) as db:
        db.execute("PRAGMA synchronous=FULL")
        page_size = max(1, int(db.execute("PRAGMA page_size").fetchone()[0]))
        configured_page_limit = max(1, SECURITY_EVENT_FALLBACK_MAX_BYTES // page_size)
        page_limit = int(db.execute(f"PRAGMA max_page_count={configured_page_limit}").fetchone()[0])
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        if page_limit > configured_page_limit or page_count > configured_page_limit:
            raise RuntimeError("The VAOS security-event fallback queue exceeded its SQLite page limit.")
        yield db


def queue_security_event_fallback(
    db_path: str | Path,
    *,
    event_type: str,
    outcome: str,
    component: str,
    actor_ref: str,
    details: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    event_id: str,
    timestamp: str,
) -> bool:
    """Durably queue an allowlisted authentication audit without client addresses or credentials."""
    if event_type not in _SECURITY_EVENT_FALLBACK_TYPES or component not in {"auth", "authorization"}:
        raise ValueError("Only authentication and authorization events can use this fallback queue.")
    if outcome not in {"accepted", "denied"} or not _OPAQUE_EVENT_ID.fullmatch(event_id):
        raise ValueError("The authentication fallback event identity or outcome is invalid.")
    if details is not None and not isinstance(details, dict):
        raise ValueError("Authentication fallback details must be an object.")
    safe_details = store.redact_details(details or {})
    if not isinstance(safe_details, dict):
        raise ValueError("Authentication fallback details must be an object.")
    safe_details.pop("outcome", None)
    details_json = store._json({**safe_details, "outcome": outcome})
    if len(details_json.encode("utf-8")) > store.MAX_EVENT_DETAILS_BYTES:
        raise ValueError("Authentication fallback details exceed the event size limit.")
    safe_actor = store._safe_text(actor_ref, 120)
    if not _SECURITY_EVENT_FALLBACK_ACTOR.fullmatch(safe_actor):
        raise ValueError("Authentication fallback actor must be a local identity or VAOS token prefix.")
    safe_correlation = store._safe_text(correlation_id, 120) if correlation_id else None
    normalized_timestamp = store._normalize_time(timestamp)
    fallback_path = initialize_security_event_fallback_store(db_path)
    with _security_event_fallback_connection(fallback_path, timeout_seconds=2.0) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT event_type,outcome,component,actor_ref,details_json,correlation_id,timestamp "
            "FROM security_event_fallback_queue WHERE event_id=?",
            (event_id,),
        ).fetchone()
        expected = (event_type, outcome, component, safe_actor, details_json, safe_correlation, normalized_timestamp)
        if existing:
            actual = tuple(existing[key] for key in ("event_type", "outcome", "component", "actor_ref", "details_json", "correlation_id", "timestamp"))
            if actual != expected:
                raise RuntimeError("An authentication fallback ID conflicts with a different event.")
            return True
        pending = int(db.execute("SELECT COUNT(*) FROM security_event_fallback_queue").fetchone()[0])
        if pending >= SECURITY_EVENT_FALLBACK_MAX_ROWS:
            db.execute("UPDATE security_event_fallback_metadata SET dropped_events=dropped_events+1 WHERE singleton_id=1")
            return False
        db.execute(
            "INSERT INTO security_event_fallback_queue(event_id,event_type,outcome,component,actor_ref,details_json,correlation_id,timestamp,queued_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, event_type, outcome, component, safe_actor, details_json, safe_correlation, normalized_timestamp, store.utc_now()),
        )
        return True


def security_event_fallback_status(db_path: str | Path) -> dict[str, Any]:
    """Return pending/drop counts without exposing queued event details or local paths."""
    path = security_event_fallback_database_path(db_path)
    if not path.exists():
        return {"available": False, "pending_rows": 0, "dropped_events": 0, "database_bytes": 0}
    with _security_event_fallback_connection(path) as db:
        row = db.execute(
            "SELECT (SELECT COUNT(*) FROM security_event_fallback_queue) AS pending_rows, "
            "dropped_events FROM security_event_fallback_metadata WHERE singleton_id=1",
        ).fetchone()
    return {
        "available": True,
        "pending_rows": int(row["pending_rows"]),
        "dropped_events": int(row["dropped_events"]),
        "database_bytes": path.stat().st_size,
    }


def security_event_fallback_pending(db_path: str | Path) -> bool:
    """Check whether a durable authentication-audit replay remains pending."""
    return security_event_fallback_status(db_path)["pending_rows"] > 0


def flush_security_event_fallback(db_path: str | Path, *, maximum: int = 100) -> int:
    """Replay queued authentication events idempotently, deleting only after primary commit."""
    path = security_event_fallback_database_path(db_path)
    if not path.exists():
        return 0
    with _security_event_fallback_connection(path) as db:
        rows = db.execute(
            "SELECT event_id,event_type,outcome,component,actor_ref,details_json,correlation_id,timestamp "
            "FROM security_event_fallback_queue ORDER BY sequence_id,event_id LIMIT ?",
            (max(1, min(1000, int(maximum))),),
        ).fetchall()
    persisted = 0
    for row in rows:
        try:
            details = json.loads(row["details_json"])
            if not isinstance(details, dict) or details.pop("outcome", None) != row["outcome"]:
                raise ValueError("The queued security event payload is malformed.")
            record_security_event_and_enqueue(
                db_path,
                event_type=str(row["event_type"]), outcome=str(row["outcome"]),
                component=str(row["component"]), actor_ref=str(row["actor_ref"]),
                details=details, correlation_id=row["correlation_id"],
                event_id=str(row["event_id"]), timestamp=str(row["timestamp"]),
            )
            with _security_event_fallback_connection(path) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM security_event_fallback_queue WHERE event_id=?", (row["event_id"],))
            persisted += 1
        except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError, json.JSONDecodeError):
            logger.warning("Authentication security-event fallback replay paused; pending records are retained", exc_info=True)
            break
    return persisted


def record_security_event_resilient(
    db_path: str | Path,
    *,
    event_type: str,
    outcome: str,
    component: str,
    actor_ref: str,
    details: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    event_id: str | None = None,
    timestamp: str | None = None,
) -> bool:
    """Persist an auth audit directly or durably queue it for ordered replay."""
    identifier = event_id or secrets.token_hex(16)
    occurred_at = store._normalize_time(timestamp or store.utc_now())
    try:
        pending = security_event_fallback_pending(db_path)
    except (OSError, sqlite3.Error, RuntimeError):
        logger.warning("Could not inspect the authentication security-event fallback queue", exc_info=True)
        pending = False
    if pending:
        try:
            flush_security_event_fallback(db_path, maximum=100)
            if security_event_fallback_pending(db_path):
                queued = queue_security_event_fallback(
                    db_path, event_type=event_type, outcome=outcome, component=component,
                    actor_ref=actor_ref, details=details, correlation_id=correlation_id,
                    event_id=identifier, timestamp=occurred_at,
                )
                if queued:
                    logger.warning("Authentication security event was durably queued for replay while the primary audit store is unavailable")
                else:
                    logger.error("Authentication security-event fallback is full; this audit event could not be queued")
                return queued
        except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError):
            logger.warning("Could not replay pending authentication security events before recording a new event", exc_info=True)
            return False
    try:
        return record_security_event_and_enqueue(
            db_path, event_type=event_type, outcome=outcome, component=component,
            actor_ref=actor_ref, details=details, correlation_id=correlation_id,
            event_id=identifier, timestamp=occurred_at,
        ) or _security_event_exists(db_path, identifier)
    except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError) as primary_error:
        try:
            queued = queue_security_event_fallback(
                db_path, event_type=event_type, outcome=outcome, component=component,
                actor_ref=actor_ref, details=details, correlation_id=correlation_id,
                event_id=identifier, timestamp=occurred_at,
            )
            if queued:
                logger.warning("Authentication security event was durably queued after primary audit persistence failed")
                return True
        except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError):
            logger.warning("Could not queue the authentication security event after primary storage failure", exc_info=True)
        logger.warning("Authentication security event could not be persisted to the primary or fallback store", exc_info=primary_error)
        return False


def _security_event_exists(db_path: str | Path, event_id: str) -> bool:
    """Resolve an ambiguous commit only when the stable audit identifier exists."""
    with store._connection(Path(db_path).expanduser().resolve()) as db:
        return bool(db.execute("SELECT 1 FROM observability_security_audit WHERE event_id=?", (event_id,)).fetchone())


def _dns_resolver_worker() -> None:
    """Resolve queued hosts off delivery workers; a stuck OS resolver is isolated."""
    while True:
        host, port, result_queue = _dns_requests.get()
        try:
            try:
                result = (True, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except Exception as exc:  # preserve resolver failures without killing the worker
                result = (False, exc)
            try:
                result_queue.put_nowait(result)
            except queue.Full:
                # The caller timed out; its one-slot result queue is no longer observed.
                pass
        finally:
            _dns_requests.task_done()


def _ensure_dns_resolver_workers() -> None:
    with _dns_workers_lock:
        while len(_dns_workers) < DNS_RESOLVER_WORKERS:
            worker = threading.Thread(
                target=_dns_resolver_worker,
                name=f"vaos-siem-dns-{len(_dns_workers) + 1}",
                daemon=True,
            )
            worker.start()
            _dns_workers.append(worker)


def _bounded_getaddrinfo(host: str, port: int, *, timeout: float = DNS_LOOKUP_TIMEOUT_SECONDS) -> list[Any]:
    if timeout <= 0:
        raise RuntimeError("dns_lookup_timeout")
    _ensure_dns_resolver_workers()
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    try:
        _dns_requests.put_nowait((host, port, result_queue))
    except queue.Full as exc:
        raise RuntimeError("dns_resolver_busy") from exc
    try:
        ok, result = result_queue.get(timeout=timeout)
    except queue.Empty as exc:
        raise RuntimeError("dns_lookup_timeout") from exc
    if not ok:
        raise RuntimeError("dns_lookup_failed") from result
    return result


def _resolve_public_ip(host: str, port: int = 443, *, timeout: float = DNS_LOOKUP_TIMEOUT_SECONDS) -> str:
    addresses = _bounded_getaddrinfo(host, port, timeout=timeout)
    unique: list[str] = []
    for item in addresses:
        address = item[4][0].split("%", 1)[0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise RuntimeError("dns_address_invalid") from exc
        if not parsed.is_global:
            raise RuntimeError("dns_private_address_blocked")
        if address not in unique:
            unique.append(address)
    if not unique:
        raise RuntimeError("dns_no_address")
    return unique[0]


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, *, timeout: float = 3.0, context: ssl.SSLContext | None = None):
        super().__init__(host, port=443, timeout=timeout, context=context if context is not None else _create_tls_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _cef_escape(value: Any, *, header: bool = False) -> str:
    text = str(value or "")
    text = text.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    if header:
        return text.replace("|", "\\|")
    return text.replace("=", "\\=")


def _ecs_payload(payload: dict[str, Any]) -> dict[str, Any]:
    severity = {"DEBUG": 7, "INFO": 6, "NOTICE": 5, "WARNING": 4, "ERROR": 3, "CRITICAL": 2, "FATAL": 1}.get(str(payload.get("level", "INFO")).upper(), 6)
    event_type = str(payload.get("event_type", "security.event"))
    category = "authentication" if "auth" in event_type.casefold() else "configuration" if any(token in event_type.casefold() for token in ("config", "setting", "role")) else "iam"
    raw_outcome = str(payload.get("details", {}).get("outcome", "")).casefold() if isinstance(payload.get("details"), dict) else ""
    outcome = "success" if raw_outcome in {"success", "succeeded", "complete", "approved", "created", "enabled"} else "failure" if raw_outcome in {"failure", "failed", "denied", "rejected", "error", "disabled"} else "unknown" if raw_outcome else None
    return {
        "@timestamp": payload.get("timestamp", store.utc_now()), "ecs": {"version": "8.11.0"},
        "event": {"id": payload.get("event_id"), "action": event_type, "kind": "event", "category": [category], "severity": severity,
                  "outcome": outcome},
        "log": {"level": str(payload.get("level", "INFO")).casefold()}, "message": payload.get("message", ""),
        "service": {"name": "VAOS", "version": "1.2.18"},
        "vaos": {"component": payload.get("component"), "correlation_id": payload.get("correlation_id"), "details": payload.get("details", {})},
    }


def _mapped_event(payload: dict[str, Any], event_format: str) -> tuple[bytes, str]:
    if event_format == "cef":
        level = str(payload.get("level", "INFO")).upper()
        severity = {"DEBUG": 1, "INFO": 2, "NOTICE": 3, "WARNING": 5, "ERROR": 7, "CRITICAL": 9, "FATAL": 10}.get(level, 2)
        extension = {
            "externalId": payload.get("event_id", ""),
            "msg": payload.get("message", ""), "cs1Label": "eventType", "cs1": payload.get("event_type", ""),
            "cs2Label": "component", "cs2": payload.get("component", ""),
            "cs3Label": "detailsJson", "cs3": json.dumps(payload.get("details", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        }
        cef_timestamp = _cef_timestamp(payload.get("timestamp"))
        if cef_timestamp:
            extension = {"rt": cef_timestamp, **extension}
        signature = _cef_escape(payload.get("event_type", "security.event"), header=True)[:1023]
        name = _cef_escape(payload.get("message", "VAOS security event"), header=True)[:63]
        header = f"CEF:0|VAOS|VAOS|1.2.18|{signature}|{name}|{severity}|"
        body = (header + " ".join(f"{key}={_cef_escape(value)}" for key, value in extension.items())).encode("utf-8")
        return body, "application/cef"
    if event_format == "ecs":
        value = _ecs_payload(payload)
    elif event_format in {"json", "ndjson"}:
        value = payload
    else:
        raise ValueError("The selected event mapping is not available.")
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if event_format == "ndjson":
        body += "\n"
        return body.encode("utf-8"), "application/x-ndjson"
    return body.encode("utf-8"), "application/json"


def _cef_timestamp(value: Any) -> str:
    """Format CEF rt as Unix epoch milliseconds; omit invalid or timezone-free input."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return ""
        return str(int(parsed.timestamp() * 1000))
    except (OverflowError, TypeError, ValueError):
        return ""


def _send(endpoint: str, secret: str, payload: dict[str, Any], *, event_format: str = "json", timeout: float = 3.0) -> int:
    if len(secret.encode("utf-8")) < MIN_HMAC_SECRET_BYTES:
        raise RuntimeError("hmac_key_too_short")
    host, path, _port = validate_endpoint(endpoint, "https_json")
    context = _create_tls_context()
    pinned_ip = _resolve_public_ip(host, timeout=timeout)
    body, content_type = _mapped_event(payload, event_format)
    if len(body) > store.MAX_EVENT_DETAILS_BYTES + 4096:
        raise RuntimeError("payload_too_large")
    timestamp = store.utc_now()
    signature = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    connection = _PinnedHttpsConnection(host, pinned_ip, timeout=timeout, context=context)
    try:
        connection.request("POST", path, body=body, headers={"Content-Type": content_type, "Accept": "application/json", "Content-Length": str(len(body)), "Connection": "close", "X-VAOS-Timestamp": timestamp, "X-VAOS-Signature": f"sha256={signature}"})
        response = connection.getresponse()
        response.read(1024)
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"http_{response.status}")
        return response.status
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise RuntimeError("https_delivery_failed") from exc
    finally:
        connection.close()


def _timestamp_ns(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return str((delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000)


def _otlp_logs_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Encode one redacted security event using OTLP/HTTP protobuf-JSON mapping."""
    timestamp = str(payload.get("timestamp") or store.utc_now())
    level = str(payload.get("level") or "INFO").upper()
    severity = {"DEBUG": 5, "INFO": 9, "NOTICE": 10, "WARNING": 13, "ERROR": 17, "CRITICAL": 18, "FATAL": 21}.get(level, 9)
    attributes = {
        "event.id": payload.get("event_id", ""),
        "event.name": payload.get("event_type", "security.event"),
        "event.severity_text": level,
        "vaos.component": payload.get("component", "observability"),
        "vaos.correlation_id": payload.get("correlation_id", ""),
        "vaos.details_json": json.dumps(payload.get("details", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }
    log_record: dict[str, Any] = {
        "timeUnixNano": _timestamp_ns(timestamp),
        "observedTimeUnixNano": _timestamp_ns(store.utc_now()),
        "severityNumber": severity,
        "severityText": level,
        "body": {"stringValue": str(payload.get("message", "Security event"))},
        "attributes": [{"key": key, "value": {"stringValue": str(value)}} for key, value in attributes.items() if value is not None],
    }
    return {"resourceLogs": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "VAOS"}},
            {"key": "service.namespace", "value": {"stringValue": "vaos"}},
        ]},
        "scopeLogs": [{"scope": {"name": "vaos.security", "version": "1.2.18"}, "logRecords": [log_record]}],
    }]}


def _send_otlp(endpoint: str, secret: str, payload: dict[str, Any], *, timeout: float = 3.0) -> int:
    host, path, _port = validate_endpoint(endpoint, "otlp")
    context = _create_tls_context()
    pinned_ip = _resolve_public_ip(host, 443, timeout=timeout)
    body = json.dumps(_otlp_logs_payload(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 64 * 1024:
        raise RuntimeError("payload_too_large")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Content-Length": str(len(body)), "Connection": "close"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    connection = _PinnedHttpsConnection(host, pinned_ip, timeout=timeout, context=context)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read(4096)
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"otlp_http_{response.status}")
        if response_body:
            try:
                result = json.loads(response_body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeError("otlp_invalid_response") from exc
            partial = result.get("partialSuccess", {}) if isinstance(result, dict) else {}
            rejected = int(partial.get("rejectedLogRecords", 0) or 0) if isinstance(partial, dict) else 0
            if rejected:
                raise RuntimeError("otlp_records_rejected")
        return response.status
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise RuntimeError("otlp_delivery_failed") from exc
    finally:
        connection.close()


def _syslog_message(payload: dict[str, Any], event_format: str = "json") -> bytes:
    level = str(payload.get("level") or "INFO").upper()
    severity = {"DEBUG": 7, "INFO": 6, "NOTICE": 5, "WARNING": 4, "ERROR": 3, "CRITICAL": 2, "FATAL": 0}.get(level, 6)
    priority = 16 * 8 + severity  # local0 facility, RFC 5424 severity.
    try:
        timestamp = datetime.fromisoformat(str(payload.get("timestamp") or store.utc_now()).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp_text = timestamp.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except ValueError:
        timestamp_text = store.utc_now()
    event_type = re.sub(r"[^A-Za-z0-9_-]", "_", str(payload.get("event_type") or "VAOS"))[:32] or "VAOS"
    body, _content_type = _mapped_event(payload, event_format)
    text_body = body.decode("utf-8").rstrip("\n")
    if event_format != "cef":
        text_body = "\ufeff" + text_body
    message = f"<{priority}>1 {timestamp_text} VAOS VAOS - {event_type} - {text_body}".encode("utf-8")
    if len(message) > 8192:
        raise RuntimeError("payload_too_large")
    return str(len(message)).encode("ascii") + b" " + message


def _send_syslog_tls(endpoint: str, payload: dict[str, Any], *, event_format: str = "json", timeout: float = 3.0) -> None:
    host, _path, port = validate_endpoint(endpoint, "syslog_tls")
    context = _create_tls_context()
    pinned_ip = _resolve_public_ip(host, port, timeout=timeout)
    raw = None
    secure = None
    try:
        raw = socket.create_connection((pinned_ip, port), timeout=timeout)
        secure = context.wrap_socket(raw, server_hostname=host)
        secure.settimeout(timeout)
        secure.sendall(_syslog_message(payload, event_format))
        # RFC 5425 requires the sender to close the TLS session when finished.
        raw = secure.unwrap()
        secure = None
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError("syslog_tls_delivery_failed") from exc
    finally:
        if secure is not None:
            secure.close()
        if raw is not None:
            raw.close()


def _deliver(transport: str, endpoint: str, secret: str, payload: dict[str, Any], *, event_format: str = "json", timeout: float = 3.0) -> int | None:
    if transport == "https_json":
        return _send(endpoint, secret, payload, event_format=event_format, timeout=timeout)
    if transport == "syslog_tls":
        _send_syslog_tls(endpoint, payload, event_format=event_format, timeout=timeout)
        return None
    if transport == "otlp":
        return _send_otlp(endpoint, secret, payload, timeout=timeout)
    raise RuntimeError("unsupported_transport")


def send_test_event(db_path: str | Path, destination_id: str) -> dict[str, Any]:
    database = Path(db_path).expanduser().resolve()
    with store._connection(database) as db:
        row = db.execute("SELECT transport,event_format,endpoint,secret_ref,archived_at FROM observability_siem_destinations WHERE destination_id=?", (destination_id,)).fetchone()
    if not row or row["archived_at"]:
        raise ValueError("SIEM destination was not found.")
    secret = _configured_secret(str(row["secret_ref"] or "")) if str(row["transport"]) == "https_json" else _configured_value(str(row["secret_ref"] or ""))
    transport = str(row["transport"])
    if transport == "https_json" and not secret:
        raise ValueError(f"The referenced HMAC key is missing or shorter than {MIN_HMAC_SECRET_BYTES} UTF-8 bytes in the service environment.")
    if transport == "otlp" and row["secret_ref"] and not secret:
        raise ValueError("The referenced OTLP bearer-token environment variable is not configured.")
    try:
        status = _deliver(transport, str(row["endpoint"]), secret, {"schema": "vaos.security-event.v1", "test": True, "event_id": secrets.token_hex(16), "timestamp": store.utc_now(), "event_type": "integration.test", "level": "INFO", "component": "observability", "message": "VAOS connectivity test; no user data included.", "details": {}}, event_format=str(row["event_format"]))
        return {"ok": True, "http_status": status, "message": "The remote endpoint accepted the test event." if status else "The event was sent over a certificate-validated TLS syslog connection."}
    except RuntimeError as exc:
        return {"ok": False, "error_code": str(exc), "message": "The connection test failed; VAOS did not store a response body or secret."}


def dispatch_pending(db_path: str | Path, *, maximum: int = 2) -> dict[str, int]:
    database = Path(db_path).expanduser().resolve()
    # Keep scheduling timestamps at microsecond precision end-to-end so their
    # ISO-8601 lexical ordering remains reliable around lease/retry boundaries.
    now = store.utc_now()
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=DISPATCH_LEASE_SECONDS)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with store._connection(database) as db:
        db.execute("BEGIN IMMEDIATE")
        selected = db.execute("SELECT e.delivery_id,e.destination_id,e.event_id,e.attempt_count,d.transport,d.event_format,d.endpoint,d.secret_ref,o.timestamp,o.event_type,o.level,o.component,o.message,o.details_json,o.correlation_id FROM observability_delivery_events e JOIN observability_siem_destinations d ON d.destination_id=e.destination_id JOIN observability_events o ON o.event_id=e.event_id WHERE d.enabled=1 AND d.archived_at IS NULL AND e.status IN ('queued','retrying') AND (e.next_attempt_at IS NULL OR e.next_attempt_at<=?) ORDER BY e.updated_at,e.delivery_id LIMIT ?", (now, max(1, min(10, int(maximum))))).fetchall()
        rows = []
        for row in selected:
            claimed = db.execute("UPDATE observability_delivery_events SET status='retrying',attempt_count=attempt_count+1,next_attempt_at=?,updated_at=? WHERE delivery_id=? AND status IN ('queued','retrying') AND (next_attempt_at IS NULL OR next_attempt_at<=?)", (lease_until, now, row["delivery_id"], now))
            if claimed.rowcount:
                rows.append(row)
    sent = failed = dead = 0
    for row in rows:
        error_code = None
        status_code = None
        delivered = False
        try:
            secret_ref = str(row["secret_ref"] or "")
            transport = str(row["transport"])
            if secret_ref and not _SECRET_REF.fullmatch(secret_ref):
                raise RuntimeError("invalid_secret_reference")
            secret = _configured_secret(secret_ref) if transport == "https_json" else _configured_value(secret_ref)
            if transport == "https_json" and not secret:
                raise RuntimeError("missing_or_short_hmac_key")
            if transport == "otlp" and secret_ref and not secret:
                raise RuntimeError("missing_otlp_bearer_token")
            details = json.loads(row["details_json"])
            payload = {"schema": "vaos.security-event.v1", "event_id": row["event_id"], "timestamp": row["timestamp"], "event_type": row["event_type"], "level": row["level"], "component": row["component"], "message": row["message"], "details": details, "correlation_id": row["correlation_id"]}
            status_code = _deliver(transport, str(row["endpoint"]), secret, payload, event_format=str(row["event_format"]))
            delivered = True
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            error_code = str(exc)[:64]
        attempt = int(row["attempt_count"]) + 1
        updated = store.utc_now()
        if delivered:
            state, next_attempt = "sent", None
        else:
            state = "dead_letter" if attempt >= MAX_ATTEMPTS else "retrying"
            next_attempt = None if state == "dead_letter" else (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 15 * (2 ** min(attempt - 1, 8))))).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with store._connection(database) as db:
            saved = db.execute("UPDATE observability_delivery_events SET status=?,next_attempt_at=?,last_status_code=?,last_error_code=?,updated_at=? WHERE delivery_id=? AND status='retrying' AND next_attempt_at=?", (state, next_attempt, status_code, error_code, updated, row["delivery_id"], lease_until))
            if not saved.rowcount:
                logger.warning("SIEM delivery result ignored because its lease was superseded delivery=%s", row["delivery_id"])
                continue
        if delivered:
            sent += 1
        else:
            failed += 1
            dead += int(state == "dead_letter")
    return {"attempted": len(rows), "sent": sent, "failed": failed, "dead_letter": dead}
