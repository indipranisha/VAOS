"""Credential-bearing VAOS HTTP calls must never forward headers on redirects."""
from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request

from safe_http import open_without_redirect


class SafeHttpTests(unittest.TestCase):
    def test_redirect_does_not_send_credentials_to_target(self):
        class CaptureHandler(BaseHTTPRequestHandler):
            authorization_headers: list[str | None] = []

            def do_GET(self):
                self.authorization_headers.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, format, *args):
                return

        capture = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        capture_thread = threading.Thread(target=capture.serve_forever, daemon=True)
        capture_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            target_url = f"http://127.0.0.1:{capture.server_port}/capture"

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", self.target_url)
                self.end_headers()

            def log_message(self, format, *args):
                return

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{redirect.server_port}/internal",
                headers={"Authorization": "Bearer test-secret"},
            )
            with self.assertRaises(HTTPError) as raised:
                open_without_redirect(request, timeout=3)
            self.assertEqual(raised.exception.code, 302)
            self.assertEqual(CaptureHandler.authorization_headers, [])
        finally:
            redirect.shutdown()
            capture.shutdown()
            redirect.server_close()
            capture.server_close()
            redirect_thread.join(timeout=3)
            capture_thread.join(timeout=3)

    def test_non_redirect_response_remains_available_to_caller(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"healthy")

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with open_without_redirect(Request(f"http://127.0.0.1:{server.server_port}/health"), timeout=3) as response:
                self.assertEqual(response.read(), b"healthy")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
