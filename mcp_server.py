"""Optional local MCP bridge for the VAOS REST API.

Run this file as a stdio subprocess from an MCP host. It never starts or
restarts VAOS and it reads its API token from the process environment only.
"""
from __future__ import annotations

import base64
import binascii
import contextvars
from collections import deque
from contextlib import closing
from functools import wraps
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Literal

try:
    from mcp.server import MCPServer
except ImportError as exc:  # optional dependency; keep the main app lightweight
    raise SystemExit(
        "VAOS MCP needs the optional package. Install it with: "
        "python -m pip install -r requirements-mcp.txt"
    ) from exc


SERVER = MCPServer("VAOS", instructions=(
    "Connect to the configured VAOS instance. Respect its live MCP policy, "
    "read-only mode, feature groups, and destructive-action controls. "
    "MCP-bound tokens require a human to approve each write once; after approval, "
    "retry the exact same operation with its approval ID. Never self-approve. "
    "Never claim that an action succeeded unless the API response confirms it."
))
MAX_REQUEST_BYTES = 100 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_METADATA_JSON_BYTES = 64 * 1024
BASE_URL = os.getenv("VAOS_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
TOKEN = os.getenv("VAOS_API_TOKEN", "").strip()
_TOOL_CALL_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("vaos_mcp_tool_call_depth", default=0)
_MAX_PENDING_INVOCATIONS = 512
_MCP_TELEMETRY_MAX_BYTES = 2 * 1024 * 1024
_MCP_TELEMETRY_LOCK_TIMEOUT_SECONDS = 0.1
_INVOCATION_FLUSH_BUDGET_SECONDS = 1.25
_INVOCATION_REQUEST_TIMEOUT_SECONDS = 0.35
_MCP_TOOL_NAME = re.compile(r"\Avaos_[a-z0-9_]{1,80}\Z")
_EPHEMERAL_PENDING_INVOCATIONS: deque[dict[str, object]] = deque()
_EPHEMERAL_PENDING_LOCK = threading.Lock()
_DROPPED_EPHEMERAL_INVOCATIONS = 0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward the MCP bearer token through API redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_API_URL_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _default_mcp_telemetry_path() -> Path:
    configured = os.getenv("VAOS_MCP_TELEMETRY_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        state_root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        state_root = Path(os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state_root / "VAOS" / "mcp-telemetry.sqlite3"


MCP_TELEMETRY_DB_PATH = _default_mcp_telemetry_path()


def _connect_mcp_telemetry_db() -> sqlite3.Connection:
    """Open a private, bounded local queue containing no API token or tool arguments."""
    path = Path(MCP_TELEMETRY_DB_PATH).expanduser()
    if not path.is_absolute():
        raise RuntimeError("The VAOS MCP telemetry queue path must be absolute.")
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError("Refusing to use a linked VAOS MCP telemetry queue path.")
    parent_created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeError("Refusing to use a linked VAOS MCP telemetry queue path.")
    if os.name != "nt" and parent_created:
        path.parent.chmod(0o700)
    if os.name != "nt" and path.parent.stat().st_mode & 0o077:
        raise RuntimeError("The VAOS MCP telemetry directory must be private to the current user.")
    file_created = False
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            file_created = True
            os.close(descriptor)
    if path.is_symlink():
        raise RuntimeError("Refusing to use a linked VAOS MCP telemetry queue path.")
    if os.name != "nt" and not file_created and path.stat().st_mode & 0o077:
        raise RuntimeError("The existing VAOS MCP telemetry database must be private to the current user.")
    db = sqlite3.connect(str(path), timeout=_MCP_TELEMETRY_LOCK_TIMEOUT_SECONDS, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute(f"PRAGMA busy_timeout={int(_MCP_TELEMETRY_LOCK_TIMEOUT_SECONDS * 1000)}")
        db.execute("PRAGMA page_size=4096")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        page_size = max(1, int(db.execute("PRAGMA page_size").fetchone()[0]))
        configured_pages = max(1, _MCP_TELEMETRY_MAX_BYTES // page_size)
        current_limit = int(db.execute(f"PRAGMA max_page_count={configured_pages}").fetchone()[0])
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        if current_limit > configured_pages or page_count > configured_pages:
            raise RuntimeError("The VAOS MCP telemetry queue exceeded its size limit.")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS mcp_telemetry_metadata (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                schema_version INTEGER NOT NULL,
                dropped_reports INTEGER NOT NULL DEFAULT 0 CHECK(dropped_reports >= 0)
            );
            INSERT OR IGNORE INTO mcp_telemetry_metadata(singleton_id,schema_version,dropped_reports) VALUES(1,1,0);
            CREATE TABLE IF NOT EXISTS mcp_telemetry_queue (
                sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT NOT NULL UNIQUE,
                tool TEXT NOT NULL,
                outcome TEXT NOT NULL,
                duration_ms REAL NOT NULL,
                queued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_telemetry_queue_order
                ON mcp_telemetry_queue(sequence_id,invocation_id);
            """
        )
        version = db.execute("SELECT schema_version FROM mcp_telemetry_metadata WHERE singleton_id=1").fetchone()
        if not version or int(version["schema_version"]) != 1:
            raise RuntimeError("The VAOS MCP telemetry queue has an unsupported schema version.")
        return db
    except Exception:
        db.close()
        raise


def _mcp_telemetry_queue_status() -> dict[str, int]:
    with closing(_connect_mcp_telemetry_db()) as db:
        row = db.execute(
            "SELECT (SELECT COUNT(*) FROM mcp_telemetry_queue) AS pending_reports, dropped_reports "
            "FROM mcp_telemetry_metadata WHERE singleton_id=1"
        ).fetchone()
    return {"pending_reports": int(row["pending_reports"]), "dropped_reports": int(row["dropped_reports"])}


def _queue_tool_invocation(item: dict[str, object]) -> int:
    """Persist one argument-free report and return how many oldest reports were dropped."""
    _validate_tool_invocation(item)
    invocation_id = str(item["invocation_id"])
    tool = str(item["tool"])
    outcome = str(item["outcome"])
    duration_ms = float(item["duration_ms"])
    with closing(_connect_mcp_telemetry_db()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            existing = db.execute(
                "SELECT tool,outcome,duration_ms FROM mcp_telemetry_queue WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if existing:
                expected = (tool, outcome, duration_ms)
                actual = (str(existing["tool"]), str(existing["outcome"]), float(existing["duration_ms"]))
                if actual != expected:
                    raise RuntimeError("An MCP invocation ID conflicts with another telemetry record.")
                db.commit()
                return 0
            pending = int(db.execute("SELECT COUNT(*) FROM mcp_telemetry_queue").fetchone()[0])
            dropped = max(0, pending - max(1, int(_MAX_PENDING_INVOCATIONS)) + 1)
            if dropped:
                db.execute(
                    "DELETE FROM mcp_telemetry_queue WHERE sequence_id IN "
                    "(SELECT sequence_id FROM mcp_telemetry_queue ORDER BY sequence_id LIMIT ?)",
                    (dropped,),
                )
                db.execute(
                    "UPDATE mcp_telemetry_metadata SET dropped_reports=dropped_reports+? WHERE singleton_id=1",
                    (dropped,),
                )
            db.execute(
                "INSERT INTO mcp_telemetry_queue(invocation_id,tool,outcome,duration_ms) VALUES(?,?,?,?)",
                (invocation_id, tool, outcome, duration_ms),
            )
            db.commit()
            return dropped
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise


def _validate_tool_invocation(item: dict[str, object]) -> None:
    invocation_id = str(item["invocation_id"])
    tool = str(item["tool"])
    outcome = str(item["outcome"])
    duration_ms = float(item["duration_ms"])
    if not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
        raise ValueError("The MCP invocation identity is invalid.")
    if not _MCP_TOOL_NAME.fullmatch(tool) or outcome not in {"success", "error"}:
        raise ValueError("The MCP invocation telemetry fields are invalid.")
    if not math.isfinite(duration_ms) or duration_ms < 0 or duration_ms > 300_000:
        raise ValueError("The MCP invocation duration is outside its allowed range.")


def _append_ephemeral_invocation(item: dict[str, object]) -> int:
    """Retain a process-local fallback when the private SQLite queue is unavailable."""
    global _DROPPED_EPHEMERAL_INVOCATIONS
    _validate_tool_invocation(item)
    dropped = 0
    with _EPHEMERAL_PENDING_LOCK:
        if len(_EPHEMERAL_PENDING_INVOCATIONS) >= _MAX_PENDING_INVOCATIONS:
            _EPHEMERAL_PENDING_INVOCATIONS.popleft()
            _DROPPED_EPHEMERAL_INVOCATIONS += 1
            dropped = 1
        _EPHEMERAL_PENDING_INVOCATIONS.append(dict(item))
    return dropped


def _oldest_ephemeral_invocation() -> dict[str, object] | None:
    with _EPHEMERAL_PENDING_LOCK:
        return dict(_EPHEMERAL_PENDING_INVOCATIONS[0]) if _EPHEMERAL_PENDING_INVOCATIONS else None


def _remove_ephemeral_invocation(invocation_id: str) -> None:
    with _EPHEMERAL_PENDING_LOCK:
        if _EPHEMERAL_PENDING_INVOCATIONS and _EPHEMERAL_PENDING_INVOCATIONS[0].get("invocation_id") == invocation_id:
            _EPHEMERAL_PENDING_INVOCATIONS.popleft()


def _oldest_pending_invocation() -> dict[str, object] | None:
    with closing(_connect_mcp_telemetry_db()) as db:
        row = db.execute(
            "SELECT invocation_id,tool,outcome,duration_ms FROM mcp_telemetry_queue "
            "ORDER BY sequence_id,invocation_id LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _delete_pending_invocation(invocation_id: str) -> None:
    with closing(_connect_mcp_telemetry_db()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM mcp_telemetry_queue WHERE invocation_id=?", (invocation_id,))
        db.commit()


def _validate_configuration() -> None:
    parsed = urllib.parse.urlsplit(BASE_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("VAOS_BASE_URL must be an http(s) origin without credentials.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise RuntimeError("VAOS_BASE_URL must be an origin only, without a path or query string.")
    if parsed.scheme != "https" and parsed.hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("Remote VAOS connections require HTTPS.")
    if not TOKEN.startswith("vaos_"):
        raise RuntimeError("Set VAOS_API_TOKEN to a token created in VAOS Settings.")


def _url_for(path: str) -> str:
    _validate_configuration()
    if not isinstance(path, str) or not path.startswith("/api/v1/") or ".." in path or "\\" in path:
        raise ValueError("VAOS calls must use an absolute /api/v1/ API path.")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("Do not include a host, scheme, or fragment in an API path.")
    return BASE_URL + path


def _decode_bounded_base64(value: str, label: str) -> bytes:
    """Reject oversized encoded input before allocating the decoded payload."""
    _validate_bounded_base64_input(value, label)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} must be valid base64.") from exc
    if len(decoded) > MAX_REQUEST_BYTES:
        raise ValueError(f"{label} exceeds the 100 MB MCP request limit.")
    return decoded


def _validate_bounded_base64_input(value: str, label: str) -> None:
    encoded_limit = 4 * ((MAX_REQUEST_BYTES + 2) // 3)
    if not isinstance(value, str) or len(value) > encoded_limit:
        raise ValueError(f"{label} exceeds the 100 MB MCP request limit.")


def _validate_bounded_text_size(value: str, label: str, maximum_bytes: int) -> None:
    if not isinstance(value, str) or len(value) > maximum_bytes:
        raise ValueError(f"{label} exceeds the MCP request limit.")
    byte_count = 0
    try:
        for offset in range(0, len(value), 64 * 1024):
            byte_count += len(value[offset:offset + 64 * 1024].encode("utf-8"))
            if byte_count > maximum_bytes:
                raise ValueError(f"{label} exceeds the MCP request limit.")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text.") from exc


def _parse_bounded_json_object(value: str, label: str, *, default: str = "{}") -> dict[str, object]:
    if not value:
        value = default
    try:
        _validate_bounded_text_size(value, label, MAX_METADATA_JSON_BYTES)
    except ValueError as exc:
        raise ValueError(f"{label} exceeds the MCP metadata limit or is not valid text.") from exc
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must contain a JSON object.")
    return parsed


def _api_request(method: str, path: str, body: bytes | None = None, content_type: str = "application/json", extra_headers: dict[str, str] | None = None, timeout_seconds: float = 90) -> str:
    url = _url_for(path)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"}
    if body is not None:
        headers["Content-Type"] = content_type
    for name, value in (extra_headers or {}).items():
        normalized = name.casefold()
        if normalized not in {"x-video-time", "x-save-samples", "x-vaos-mcp-approval-id"}:
            raise ValueError(f"Header {name} is not supported by the VAOS MCP bridge.")
        if len(name) > 80 or len(value) > 2000:
            raise ValueError("A request header is too long.")
        headers[name] = value
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with _API_URL_OPENER.open(request, timeout=max(0.1, min(float(timeout_seconds), 90))) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                return json.dumps({"status": response.status, "content_type": response.headers.get("Content-Type"), "message": "Response exceeded the MCP text limit; use the REST API directly for large downloads."})
            content_type_response = response.headers.get("Content-Type", "")
            text = payload.decode("utf-8", errors="replace")
            if "json" in content_type_response or "text/" in content_type_response:
                return text
            return json.dumps({"status": response.status, "content_type": content_type_response, "data_base64": base64.b64encode(payload).decode("ascii")})
    except urllib.error.HTTPError as exc:
        details = exc.read(32_000).decode("utf-8", errors="replace")
        if exc.code == 428:
            try:
                payload = json.loads(details)
                detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
                if isinstance(detail, dict) and detail.get("approval_required"):
                    return json.dumps({"error": True, "status": 428, **detail}, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        return json.dumps({"error": True, "status": exc.code, "detail": details or exc.reason})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return json.dumps({"error": True, "detail": f"VAOS request failed: {exc.__class__.__name__}"})


def _response_is_error(result: object) -> bool:
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and (
        payload.get("error") is True
        or isinstance(payload.get("status"), int) and payload["status"] >= 400
    )


def _invocation_telemetry_succeeded(result: object) -> bool:
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def _flush_pending_invocations() -> None:
    """Retry durable reports, then volatile fallbacks, without replaying the tool."""
    deadline = time.monotonic() + _INVOCATION_FLUSH_BUDGET_SECONDS
    while time.monotonic() < deadline:
        persistent = True
        try:
            item = _oldest_pending_invocation()
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            item = None
            persistent = False
        if not item:
            item = _oldest_ephemeral_invocation()
            persistent = False
        if not item:
            return
        payload = {key: item[key] for key in ("invocation_id", "tool", "outcome", "duration_ms")}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        remaining = deadline - time.monotonic()
        try:
            result = _api_request(
                "POST", "/api/v1/admin/observability/mcp/invocations", body=body,
                timeout_seconds=min(_INVOCATION_REQUEST_TIMEOUT_SECONDS, max(0.1, remaining)),
            )
        except Exception:
            result = None
        if not _invocation_telemetry_succeeded(result):
            print(
                "VAOS MCP invocation telemetry remains queued locally; the tool result is preserved and reporting will retry on a later call.",
                file=sys.stderr,
            )
            return
        if persistent:
            try:
                _delete_pending_invocation(str(item["invocation_id"]))
            except (OSError, sqlite3.Error, RuntimeError, ValueError):
                # The receiver deduplicates by invocation_id if this accepted
                # row has to be posted again after a local delete failure.
                print(
                    "VAOS MCP invocation telemetry was accepted but remains queued locally for an idempotent retry.",
                    file=sys.stderr,
                )
                return
        else:
            _remove_ephemeral_invocation(str(item["invocation_id"]))


def _record_tool_invocation(tool_name: str, outcome: str, duration_ms: float) -> None:
    """Persist minimal telemetry, retaining a bounded memory fallback on local I/O failure."""
    elapsed = float(duration_ms)
    item: dict[str, object] = {
        "invocation_id": uuid.uuid4().hex,
        "tool": tool_name,
        "outcome": outcome,
        "duration_ms": round(min(300_000.0, max(0.0, elapsed)), 3),
    }
    _validate_tool_invocation(item)
    try:
        dropped = _queue_tool_invocation(item)
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        dropped = _append_ephemeral_invocation(item)
        print(
            "VAOS MCP durable telemetry storage is unavailable; a bounded process-local retry was retained.",
            file=sys.stderr,
        )
    if dropped:
        print(
            f"VAOS MCP telemetry queue limit reached; {dropped} oldest pending report(s) were dropped.",
            file=sys.stderr,
        )
    _flush_pending_invocations()


def _instrument_tool(tool_name: str):
    """Register a tool with bounded, argument-free invocation telemetry."""
    def register(function):
        @wraps(function)
        def measured(*args, **kwargs):
            depth = _TOOL_CALL_DEPTH.get()
            call_token = _TOOL_CALL_DEPTH.set(depth + 1)
            started = time.perf_counter()
            outcome = "success"
            try:
                result = function(*args, **kwargs)
                if _response_is_error(result):
                    outcome = "error"
                return result
            except Exception:
                outcome = "error"
                raise
            finally:
                duration_ms = min(300_000.0, max(0.0, (time.perf_counter() - started) * 1000))
                _TOOL_CALL_DEPTH.reset(call_token)
                if depth == 0:
                    try:
                        _record_tool_invocation(tool_name, outcome, duration_ms)
                    except Exception:
                        # Telemetry must never replace a tool's actual result or error.
                        print("VAOS MCP invocation telemetry could not be recorded; the tool result is preserved.", file=sys.stderr)

        @wraps(function)
        def mcp_handler(*args, **kwargs):
            try:
                return measured(*args, **kwargs)
            except (PermissionError, ValueError) as exc:
                # Expected policy/input rejections should be actionable to an
                # MCP host, not collapsed by the SDK into a generic tool error.
                # Keep the detail bounded and never include request secrets.
                return json.dumps({"error": True, "detail": str(exc)[:500]}, ensure_ascii=False)

        SERVER.tool()(mcp_handler)
        # Keep the Python callable's exception contract for local callers/tests;
        # only the protocol-facing handler converts expected rejections.
        return measured
    return register


def _policy() -> dict:
    try:
        result = json.loads(_api_request("GET", "/api/v1/admin/integrations/mcp-policy"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("VAOS returned an invalid MCP policy response.") from exc
    if result.get("error"):
        raise RuntimeError(f"Could not read VAOS MCP policy: {result.get('detail', result.get('status'))}")
    if not result.get("api_enabled") or not result.get("mcp_enabled"):
        raise PermissionError("Enable API access and the MCP bridge in VAOS Settings first.")
    if not result.get("token_mcp_agent"):
        raise PermissionError("This API token was not designated for MCP. Create a token with the MCP agent approval option enabled.")
    return result


def _operation_for(method: str, path: str) -> dict:
    schema_text = _api_request("GET", "/api/v1/openapi.json")
    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("VAOS OpenAPI response could not be read.") from exc
    request_path = urllib.parse.urlsplit(path).path.removeprefix("/api/v1") or "/"
    for template, operations in schema.get("paths", {}).items():
        template_parts = template.strip("/").split("/") if template != "/" else []
        request_parts = request_path.strip("/").split("/") if request_path != "/" else []
        matches = len(template_parts) == len(request_parts) and all(
            (expected.startswith("{") and expected.endswith("}")) or expected == actual
            for expected, actual in zip(template_parts, request_parts)
        )
        if matches and method.lower() in operations:
            return operations[method.lower()]
    raise ValueError("That path and method are not present in the VAOS API reference.")


def _authorize_operation(method: str, path: str) -> None:
    policy = _policy()
    verb = method.upper()
    normalized = urllib.parse.urlsplit(path).path
    if normalized.startswith("/api/v1/admin/integrations") or normalized.startswith("/api/v1/admin/api-tokens"):
        raise PermissionError("MCP cannot manage API secrets or integration settings.")
    if "/api/v1/admin/mcp-approvals" in normalized and not re.fullmatch(r"/api/v1/admin/mcp-approvals/[A-Za-z0-9_-]+", normalized):
        raise PermissionError("MCP can only read the status of its own approval request; only an administrator can approve or deny it.")
    operation = _operation_for(verb, path)
    group = operation.get("x-vaos-group") or _infer_group(normalized)
    if group not in policy.get("token_groups", []):
        raise PermissionError(f"The API token does not grant the '{group}' capability group.")
    if group not in policy.get("allowed_groups", []):
        raise PermissionError(f"The '{group}' capability is disabled in VAOS Settings.")
    if _is_sensitive_read(verb, normalized) and not (policy.get("allow_sensitive_reads") and policy.get("token_sensitive_read")):
        raise PermissionError("This read can reveal profiles, camera links, events, or sample images. Enable sensitive reads in VAOS Settings and issue a token specifically granted that access.")
    if policy.get("read_only", True) and verb not in {"GET", "HEAD", "OPTIONS"}:
        raise PermissionError("MCP is in read-only mode. Turn off read-only mode in VAOS Settings to allow changes.")
    if verb not in {"GET", "HEAD", "OPTIONS"}:
        if policy.get("token_scope") != "control":
            raise PermissionError("This MCP connection uses a read-only API token. Create a control token to make changes.")
        if _is_delete_action(verb, normalized) and not policy.get("allow_deletes"):
            raise PermissionError("Deletion and revoke actions are disabled in VAOS Settings.")
        if _is_sensitive_action(verb, normalized) and not policy.get("allow_sensitive"):
            raise PermissionError("This sensitive action is disabled in VAOS Settings.")


def _infer_group(path: str) -> str:
    path = path.casefold()
    if path.endswith("/health"):
        return "diagnostics"
    if "/observability" in path or "/statistics" in path:
        return "diagnostics"
    checks = (
        ("/cameras", "cameras"), ("/camera/", "cameras"), ("/enrollment-links", "cameras"),
        ("/projects", "projects"),
        ("/mcp-approvals/", "settings"),
        ("/participants", "people"), ("/enroll/", "people"), ("/events", "events"),
        ("/tracking/rules", "events"), ("/tracking/events", "events"),
        ("/samples", "review"), ("/review", "review"), ("/capture", "review"),
        ("/train", "training"), ("/history", "training"), ("/logs", "training"),
        ("/datasets", "datasets"), ("/dataset", "datasets"), ("/engine-settings", "settings"),
        ("/model-selection", "settings"), ("/model", "models"), ("/huggingface", "models"),
        ("/enrichment", "models"), ("/tunnel", "tunnels"), ("/admin-tunnel", "tunnels"), ("/video-review", "video"),
        ("/services/", "settings"),
    )
    for needle, group in checks:
        if needle in path:
            return group
    return "unclassified"


def _is_delete_action(method: str, path: str) -> bool:
    return method == "DELETE" or path.endswith("/revoke")


def _is_sensitive_read(method: str, path: str) -> bool:
    if method.upper() not in {"GET", "HEAD"}:
        return False
    exact = {
        "/api/v1/admin/cameras", "/api/v1/admin/tunnel", "/api/v1/admin/admin-tunnel",
        "/api/v1/admin/participants", "/api/v1/admin/events", "/api/v1/admin/tracking/rules",
        "/api/v1/admin/tracking/events",
    }
    return path in exact or path.startswith(("/api/v1/admin/tracking/rules/", "/api/v1/admin/tracking/events/", "/api/v1/admin/observability/events", "/api/v1/admin/observability/lifecycle", "/api/v1/admin/observability/security-audit", "/api/v1/admin/observability/reports", "/api/v1/admin/observability/integrations")) or any(path.startswith(prefix) and path.endswith(suffix) for prefix, suffix in (
        ("/api/v1/admin/participants/", ""), ("/api/v1/admin/cameras/", "/qr"),
        ("/api/v1/admin/enrollment-links/", "/qr"), ("/api/v1/admin/ml/samples/", "/image"),
    ))


def _is_sensitive_action(method: str, path: str) -> bool:
    if method in {"POST", "PATCH", "DELETE"} and path.startswith("/api/v1/admin/tracking/rules"):
        return True
    if method in {"PUT", "PATCH", "DELETE"} and path.startswith("/api/v1/admin/observability/integrations/"):
        return True
    if method != "POST":
        return False
    if "/projects/import" in path or ("/projects/" in path and path.endswith("/apply")):
        return True
    return path == "/api/v1/admin/cameras" or any(part in path for part in (
        "/services/restart", "/tunnel/", "/admin-tunnel/", "/train/stop", "/observability/retention", "/observability/reports", "/observability/integrations",
        "/ml/train", "/ml/enrichment", "/ml/datasets/prepare", "/ml/datasets/import",
        "/ml/model-sources", "/ml/capture", "/model-selection", "/huggingface-models",
        "/video-review/sessions", "/participants", "/enrollment-links", "/enroll/", "/camera/",
    ))


@_instrument_tool("vaos_api_request")
def vaos_api_request(method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"], path: str, body_json: str = "{}", approval_id: str = "") -> str:
    """Call one documented VAOS v1 API operation. For writes, JSON must match its OpenAPI schema."""
    if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        _validate_bounded_text_size(body_json, "body_json", MAX_REQUEST_BYTES)
    _authorize_operation(method, path)
    body = None
    if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        try:
            parsed_body = json.loads(body_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("body_json must contain valid JSON.") from exc
        body = json.dumps(parsed_body, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("Request body is larger than the MCP limit.")
    headers = {"X-VAOS-MCP-Approval-ID": approval_id} if approval_id else None
    return _api_request(method, path, body=body, extra_headers=headers)


def _observability_read(path: str) -> str:
    return vaos_api_request("GET", path)


@_instrument_tool("vaos_get_statistics")
def vaos_get_statistics(period: Literal["lifetime", "24h", "7d", "30d"] = "lifetime", after: str = "", before: str = "") -> str:
    """Read VAOS installation statistics. Custom ranges use UTC timestamps and an exclusive end."""
    query = urllib.parse.urlencode({key: value for key, value in {"period": "custom" if after or before else period, "after": after, "before": before}.items() if value})
    return _observability_read("/api/v1/admin/observability/overview?" + query)


@_instrument_tool("vaos_query_metric")
def vaos_query_metric(
    metric_key: str, after: str, before: str, limit: int = 500, dimensions_json: str = "{}",
    aggregation: Literal["raw", "sum", "average", "min", "max"] = "raw",
    interval: Literal["minute", "5min", "hour", "day"] = "hour", group_by: str = "",
) -> str:
    """Read bounded raw or aggregated metric history through the authorized REST API."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    try:
        dimensions = json.loads(dimensions_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("dimensions_json must be valid JSON") from exc
    if not isinstance(dimensions, dict):
        raise ValueError("dimensions_json must contain an object")
    query_values = {
        "metric": metric_key, "after": after, "before": before, "limit": limit,
        "dimensions": json.dumps(dimensions, separators=(",", ":")),
        "aggregation": aggregation, "interval": interval,
    }
    if group_by:
        query_values["group_by"] = group_by
    query = urllib.parse.urlencode(query_values)
    return _observability_read("/api/v1/admin/observability/series?" + query)


@_instrument_tool("vaos_list_runtime_sessions")
def vaos_list_runtime_sessions(limit: int = 50, after: str = "", before: str = "", cursor: str = "") -> str:
    """List lifecycle sessions with stable cursor pagination and honest unknown-stop states."""
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    query = urllib.parse.urlencode({key: value for key, value in {"limit": limit, "after": after, "before": before, "cursor": cursor}.items() if value != ""})
    return _observability_read("/api/v1/admin/observability/lifecycle?" + query)


@_instrument_tool("vaos_search_logs")
def vaos_search_logs(search: str = "", level: str = "", component: str = "", event_type: str = "", session_id: str = "", correlation_id: str = "", training_job_id: str = "", dataset_id: str = "", model_id: str = "", after: str = "", before: str = "", limit: int = 50, cursor: str = "") -> str:
    """Search persistent redacted operational events with bounded results."""
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    query = urllib.parse.urlencode({key: value for key, value in {"search": search, "level": level, "component": component, "event_type": event_type, "session_id": session_id, "correlation_id": correlation_id, "training_job_id": training_job_id, "dataset_id": dataset_id, "model_id": model_id, "after": after, "before": before, "limit": limit, "cursor": cursor}.items() if value != ""})
    return _observability_read("/api/v1/admin/observability/events?" + query)


@_instrument_tool("vaos_get_system_health")
def vaos_get_system_health() -> str:
    """Read the public VAOS health endpoint; this does not expose private dashboard metrics."""
    return _observability_read("/api/v1/health")


@_instrument_tool("vaos_list_report_types")
def vaos_list_report_types() -> str:
    """List currently implemented local observability report types and limits."""
    return _observability_read("/api/v1/admin/observability/reports/types")


@_instrument_tool("vaos_generate_report")
def vaos_generate_report(report_type: Literal["lifetime-statistics", "events", "security-events", "lifecycle", "metric-series", "training", "datasets", "models", "inference", "cameras", "tracking-events", "hardware-energy", "api-activity", "mcp-activity"], output_format: Literal["csv", "pdf"], filters_json: str = "{}", approval_id: str = "") -> str:
    """Queue a bounded report. Sensitive exports require the normal exact-request human approval."""
    try:
        filters = json.loads(filters_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("filters_json must be valid JSON") from exc
    if not isinstance(filters, dict):
        raise ValueError("filters_json must contain an object")
    return vaos_api_request("POST", "/api/v1/admin/observability/reports/jobs", json.dumps({"report_type": report_type, "format": output_format, "filters": filters}, separators=(",", ":")), approval_id)


@_instrument_tool("vaos_get_report_status")
def vaos_get_report_status(job_id: str) -> str:
    """Read the status of a report created by this authenticated API identity."""
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Report job ID format is invalid")
    return _observability_read(f"/api/v1/admin/observability/reports/jobs/{job_id}")


@_instrument_tool("vaos_check_approval")
def vaos_check_approval(approval_id: str) -> str:
    """Check whether an exact MCP write request has been approved, denied, used, or expired."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", approval_id):
        raise ValueError("Approval ID format is not valid.")
    _policy()
    return _api_request("GET", f"/api/v1/admin/mcp-approvals/{approval_id}")


@_instrument_tool("vaos_upload_file")
def vaos_upload_file(path: str, file_field: str, filename: str, content_type: str, content_base64: str, fields_json: str = "{}", approval_id: str = "") -> str:
    """Upload a file to a documented VAOS multipart endpoint using base64 content."""
    _validate_bounded_base64_input(content_base64, "File content")
    fields = _parse_bounded_json_object(fields_json, "fields_json")
    if len(file_field) > 80 or len(filename) > 180 or any(len(str(k)) > 80 for k in fields):
        raise ValueError("Multipart fields or filename are not valid.")
    if len(content_type) > 200 or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", content_type):
        raise ValueError("Content type must be a simple media type such as image/jpeg or application/octet-stream.")
    _authorize_operation("POST", path)
    content = _decode_bounded_base64(content_base64, "File content")
    boundary = "vaos-" + os.urandom(18).hex()
    chunks: list[bytes] = []
    for key, value in fields.items():
        safe_key = re.sub(r"[^A-Za-z0-9_-]", "", str(key))
        chunks.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"{safe_key}\"\r\n\r\n".encode(), str(value).encode(), b"\r\n"])
    safe_field = re.sub(r"[^A-Za-z0-9_-]", "", file_field)
    safe_filename = filename.replace("\\", "/").split("/")[-1].replace("\r", "").replace("\n", "").replace('"', "")[:180]
    if not safe_field or not safe_filename:
        raise ValueError("File field and filename cannot be empty.")
    chunks.extend([f"--{boundary}\r\nContent-Disposition: form-data; name=\"{safe_field}\"; filename=\"{safe_filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode(), content, f"\r\n--{boundary}--\r\n".encode()])
    body = b"".join(chunks)
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("Multipart request exceeds the 100 MB MCP limit.")
    headers = {"X-VAOS-MCP-Approval-ID": approval_id} if approval_id else None
    return _api_request("POST", path, body=body, content_type=f"multipart/form-data; boundary={boundary}", extra_headers=headers)


@_instrument_tool("vaos_send_binary")
def vaos_send_binary(method: Literal["POST", "PUT", "PATCH"], path: str, content_type: str, content_base64: str, headers_json: str = "{}", approval_id: str = "") -> str:
    """Send a documented raw binary payload such as an image frame to a VAOS endpoint."""
    _validate_bounded_base64_input(content_base64, "Binary content")
    headers = _parse_bounded_json_object(headers_json, "headers_json")
    if len(content_type) > 200 or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", content_type):
        raise ValueError("Content type must be a simple media type such as image/jpeg.")
    _authorize_operation(method, path)
    body = _decode_bounded_base64(content_base64, "Binary content")
    allowed_headers = {str(k): str(v) for k, v in headers.items()}
    if approval_id:
        allowed_headers["X-VAOS-MCP-Approval-ID"] = approval_id
    return _api_request(method, path, body=body, content_type=content_type, extra_headers=allowed_headers)


@SERVER.resource("vaos://openapi-v1")
def vaos_openapi_reference() -> str:
    """Read the authenticated OpenAPI description for VAOS API v1."""
    _policy()
    return _api_request("GET", "/api/v1/openapi.json")


@SERVER.resource("vaos://agent-policy")
def vaos_agent_policy() -> str:
    """Read the current, administrator-configured MCP access policy."""
    return json.dumps(_policy(), indent=2)


if __name__ == "__main__":
    # MCP uses stdout for protocol messages; diagnostics belong on stderr only.
    SERVER.run()
