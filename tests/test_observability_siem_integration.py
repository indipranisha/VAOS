"""Loopback TLS transport checks; no external receiver or user data is used."""
from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import sqlite3
import socket
import ssl
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import observability_siem as siem


FIXTURES = Path(__file__).parent / "fixtures"
SERVER_CERTIFICATE = FIXTURES / "siem-loopback-server.pem"
PRIVATE_KEY = FIXTURES / "siem-loopback-key.pem"
TRUSTED_ROOT_CA = FIXTURES / "siem-test-root-ca.pem"
TRUSTED_SERVER_CERTIFICATE = FIXTURES / "siem-trusted-server-chain.pem"
TRUSTED_SERVER_KEY = FIXTURES / "siem-trusted-server-key.pem"
TEST_HOST = "logs.example.com"


class _HttpCapture(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        self.server.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
        reply = b'{"partialSuccess":{}}'
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class SiemLoopbackTlsIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.http_server, self.http_thread, self.server_context, self.sni_names = self._start_http_server(
            TRUSTED_SERVER_CERTIFICATE, TRUSTED_SERVER_KEY
        )

    def _start_http_server(self, certificate: Path, private_key: Path):
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(certificate), str(private_key))
        sni_names: list[str | None] = []
        server_context.set_servername_callback(
            lambda _sock, server_name, _ctx: sni_names.append(server_name)
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HttpCapture)
        server.daemon_threads = True
        server.requests = []
        original_get_request = server.get_request

        def get_tls_request():
            raw_socket, address = original_get_request()
            return server_context.wrap_socket(raw_socket, server_side=True), address

        server.get_request = get_tls_request
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, server_context, sni_names

    def tearDown(self) -> None:
        self.http_server.shutdown()
        self.http_server.server_close()
        self.http_thread.join(timeout=2)
        self.assertFalse(self.http_thread.is_alive())

    def _test_client_context(self, *_args: object, **_kwargs: object) -> ssl.SSLContext:
        # The host's HTTPS inspection proxy replaces loopback certificates. These
        # socket checks cover request framing/signatures only; strict trust and
        # hostname checks are exercised separately with MemoryBIO below.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _connection_class(self, port: int | None = None):
        port = port or self.http_server.server_port

        class LoopbackPinnedConnection(siem._PinnedHttpsConnection):
            def __init__(self, host: str, pinned_ip: str, *, timeout: float = 3.0, context: ssl.SSLContext | None = None):
                super().__init__(host, pinned_ip, timeout=timeout, context=context)
                self.port = port

        return LoopbackPinnedConnection

    def _patch_http(self):
        # Do not treat a loopback socket success as evidence of TLS trust.
        return (
            patch.object(siem, "_resolve_public_ip", return_value="127.0.0.1"),
            patch.object(siem, "_PinnedHttpsConnection", self._connection_class()),
            patch.object(siem, "_create_tls_context", side_effect=self._test_client_context),
        )

    def _trusted_memory_bio_handshake(
        self, hostname: str, client_context: ssl.SSLContext | None = None
    ) -> tuple[dict[str, object], list[str | None]]:
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(TRUSTED_SERVER_CERTIFICATE), str(TRUSTED_SERVER_KEY))
        sni_names: list[str | None] = []
        server_context.set_servername_callback(
            lambda _sock, server_name, _ctx: sni_names.append(server_name)
        )
        client_context = client_context or ssl.create_default_context(cafile=str(TRUSTED_ROOT_CA))
        client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        client = client_context.wrap_bio(
            client_in, client_out, server_side=False, server_hostname=hostname
        )
        server = server_context.wrap_bio(server_in, server_out, server_side=True)
        client_done = server_done = False

        for _ in range(20):
            for current, outgoing, peer_in in (
                (client, client_out, server_in),
                (server, server_out, client_in),
            ):
                try:
                    current.do_handshake()
                    if current is client:
                        client_done = True
                    else:
                        server_done = True
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    pass
                pending = outgoing.read()
                if pending:
                    peer_in.write(pending)
            if client_done and server_done:
                break

        if not (client_done and server_done):
            raise AssertionError("TLS handshake did not complete within the in-memory exchange limit")
        return client.getpeercert(), sni_names

    def test_signed_https_posts_hmac_json_over_loopback_tls(self) -> None:
        event = {"event_id": "loopback-http", "event_type": "security.test", "message": "local only", "details": {"ok": True}}
        patches = self._patch_http()
        with patches[0], patches[1], patches[2]:
            status = siem._send(f"https://{TEST_HOST}/events", "k" * 32, event, timeout=2.0)
        self.assertEqual(status, 202)
        request = self.http_server.requests[0]
        self.assertEqual(
            (request["path"], request["headers"]["Host"]),
            ("/events", f"{TEST_HOST}:{self.http_server.server_port}"),
        )
        self.assertEqual(json.loads(request["body"]), event)
        timestamp = request["headers"]["X-VAOS-Timestamp"]
        signature = hmac.new(b"k" * 32, timestamp.encode("ascii") + b"." + request["body"], hashlib.sha256).hexdigest()
        self.assertEqual(request["headers"]["X-VAOS-Signature"], f"sha256={signature}")
        self.assertEqual(self.sni_names, [TEST_HOST])

    def test_security_event_outbox_dispatch_reaches_https_receiver_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vaos-siem-dispatch-loopback-") as folder:
            database = Path(folder) / "observability.db"
            siem.store.initialize_database(database)
            with patch.dict(os.environ, {"VAOS_ISOLATED_TEST_HMAC": "h" * 40}), \
                    patch.object(siem, "_resolve_public_ip", return_value="127.0.0.1"):
                destination = siem.configure_destination(
                    database,
                    display_name="Loopback delivery test",
                    endpoint=f"https://{TEST_HOST}/events",
                    secret_ref="VAOS_ISOLATED_TEST_HMAC",
                    transport="https_json",
                    event_format="json",
                    enabled=True,
                )
                inserted = siem.record_security_event_and_enqueue(
                    database,
                    event_id="loopback-outbox-event",
                    event_type="auth.login_failed",
                    outcome="denied",
                    component="auth",
                    actor_ref="qa-loopback:synthetic",
                    correlation_id="qa-loopback-correlation",
                    details={"reason": "synthetic fixture only"},
                    timestamp="2099-01-01T10:00:00Z",
                )
                self.assertTrue(inserted)
                self.assertFalse(siem.record_security_event_and_enqueue(
                    database,
                    event_id="loopback-outbox-event",
                    event_type="auth.login_failed",
                    outcome="denied",
                    component="auth",
                    actor_ref="qa-loopback:synthetic",
                    correlation_id="qa-loopback-correlation",
                    details={"reason": "synthetic fixture only"},
                    timestamp="2099-01-01T10:00:00Z",
                ))
                with closing(sqlite3.connect(database)) as db:
                    queued = db.execute(
                        "SELECT status,next_attempt_at FROM observability_delivery_events WHERE event_id=? AND destination_id=?",
                        ("loopback-outbox-event", destination["destination_id"]),
                    ).fetchone()
                    occurrence_time = db.execute(
                        "SELECT timestamp FROM observability_events WHERE event_id=?",
                        ("loopback-outbox-event",),
                    ).fetchone()[0]
                self.assertIsNotNone(queued)
                self.assertEqual(queued[0], "queued")
                self.assertLessEqual(queued[1], siem.store.utc_now())
                self.assertTrue(occurrence_time.startswith("2099-01-01T10:00:00"))

                patches = self._patch_http()
                with patches[0], patches[1], patches[2]:
                    result = siem.dispatch_pending(database, maximum=10)
                    repeated = siem.dispatch_pending(database, maximum=10)

            self.assertEqual(result, {"attempted": 1, "sent": 1, "failed": 0, "dead_letter": 0})
            self.assertEqual(repeated, {"attempted": 0, "sent": 0, "failed": 0, "dead_letter": 0})
            self.assertEqual(len(self.http_server.requests), 1)
            request = self.http_server.requests[0]
            body = json.loads(request["body"])
            self.assertEqual(request["path"], "/events")
            self.assertEqual(body["event_id"], "loopback-outbox-event")
            self.assertEqual(body["correlation_id"], "qa-loopback-correlation")
            self.assertEqual(body["event_type"], "security.auth.login_failed")
            signature_timestamp = request["headers"]["X-VAOS-Timestamp"]
            expected_signature = hmac.new(
                ("h" * 40).encode("ascii"),
                signature_timestamp.encode("ascii") + b"." + request["body"],
                hashlib.sha256,
            ).hexdigest()
            self.assertEqual(request["headers"]["X-VAOS-Signature"], f"sha256={expected_signature}")
            self.assertEqual(self.sni_names, [TEST_HOST])
            with closing(sqlite3.connect(database)) as db:
                delivery = db.execute(
                    "SELECT status,attempt_count,last_status_code FROM observability_delivery_events WHERE event_id=? AND destination_id=?",
                    ("loopback-outbox-event", destination["destination_id"]),
                ).fetchone()
            self.assertEqual(delivery, ("sent", 1, 202))


    def test_default_tls_context_rejects_untrusted_certificate_before_sending(self) -> None:
        untrusted_server, untrusted_thread, _context, _sni = self._start_http_server(
            SERVER_CERTIFICATE, PRIVATE_KEY
        )
        try:
            connection_class = self._connection_class(untrusted_server.server_port)
            with patch.object(siem, "_resolve_public_ip", return_value="127.0.0.1"), \
                    patch.object(siem, "_PinnedHttpsConnection", connection_class):
                with self.assertRaisesRegex(RuntimeError, "https_delivery_failed") as raised:
                    siem._send(
                        f"https://{TEST_HOST}/events",
                        "k" * 32,
                        {"event_id": "untrusted-certificate", "event_type": "security.test"},
                        timeout=2.0,
                    )
        finally:
            untrusted_server.shutdown()
            untrusted_server.server_close()
            untrusted_thread.join(timeout=2)
        self.assertFalse(untrusted_thread.is_alive())
        self.assertIsInstance(raised.exception.__cause__, ssl.SSLCertVerificationError)
        self.assertEqual(untrusted_server.requests, [])

    def test_trusted_root_accepts_matching_hostname_in_tls_handshake(self) -> None:
        peer_certificate, sni_names = self._trusted_memory_bio_handshake(TEST_HOST)
        self.assertIn(("DNS", TEST_HOST), peer_certificate.get("subjectAltName", ()))
        self.assertEqual(sni_names, [TEST_HOST])

    def test_trusted_root_rejects_mismatched_hostname_in_tls_handshake(self) -> None:
        with self.assertRaises(ssl.SSLCertVerificationError) as raised:
            self._trusted_memory_bio_handshake("wrong.example.com")
        self.assertIn("hostname mismatch", raised.exception.verify_message.casefold())

    def test_operator_ca_bundle_adds_trust_without_disabling_hostname_checks(self) -> None:
        with patch.dict(os.environ, {siem.SIEM_CA_BUNDLE_ENV: str(TRUSTED_ROOT_CA)}):
            context = siem._create_tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        peer_certificate, sni_names = self._trusted_memory_bio_handshake(TEST_HOST, context)
        self.assertIn(("DNS", TEST_HOST), peer_certificate.get("subjectAltName", ()))
        self.assertEqual(sni_names, [TEST_HOST])
        with self.assertRaises(ssl.SSLCertVerificationError) as raised:
            self._trusted_memory_bio_handshake("wrong.example.com", context)
        self.assertIn("hostname mismatch", raised.exception.verify_message.casefold())

    def test_invalid_operator_ca_bundle_fails_closed_without_path_disclosure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vaos-invalid-siem-ca-") as folder:
            missing_bundle = Path(folder) / "missing-ca.pem"
            with patch.dict(os.environ, {siem.SIEM_CA_BUNDLE_ENV: str(missing_bundle)}):
                with self.assertRaisesRegex(RuntimeError, "tls_ca_bundle_invalid") as raised:
                    siem._create_tls_context()
        self.assertNotIn(str(missing_bundle), str(raised.exception))

    def test_relative_operator_ca_bundle_fails_closed(self) -> None:
        with patch.dict(os.environ, {siem.SIEM_CA_BUNDLE_ENV: "certs/custom-ca.pem"}):
            with self.assertRaisesRegex(RuntimeError, "tls_ca_bundle_invalid"):
                siem._create_tls_context()

    def test_unresolvable_user_home_is_wrapped_without_disclosing_path(self) -> None:
        configured_path = "~vaos-private-account/certs/ca.pem"
        with patch.dict(os.environ, {siem.SIEM_CA_BUNDLE_ENV: configured_path}), \
                patch.object(Path, "expanduser", side_effect=RuntimeError("cannot resolve ~vaos-private-account")):
            with self.assertRaisesRegex(RuntimeError, "tls_ca_bundle_invalid") as raised:
                siem._create_tls_context()
        self.assertNotIn("vaos-private-account", str(raised.exception))

    def test_malformed_operator_ca_bundle_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vaos-malformed-siem-ca-") as folder:
            malformed_bundle = Path(folder) / "malformed-ca.pem"
            malformed_bundle.write_text("not a certificate", encoding="ascii")
            with patch.dict(os.environ, {siem.SIEM_CA_BUNDLE_ENV: str(malformed_bundle)}):
                with self.assertRaisesRegex(RuntimeError, "tls_ca_bundle_invalid"):
                    siem._create_tls_context()

    def test_oversized_operator_ca_bundle_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vaos-oversized-siem-ca-") as folder:
            oversized_bundle = Path(folder) / "oversized-ca.pem"
            with oversized_bundle.open("wb") as output:
                output.truncate(siem.MAX_SIEM_CA_BUNDLE_BYTES + 1)
            with patch.dict(os.environ, {siem.SIEM_CA_BUNDLE_ENV: str(oversized_bundle)}):
                with self.assertRaisesRegex(RuntimeError, "tls_ca_bundle_invalid"):
                    siem._create_tls_context()

    def test_invalid_ca_bundle_fails_before_outbound_dns_for_each_transport(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vaos-missing-siem-ca-") as folder:
            missing_bundle = Path(folder) / "missing-ca.pem"
            senders = (
                lambda: siem._send("https://logs.example.com/events", "k" * 32, {"event_id": "test"}),
                lambda: siem._send_otlp("https://logs.example.com/v1/logs", "", {"event_id": "test"}),
                lambda: siem._send_syslog_tls("syslog-tls://logs.example.com", {"event_id": "test"}),
            )
            with patch.dict(os.environ, {siem.SIEM_CA_BUNDLE_ENV: str(missing_bundle)}), \
                    patch.object(siem, "_resolve_public_ip") as resolve:
                for send in senders:
                    with self.subTest(sender=send), self.assertRaisesRegex(RuntimeError, "tls_ca_bundle_invalid"):
                        send()
                resolve.assert_not_called()

    def test_otlp_posts_expected_logs_json_and_bearer_header_over_loopback_tls(self) -> None:
        event = {"timestamp": "2026-11-12T10:00:00Z", "event_id": "loopback-otlp", "level": "WARNING", "message": "local OTLP"}
        patches = self._patch_http()
        with patches[0], patches[1], patches[2]:
            status = siem._send_otlp(f"https://{TEST_HOST}/v1/logs", "test-token", event, timeout=2.0)
        self.assertEqual(status, 202)
        request = self.http_server.requests[0]
        self.assertEqual((request["path"], request["headers"]["Authorization"]), ("/v1/logs", "Bearer test-token"))
        record = json.loads(request["body"])["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(record["body"], {"stringValue": "local OTLP"})
        self.assertEqual(self.sni_names, [TEST_HOST])

    def test_https_sends_endpoint_hostname_as_sni(self) -> None:
        patches = self._patch_http()
        with patches[0], patches[1], patches[2]:
            status = siem._send("https://wrong.example.com/events", "k" * 32, {"event_id": "sni"}, timeout=2.0)
        self.assertEqual(status, 202)
        self.assertEqual(self.sni_names, ["wrong.example.com"])

    def test_syslog_tls_sends_octet_counted_frame_to_loopback(self) -> None:
        receiver = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.listen(1)
        receiver.settimeout(3.0)
        port = receiver.getsockname()[1]
        captured: list[bytes] = []
        failures: list[BaseException] = []

        def receive_frame() -> None:
            secure = None
            try:
                raw, _address = receiver.accept()
                secure = self.server_context.wrap_socket(raw, server_side=True)
                secure.settimeout(3.0)
                prefix = bytearray()
                while not prefix.endswith(b" "):
                    prefix.extend(secure.recv(1))
                declared_size = int(prefix[:-1])
                message = bytearray()
                while len(message) < declared_size:
                    chunk = secure.recv(declared_size - len(message))
                    if not chunk:
                        break
                    message.extend(chunk)
                if len(message) != declared_size:
                    raise AssertionError("truncated RFC 5425 frame")
                captured.append(bytes(message))
                raw = secure.unwrap()
                secure = None
                raw.close()
            except BaseException as exc:
                failures.append(exc)
            finally:
                if secure is not None:
                    secure.close()
                receiver.close()

        thread = threading.Thread(target=receive_frame, daemon=True)
        thread.start()
        validate_endpoint = siem.validate_endpoint

        def validate_for_ephemeral_port(endpoint: str, transport: str = "https_json"):
            validated = validate_endpoint(endpoint, transport)
            return (validated[0], validated[1], port) if transport == "syslog_tls" else validated

        try:
            with patch.object(siem, "validate_endpoint", side_effect=validate_for_ephemeral_port), \
                    patch.object(siem, "_resolve_public_ip", return_value="127.0.0.1"), \
                    patch.object(siem, "_create_tls_context", side_effect=self._test_client_context):
                siem._send_syslog_tls(
                    f"syslog-tls://{TEST_HOST}",
                    {"event_id": "loopback-syslog", "event_type": "security.test", "message": "local syslog"},
                    timeout=2.0,
                )
        finally:
            thread.join(timeout=4.0)
            receiver.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].startswith(b"<134>1 "))
        self.assertIn(b"loopback-syslog", captured[0])
        self.assertIn(b"local syslog", captured[0])

if __name__ == "__main__":
    unittest.main()
