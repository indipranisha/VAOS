"""Optional local MCP bridge for the VAOS REST API.

Run this file as a stdio subprocess from an MCP host. It never starts or
restarts VAOS and it reads its API token from the process environment only.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
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
    "Never claim that an action succeeded unless the API response confirms it."
))
MAX_REQUEST_BYTES = 100 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
BASE_URL = os.getenv("VAOS_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
TOKEN = os.getenv("VAOS_API_TOKEN", "").strip()


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


def _api_request(method: str, path: str, body: bytes | None = None, content_type: str = "application/json", extra_headers: dict[str, str] | None = None) -> str:
    url = _url_for(path)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"}
    if body is not None:
        headers["Content-Type"] = content_type
    for name, value in (extra_headers or {}).items():
        normalized = name.casefold()
        if normalized not in {"x-video-time", "x-save-samples"}:
            raise ValueError(f"Header {name} is not supported by the VAOS MCP bridge.")
        if len(name) > 80 or len(value) > 2000:
            raise ValueError("A request header is too long.")
        headers[name] = value
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
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
        return json.dumps({"error": True, "status": exc.code, "detail": details or exc.reason})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return json.dumps({"error": True, "detail": f"VAOS request failed: {exc.__class__.__name__}"})


def _policy() -> dict:
    try:
        result = json.loads(_api_request("GET", "/api/v1/admin/integrations/mcp-policy"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("VAOS returned an invalid MCP policy response.") from exc
    if result.get("error"):
        raise RuntimeError(f"Could not read VAOS MCP policy: {result.get('detail', result.get('status'))}")
    if not result.get("api_enabled") or not result.get("mcp_enabled"):
        raise PermissionError("Enable API access and the MCP bridge in VAOS Settings first.")
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
    normalized = urllib.parse.urlsplit(path).path
    if normalized.startswith("/api/v1/admin/integrations") or normalized.startswith("/api/v1/admin/api-tokens"):
        raise PermissionError("MCP cannot manage API secrets or integration settings.")
    operation = _operation_for(method, path)
    group = operation.get("x-vaos-group") or _infer_group(normalized)
    if group not in policy.get("token_groups", []):
        raise PermissionError(f"The API token does not grant the '{group}' capability group.")
    if group not in policy.get("allowed_groups", []):
        raise PermissionError(f"The '{group}' capability is disabled in VAOS Settings.")
    verb = method.upper()
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
    checks = (
        ("/cameras", "cameras"), ("/camera/", "cameras"), ("/enrollment-links", "cameras"),
        ("/participants", "people"), ("/enroll/", "people"), ("/events", "events"),
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


def _is_sensitive_action(method: str, path: str) -> bool:
    if method != "POST":
        return False
    return path == "/api/v1/admin/cameras" or any(part in path for part in (
        "/services/restart", "/tunnel/", "/admin-tunnel/", "/train/stop",
        "/ml/train", "/ml/enrichment", "/ml/datasets/prepare", "/ml/datasets/import",
        "/ml/model-sources", "/ml/capture", "/model-selection", "/huggingface-models",
        "/video-review/sessions", "/participants", "/enrollment-links", "/enroll/", "/camera/",
    ))


@SERVER.tool()
def vaos_api_request(method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"], path: str, body_json: str = "{}") -> str:
    """Call one documented VAOS v1 API operation. For writes, JSON must match its OpenAPI schema."""
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
    return _api_request(method, path, body=body)


@SERVER.tool()
def vaos_upload_file(path: str, file_field: str, filename: str, content_type: str, content_base64: str, fields_json: str = "{}") -> str:
    """Upload a file to a documented VAOS multipart endpoint using base64 content."""
    _authorize_operation("POST", path)
    try:
        content = base64.b64decode(content_base64, validate=True)
        fields = json.loads(fields_json or "{}")
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("File data must be valid base64 and fields_json must be valid JSON.") from exc
    if len(content) > MAX_REQUEST_BYTES:
        raise ValueError("Upload exceeds the 100 MB MCP limit; use the REST API directly for large model files.")
    if not isinstance(fields, dict) or len(file_field) > 80 or len(filename) > 180 or any(len(str(k)) > 80 for k in fields):
        raise ValueError("Multipart fields or filename are not valid.")
    if not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", content_type):
        raise ValueError("Content type must be a simple media type such as image/jpeg or application/octet-stream.")
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
    return _api_request("POST", path, body=body, content_type=f"multipart/form-data; boundary={boundary}")


@SERVER.tool()
def vaos_send_binary(method: Literal["POST", "PUT", "PATCH"], path: str, content_type: str, content_base64: str, headers_json: str = "{}") -> str:
    """Send a documented raw binary payload such as an image frame to a VAOS endpoint."""
    _authorize_operation(method, path)
    try:
        body = base64.b64decode(content_base64, validate=True)
        headers = json.loads(headers_json or "{}")
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Binary content must be base64 and headers_json must be valid JSON.") from exc
    if len(body) > MAX_REQUEST_BYTES or not isinstance(headers, dict):
        raise ValueError("Binary request is too large or its headers are invalid.")
    if not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", content_type):
        raise ValueError("Content type must be a simple media type such as image/jpeg.")
    return _api_request(method, path, body=body, content_type=content_type, extra_headers={str(k): str(v) for k, v in headers.items()})


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
