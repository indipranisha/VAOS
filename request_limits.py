"""Bound request bodies before multipart parsers or handlers allocate them."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[dict[str, Any]]], Callable[..., Awaitable[None]]], Awaitable[None]]

MIB = 1024 * 1024
GIB = 1024 * MIB
DEFAULT_REQUEST_LIMIT = 16 * MIB
BODY_LIMITS: tuple[tuple[str, int, bool], ...] = (
    ("/api/admin/ml/model-sources", 2 * GIB + MIB, True),
    ("/api/enroll/", 32 * MIB + MIB, False),
    ("/api/admin/participants", 7 * MIB, True),
    ("/api/admin/projects/import", 3 * MIB, True),
    ("/api/admin/video-review/sessions/", 7 * MIB, False),
    ("/api/camera/", 7 * MIB, False),
)


class _RequestBodyTooLarge(Exception):
    pass


def _effective_path(path: str) -> str:
    if path == "/api/v1":
        return "/api"
    if path.startswith("/api/v1/"):
        return "/api" + path[len("/api/v1"):]
    return path


def _limit_for(method: str, path: str, default_limit: int) -> int:
    if method.upper() not in {"POST", "PUT", "PATCH"}:
        return default_limit
    normalized = _effective_path(path)
    for prefix, limit, exact in BODY_LIMITS:
        if (normalized == prefix if exact else normalized.startswith(prefix)):
            return limit
    return default_limit


class RequestBodyLimitMiddleware:
    """Reject oversized requests while they stream, including chunked bodies.

    The small default limit covers ordinary API writes. Explicit larger caps
    preserve the existing image, enrollment, project bundle and 2 GiB model
    upload features. ``Content-Length`` is only an early hint; received bytes
    are counted independently so a missing or false header cannot bypass it.
    """

    def __init__(self, app: ASGIApp, default_limit: int = DEFAULT_REQUEST_LIMIT) -> None:
        if default_limit <= 0:
            raise ValueError("default_limit must be positive")
        self.app = app
        self.default_limit = default_limit

    async def __call__(self, scope: dict[str, Any], receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        method = str(scope.get("method", "GET"))
        path = str(scope.get("path", ""))
        limit = _limit_for(method, path, self.default_limit)
        content_length = headers.get(b"content-length", b"")
        if content_length:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                declared_length = -1
            if declared_length > limit:
                await self._reject(send, limit)
                return

        received = 0
        response_started = False

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestBodyTooLarge
            return message

        async def track_response_start(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, track_response_start)
        except _RequestBodyTooLarge:
            if not response_started:
                await self._reject(send, limit)

    @staticmethod
    async def _reject(send, limit: int) -> None:
        body = json.dumps({"detail": "Request body exceeds the configured size limit.", "max_bytes": limit}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})
