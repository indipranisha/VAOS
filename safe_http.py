"""HTTP helpers for calls that carry credentials and must not follow redirects."""
from __future__ import annotations

from urllib.request import HTTPRedirectHandler, Request, build_opener


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_without_redirect(request: Request, *, timeout: float):
    """Open one HTTP request and surface 3xx responses without following them.

    This is intended for requests carrying API or internal-service credentials.
    A redirect is returned as urllib's normal HTTPError so callers can report
    an unavailable/misconfigured service without sending the credential again.
    """
    return build_opener(_RejectRedirectHandler).open(request, timeout=timeout)
