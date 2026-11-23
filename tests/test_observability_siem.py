"""Security checks for opt-in SIEM webhook configuration and delivery queue."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import tempfile
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import observability_siem as siem
from project_metadata import PROJECT_METADATA
from observability_store import initialize_database, query_events, query_security_audit, record_event, verify_security_chain


class ObservabilitySiemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vaos-siem-")
        self.database = Path(self.temp.name) / "app.db"
        initialize_database(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_authentication_audit_fallback_replays_after_primary_recovery(self):
        event = {
            "event_type": "auth.login_failed", "outcome": "denied", "component": "auth",
            "actor_ref": "unauthenticated-client", "details": {"reason": "invalid credentials"},
            "correlation_id": "qa-auth-fallback:request-1", "event_id": "a" * 32,
            "timestamp": "2026-11-14T00:00:00Z",
        }
        with patch.object(siem, "record_security_event_and_enqueue", side_effect=sqlite3.OperationalError("primary unavailable")):
            self.assertTrue(siem.record_security_event_resilient(self.database, **event))

        queued = siem.security_event_fallback_status(self.database)
        self.assertEqual(queued["pending_rows"], 1)
        self.assertEqual(queued["dropped_events"], 0)
        self.assertEqual(query_security_audit(self.database), [])

        self.assertEqual(siem.flush_security_event_fallback(self.database, maximum=10), 1)
        self.assertFalse(siem.security_event_fallback_pending(self.database))
        with closing(sqlite3.connect(self.database)) as db:
            audit_count = db.execute(
                "SELECT COUNT(*) FROM observability_security_audit WHERE event_id=?", (event["event_id"],)
            ).fetchone()[0]
            event_count = db.execute(
                "SELECT COUNT(*) FROM observability_events WHERE event_id=?", (event["event_id"],)
            ).fetchone()[0]
        self.assertEqual(audit_count, 1)
        self.assertEqual(event_count, 1)
        self.assertTrue(verify_security_chain(self.database)["valid"])

    def test_sqlite_full_primary_store_queues_auth_audit_and_replays_once(self):
        """A real SQLite page-limit error must use the durable sidecar, then replay once."""
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("VACUUM")
            page_limit = int(db.execute("PRAGMA page_count").fetchone()[0])
            db.commit()

        original_connection = siem.store._connection

        @contextmanager
        def page_limited_connection(path, *, timeout_seconds=15):
            resolved = Path(path).expanduser().resolve()
            with original_connection(resolved, timeout_seconds=timeout_seconds) as db:
                if resolved == self.database.resolve():
                    db.execute(f"PRAGMA max_page_count={page_limit}")
                yield db

        saw_full = False
        with patch.object(siem.store, "_connection", page_limited_connection):
            for index in range(40):
                try:
                    siem.store.record_event(
                        self.database,
                        event_type="test.storage_pressure",
                        component="test",
                        message="synthetic SQLite page-limit pressure",
                        details={"payload": f"{index}-" + ("x" * 14_000)},
                        event_id=f"sqlite-full-{index}",
                    )
                except sqlite3.OperationalError as exc:
                    self.assertIn("full", str(exc).casefold())
                    saw_full = True
                    break
            self.assertTrue(saw_full, "the page-limited primary store did not reach SQLITE_FULL")
            event_id = "f" * 32
            self.assertTrue(siem.record_security_event_resilient(
                self.database,
                event_type="auth.login_failed",
                outcome="denied",
                component="auth",
                actor_ref="unauthenticated-client",
                details={"reason": "synthetic primary database full", "context": "x" * 13_000},
                event_id=event_id,
                timestamp="2026-11-14T00:00:00Z",
            ))

        queued = siem.security_event_fallback_status(self.database)
        self.assertEqual(queued["pending_rows"], 1)
        self.assertEqual(queued["dropped_events"], 0)

        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA max_page_count=1073741823")
            db.commit()
        self.assertEqual(siem.flush_security_event_fallback(self.database, maximum=10), 1)
        self.assertEqual(siem.flush_security_event_fallback(self.database, maximum=10), 0)
        self.assertEqual(siem.security_event_fallback_status(self.database)["pending_rows"], 0)
        with closing(sqlite3.connect(self.database)) as db:
            audit_count = db.execute(
                "SELECT COUNT(*) FROM observability_security_audit WHERE event_id=?", (event_id,)
            ).fetchone()[0]
            event_count = db.execute(
                "SELECT COUNT(*) FROM observability_events WHERE event_id=?", (event_id,)
            ).fetchone()[0]
        self.assertEqual(audit_count, 1)
        self.assertEqual(event_count, 1)
        self.assertTrue(verify_security_chain(self.database)["valid"])

    def test_security_audit_fallback_deduplicates_ambiguous_primary_commit(self):
        event = {
            "event_type": "authz.request_denied", "outcome": "denied", "component": "authorization",
            "actor_ref": "unauthenticated-client", "details": {"http_status": 401, "route": "/api/admin/status"},
            "correlation_id": "qa-auth-fallback:request-2", "event_id": "b" * 32,
            "timestamp": "2026-11-14T00:01:00Z",
        }
        original = siem.record_security_event_and_enqueue

        def commit_then_lose_response(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("response lost after commit")

        with patch.object(siem, "record_security_event_and_enqueue", side_effect=commit_then_lose_response):
            self.assertTrue(siem.record_security_event_resilient(self.database, **event))
        self.assertEqual(siem.security_event_fallback_status(self.database)["pending_rows"], 1)
        self.assertEqual(siem.flush_security_event_fallback(self.database, maximum=10), 1)
        with closing(sqlite3.connect(self.database)) as db:
            audit_count = db.execute(
                "SELECT COUNT(*) FROM observability_security_audit WHERE event_id=?", (event["event_id"],)
            ).fetchone()[0]
            operational_count = db.execute(
                "SELECT COUNT(*) FROM observability_events WHERE event_id=?", (event["event_id"],)
            ).fetchone()[0]
        self.assertEqual(audit_count, 1)
        self.assertEqual(operational_count, 1)
        self.assertTrue(verify_security_chain(self.database)["valid"])

    def test_authentication_audit_fallback_is_bounded_and_counts_drops(self):
        kwargs = {
            "event_type": "auth.login_failed", "outcome": "denied", "component": "auth",
            "actor_ref": "unauthenticated-client", "details": {"reason": "invalid credentials"},
            "correlation_id": None, "timestamp": "2026-11-14T00:00:00Z",
        }
        with patch.object(siem, "SECURITY_EVENT_FALLBACK_MAX_ROWS", 1):
            self.assertTrue(siem.queue_security_event_fallback(self.database, event_id="c" * 32, **kwargs))
            self.assertFalse(siem.queue_security_event_fallback(self.database, event_id="d" * 32, **kwargs))
        status = siem.security_event_fallback_status(self.database)
        self.assertEqual(status["pending_rows"], 1)
        self.assertEqual(status["dropped_events"], 1)

    def test_authentication_fallback_reserves_outcome_and_redacts_secrets(self):
        kwargs = {
            "event_type": "auth.login_failed", "outcome": "denied", "component": "auth",
            "actor_ref": "unauthenticated-client",
            "details": {"outcome": "accepted", "password": "do-not-store", "api_token": "raw-secret", "reason": "bad credentials"},
            "correlation_id": None, "timestamp": "2026-11-14T00:00:00Z",
        }
        self.assertTrue(siem.queue_security_event_fallback(self.database, event_id="e" * 32, **kwargs))
        fallback_path = siem.security_event_fallback_database_path(self.database)
        with closing(sqlite3.connect(fallback_path)) as db:
            details_json = db.execute(
                "SELECT details_json FROM security_event_fallback_queue WHERE event_id=?", ("e" * 32,)
            ).fetchone()[0]
        self.assertEqual(json.loads(details_json)["outcome"], "denied")
        self.assertNotIn("password", details_json)
        self.assertNotIn("raw-secret", details_json)
        self.assertEqual(siem.flush_security_event_fallback(self.database, maximum=10), 1)
        audit = query_security_audit(self.database, limit=1, event_type="auth.login_failed")[0]
        self.assertEqual(audit["outcome"], "denied")

    def test_authentication_fallback_rejects_non_auth_event_types(self):
        with self.assertRaises(ValueError):
            siem.queue_security_event_fallback(
                self.database, event_type="settings.changed", outcome="denied", component="settings",
                actor_ref="local-admin", details={}, correlation_id=None,
                event_id="f" * 32, timestamp="2026-11-14T00:00:00Z",
            )

    def test_authentication_fallback_accepts_only_token_prefix_not_full_token(self):
        kwargs = {
            "event_type": "authz.request_denied", "outcome": "denied", "component": "authorization",
            "details": {"http_status": 403}, "correlation_id": None,
            "timestamp": "2026-11-14T00:00:00Z",
        }
        with self.assertRaises(ValueError):
            siem.queue_security_event_fallback(
                self.database, actor_ref="api-token:" + ("vaos_" + "X" * 48),
                event_id="a" * 32, **kwargs,
            )
        prefix = "api-token:vaos_Abcdef_09"
        self.assertTrue(siem.queue_security_event_fallback(
            self.database, actor_ref=prefix, event_id="b" * 32, **kwargs,
        ))
        fallback_path = siem.security_event_fallback_database_path(self.database)
        with closing(sqlite3.connect(fallback_path)) as db:
            actor_ref = db.execute(
                "SELECT actor_ref FROM security_event_fallback_queue WHERE event_id=?", ("b" * 32,)
            ).fetchone()[0]
        self.assertEqual(actor_ref, prefix)

    def test_resilient_auth_event_fails_when_both_stores_are_unavailable(self):
        event = {
            "event_type": "auth.login_succeeded", "outcome": "accepted", "component": "auth",
            "actor_ref": "local-admin", "details": {"mechanism": "http_basic"},
            "event_id": "9" * 32, "timestamp": "2026-11-14T00:00:00Z",
        }
        with patch.object(siem, "record_security_event_and_enqueue", side_effect=sqlite3.OperationalError("primary unavailable")):
            with patch.object(siem, "initialize_security_event_fallback_store", side_effect=OSError("sidecar unavailable")):
                self.assertFalse(siem.record_security_event_resilient(self.database, **event))

    def test_destination_url_blocks_private_hosts_credentials_queries_and_nonstandard_ports(self):
        for url in (
            "http://siem.example.com/webhook", "https://127.0.0.1/webhook", "https://localhost/webhook",
            "https://siem.example.com:8443/webhook", "https://user:pass@siem.example.com/webhook",
            "https://siem.example.com/webhook?token=secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                siem.validate_endpoint(url)

    def test_siem_serialization_receives_redacted_posix_paths_but_keeps_api_routes(self):
        record_event(
            self.database, event_id="posix-redaction-fixture", event_type="security.config.changed",
            component="settings", level="WARNING",
            message="Rejected source /mnt/vaos/private/config.json through /api/admin/observability/integrations",
            details={"path": "/home/aduit/VAOS_Datasets/secret.yaml", "route": "/api/admin/observability/integrations"},
        )
        event = query_events(self.database, event_id="posix-redaction-fixture", limit=1)[0]
        payload = {key: event.get(key) for key in ("event_id", "timestamp", "event_type", "level", "component", "message", "details")}
        body, _ = siem._mapped_event(payload, "json")
        serialized = body.decode("utf-8")
        self.assertNotIn("/mnt/vaos", serialized)
        self.assertNotIn("/home/aduit", serialized)
        self.assertIn("/api/admin/observability/integrations", serialized)

    def test_dns_lookup_timeout_does_not_block_delivery_worker_indefinitely(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        def delayed_getaddrinfo(host, port, **kwargs):
            entered.set()
            release.wait(1.0)
            try:
                return [(siem.socket.AF_INET, siem.socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]
            finally:
                finished.set()

        try:
            with patch.object(siem.socket, "getaddrinfo", side_effect=delayed_getaddrinfo):
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "dns_lookup_timeout"):
                    siem._resolve_public_ip("siem.example.com", timeout=0.05)
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
        self.assertTrue(finished.wait(1.0))

        # A timed-out resolver call must not poison the bounded pool after it returns.
        synthetic_dns = [(siem.socket.AF_INET, siem.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch.object(siem.socket, "getaddrinfo", return_value=synthetic_dns):
            self.assertEqual(siem._resolve_public_ip("siem.example.com", timeout=0.5), "8.8.8.8")

    def test_dns_resolver_queue_is_bounded_when_all_resolvers_stall(self):
        idle_deadline = time.monotonic() + 1.0
        while siem._dns_requests.unfinished_tasks and time.monotonic() < idle_deadline:
            time.sleep(0.005)
        self.assertEqual(siem._dns_requests.unfinished_tasks, 0)

        started = 0
        started_lock = threading.Lock()
        both_workers_started = threading.Event()
        release = threading.Event()
        callers: list[threading.Thread] = []
        results: list[list] = []

        def stalled_getaddrinfo(_host, port, **_kwargs):
            nonlocal started
            with started_lock:
                started += 1
                if started >= siem.DNS_RESOLVER_WORKERS:
                    both_workers_started.set()
            release.wait(2.0)
            return [(siem.socket.AF_INET, siem.socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        def waiting_caller():
            results.append(siem._bounded_getaddrinfo("siem.example.com", 443, timeout=1.0))

        try:
            with patch.object(siem.socket, "getaddrinfo", side_effect=stalled_getaddrinfo):
                for _ in range(siem.DNS_RESOLVER_WORKERS):
                    caller = threading.Thread(target=waiting_caller)
                    caller.start()
                    callers.append(caller)
                self.assertTrue(both_workers_started.wait(0.5))

                for _ in range(siem.DNS_RESOLVER_QUEUE_SIZE):
                    with self.assertRaisesRegex(RuntimeError, "dns_lookup_timeout"):
                        siem._bounded_getaddrinfo("queued.example.com", 443, timeout=0.01)
                with self.assertRaisesRegex(RuntimeError, "dns_resolver_busy"):
                    siem._bounded_getaddrinfo("overflow.example.com", 443, timeout=0.01)

                release.set()
                for caller in callers:
                    caller.join(timeout=1.0)
                self.assertTrue(all(not caller.is_alive() for caller in callers))
                self.assertEqual(len(results), siem.DNS_RESOLVER_WORKERS)
                self.assertTrue(all(len(result) == 1 for result in results))

                drain_deadline = time.monotonic() + 1.0
                while siem._dns_requests.unfinished_tasks and time.monotonic() < drain_deadline:
                    time.sleep(0.005)
                self.assertEqual(siem._dns_requests.unfinished_tasks, 0)
        finally:
            release.set()

    def test_dns_resolution_rejects_private_or_reserved_answers_even_when_mixed(self):
        public = (siem.socket.AF_INET, siem.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        private = (siem.socket.AF_INET, siem.socket.SOCK_STREAM, 6, "", ("10.2.3.4", 443))
        reserved = (siem.socket.AF_INET, siem.socket.SOCK_STREAM, 6, "", ("192.0.2.10", 443))

        for answers in ([private], [public, private], [public, reserved]):
            with self.subTest(answers=answers), patch.object(siem, "_bounded_getaddrinfo", return_value=answers):
                with self.assertRaisesRegex(RuntimeError, "dns_private_address_blocked"):
                    siem._resolve_public_ip("siem.example.com")

        with patch.object(siem, "_bounded_getaddrinfo", return_value=[public, (siem.socket.AF_INET, siem.socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]):
            self.assertEqual(siem._resolve_public_ip("siem.example.com"), "8.8.8.8")

    def test_syslog_and_otlp_endpoint_validation_stays_on_fixed_secure_transports(self):
        self.assertEqual(siem.validate_endpoint("syslog-tls://logs.example.com", "syslog_tls"), ("logs.example.com", "", 6514))
        self.assertEqual(siem.validate_endpoint("https://collector.example.com/v1/logs", "otlp"), ("collector.example.com", "/v1/logs", 443))
        for endpoint in (
            "syslog-tls://127.0.0.1:6514", "syslog-tls://localhost:6514", "syslog-tls://logs.example.com:6515",
            "syslog-tls://user:pass@logs.example.com", "syslog-tls://logs.example.com/path",
            "http://collector.example.com/v1/logs", "https://collector.example.com:8443/v1/logs",
        ):
            transport = "syslog_tls" if endpoint.startswith("syslog-tls:") else "otlp"
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                siem.validate_endpoint(endpoint, transport)

    def test_syslog_destination_has_no_secret_and_remains_disabled_by_default(self):
        saved = siem.configure_destination(
            self.database, display_name="TLS syslog", transport="syslog_tls",
            endpoint="syslog-tls://logs.example.com", secret_ref="",
        )
        self.assertEqual(saved["endpoint_port"], 6514)
        self.assertFalse(saved["enabled"])
        self.assertTrue(siem.set_destination_enabled(self.database, saved["destination_id"], True))
        listing = siem.list_destinations(self.database)
        self.assertEqual(listing["supported_transports"], ["https_json", "syslog_tls", "otlp"])
        self.assertEqual(listing["items"][0]["transport"], "syslog_tls")
        self.assertFalse(listing["items"][0]["credential_present"])
        self.assertNotIn("secret_ref", listing["items"][0])

    def test_edit_preserves_credential_reference_but_clears_it_on_transport_change(self):
        destination = siem.configure_destination(
            self.database, display_name="Edit me", endpoint="https://logs.example.com/old-path",
            secret_ref="VAOS_SIEM_TEST_KEY", enabled=False,
        )
        edited = siem.configure_destination(
            self.database, destination_id=destination["destination_id"], display_name="Edited",
            endpoint="https://logs.example.com/new-path", secret_ref=None, enabled=False,
        )
        self.assertEqual(edited["display_name"], "Edited")
        with closing(sqlite3.connect(self.database)) as db:
            saved = db.execute("SELECT secret_ref FROM observability_siem_destinations WHERE destination_id=?", (destination["destination_id"],)).fetchone()
        self.assertEqual(saved[0], "VAOS_SIEM_TEST_KEY")

        changed_transport = siem.configure_destination(
            self.database, destination_id=destination["destination_id"], display_name="Edited",
            transport="syslog_tls", endpoint="syslog-tls://logs.example.com", secret_ref=None, enabled=False,
        )
        self.assertEqual(changed_transport["transport"], "syslog_tls")
        with closing(sqlite3.connect(self.database)) as db:
            saved = db.execute("SELECT secret_ref FROM observability_siem_destinations WHERE destination_id=?", (destination["destination_id"],)).fetchone()
        self.assertEqual(saved[0], "")

    def test_security_audit_event_and_delivery_queue_are_recorded_atomically(self):
        destination = siem.configure_destination(
            self.database, display_name="Atomic queue", transport="syslog_tls",
            endpoint="syslog-tls://logs.example.com", secret_ref="", enabled=True,
        )
        result = siem.record_security_event_and_enqueue(
            self.database, event_id="atomic-security-event", event_type="auth.login_failed",
            outcome="denied", component="auth", details={"reason": "invalid credentials"},
            timestamp="2026-11-12T12:00:00Z",
        )
        self.assertTrue(result)
        with closing(sqlite3.connect(self.database)) as db:
            audit = db.execute("SELECT event_type FROM observability_security_audit WHERE event_id=?", ("atomic-security-event",)).fetchone()
            event = db.execute("SELECT event_type FROM observability_events WHERE event_id=?", ("atomic-security-event",)).fetchone()
            delivery = db.execute("SELECT status FROM observability_delivery_events WHERE destination_id=? AND event_id=?", (destination["destination_id"], "atomic-security-event")).fetchone()
        self.assertEqual(audit[0], "auth.login_failed")
        self.assertEqual(event[0], "security.auth.login_failed")
        self.assertEqual(delivery[0], "queued")
        self.assertFalse(siem.record_security_event_and_enqueue(
            self.database, event_id="atomic-security-event", event_type="auth.login_failed",
            outcome="denied", component="auth", timestamp="2026-11-12T12:00:00Z",
        ))

    def test_security_event_transaction_rolls_back_all_records_when_outbox_insert_fails(self):
        siem.configure_destination(
            self.database, display_name="Atomic failure", transport="syslog_tls",
            endpoint="syslog-tls://logs.example.com", secret_ref="", enabled=True,
        )
        with patch.object(siem, "_enqueue_security_event_in_transaction", side_effect=sqlite3.OperationalError("fixture failure")):
            with self.assertRaises(sqlite3.OperationalError):
                siem.record_security_event_and_enqueue(
                    self.database, event_id="atomic-rollback-event", event_type="auth.login_failed",
                    outcome="denied", component="auth", timestamp="2026-11-12T12:00:00Z",
                )
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_id=?", ("atomic-rollback-event",)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_events WHERE event_id=?", ("atomic-rollback-event",)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_delivery_events WHERE event_id=?", ("atomic-rollback-event",)).fetchone()[0], 0)

    def test_remove_archives_destination_and_preserves_delivery_history(self):
        destination = siem.configure_destination(self.database, display_name="Retired", transport="syslog_tls", endpoint="syslog-tls://logs.example.com", secret_ref="", enabled=True)
        record_event(self.database, event_id="remove-fixture", event_type="security.config.changed", component="settings", level="INFO", message="redacted", details={})
        self.assertEqual(siem.enqueue_security_event(self.database, "remove-fixture"), 1)
        archived = siem.archive_destination(self.database, destination["destination_id"])
        self.assertEqual(archived["display_name"], "Retired")
        self.assertEqual(archived["endpoint_host"], "logs.example.com")
        self.assertEqual(siem.list_destinations(self.database)["items"], [])
        self.assertFalse(siem.set_destination_enabled(self.database, destination["destination_id"], True))
        with closing(sqlite3.connect(self.database)) as db:
            saved = db.execute("SELECT endpoint,secret_ref,archived_at,dropped_events FROM observability_siem_destinations WHERE destination_id=?", (destination["destination_id"],)).fetchone()
            delivery = db.execute("SELECT status FROM observability_delivery_events WHERE destination_id=?", (destination["destination_id"],)).fetchone()
        self.assertEqual(tuple(saved), ("https://removed.invalid/", "", archived["archived_at"], 0))
        self.assertEqual(delivery[0], "disabled")
        with self.assertRaisesRegex(ValueError, "not found"):
            siem.send_test_event(self.database, destination["destination_id"])

    def test_delivery_queue_caps_pending_rows_and_reports_drops(self):
        siem.configure_destination(self.database, display_name="Bounded", transport="syslog_tls", endpoint="syslog-tls://logs.example.com", secret_ref="", enabled=True)
        with patch.object(siem, "MAX_PENDING_PER_DESTINATION", 1):
            for event_id in ("queue-one", "queue-two"):
                record_event(self.database, event_id=event_id, event_type="security.config.changed", component="settings", level="INFO", message="redacted", details={})
                siem.enqueue_security_event(self.database, event_id)
        item = siem.list_destinations(self.database)["items"][0]
        self.assertEqual(item["pending"], 1)
        self.assertEqual(item["dropped_events"], 1)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_delivery_events").fetchone()[0], 1)

    def test_terminal_delivery_history_is_pruned_when_queue_reaches_global_cap(self):
        destination = siem.configure_destination(self.database, display_name="Bounded", transport="syslog_tls", endpoint="syslog-tls://logs.example.com", secret_ref="", enabled=True)
        record_event(self.database, event_id="old-sent", event_type="security.config.changed", component="settings", level="INFO", message="redacted", details={})
        self.assertEqual(siem.enqueue_security_event(self.database, "old-sent"), 1)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE observability_delivery_events SET status='sent',updated_at='2026-01-01T00:00:00Z' WHERE event_id='old-sent'")
            db.commit()
        record_event(self.database, event_id="new-pending", event_type="security.config.changed", component="settings", level="INFO", message="redacted", details={})
        with patch.object(siem, "MAX_DELIVERY_ROWS", 1):
            self.assertEqual(siem.enqueue_security_event(self.database, "new-pending"), 1)
        with closing(sqlite3.connect(self.database)) as db:
            rows = db.execute("SELECT event_id,status FROM observability_delivery_events WHERE destination_id=?", (destination["destination_id"],)).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("new-pending", "queued")])

    def test_dispatch_lease_prevents_two_workers_sending_same_row(self):
        destination = siem.configure_destination(self.database, display_name="Concurrent", transport="syslog_tls", endpoint="syslog-tls://logs.example.com", secret_ref="", enabled=True)
        record_event(self.database, event_id="concurrent-fixture", event_type="security.config.changed", component="settings", level="INFO", message="redacted", details={})
        siem.enqueue_security_event(self.database, "concurrent-fixture")
        sending = threading.Event()
        release = threading.Event()
        results = []

        def mocked_delivery(*_args, **_kwargs):
            sending.set()
            if not release.wait(3):
                raise RuntimeError("test_delivery_timeout")
            return None

        def dispatch_first():
            results.append(siem.dispatch_pending(self.database, maximum=1))

        with patch.object(siem, "_deliver", side_effect=mocked_delivery) as delivery:
            worker = threading.Thread(target=dispatch_first)
            worker.start()
            try:
                self.assertTrue(sending.wait(3), "first worker did not reach its delivery claim")
                second = siem.dispatch_pending(self.database, maximum=1)
                self.assertEqual(second["attempted"], 0)
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive(), "first worker did not finish")
            self.assertEqual(delivery.call_count, 1)
        self.assertEqual(results[0]["sent"], 1)
        self.assertEqual(destination["display_name"], "Concurrent")

    def test_expired_dispatch_lease_cannot_overwrite_newer_delivery_result(self):
        siem.configure_destination(self.database, display_name="Lease", transport="syslog_tls", endpoint="syslog-tls://logs.example.com", secret_ref="", enabled=True)
        record_event(self.database, event_id="lease-fixture", event_type="security.config.changed", component="settings", level="INFO", message="redacted", details={})
        siem.enqueue_security_event(self.database, "lease-fixture")
        first_started = threading.Event()
        release_first = threading.Event()
        first_result = []
        calls = []
        calls_lock = threading.Lock()

        def mocked_delivery(*_args, **_kwargs):
            with calls_lock:
                calls.append(threading.get_ident())
                invocation = len(calls)
            if invocation == 1:
                first_started.set()
                if not release_first.wait(5):
                    raise RuntimeError("test_delivery_timeout")
                raise RuntimeError("simulated_stalled_delivery")
            return None

        def dispatch_first():
            first_result.append(siem.dispatch_pending(self.database, maximum=1))

        with patch.object(siem, "DISPATCH_LEASE_SECONDS", 1), patch.object(siem, "_deliver", side_effect=mocked_delivery):
            worker = threading.Thread(target=dispatch_first)
            worker.start()
            try:
                self.assertTrue(first_started.wait(3), "first worker did not reach its delivery claim")
                time.sleep(1.2)  # Let the first claim expire before the second worker reclaims it.
                second = siem.dispatch_pending(self.database, maximum=1)
                self.assertEqual(second["sent"], 1)
            finally:
                release_first.set()
                worker.join(3)
            self.assertFalse(worker.is_alive(), "first worker did not finish")

        with closing(sqlite3.connect(self.database)) as db:
            status, next_attempt = db.execute("SELECT status,next_attempt_at FROM observability_delivery_events WHERE event_id='lease-fixture'").fetchone()
        self.assertEqual((status, next_attempt), ("sent", None))
        self.assertEqual(first_result[0]["attempted"], 1)
        self.assertEqual(first_result[0]["failed"], 0)

    def test_dispatch_does_not_treat_fractional_future_retry_as_due(self):
        siem.configure_destination(self.database, display_name="Precision", transport="syslog_tls", endpoint="syslog-tls://logs.example.com", secret_ref="", enabled=True)
        record_event(self.database, event_id="precision-fixture", event_type="security.config.changed", component="settings", level="INFO", message="redacted", details={})
        siem.enqueue_security_event(self.database, "precision-fixture")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE observability_delivery_events SET status='retrying',next_attempt_at='2026-11-12T10:30:00.900000Z' WHERE event_id='precision-fixture'")
            db.commit()
        with patch.object(siem.store, "utc_now", return_value="2026-11-12T10:30:00.100000Z"), patch.object(siem, "_deliver") as delivery:
            result = siem.dispatch_pending(self.database, maximum=1)
        self.assertEqual(result["attempted"], 0)
        delivery.assert_not_called()

    def test_syslog_message_uses_rfc5424_and_octet_counting(self):
        frame = siem._syslog_message({
            "timestamp": "2026-11-12T10:00:00Z", "event_type": "security.auth.login_failed",
            "event_id": "abc123", "level": "WARNING", "message": "redacted event", "details": {"reason": "denied"},
        })
        length, message = frame.split(b" ", 1)
        self.assertEqual(int(length), len(message))
        self.assertTrue(message.startswith(b"<132>1 2026-11-12T10:00:00.000Z VAOS VAOS - security_auth_login_failed - "))
        self.assertIn("\ufeff".encode("utf-8"), message)

    def test_optional_cef_ecs_and_ndjson_mappings_have_expected_shapes(self):
        event = {"timestamp": "2026-11-12T10:00:00Z", "event_id": "evt-1", "event_type": "security.auth.login_failed", "level": "WARNING", "component": "auth", "message": "login denied", "details": {"outcome": "denied", "reason": "policy"}}
        cef, content_type = siem._mapped_event(event, "cef")
        self.assertEqual(content_type, "application/cef")
        self.assertTrue(cef.startswith(f"CEF:0|VAOS|VAOS|{PROJECT_METADATA['version']}|".encode()))
        self.assertIn(b"rt=1794477600000", cef)
        self.assertIn(b"externalId=evt-1", cef)
        self.assertIn(b"msg=login denied", cef)
        ecs, _content_type = siem._mapped_event(event, "ecs")
        ecs_obj = json.loads(ecs)
        self.assertEqual(ecs_obj["event"]["id"], "evt-1")
        self.assertEqual(ecs_obj["event"]["category"], ["authentication"])
        self.assertEqual(ecs_obj["vaos"]["details"]["outcome"], "denied")
        ndjson, content_type = siem._mapped_event(event, "ndjson")
        self.assertEqual(content_type, "application/x-ndjson")
        self.assertTrue(ndjson.endswith(b"\n"))
        self.assertEqual(json.loads(ndjson), event)

    def test_otlp_destination_rejects_non_otlp_mapping(self):
        with self.assertRaisesRegex(ValueError, "standard OpenTelemetry"):
            siem.configure_destination(self.database, display_name="OTLP", transport="otlp", event_format="cef", endpoint="https://collector.example.com/v1/logs", secret_ref="")

    def test_ocsf_mapping_is_rejected_until_required_identity_fields_are_available(self):
        with self.assertRaisesRegex(ValueError, "Choose JSON, NDJSON, CEF, or ECS"):
            siem.configure_destination(self.database, display_name="OCSF", event_format="ocsf", endpoint="https://siem.example.com/hook", secret_ref="VAOS_TEST_KEY")

    def test_syslog_tls_sends_to_pinned_public_ip_and_closes_tls(self):
        class RawSocket:
            def close(self): self.closed = True
        class TlsSocket:
            def __init__(self): self.payload = None
            def settimeout(self, value): self.timeout = value
            def sendall(self, value): self.payload = value
            def unwrap(self): return RawSocket()
            def close(self): pass
        class Context:
            def __init__(self, tls): self.tls = tls
            def wrap_socket(self, raw, server_hostname): self.raw = raw; self.server_hostname = server_hostname; return self.tls
        tls = TlsSocket(); context = Context(tls)
        with patch.object(siem, "_resolve_public_ip", return_value="203.0.113.5") as resolve, \
                patch.object(siem.socket, "create_connection", return_value=RawSocket()) as connect, \
                patch.object(siem, "_create_tls_context", return_value=context):
            siem._send_syslog_tls("syslog-tls://logs.example.com", {"event_type": "integration.test", "message": "safe"})
        resolve.assert_called_once_with("logs.example.com", 6514, timeout=3.0)
        connect.assert_called_once_with(("203.0.113.5", 6514), timeout=3.0)
        self.assertEqual(context.server_hostname, "logs.example.com")
        self.assertIsNotNone(tls.payload)

    def test_otlp_json_logs_use_otlp_fields_and_optional_bearer_token(self):
        class Response:
            status = 200
            def read(self, _limit): return b'{"partialSuccess":{}}'
        class FakeConnection:
            def __init__(self, host, ip, timeout=3.0): self.host, self.ip, self.timeout = host, ip, timeout
            def request(self, method, path, body, headers): self.method, self.path, self.body, self.headers = method, path, body, headers
            def getresponse(self): return Response()
            def close(self): pass
        fake = FakeConnection("collector.example.com", "203.0.113.7")
        event = {"timestamp": "2026-11-12T10:00:00Z", "event_id": "evt-1", "event_type": "security.auth.login_failed", "level": "WARNING", "component": "auth", "message": "redacted", "details": {"reason": "denied"}}
        with patch.object(siem, "_resolve_public_ip", return_value="203.0.113.7"), patch.object(siem, "_PinnedHttpsConnection", return_value=fake):
            status = siem._send_otlp("https://collector.example.com/v1/logs", "collector-token", event)
        document = json.loads(fake.body)
        record = document["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(status, 200)
        self.assertEqual((fake.method, fake.path), ("POST", "/v1/logs"))
        self.assertEqual(fake.headers["Content-Type"], "application/json")
        self.assertEqual(fake.headers["Authorization"], "Bearer collector-token")
        self.assertEqual(record["severityNumber"], 13)
        self.assertEqual(record["timeUnixNano"], "1794477600000000000")
        self.assertEqual(record["body"], {"stringValue": "redacted"})
        with patch.object(siem, "_resolve_public_ip", return_value="203.0.113.7"), patch.object(siem, "_PinnedHttpsConnection", return_value=fake):
            siem._send_otlp("https://collector.example.com/v1/logs", "", event)
        self.assertNotIn("Authorization", fake.headers)

    def test_otlp_partial_success_with_rejected_record_is_retried(self):
        class Response:
            status = 200
            def read(self, _limit): return b'{"partialSuccess":{"rejectedLogRecords":"1"}}'
        class FakeConnection:
            def __init__(self, host, ip, timeout=3.0): pass
            def request(self, *args, **kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch.object(siem, "_resolve_public_ip", return_value="203.0.113.8"), patch.object(siem, "_PinnedHttpsConnection", return_value=FakeConnection("collector.example.com", "203.0.113.8")):
            with self.assertRaisesRegex(RuntimeError, "otlp_records_rejected"):
                siem._send_otlp("https://collector.example.com/v1/logs", "", {"timestamp": "2026-11-12T10:00:00Z"})

    def test_configuration_is_disabled_by_default_and_never_returns_secret_reference_or_path(self):
        with patch.dict("os.environ", {}, clear=True):
            saved = siem.configure_destination(self.database, display_name="Team webhook", endpoint="https://siem.example.com/private-route", secret_ref="VAOS_SIEM_TEST_KEY")
            self.assertFalse(saved["enabled"])
            self.assertFalse(saved["secret_configured"])
            listing = siem.list_destinations(self.database)
        serialized = json.dumps(listing)
        self.assertNotIn("VAOS_SIEM_TEST_KEY", serialized)
        self.assertNotIn("private-route", serialized)
        self.assertEqual(listing["items"][0]["endpoint_host"], "siem.example.com")
        with self.assertRaisesRegex(ValueError, "before enabling"):
            siem.set_destination_enabled(self.database, saved["destination_id"], True)

    def test_signed_https_payload_uses_hmac_and_does_not_follow_any_redirect(self):
        class Response:
            status = 202
            def read(self, _limit): return b"ok"
        class FakeConnection:
            def __init__(self, host, ip, timeout=3.0):
                self.host, self.ip, self.timeout = host, ip, timeout
            def request(self, method, path, body, headers):
                self.method, self.path, self.body, self.headers = method, path, body, headers
            def getresponse(self): return Response()
            def close(self): pass
        fake = FakeConnection("siem.example.com", "203.0.113.4")
        with patch.object(siem, "_resolve_public_ip", return_value="203.0.113.4"), patch.object(siem, "_PinnedHttpsConnection", return_value=fake):
            status = siem._send("https://siem.example.com/ingest", "k" * 32, {"event_id": "abc"})
        payload = json.loads(fake.body)
        expected = hmac.new(b"k" * 32, fake.headers["X-VAOS-Timestamp"].encode("ascii") + b"." + fake.body, hashlib.sha256).hexdigest()
        self.assertEqual(status, 202)
        self.assertEqual(fake.method, "POST")
        self.assertEqual(fake.path, "/ingest")
        self.assertEqual(payload["event_id"], "abc")
        self.assertEqual(fake.headers["X-VAOS-Signature"], f"sha256={expected}")

    def test_signed_cef_webhook_signs_the_exact_mapped_body(self):
        class Response:
            status = 202
            def read(self, _limit): return b"accepted"
        class FakeConnection:
            def __init__(self, host, ip, timeout=3.0): pass
            def request(self, method, path, body, headers): self.method, self.path, self.body, self.headers = method, path, body, headers
            def getresponse(self): return Response()
            def close(self): pass
        fake = FakeConnection("siem.example.com", "203.0.113.4")
        event = {"event_id": "cef-1", "event_type": "security.event", "message": "a|b", "details": {"reason": "x=y"}}
        with patch.object(siem, "_resolve_public_ip", return_value="203.0.113.4"), patch.object(siem, "_PinnedHttpsConnection", return_value=fake):
            status = siem._send("https://siem.example.com/ingest", "k" * 32, event, event_format="cef")
        expected = hmac.new(b"k" * 32, fake.headers["X-VAOS-Timestamp"].encode("ascii") + b"." + fake.body, hashlib.sha256).hexdigest()
        self.assertEqual(status, 202)
        self.assertEqual(fake.headers["Content-Type"], "application/cef")
        self.assertIn(b"a\\|b", fake.body)
        self.assertIn(b"x\\=y", fake.body)
        self.assertEqual(fake.headers["X-VAOS-Signature"], f"sha256={expected}")

    def test_short_hmac_key_is_rejected_before_any_network_operation(self):
        with patch.object(siem, "_resolve_public_ip") as resolve:
            with self.assertRaisesRegex(RuntimeError, "hmac_key_too_short"):
                siem._send("https://siem.example.com/ingest", "too-short", {"event_id": "abc"})
        resolve.assert_not_called()

    def test_enabled_destination_requires_a_minimum_length_hmac_key(self):
        with patch.dict("os.environ", {"VAOS_SIEM_TEST_KEY": "short"}):
            with self.assertRaisesRegex(ValueError, "at least 32 UTF-8 bytes"):
                siem.configure_destination(self.database, display_name="Fixture", endpoint="https://siem.example.com/hook", secret_ref="VAOS_SIEM_TEST_KEY", enabled=True)

    def test_delivery_is_idempotent_and_failures_move_to_dead_letter(self):
        with patch.dict("os.environ", {"VAOS_SIEM_TEST_KEY": "a-test-key-not-a-production-secret"}):
            destination = siem.configure_destination(self.database, display_name="Fixture", endpoint="https://siem.example.com/hook", secret_ref="VAOS_SIEM_TEST_KEY", enabled=True)
            record_event(self.database, event_id="security-fixture", event_type="security.auth.login_failed", component="auth", level="WARNING", message="redacted security event", details={"reason": "test"})
            self.assertEqual(siem.enqueue_security_event(self.database, "security-fixture"), 1)
            self.assertEqual(siem.enqueue_security_event(self.database, "security-fixture"), 0)
            with patch.object(siem, "_send", side_effect=RuntimeError("https_delivery_failed")):
                for _ in range(siem.MAX_ATTEMPTS):
                    with closing(sqlite3.connect(self.database)) as db:
                        db.execute("UPDATE observability_delivery_events SET next_attempt_at=NULL WHERE destination_id=?", (destination["destination_id"],))
                        db.commit()
                    result = siem.dispatch_pending(self.database, maximum=1)
            self.assertEqual(result["dead_letter"], 1)
            listing = siem.list_destinations(self.database)["items"][0]
            self.assertEqual(listing["dead_letter"], 1)


if __name__ == "__main__":
    unittest.main()
