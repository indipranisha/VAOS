"""Isolated API contract and access-control tests; never use the user's VAOS data."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import deque
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from project_bundles import build_bundle

_TEST_HOME = tempfile.TemporaryDirectory(prefix="vaos-api-tests-")
os.environ["VAOS_DATA_DIR"] = str(Path(_TEST_HOME.name) / "data")
os.environ["MODEL_DIR"] = str(Path(_TEST_HOME.name) / "models")

import main  # noqa: E402  (environment must be isolated before importing app)
import observability_store  # noqa: E402


class ApiSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(main.app, base_url="http://127.0.0.1")
        cls.client = cls.client_context.__enter__()
        cls.secure_context = TestClient(main.app, base_url="https://127.0.0.1")
        cls.secure_client = cls.secure_context.__enter__()
        cls.basic_auth = ("admin", main.ADMIN_PASSWORD)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.secure_context.__exit__(None, None, None)
            cls.client_context.__exit__(None, None, None)
            with closing(sqlite3.connect(main.DB_PATH)) as db:
                row = db.execute(
                    "SELECT exit_reason,clean_shutdown_at FROM observability_runtime_sessions "
                    "WHERE component='admin' AND process_id=? ORDER BY started_at DESC LIMIT 1",
                    (os.getpid(),),
                ).fetchone()
            if not row or row[0] != "clean" or not row[1]:
                raise AssertionError(f"TestClient app shutdown did not persist a clean lifecycle session: {row!r}")
        finally:
            _TEST_HOME.cleanup()

    def test_mcp_setup_template_uses_current_server_origin(self):
        response = self.client.get("http://127.0.0.1:8470/api/admin/integrations", auth=self.basic_auth)
        self.assertEqual(response.status_code, 200)
        env = response.json()["mcp_config_template"]["mcpServers"]["vaos"]["env"]
        self.assertEqual(env["VAOS_BASE_URL"], "http://127.0.0.1:8470")
        self.assertEqual(env["VAOS_API_TOKEN"], "PASTE_A_READ_OR_CONTROL_TOKEN_HERE")

    def test_public_coco_purge_requires_confirmation_and_removes_only_its_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            coco = root / "coco2017"
            coco.mkdir()
            manifest = coco / "dataset.yaml"
            manifest.write_text("names: [person]\n")
            sibling = root / "coco2017_detection"
            sibling.mkdir()
            record = {"id": "coco2017", "name": "COCO 2017", "data_yaml": str(manifest)}
            with patch.object(main, "ML_DATASETS_DIR", root), patch.object(main, "_dataset_allowed_roots", return_value=[root]), patch.object(main, "load_public_dataset_imports", return_value={"coco2017": record}), patch.object(main, "write_public_dataset_imports") as save, patch.object(main, "ml_training_lock") as training, patch.object(main, "ml_dataset_import_lock") as importing, patch.object(main, "ml_dataset_prepare_lock") as preparing:
                training.locked.return_value = importing.locked.return_value = preparing.locked.return_value = False
                with self.assertRaises(main.HTTPException) as wrong:
                    main.purge_managed_dataset("coco2017", {"confirm_name": "wrong"})
                self.assertEqual(wrong.exception.status_code, 400)
                self.assertTrue(coco.exists())
                training.locked.return_value = True
                with self.assertRaises(main.HTTPException) as busy:
                    main.purge_managed_dataset("coco2017", {"confirm_name": "COCO 2017"})
                self.assertEqual(busy.exception.status_code, 409)
                training.locked.return_value = False
                result = main.purge_managed_dataset("coco2017", {"confirm_name": "COCO 2017"})
                self.assertTrue(result["folder_removed"])
                self.assertFalse(coco.exists())
                self.assertTrue(sibling.exists())
                save.assert_called_once_with({})

    def save_integrations(self, **overrides):
        payload = {
            "api_enabled": True,
            "api_allow_sensitive_reads": False,
            "mcp_enabled": False,
            "mcp_read_only": True,
            "mcp_allow_sensitive": False,
            "mcp_allow_deletes": False,
            "api_rate_limit": 120,
            "api_token_default_days": 90,
            "mcp_allowed_groups": list(main.API_MCP_GROUPS),
        }
        payload.update(overrides)
        response = self.client.post("/api/admin/integrations", auth=self.basic_auth, json=payload)
        self.assertEqual(response.status_code, 200, response.text)

    def issue_token(self, *, groups, sensitive_read=False, mcp_agent=False, access="read"):
        response = self.client.post(
            "/api/admin/api-tokens",
            auth=self.basic_auth,
            json={"label": "isolated test", "scope": access, "groups": groups, "expires_days": 1, "sensitive_read": sensitive_read, "mcp_agent": mcp_agent},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_local_service_restart_is_rejected_without_a_supported_supervisor(self):
        with patch.object(main, "LOCAL_WINDOWS_LAUNCHER", False):
            response = self.client.post("/api/admin/services/restart", auth=self.basic_auth)
            dashboard = self.client.get("/settings", auth=self.basic_auth)

        self.assertEqual(response.status_code, 409)
        self.assertIn("restart both VAOS Uvicorn services", response.json()["detail"])
        self.assertFalse(main.LAUNCHER_COMMAND_PATH.exists())
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('id="restart-services" hidden', dashboard.text)
        self.assertIn('id="dashboard-tunnel-controls" hidden', dashboard.text)
        self.assertIn("Temporary tunnel controls are not available in this local setup.", dashboard.text)

    @staticmethod
    def recent_metric_window() -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        after = (now - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        before = (now + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        return after, before

    def test_admin_lifecycle_session_is_persisted_while_app_runs(self):
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            row = db.execute(
                "SELECT exit_reason,clean_shutdown_at FROM observability_runtime_sessions "
                "WHERE component='admin' AND process_id=? ORDER BY started_at DESC LIMIT 1",
                (os.getpid(),),
            ).fetchone()
        self.assertEqual(row, ("running", None))

    def test_camera_service_lifecycle_uses_shared_installation_and_stops_cleanly(self):
        import camera_service

        with TestClient(camera_service.app, base_url="http://127.0.0.1") as camera_client:
            response = camera_client.get(
                "/health",
                headers={main.OBSERVABILITY_CORRELATION_HEADER: "qa-camera:health-check"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get(main.OBSERVABILITY_CORRELATION_HEADER.lower()), "qa-camera:health-check")
            self.assertEqual(response.json()["app"], "vaos-camera")
            with closing(sqlite3.connect(main.DB_PATH)) as db:
                camera_session = db.execute(
                    "SELECT exit_reason,clean_shutdown_at,installation_id FROM observability_runtime_sessions "
                    "WHERE component='camera' AND process_id=? ORDER BY started_at DESC LIMIT 1",
                    (os.getpid(),),
                ).fetchone()
                admin_installations = {
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT installation_id FROM observability_runtime_sessions "
                        "WHERE component='admin' AND process_id=?",
                        (os.getpid(),),
                    ).fetchall()
                }
            self.assertTrue(admin_installations)
            self.assertIsNotNone(camera_session)
            self.assertEqual(camera_session[:2], ("running", None))
            self.assertIn(camera_session[2], admin_installations)

        with closing(sqlite3.connect(main.DB_PATH)) as db:
            stopped = db.execute(
                "SELECT exit_reason,clean_shutdown_at FROM observability_runtime_sessions "
                "WHERE component='camera' AND process_id=? ORDER BY started_at DESC LIMIT 1",
                (os.getpid(),),
            ).fetchone()
        self.assertEqual(stopped[0], "clean")
        self.assertTrue(stopped[1])

    def test_versioned_health_and_repeated_openapi_are_stable(self):
        health = self.client.get("/api/v1/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["version"], main.APP_VERSION)

        first = self.client.get("/api/v1/openapi.json", auth=self.basic_auth)
        second = self.client.get("/api/v1/openapi.json", auth=self.basic_auth)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        schema_a, schema_b = first.json(), second.json()
        self.assertEqual(schema_a["paths"].keys(), schema_b["paths"].keys())
        self.assertIn("/admin/ml/train", schema_a["paths"])
        self.assertIn("/admin/hardware", schema_a["paths"])
        self.assertIn("/admin/tracking/rules", schema_a["paths"])
        self.assertIn("/admin/tracking/events", schema_a["paths"])
        self.assertIn("/admin/projects", schema_a["paths"])
        self.assertIn("/admin/projects/import", schema_a["paths"])
        self.assertIn("/admin/observability/overview", schema_a["paths"])
        self.assertIn("/admin/observability/events", schema_a["paths"])
        self.assertIn("/admin/observability/events/{event_id}", schema_a["paths"])
        event_parameters = {item["name"]: item for item in schema_a["paths"]["/admin/observability/events"]["get"]["parameters"]}
        for field in ("training_job_id", "dataset_id", "model_id"):
            self.assertIn("Exact", event_parameters[field]["description"])
            self.assertIn("only events", event_parameters[field]["description"])
        self.assertIn("/admin/observability/log-filters", schema_a["paths"])
        self.assertIn("/admin/observability/log-filters/{filter_id}", schema_a["paths"])
        self.assertIn("/admin/observability/reports/jobs", schema_a["paths"])
        self.assertIn("/admin/observability/security-audit/events", schema_a["paths"])
        self.assertIn("/admin/observability/integrations", schema_a["paths"])
        self.assertIn("/admin/observability/retention", schema_a["paths"])
        self.assertEqual(set(schema_a["paths"]["/admin/observability/retention"]), {"get", "put", "post"})
        self.assertEqual(len(schema_a["paths"]), 72)
        self.assertEqual(sum(len(path_item) for path_item in schema_a["paths"].values()), 88)
        self.assertIn("VAOSBearer", schema_a["components"]["securitySchemes"])
        self.assertEqual(self.client.get("/api/docs").status_code, 401)
        self.assertEqual(self.client.get("/api/docs", auth=self.basic_auth).status_code, 200)

    def test_counter_queue_health_is_authenticated_and_reports_recovery_without_paths(self):
        self.assertEqual(self.client.get("/api/admin/observability/counter-queue").status_code, 401)
        route = "/api/v1/observability/queue-status-fixture"
        observability_store.buffer_counter_fallback(
            main.DB_PATH,
            metric_key="api.requests",
            dimensions={"route": route, "method": "GET", "status": 200},
        )
        status = self.client.get("/api/admin/observability/counter-queue", auth=self.basic_auth)
        self.assertEqual(status.status_code, 200, status.text)
        payload = status.json()
        self.assertEqual(payload["state"], "recovery_pending")
        self.assertEqual(payload["scope"], "admin_process_snapshot")
        self.assertFalse(payload["writes_verified"])
        self.assertGreaterEqual(payload["fallback"]["pending_increments"], 1)
        self.assertTrue(payload["fallback"]["available"])
        self.assertTrue(payload["security_event_fallback"]["available"])
        self.assertEqual(payload["security_event_fallback"]["pending_rows"], 0)
        self.assertNotIn(str(main.DB_PATH), json.dumps(payload))
        self.assertGreaterEqual(main.flush_observability_api_requests(max_events=5000), 1)
        clear_status = self.client.get("/api/admin/observability/counter-queue", auth=self.basic_auth).json()
        self.assertEqual(clear_status["state"], "no_backlog_observed")

    def test_counter_queue_status_requires_explicit_sensitive_read_for_api_tokens(self):
        self.save_integrations(api_enabled=True, api_allow_sensitive_reads=True)
        diagnostics = self.issue_token(groups=["diagnostics"])
        path = "/api/v1/admin/observability/counter-queue"
        denied = self.secure_client.get(path, headers=self.bearer(diagnostics["token"]))
        self.assertEqual(denied.status_code, 403)

        sensitive = self.issue_token(groups=["diagnostics"], sensitive_read=True)
        allowed = self.secure_client.get(path, headers=self.bearer(sensitive["token"]))
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["scope"], "admin_process_snapshot")

    def test_successful_basic_auth_is_audited_and_coalesced(self):
        with main.admin_auth_lock:
            main.admin_auth_success_audit_last.clear()
            main.admin_auth_audit_last.clear()
            main.admin_auth_failures.clear()
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            before_success = db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_type='auth.login_succeeded'").fetchone()[0]
            before_failure = db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_type='auth.login_failed'").fetchone()[0]

        supplied_correlation = "qa-auth:request-20261113"
        first = self.client.get(
            "/api/admin/observability/overview", auth=self.basic_auth,
            headers={main.OBSERVABILITY_CORRELATION_HEADER: supplied_correlation},
        )
        second = self.client.get("/api/admin/observability/overview", auth=self.basic_auth)
        bad_one = self.client.get("/api/admin/observability/overview", auth=("admin", "wrong-test-password"))
        bad_two = self.client.get("/api/admin/observability/overview", auth=("admin", "wrong-test-password"))
        after_failure = self.client.get("/api/admin/observability/overview", auth=self.basic_auth)
        repeated_after_failure = self.client.get("/api/admin/observability/overview", auth=self.basic_auth)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers.get(main.OBSERVABILITY_CORRELATION_HEADER.lower()), supplied_correlation)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(bad_one.status_code, 401)
        self.assertEqual(bad_two.status_code, 401)
        self.assertEqual(after_failure.status_code, 200, after_failure.text)
        self.assertEqual(repeated_after_failure.status_code, 200, repeated_after_failure.text)

        with closing(sqlite3.connect(main.DB_PATH)) as db:
            after_success = db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_type='auth.login_succeeded'").fetchone()[0]
            after_failure = db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_type='auth.login_failed'").fetchone()[0]
            row = db.execute("SELECT outcome,actor_ref,details_json FROM observability_security_audit WHERE event_type='auth.login_succeeded' ORDER BY sequence_id DESC LIMIT 1").fetchone()
            correlated_success = db.execute(
                "SELECT correlation_id FROM observability_security_audit WHERE event_type='auth.login_succeeded' AND correlation_id=?",
                (supplied_correlation,),
            ).fetchone()
            failed_row = db.execute("SELECT outcome,actor_ref,details_json FROM observability_security_audit WHERE event_type='auth.login_failed' ORDER BY sequence_id DESC LIMIT 1").fetchone()
        self.assertEqual(after_success, before_success + 2, "routine authenticated requests are coalesced, and success is recorded again after a failed request")
        self.assertEqual(after_failure, before_failure + 1, "repeated failed requests from one client are coalesced")
        self.assertEqual(row[0], "accepted")
        self.assertEqual(row[1], "local-admin")
        self.assertIsNotNone(correlated_success, "the correlation ID is attached to the accepted-authentication audit record")
        details = json.loads(row[2])
        self.assertEqual(details["mechanism"], "http_basic")
        self.assertEqual(details["repeated_successes_coalesced_seconds"], 60)
        self.assertNotIn("127.0.0.1", row[2])
        self.assertNotIn(main.ADMIN_PASSWORD, row[2])
        self.assertNotIn("wrong-test-password", row[2])
        self.assertEqual(failed_row[0], "denied")
        self.assertEqual(failed_row[1], "unauthenticated-client")
        self.assertNotIn("127.0.0.1", failed_row[2])
        self.assertNotIn("wrong-test-password", failed_row[2])

    def test_security_event_helper_reports_persistence_failure_without_claiming_success(self):
        with patch.object(main.observability_siem, "record_security_event_resilient", return_value=True):
            self.assertTrue(main._record_observability_security_event(
                "test.persistence_success", "complete", component="test",
                details={"fixture": True},
            ))

        with patch.object(main.observability_siem, "record_security_event_resilient", return_value=False):
            persisted = main._record_observability_security_event(
                "test.persistence_failure", "failed", component="test",
                details={"fixture": True},
            )

        self.assertFalse(persisted)

    def test_settings_and_token_changes_roll_back_with_failed_security_audit(self):
        self.save_integrations(api_enabled=False, mcp_enabled=False)
        baseline_settings = main.get_api_settings()
        token = self.issue_token(groups=["diagnostics"])
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            token_event_count = db.execute(
                "SELECT COUNT(*) FROM observability_security_audit WHERE event_type='auth.api_token_created'"
            ).fetchone()[0]

        original = main.observability_siem.record_security_event_and_enqueue_in_transaction
        failing_events = {
            "config.api_mcp_settings_changed", "auth.api_token_created", "auth.api_token_revoked",
        }

        def fail_selected_audit(db, **kwargs):
            if kwargs.get("event_type") in failing_events:
                raise sqlite3.OperationalError("fixture audit persistence failure")
            return original(db, **kwargs)

        with patch.object(main.observability_siem, "record_security_event_and_enqueue_in_transaction", side_effect=fail_selected_audit):
            changed_settings = self.client.post(
                "/api/admin/integrations", auth=self.basic_auth,
                json={"api_enabled": True, "mcp_enabled": True, "mcp_read_only": False},
            )
            failed_create = self.client.post(
                "/api/admin/api-tokens", auth=self.basic_auth,
                json={"label": "audit failure rollback", "scope": "read", "groups": ["diagnostics"], "expires_days": 1},
            )
            failed_revoke = self.client.delete(
                f"/api/admin/api-tokens/{token['prefix']}", auth=self.basic_auth,
            )

        self.assertEqual(changed_settings.status_code, 503, changed_settings.text)
        self.assertEqual(failed_create.status_code, 503, failed_create.text)
        self.assertEqual(failed_revoke.status_code, 503, failed_revoke.text)
        self.assertEqual(main.get_api_settings(), baseline_settings)
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            failed_token = db.execute(
                "SELECT COUNT(*) FROM api_tokens WHERE label='audit failure rollback'"
            ).fetchone()[0]
            still_active = db.execute(
                "SELECT COUNT(*) FROM api_tokens WHERE token_prefix=? AND revoked_at IS NULL", (token["prefix"],)
            ).fetchone()[0]
            token_event_count_after = db.execute(
                "SELECT COUNT(*) FROM observability_security_audit WHERE event_type='auth.api_token_created'"
            ).fetchone()[0]
        self.assertEqual(failed_token, 0)
        self.assertEqual(still_active, 1)
        self.assertEqual(token_event_count_after, token_event_count + 0)

    def test_mcp_approval_decision_rolls_back_when_audit_write_fails(self):
        self.enable_mcp_control()
        token = self.issue_token(groups=["settings"], mcp_agent=True, access="control")
        headers = self.bearer(token["token"])
        pending = self.secure_client.post(
            "/api/v1/admin/engine-settings", headers=headers,
            json={"object_confidence": 0.37},
        )
        self.assertEqual(pending.status_code, 428, pending.text)
        request_id = pending.json()["detail"]["request_id"]

        original = main.observability_siem.record_security_event_and_enqueue_in_transaction

        def fail_decision_audit(db, **kwargs):
            if kwargs.get("event_type") == "mcp.approval_decided":
                raise sqlite3.OperationalError("fixture approval audit failure")
            return original(db, **kwargs)

        with patch.object(main.observability_siem, "record_security_event_and_enqueue_in_transaction", side_effect=fail_decision_audit):
            decision = self.client.post(
                f"/api/admin/mcp-approvals/{request_id}/decision", auth=self.basic_auth,
                json={"decision": "approve"},
            )

        self.assertEqual(decision.status_code, 503, decision.text)
        status = self.secure_client.get(f"/api/v1/admin/mcp-approvals/{request_id}", headers=headers)
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "pending")

    def test_admin_action_is_not_run_when_successful_login_audit_fails(self):
        self.save_integrations(api_enabled=False)
        with main.admin_auth_lock:
            main.admin_auth_success_audit_last.clear()
        baseline_settings = main.get_api_settings()

        with patch.object(main.observability_siem, "record_security_event_resilient", return_value=False):
            response = self.client.post(
                "/api/admin/integrations", auth=self.basic_auth,
                json={"api_enabled": True, "mcp_enabled": False},
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(main.get_api_settings(), baseline_settings)
        with main.admin_auth_lock:
            self.assertFalse(main.admin_auth_success_audit_last)

    def test_authentication_and_authorization_denials_are_coalesced_and_redacted(self):
        with main.admin_auth_lock:
            main.authorization_denial_audit_last.clear()
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            before = db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_type='authz.request_denied'").fetchone()[0]

        supplied_correlation = "qa-denial:request-20261113"
        missing_one = self.client.get(
            "/api/admin/observability/overview",
            headers={main.OBSERVABILITY_CORRELATION_HEADER: supplied_correlation},
        )
        missing_two = self.client.get("/api/admin/observability/overview")
        self.assertEqual(missing_one.status_code, 401)
        self.assertEqual(missing_one.headers.get(main.OBSERVABILITY_CORRELATION_HEADER.lower()), supplied_correlation)
        self.assertEqual(missing_two.status_code, 401)
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            after_missing = db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_type='authz.request_denied'").fetchone()[0]
            correlated = db.execute(
                "SELECT correlation_id FROM observability_security_audit WHERE event_type='authz.request_denied' AND correlation_id=?",
                (supplied_correlation,),
            ).fetchone()
        self.assertEqual(after_missing, before + 1, "repeated unauthenticated requests to the same route are coalesced")
        self.assertIsNotNone(correlated, "the response correlation ID is searchable on its security audit record")

        self.save_integrations(api_enabled=True)
        token = self.issue_token(groups=["people"])
        denied = self.secure_client.get("/api/v1/admin/participants", headers=self.bearer(token["token"]))
        self.assertEqual(denied.status_code, 403, denied.text)
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            rows = db.execute("SELECT outcome,actor_ref,details_json FROM observability_security_audit WHERE event_type='authz.request_denied' ORDER BY sequence_id DESC LIMIT 2").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[0] == "denied" for row in rows))
        denial_payloads = [json.loads(row[2]) for row in rows]
        self.assertTrue(any(payload["http_status"] == 401 for payload in denial_payloads))
        self.assertTrue(any(payload["http_status"] == 403 and payload["reason"] == "authorization_denied" for payload in denial_payloads))
        self.assertTrue(any(row[1] == f"api-token:{token['prefix']}" for row in rows))
        for row in rows:
            self.assertNotIn("127.0.0.1", row[2])
            self.assertNotIn(token["token"], row[2])

    def test_failed_authorization_denial_audit_is_queued_and_coalesced(self):
        with main.admin_auth_lock:
            main.authorization_denial_audit_last.clear()
        with patch.object(
            main.observability_siem,
            "record_security_event_and_enqueue",
            side_effect=sqlite3.OperationalError("fixture denial audit failure"),
        ) as record:
            with patch.object(main.observability_siem, "queue_security_event_fallback", return_value=True) as queue:
                first = self.client.get("/api/admin/observability/overview")
                second = self.client.get("/api/admin/observability/overview")
        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 401)
        self.assertEqual(record.call_count, 1)
        queue.assert_called_once()
        with main.admin_auth_lock:
            self.assertTrue(main.authorization_denial_audit_last)

    def test_failed_login_audit_does_not_suppress_retry(self):
        with main.admin_auth_lock:
            main.admin_auth_audit_last.clear()
            main.admin_auth_failures.clear()
        with patch.object(
            main.observability_siem,
            "record_security_event_resilient", return_value=True,
        ) as record:
            first = self.client.get("/api/admin/observability/overview", auth=("admin", "wrong-test-password"))
            second = self.client.get("/api/admin/observability/overview", auth=("admin", "wrong-test-password"))
        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 401)
        self.assertEqual(record.call_count, 1)
        with main.admin_auth_lock:
            self.assertTrue(main.admin_auth_audit_last)

    def test_invalid_request_correlation_header_is_replaced_with_opaque_id(self):
        response = self.client.get(
            "/api/v1/health",
            headers={main.OBSERVABILITY_CORRELATION_HEADER: "contains unsafe spaces"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        correlation_id = response.headers.get(main.OBSERVABILITY_CORRELATION_HEADER.lower(), "")
        self.assertRegex(correlation_id, r"\A[0-9a-f]{32}\Z")
        self.assertNotEqual(correlation_id, "contains unsafe spaces")

    def test_missing_request_correlation_header_generates_fresh_ids(self):
        first = self.client.get("/api/v1/health")
        second = self.client.get("/api/v1/health")
        first_id = first.headers.get(main.OBSERVABILITY_CORRELATION_HEADER.lower(), "")
        second_id = second.headers.get(main.OBSERVABILITY_CORRELATION_HEADER.lower(), "")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertRegex(first_id, r"\A[0-9a-f]{32}\Z")
        self.assertRegex(second_id, r"\A[0-9a-f]{32}\Z")
        self.assertNotEqual(first_id, second_id, "a missing caller value receives a new request-scoped ID")

    def test_request_correlation_context_is_copied_to_worker_without_leaking(self):
        supplied_id = "qa-worker:request-20261113"
        seen: list[str | None] = []
        token = main.observability_correlation_context.set(supplied_id)
        try:
            worker = main._start_context_preserving_thread(lambda: seen.append(main.observability_correlation_context.get()), name="vaos-correlation-test")
        finally:
            main.observability_correlation_context.reset(token)
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive(), "the isolated context test worker completed")
        self.assertEqual(seen, [supplied_id])
        self.assertIsNone(main.observability_correlation_context.get(), "resetting the caller context does not leak its request ID")

    def test_api_explorer_is_admin_protected_and_uses_shared_openapi_contract(self):
        self.assertEqual(self.client.get("/api-explorer").status_code, 401)
        response = self.client.get("/api-explorer", auth=self.basic_auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("API Explorer", response.text)
        self.assertIn("/static/api_explorer.js", response.text)

    def test_observability_audit_integrity_endpoint_reports_valid_and_tampered_chain(self):
        endpoint = "/api/admin/observability/security-audit/integrity"
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        with tempfile.TemporaryDirectory(prefix="vaos-audit-integrity-api-") as temporary:
            database = Path(temporary) / "app.db"
            observability_store.initialize_database(database)
            observability_store.record_security_event(
                database,
                event_id="audit-integrity-api-fixture",
                event_type="security.fixture",
                outcome="allowed",
                component="test",
                actor_ref="synthetic-test-user",
                details={"fixture": "integrity-api"},
            )
            with patch.object(main, "DB_PATH", database):
                page = self.client.get("/statistics", auth=self.basic_auth)
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn('id="obs-audit-verify"', page.text)

                valid = self.client.get(endpoint, auth=self.basic_auth)
                self.assertEqual(valid.status_code, 200, valid.text)
                self.assertTrue(valid.json()["valid"], valid.json())
                self.assertGreaterEqual(valid.json()["checked"], 1)
                self.assertIsNone(valid.json()["failed_sequence"])

                with closing(sqlite3.connect(database)) as db:
                    db.execute("BEGIN")
                    db.execute("DROP TRIGGER trg_obs_security_no_update")
                    db.execute(
                        "UPDATE observability_security_audit SET outcome='denied' "
                        "WHERE event_id='audit-integrity-api-fixture'"
                    )
                    db.commit()
                tampered = self.client.get(endpoint, auth=self.basic_auth)
                self.assertEqual(tampered.status_code, 200, tampered.text)
                self.assertEqual(
                    tampered.json(),
                    {"valid": False, "checked": 1, "failed_sequence": 1},
                )

    def test_retention_policy_api_auth_confirms_shortening_and_audits_changes(self):
        with tempfile.TemporaryDirectory(prefix="vaos-retention-api-") as folder:
            database = Path(folder) / "observability.db"
            observability_store.initialize_database(database)
            old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds").replace("+00:00", "Z")
            payload = {
                "event_details_days": 30,
                "metric_observations_max_days": 30,
                "delivery_history_days": 30,
                "confirm_shorter": False,
            }
            with patch.object(main, "DB_PATH", database):
                self.assertEqual(self.client.get("/api/admin/observability/retention").status_code, 401)
                self.assertEqual(self.client.put("/api/admin/observability/retention", json=payload).status_code, 401)
                self.assertEqual(self.client.post("/api/admin/observability/retention", json={"confirm_expired_data_redaction": True}).status_code, 401)

                current = self.client.get("/api/admin/observability/retention", auth=self.basic_auth)
                self.assertEqual(current.status_code, 200, current.text)
                self.assertEqual(current.json()["event_details_days"], 90)
                self.assertEqual(current.json()["options"], [30, 90, 180, 365])
                self.assertEqual(self.client.put("/api/admin/observability/retention", auth=self.basic_auth, json={**payload, "event_details_days": 31}).status_code, 400)
                self.assertEqual(self.client.put("/api/admin/observability/retention", auth=self.basic_auth, json=payload).status_code, 400)
                saved = self.client.put("/api/admin/observability/retention", auth=self.basic_auth, json={**payload, "confirm_shorter": True})
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertTrue(saved.json()["shortened"])
                self.assertEqual(saved.json()["event_details_days"], 30)

                observability_store.record_event(
                    database, event_id="retention-api-old-event", event_type="observability.fixture", component="test",
                    message="expired event detail", timestamp=old_time,
                )
                self.assertEqual(self.client.post("/api/admin/observability/retention", auth=self.basic_auth, json={"confirm_expired_data_redaction": False}).status_code, 400)
                self.assertEqual(self.client.post("/api/admin/observability/retention", auth=self.basic_auth, json={}).status_code, 422)
                applied = self.client.post("/api/admin/observability/retention", auth=self.basic_auth, json={"confirm_expired_data_redaction": True})
                self.assertEqual(applied.status_code, 200, applied.text)
                self.assertGreaterEqual(applied.json()["event_details_redacted"], 1)
                self.assertIn("remain available until their own 24-hour expiry", applied.json()["message"])
                self.assertEqual(observability_store.query_events(database, event_id="retention-api-old-event"), [])
                with closing(sqlite3.connect(database)) as db:
                    audited = db.execute(
                        "SELECT COUNT(*) FROM observability_security_audit WHERE event_type IN ('observability.retention_policy_changed','observability.retention_applied')"
                    ).fetchone()[0]
                self.assertEqual(audited, 2)

    def test_retention_policy_and_audit_roll_back_together_on_storage_failure(self):
        with tempfile.TemporaryDirectory(prefix="vaos-retention-audit-rollback-") as folder:
            database = Path(folder) / "observability.db"
            observability_store.initialize_database(database)
            with patch.object(main, "DB_PATH", database):
                before = self.client.get("/api/admin/observability/retention", auth=self.basic_auth)
                self.assertEqual(before.status_code, 200, before.text)
                original = main.observability_siem.record_security_event_and_enqueue_in_transaction

                def fail_policy_audit(db, **kwargs):
                    if kwargs.get("event_type") == "observability.retention_policy_changed":
                        raise sqlite3.OperationalError("fixture retention audit failure")
                    return original(db, **kwargs)

                with patch.object(main.observability_siem, "record_security_event_and_enqueue_in_transaction", side_effect=fail_policy_audit):
                    failed = self.client.put(
                        "/api/admin/observability/retention", auth=self.basic_auth,
                        json={"event_details_days": 180, "metric_observations_max_days": 365, "delivery_history_days": 90},
                    )

                self.assertEqual(failed.status_code, 503, failed.text)
                after = self.client.get("/api/admin/observability/retention", auth=self.basic_auth)
                self.assertEqual(after.status_code, 200, after.text)
                self.assertEqual(after.json()["event_details_days"], before.json()["event_details_days"])
                with closing(sqlite3.connect(database)) as db:
                    changed_events = db.execute(
                        "SELECT COUNT(*) FROM observability_security_audit WHERE event_type='observability.retention_policy_changed'"
                    ).fetchone()[0]
                    current_days = db.execute(
                        "SELECT event_details_days FROM observability_retention_policy WHERE singleton_id=1"
                    ).fetchone()[0]
                self.assertEqual(changed_events, 0)
                self.assertEqual(current_days, 90)

    def test_retention_cleanup_rolls_back_when_audit_storage_fails(self):
        with tempfile.TemporaryDirectory(prefix="vaos-retention-cleanup-audit-") as folder:
            database = Path(folder) / "observability.db"
            observability_store.initialize_database(database)
            old_time = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(timespec="seconds").replace("+00:00", "Z")
            observability_store.record_event(
                database, event_id="retention-audit-rollback-event", event_type="observability.fixture",
                component="test", message="keep this detail if audit storage fails",
                details={"fixture": "preserve"}, timestamp=old_time,
            )
            with patch.object(main, "DB_PATH", database):
                with patch.object(main, "_record_observability_security_event_in_transaction", side_effect=sqlite3.OperationalError("audit unavailable")):
                    response = self.client.post("/api/admin/observability/retention", auth=self.basic_auth, json={"confirm_expired_data_redaction": True})
            self.assertEqual(response.status_code, 503, response.text)
            with closing(sqlite3.connect(database)) as db:
                event = db.execute("SELECT message,details_json FROM observability_events WHERE event_id='retention-audit-rollback-event'").fetchone()
                audit_count = db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_type='observability.retention_applied'").fetchone()[0]
            self.assertEqual(event[0], "keep this detail if audit storage fails")
            self.assertIn("preserve", event[1])
            self.assertEqual(audit_count, 0)

    def test_observability_routes_reuse_diagnostics_scope_and_cursor_filters(self):
        self.assertEqual(self.client.get("/api/admin/observability/overview").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/observability/log-filters").status_code, 401)
        page = self.client.get("/statistics", auth=self.basic_auth)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Statistics &amp; Observability", page.text)
        self.assertIn('id="obs-log-tail-toggle"', page.text)
        self.assertIn('id="obs-log-correlation-id"', page.text)
        self.assertIn('id="obs-log-job-id"', page.text)
        self.assertIn('id="obs-log-dataset-id"', page.text)
        self.assertIn('id="obs-log-model-id"', page.text)
        self.assertIn('id="obs-log-export-csv"', page.text)
        self.assertIn('id="obs-log-export-pdf"', page.text)
        self.assertIn('id="obs-log-export-ndjson"', page.text)
        self.assertIn('id="obs-saved-log-filters"', page.text)
        self.assertIn('id="obs-save-log-filter"', page.text)
        self.assertIn('id="obs-integration-cancel"', page.text)
        self.assertIn('id="obs-integration-edit-help"', page.text)

        catalogue = self.client.get("/api/admin/observability/metrics", auth=self.basic_auth)
        self.assertEqual(catalogue.status_code, 200, catalogue.text)
        self.assertTrue(any(item["metric_key"] == "training.dfl_loss" for item in catalogue.json()["items"]))
        backup_metric = next(item for item in catalogue.json()["items"] if item["metric_key"] == "storage.schema_migration_backups")
        self.assertEqual((backup_metric["metric_type"], backup_metric["unit"]), ("counter", "backups"))
        self.assertTrue(backup_metric["available"], "the isolated legacy API database should expose its successful upgrade backup")
        backup_events = self.client.get(
            "/api/admin/observability/events",
            params={"event_type": "storage.schema_migration_backup_verified"},
            auth=self.basic_auth,
        )
        self.assertEqual(backup_events.status_code, 200, backup_events.text)
        self.assertTrue(backup_events.json()["items"], "the migration backup audit event should be available through the protected API")
        after, before = self.recent_metric_window()
        backup_series = self.client.get(
            "/api/admin/observability/series",
            params={"metric": "storage.schema_migration_backups", "after": after, "before": before},
            auth=self.basic_auth,
        )
        self.assertEqual(backup_series.status_code, 200, backup_series.text)
        self.assertEqual(
            sum(float(item["value"]) for item in backup_series.json()["items"]),
            1.0,
            "the protected series API should expose the successful migration-backup count",
        )
        training_metric = next(item for item in catalogue.json()["items"] if item["metric_key"] == "training.box_loss")
        self.assertEqual(training_metric["series_aggregations"], ["average", "min", "max"])
        request_metric = next(item for item in catalogue.json()["items"] if item["metric_key"] == "api.requests")
        self.assertEqual(request_metric["series_aggregations"], ["sum"])
        overview = self.client.get("/api/v1/admin/observability/overview", auth=self.basic_auth)
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertTrue(overview.json()["kpis"]["application_starts"]["available"])
        self.assertFalse(overview.json()["kpis"]["energy_wh"]["available"])
        series = self.client.get("/api/admin/observability/series", params={"metric": "training.box_loss", "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z"}, auth=self.basic_auth)
        self.assertEqual(series.status_code, 200, series.text)
        self.assertEqual(series.json()["metric"]["metric_key"], "training.box_loss")
        for suffix, value, timestamp in (("a", 0.4, "2026-11-12T10:00:10Z"), ("b", 0.6, "2026-11-12T10:00:40Z")):
            event_id = f"api-series-{suffix}"
            observability_store.record_event(main.DB_PATH, event_id=event_id, event_type="training.epoch_completed", component="training", message="epoch", timestamp=timestamp)
            observability_store.record_metric(
                main.DB_PATH, metric_key="training.box_loss", value=value, source_event_id=event_id,
                dimensions={"training_job_id": "grouped-run", "dataset_id": "dataset-a", "model_id": "model-a", "epoch": 1},
                provenance="test", timestamp=timestamp,
            )
        grouped = self.client.get("/api/admin/observability/series", params={
            "metric": "training.box_loss", "after": "2026-11-12T10:00:00Z", "before": "2026-11-12T10:01:00Z",
            "aggregation": "average", "interval": "minute", "group_by": "training_job_id",
        }, auth=self.basic_auth)
        self.assertEqual(grouped.status_code, 200, grouped.text)
        self.assertAlmostEqual(grouped.json()["items"][0]["value"], 0.5)
        self.assertEqual(grouped.json()["items"][0]["dimensions"], {"training_job_id": "grouped-run"})
        self.assertEqual(grouped.json()["group_count"], 1)
        grouped_v1 = self.client.get("/api/v1/admin/observability/series", params={
            "metric": "training.box_loss", "after": "2026-11-12T10:00:00Z", "before": "2026-11-12T10:01:00Z",
            "aggregation": "average", "interval": "minute", "group_by": "training_job_id",
        }, auth=self.basic_auth)
        self.assertEqual(grouped_v1.status_code, 200, grouped_v1.text)
        self.assertEqual(grouped_v1.json()["items"][0]["value"], 0.5)
        invalid_group = self.client.get("/api/admin/observability/series", params={
            "metric": "training.box_loss", "after": "2026-11-12T10:00:00Z", "before": "2026-11-12T10:01:00Z",
            "aggregation": "average", "interval": "minute", "group_by": "route",
        }, auth=self.basic_auth)
        self.assertEqual(invalid_group.status_code, 400)
        invalid_aggregation = self.client.get("/api/admin/observability/series", params={
            "metric": "api.requests", "after": "2026-11-12T10:00:00Z", "before": "2026-11-12T10:01:00Z",
            "aggregation": "average", "interval": "minute",
        }, auth=self.basic_auth)
        self.assertEqual(invalid_aggregation.status_code, 400)
        self.save_integrations(api_enabled=True)
        diagnostics = self.issue_token(groups=["diagnostics"])
        permitted = self.secure_client.get("/api/v1/admin/observability/metrics", headers=self.bearer(diagnostics["token"]))
        self.assertEqual(permitted.status_code, 200, permitted.text)
        token_filters = self.secure_client.get("/api/v1/admin/observability/log-filters", headers=self.bearer(diagnostics["token"]))
        self.assertEqual(token_filters.status_code, 200, token_filters.text)
        self.assertEqual(token_filters.json()["items"], [], "saved filters must not leak between the Basic-auth account and an API principal")

        session_ids = {}
        for suffix, process_id in (("one", 987654321), ("two", 987654322)):
            session_ids[suffix] = observability_store.start_session(
                main.DB_PATH, component=f"fixture-{suffix}", app_version=main.APP_VERSION,
                now=f"2026-11-12T00:00:0{1 if suffix == 'one' else 2}Z", process_id=process_id,
            )[0]
            observability_store.record_event(
                main.DB_PATH, event_id=f"fixture-{suffix}", event_type="observability.fixture",
                component="test", message=f"fixture {suffix}", timestamp=f"2026-11-12T00:00:0{1 if suffix == 'one' else 2}Z",
                session_id=session_ids[suffix], correlation_id=f"request-{suffix}",
                details={"run_id": "train-run-42", "source": "idd-lite", "initial_model": "best.pt"} if suffix == "one" else {},
            )
        correlated = self.client.get("/api/admin/observability/events", params={"event_type": "observability.fixture", "correlation_id": "request-one"}, auth=self.basic_auth)
        self.assertEqual(correlated.status_code, 200, correlated.text)
        self.assertEqual([item["event_id"] for item in correlated.json()["items"]], ["fixture-one"])
        self.assertEqual(correlated.json()["items"][0]["correlation_id"], "request-one")
        session_filtered = self.client.get("/api/admin/observability/events", params={"event_type": "observability.fixture", "session_id": session_ids["two"]}, auth=self.basic_auth)
        self.assertEqual([item["event_id"] for item in session_filtered.json()["items"]], ["fixture-two"])
        by_job = self.client.get("/api/admin/observability/events", params={"event_type": "observability.fixture", "training_job_id": "train-run-42"}, auth=self.basic_auth)
        self.assertEqual([item["event_id"] for item in by_job.json()["items"]], ["fixture-one"])
        by_dataset_model = self.client.get("/api/admin/observability/events", params={"event_type": "observability.fixture", "dataset_id": "idd-lite", "model_id": "best.pt"}, auth=self.basic_auth)
        self.assertEqual([item["event_id"] for item in by_dataset_model.json()["items"]], ["fixture-one"])
        saved = self.client.post("/api/admin/observability/log-filters", auth=self.basic_auth, json={"name": "Training errors", "filters": {"period": "7d", "component": "training", "level": "ERROR", "session_id": session_ids["two"], "training_job_id": "train-run-42", "dataset_id": "idd-lite", "model_id": "best.pt"}})
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["item"]["filters"]["session_id"], session_ids["two"])
        self.assertEqual(saved.json()["item"]["filters"]["training_job_id"], "train-run-42")
        self.assertEqual(saved.json()["item"]["filters"]["dataset_id"], "idd-lite")
        self.assertEqual(saved.json()["item"]["filters"]["model_id"], "best.pt")
        saved_id = saved.json()["item"]["filter_id"]
        self.assertEqual(self.client.get("/api/admin/observability/log-filters", auth=self.basic_auth).json()["items"][0]["filter_id"], saved_id)
        duplicate = self.client.post("/api/admin/observability/log-filters", auth=self.basic_auth, json={"name": "TRAINING ERRORS", "filters": {"period": "24h"}})
        self.assertEqual(duplicate.status_code, 409)
        invalid_saved = self.client.post("/api/admin/observability/log-filters", auth=self.basic_auth, json={"name": "Bad filter", "filters": {"regex": "(.*)+"}})
        self.assertEqual(invalid_saved.status_code, 400)
        invalid_saved_time = self.client.post("/api/admin/observability/log-filters", auth=self.basic_auth, json={"name": "Bad time", "filters": {"period": "custom", "after": "x" * 65, "before": "2026-11-12T00:00:00Z"}})
        self.assertEqual(invalid_saved_time.status_code, 400)
        control_diagnostics = self.issue_token(groups=["diagnostics"], access="control")
        token_saved = self.secure_client.post("/api/v1/admin/observability/log-filters", headers=self.bearer(control_diagnostics["token"]), json={"name": "Token-only", "filters": {"period": "24h"}})
        self.assertEqual(token_saved.status_code, 201, token_saved.text)
        token_filter_id = token_saved.json()["item"]["filter_id"]
        self.assertEqual(self.client.delete(f"/api/admin/observability/log-filters/{token_filter_id}", auth=self.basic_auth).status_code, 404)
        self.assertEqual(self.secure_client.delete(f"/api/v1/admin/observability/log-filters/{token_filter_id}", headers=self.bearer(control_diagnostics["token"])).status_code, 200)
        direct = self.client.get("/api/admin/observability/events/fixture-one", auth=self.basic_auth)
        self.assertEqual(direct.status_code, 200, direct.text)
        self.assertEqual(direct.json()["event_id"], "fixture-one")
        direct_v1 = self.client.get("/api/v1/admin/observability/events/fixture-one", auth=self.basic_auth)
        self.assertEqual(direct_v1.status_code, 200, direct_v1.text)
        self.assertEqual(direct_v1.json()["event_id"], "fixture-one")
        self.assertEqual(self.client.get("/api/admin/observability/events/unknown-fixture", auth=self.basic_auth).status_code, 404)
        self.assertEqual(self.client.get("/api/admin/observability/events/fixture-one").status_code, 401)
        first = self.client.get("/api/admin/observability/events?event_type=observability.fixture&limit=1", auth=self.basic_auth)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["has_more"])
        second = self.client.get("/api/admin/observability/events", params={"event_type": "observability.fixture", "limit": 1, "cursor": first.json()["next_cursor"]}, auth=self.basic_auth)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotEqual(first.json()["items"][0]["event_id"], second.json()["items"][0]["event_id"])
        invalid = self.client.get("/api/admin/observability/events?level=TRACE", auth=self.basic_auth)
        self.assertEqual(invalid.status_code, 400)
        invalid_correlation = self.client.get("/api/admin/observability/events", params={"correlation_id": "x" * 121}, auth=self.basic_auth)
        self.assertEqual(invalid_correlation.status_code, 400)
        self.assertIn("120 characters", invalid_correlation.json()["detail"])
        invalid_session = self.client.get("/api/admin/observability/events", params={"session_id": "x" * 121}, auth=self.basic_auth)
        self.assertEqual(invalid_session.status_code, 400)
        self.assertIn("Session ID", invalid_session.json()["detail"])
        invalid_job = self.client.get("/api/admin/observability/events", params={"training_job_id": "x" * 121}, auth=self.basic_auth)
        self.assertEqual(invalid_job.status_code, 400)
        self.assertIn("Training job ID", invalid_job.json()["detail"])
        invalid_dataset = self.client.get("/api/admin/observability/events", params={"dataset_id": "x" * 121}, auth=self.basic_auth)
        self.assertEqual(invalid_dataset.status_code, 400)
        invalid_model = self.client.get("/api/admin/observability/events", params={"model_id": "x" * 121}, auth=self.basic_auth)
        self.assertEqual(invalid_model.status_code, 400)
        invalid_saved_id = self.client.post("/api/admin/observability/log-filters", auth=self.basic_auth, json={"name": "Long dataset ID", "filters": {"period": "7d", "dataset_id": "x" * 121}})
        self.assertEqual(invalid_saved_id.status_code, 400)

    def test_report_jobs_are_scoped_audited_and_export_files_are_downloadable(self):
        self.assertEqual(self.client.get("/api/admin/observability/reports").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/observability/reports/types", auth=self.basic_auth).status_code, 200)
        self.save_integrations(api_enabled=True, api_allow_sensitive_reads=True)
        observability_store.record_event(main.DB_PATH, event_id="report-api-fixture", event_type="observability.fixture", component="test", message="report fixture", timestamp="2026-11-12T12:00:00Z")
        readonly = self.issue_token(groups=["diagnostics"], sensitive_read=False)
        denied = self.secure_client.get("/api/v1/admin/observability/reports", headers=self.bearer(readonly["token"]))
        self.assertEqual(denied.status_code, 403)
        token = self.issue_token(groups=["diagnostics"], sensitive_read=True, access="control")
        payload = {"report_type": "events", "format": "csv", "filters": {"after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z", "event_type": "observability.fixture"}}
        created = self.secure_client.post("/api/v1/admin/observability/reports/jobs", headers=self.bearer(token["token"]), json=payload)
        self.assertEqual(created.status_code, 202, created.text)
        self.assertNotIn("requested_by", created.json())
        job_id = created.json()["job_id"]
        import time
        deadline = time.monotonic() + 5
        job = created.json()
        while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.03)
            response = self.secure_client.get(f"/api/v1/admin/observability/reports/jobs/{job_id}", headers=self.bearer(token["token"]))
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
        self.assertEqual(job["status"], "complete")
        download = self.secure_client.get(f"/api/v1/admin/observability/reports/jobs/{job_id}/download", headers=self.bearer(token["token"]))
        self.assertEqual(download.status_code, 200, download.text)
        self.assertIn("observability.fixture", download.content.decode("utf-8-sig"))
        with main.db_connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_export_audit WHERE report_job_id=?", (job_id,)).fetchone()[0], 1)

    def test_report_create_cancel_and_download_roll_back_when_security_audit_fails(self):
        import observability_reports

        self.save_integrations(api_enabled=True, api_allow_sensitive_reads=True)
        create_payload = {"report_type": "lifetime-statistics", "format": "csv", "filters": {}}
        with main.db_connection() as db:
            report_count_before = db.execute("SELECT COUNT(*) FROM observability_report_jobs").fetchone()[0]
        with patch.object(observability_reports, "submit_report_job") as submit, \
             patch.object(main, "_record_observability_security_event_in_transaction", side_effect=sqlite3.OperationalError("audit unavailable")):
            failed_create = self.client.post("/api/admin/observability/reports/jobs", auth=self.basic_auth, json=create_payload)
        self.assertEqual(failed_create.status_code, 503, failed_create.text)
        submit.assert_not_called()
        with main.db_connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_report_jobs").fetchone()[0], report_count_before)

        queued = observability_reports.create_report_job(
            main.DB_PATH, report_type="lifetime-statistics", output_format="csv",
            requested_by="admin", parameters={},
        )
        with patch.object(main, "_record_observability_security_event_in_transaction", side_effect=sqlite3.OperationalError("audit unavailable")):
            failed_cancel = self.client.post(f"/api/admin/observability/reports/jobs/{queued['job_id']}/cancel", auth=self.basic_auth)
        self.assertEqual(failed_cancel.status_code, 503, failed_cancel.text)
        self.assertEqual(observability_reports.get_report_job(main.DB_PATH, queued["job_id"])["status"], "queued")

        completed = observability_reports.process_report_job(main.DB_PATH, main.OBSERVABILITY_REPORT_DIR, queued["job_id"])
        self.assertEqual(completed["status"], "complete")
        with patch.object(main, "_record_observability_security_event_in_transaction", side_effect=sqlite3.OperationalError("audit unavailable")):
            failed_download = self.client.get(f"/api/admin/observability/reports/jobs/{queued['job_id']}/download", auth=self.basic_auth)
        self.assertEqual(failed_download.status_code, 503, failed_download.text)
        with main.db_connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_export_audit WHERE report_job_id=?", (queued["job_id"],)).fetchone()[0], 0)

    def test_report_queue_failure_says_request_was_saved(self):
        import observability_reports

        self.save_integrations(api_enabled=True, api_allow_sensitive_reads=True)
        with patch.object(observability_reports, "submit_report_job", side_effect=RuntimeError("worker stopping")):
            response = self.client.post(
                "/api/admin/observability/reports/jobs", auth=self.basic_auth,
                json={"report_type": "lifetime-statistics", "format": "csv", "filters": {}},
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("request was saved", response.json()["detail"])
        with main.db_connection() as db:
            audit = db.execute(
                "SELECT details_json FROM observability_security_audit WHERE event_type='observability.report_requested' ORDER BY sequence_id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(audit)
            job_id = json.loads(audit[0])["job_id"]
            job = db.execute("SELECT status FROM observability_report_jobs WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(job[0], "queued")

    def test_report_api_generates_and_downloads_every_registered_type_and_format(self):
        type_response = self.client.get("/api/admin/observability/reports/types", auth=self.basic_auth)
        self.assertEqual(type_response.status_code, 200, type_response.text)
        definitions = type_response.json()["items"]
        self.assertEqual(len(definitions), 14)
        self.save_integrations(api_enabled=True, api_allow_sensitive_reads=True)
        token = self.issue_token(groups=["diagnostics"], sensitive_read=True, access="control")
        headers = self.bearer(token["token"])
        after, before = self.recent_metric_window()
        camera_token = "qa-private-camera-token-api"
        camera_created = main.utc_now()
        camera_last_frame = main.utc_now()
        with main.db_connection() as db:
            db.execute(
                "INSERT INTO cameras(token,name,created_at,active,source_type) VALUES(?,?,?,?,?)",
                (camera_token, "QA Populated Entry Camera", camera_created, 1, "rtsp"),
            )
            db.execute(
                "INSERT INTO camera_status(camera_token,last_frame_at,faces_detected,matched_count,objects_json,error) VALUES(?,?,?,?,?,?)",
                (camera_token, camera_last_frame, 3, 2, "[]", None),
            )

        for definition in definitions:
            report_type = definition["report_type"]
            filters = {} if report_type == "lifetime-statistics" else {"after": after, "before": before}
            if report_type == "metric-series":
                filters["metric"] = "training.box_loss"
            if report_type == "cameras":
                filters["search"] = "populated entry"
            for output_format in definition["formats"]:
                with self.subTest(report_type=report_type, output_format=output_format):
                    created = self.secure_client.post(
                        "/api/v1/admin/observability/reports/jobs", headers=headers,
                        json={"report_type": report_type, "format": output_format, "filters": filters},
                    )
                    self.assertEqual(created.status_code, 202, created.text)
                    job_id = created.json()["job_id"]
                    self.assertNotIn("requested_by", created.json())
                    import time
                    deadline = time.monotonic() + 10
                    job = created.json()
                    while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
                        time.sleep(0.02)
                        status = self.secure_client.get(
                            f"/api/v1/admin/observability/reports/jobs/{job_id}", headers=headers,
                        )
                        self.assertEqual(status.status_code, 200, status.text)
                        job = status.json()
                    self.assertEqual(job["status"], "complete", job)
                    self.assertEqual(job["progress_percent"], 100)
                    downloaded = self.secure_client.get(
                        f"/api/v1/admin/observability/reports/jobs/{job_id}/download", headers=headers,
                    )
                    self.assertEqual(downloaded.status_code, 200, downloaded.text)
                    if output_format == "csv":
                        self.assertTrue(downloaded.content.startswith(b"\xef\xbb\xbf"))
                        self.assertIn("text/csv", downloaded.headers.get("content-type", ""))
                        if report_type == "cameras":
                            camera_csv = downloaded.content.decode("utf-8-sig")
                            camera_rows = list(csv.DictReader(camera_csv.splitlines()))
                            self.assertEqual(len(camera_rows), 1)
                            self.assertEqual(
                                json.loads(camera_rows[0]["dimensions_json"]),
                                {"name": "QA Populated Entry Camera", "source_type": "rtsp"},
                            )
                            camera_details = json.loads(camera_rows[0]["details_json"])
                            self.assertEqual(camera_details["last_frame_at"], camera_last_frame)
                            self.assertEqual(camera_details["faces_detected"], 3)
                            self.assertEqual(camera_details["matched_count"], 2)
                            self.assertNotIn(camera_token, camera_csv)
                    else:
                        self.assertTrue(downloaded.content.startswith(b"%PDF-1.4"))
                        self.assertIn("application/pdf", downloaded.headers.get("content-type", ""))
                    with main.db_connection() as db:
                        downloads = db.execute(
                            "SELECT COUNT(*) FROM observability_export_audit WHERE report_job_id=?", (job_id,),
                        ).fetchone()[0]
                    self.assertEqual(downloads, 1)

        with main.db_connection() as db:
            db.execute("DELETE FROM camera_status WHERE camera_token=?", (camera_token,))
            db.execute("DELETE FROM cameras WHERE token=?", (camera_token,))

    def test_security_audit_cursor_query_is_protected_and_stable(self):
        self.assertEqual(self.client.get("/api/admin/observability/security-audit/events").status_code, 401)
        for index in range(2):
            observability_store.record_security_event(main.DB_PATH, event_id=f"audit-fixture-{index}", event_type="test.security_event", outcome="denied", component="test", timestamp=f"2026-11-12T01:00:0{index}Z")
        first = self.client.get("/api/admin/observability/security-audit/events", params={"event_type": "test.security_event", "limit": 1}, auth=self.basic_auth)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["has_more"])
        second = self.client.get("/api/admin/observability/security-audit/events", params={"limit": 1, "cursor": first.json()["next_cursor"]}, auth=self.basic_auth)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotEqual(first.json()["items"][0]["event_id"], second.json()["items"][0]["event_id"])

    def test_security_audit_exact_actor_filter_and_length_limit(self):
        for event_id, actor_ref in (("actor-api-a", "api-token:abc"), ("actor-api-b", "api-token:abcd")):
            observability_store.record_security_event(
                main.DB_PATH, event_id=event_id, event_type="authz.test", outcome="denied",
                component="authz", actor_ref=actor_ref, timestamp="2026-11-12T01:00:00Z",
            )
        response = self.client.get(
            "/api/admin/observability/security-audit/events", params={"actor_ref": "api-token:abc"}, auth=self.basic_auth,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["event_id"] for item in response.json()["items"]], ["actor-api-a"])
        too_long = self.client.get(
            "/api/admin/observability/security-audit/events", params={"actor_ref": "a" * 121}, auth=self.basic_auth,
        )
        self.assertEqual(too_long.status_code, 400)

    def test_siem_routes_are_opt_in_scoped_and_mcp_sensitive_actions_need_approval(self):
        self.assertEqual(self.client.get("/api/admin/observability/integrations").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/observability/integrations", auth=self.basic_auth).status_code, 200)
        response = self.client.post("/api/admin/observability/integrations", auth=self.basic_auth, json={"display_name": "Fixture", "endpoint": "https://siem.example.com/hook", "secret_ref": "VAOS_SIEM_FIXTURE_KEY"})
        self.assertEqual(response.status_code, 201, response.text)
        destination_id = response.json()["destination_id"]
        self.assertFalse(response.json()["enabled"])
        self.assertNotIn("secret_ref", response.json())
        updated = self.client.put(
            f"/api/admin/observability/integrations/{destination_id}", auth=self.basic_auth,
            json={"display_name": "Fixture edited", "transport": "https_json", "event_format": "json", "endpoint": "https://siem.example.com/edited-hook", "enabled": False},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["display_name"], "Fixture edited")
        self.assertNotIn("secret_ref", updated.json())
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            retained_secret_ref = db.execute("SELECT secret_ref FROM observability_siem_destinations WHERE destination_id=?", (destination_id,)).fetchone()[0]
        self.assertEqual(retained_secret_ref, "VAOS_SIEM_FIXTURE_KEY")
        syslog = self.client.post("/api/admin/observability/integrations", auth=self.basic_auth, json={"display_name": "Syslog fixture", "transport": "syslog_tls", "endpoint": "syslog-tls://logs.example.com", "event_format": "cef"})
        self.assertEqual(syslog.status_code, 201, syslog.text)
        self.assertEqual(syslog.json()["event_format"], "cef")
        otlp = self.client.post("/api/admin/observability/integrations", auth=self.basic_auth, json={"display_name": "OTLP fixture", "transport": "otlp", "endpoint": "https://collector.example.com/v1/logs"})
        self.assertEqual(otlp.status_code, 201, otlp.text)
        self.assertEqual(otlp.json()["event_format"], "json")
        invalid_otlp = self.client.post("/api/admin/observability/integrations", auth=self.basic_auth, json={"display_name": "Invalid OTLP", "transport": "otlp", "endpoint": "https://collector.example.com/v1/logs", "event_format": "cef"})
        self.assertEqual(invalid_otlp.status_code, 400)
        enabled = self.client.post(f"/api/admin/observability/integrations/{destination_id}/enabled", auth=self.basic_auth, json={"enabled": True})
        self.assertEqual(enabled.status_code, 400)
        self.save_integrations(api_enabled=True, api_allow_sensitive_reads=True, mcp_enabled=True, mcp_read_only=False, mcp_allow_sensitive=True, mcp_allow_deletes=True)
        token = self.issue_token(groups=["diagnostics"], sensitive_read=False)
        denied = self.secure_client.get("/api/v1/admin/observability/integrations", headers=self.bearer(token["token"]))
        self.assertEqual(denied.status_code, 403)
        agent = self.issue_token(groups=["settings"], sensitive_read=True, mcp_agent=True, access="control")
        approval = self.secure_client.post("/api/v1/admin/observability/integrations", headers=self.bearer(agent["token"]), json={"display_name": "Agent fixture", "endpoint": "https://siem.example.net/hook", "secret_ref": "VAOS_SIEM_FIXTURE_KEY"})
        self.assertEqual(approval.status_code, 428, approval.text)
        self.assertIn("approval", approval.text.casefold())
        delete_approval = self.secure_client.delete(f"/api/v1/admin/observability/integrations/{destination_id}", headers=self.bearer(agent["token"]))
        self.assertEqual(delete_approval.status_code, 428, delete_approval.text)
        removed = self.client.delete(f"/api/admin/observability/integrations/{destination_id}", auth=self.basic_auth)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json()["endpoint_host"], "siem.example.com")
        self.assertTrue(removed.json()["archived_at"])
        self.assertFalse(any(item["destination_id"] == destination_id for item in self.client.get("/api/admin/observability/integrations", auth=self.basic_auth).json()["items"]))

    def test_siem_destination_changes_roll_back_when_audit_storage_fails(self):
        payload = {
            "display_name": "Atomic audit fixture",
            "endpoint": "https://atomic-one.example.com/hook",
            "secret_ref": "VAOS_SIEM_ATOMIC_TEST_KEY",
        }
        with patch.object(main, "_record_observability_security_event_in_transaction", side_effect=sqlite3.OperationalError("audit unavailable")):
            failed_create = self.client.post("/api/admin/observability/integrations", auth=self.basic_auth, json=payload)
        self.assertEqual(failed_create.status_code, 503, failed_create.text)
        self.assertFalse(any(item["display_name"] == payload["display_name"] for item in self.client.get("/api/admin/observability/integrations", auth=self.basic_auth).json()["items"]))

        created = self.client.post("/api/admin/observability/integrations", auth=self.basic_auth, json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        destination_id = created.json()["destination_id"]
        update_payload = {**payload, "endpoint": "https://atomic-two.example.com/hook"}
        with patch.object(main, "_record_observability_security_event_in_transaction", side_effect=sqlite3.OperationalError("audit unavailable")):
            failed_update = self.client.put(f"/api/admin/observability/integrations/{destination_id}", auth=self.basic_auth, json=update_payload)
        self.assertEqual(failed_update.status_code, 503, failed_update.text)

        with patch.dict(os.environ, {"VAOS_SIEM_ATOMIC_TEST_KEY": "x" * 32}):
            with patch.object(main, "_record_observability_security_event_in_transaction", side_effect=sqlite3.OperationalError("audit unavailable")):
                failed_enable = self.client.post(f"/api/admin/observability/integrations/{destination_id}/enabled", auth=self.basic_auth, json={"enabled": True})
        self.assertEqual(failed_enable.status_code, 503, failed_enable.text)

        with patch.object(main, "_record_observability_security_event_in_transaction", side_effect=sqlite3.OperationalError("audit unavailable")):
            failed_delete = self.client.delete(f"/api/admin/observability/integrations/{destination_id}", auth=self.basic_auth)
        self.assertEqual(failed_delete.status_code, 503, failed_delete.text)

        listed = self.client.get("/api/admin/observability/integrations", auth=self.basic_auth)
        self.assertEqual(listed.status_code, 200, listed.text)
        saved = next(item for item in listed.json()["items"] if item["destination_id"] == destination_id)
        self.assertEqual(saved["endpoint_host"], "atomic-one.example.com")
        self.assertFalse(saved["enabled"])

    def test_siem_destination_audit_oserror_maps_to_503_and_rolls_back(self):
        payload = {
            "display_name": "Audit I/O failure fixture",
            "transport": "syslog_tls",
            "endpoint": "syslog-tls://audit-io.example.com",
        }
        with patch.object(
            main.observability_siem,
            "record_security_event_and_enqueue_in_transaction",
            side_effect=OSError("fixture storage path unavailable"),
        ):
            response = self.client.post("/api/admin/observability/integrations", auth=self.basic_auth, json=payload)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("fixture storage path", response.text)
        self.assertFalse(any(
            item["display_name"] == payload["display_name"]
            for item in self.client.get("/api/admin/observability/integrations", auth=self.basic_auth).json()["items"]
        ))

    def test_siem_test_does_not_send_if_attempt_audit_cannot_be_recorded(self):
        created = self.client.post(
            "/api/admin/observability/integrations", auth=self.basic_auth,
            json={"display_name": "Audit failure fixture", "transport": "syslog_tls", "endpoint": "syslog-tls://audit-test.example.com"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        destination_id = created.json()["destination_id"]
        with patch.object(main, "_record_observability_security_event_in_transaction", side_effect=sqlite3.OperationalError("audit unavailable before network test")), \
             patch.object(main.observability_siem, "send_test_event") as send:
            response = self.client.post(f"/api/admin/observability/integrations/{destination_id}/test", auth=self.basic_auth)
        self.assertEqual(response.status_code, 503, response.text)
        send.assert_not_called()

        with closing(sqlite3.connect(main.DB_PATH)) as db:
            recorded = db.execute(
                "SELECT COUNT(*) FROM observability_security_audit WHERE event_type='observability.integration_test_requested'"
            ).fetchone()[0]
        self.assertEqual(recorded, 0)

    def test_siem_test_records_intent_before_network_and_result_afterward(self):
        created = self.client.post(
            "/api/admin/observability/integrations", auth=self.basic_auth,
            json={"display_name": "Test event fixture", "transport": "syslog_tls", "endpoint": "syslog-tls://audit-test.example.com"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        destination_id = created.json()["destination_id"]
        original = main.observability_siem.record_security_event_and_enqueue_in_transaction

        def fail_result_event(db, **kwargs):
            if kwargs.get("event_type") == "observability.integration_tested":
                raise sqlite3.OperationalError("audit unavailable after network test")
            return original(db, **kwargs)

        with patch.object(main.observability_siem, "send_test_event", return_value={"ok": True, "http_status": 200, "error_code": None}) as send, \
             patch.object(main.observability_siem, "record_security_event_and_enqueue_in_transaction", side_effect=fail_result_event):
            incomplete = self.client.post(f"/api/admin/observability/integrations/{destination_id}/test", auth=self.basic_auth)
        self.assertEqual(incomplete.status_code, 503, incomplete.text)
        send.assert_called_once()

        with closing(sqlite3.connect(main.DB_PATH)) as db:
            recorded = {row[0] for row in db.execute(
                "SELECT event_type FROM observability_security_audit WHERE event_type IN ('observability.integration_test_requested','observability.integration_tested')"
            ).fetchall()}
        self.assertIn("observability.integration_test_requested", recorded)
        self.assertNotIn("observability.integration_tested", recorded)

    def test_diagnostics_tokens_cannot_manage_siem_settings(self):
        self.save_integrations(api_enabled=True, api_allow_sensitive_reads=True)
        diagnostics = self.issue_token(groups=["diagnostics"], sensitive_read=True, access="control")
        headers = self.bearer(diagnostics["token"])
        listing = self.secure_client.get("/api/v1/admin/observability/integrations", headers=headers)
        self.assertEqual(listing.status_code, 403)
        payload = {"display_name": "Denied diagnostics fixture", "endpoint": "https://siem.example.com/hook", "secret_ref": "VAOS_SIEM_TEST_KEY"}
        created = self.secure_client.post("/api/v1/admin/observability/integrations", headers=headers, json=payload)
        self.assertEqual(created.status_code, 403)

        settings = self.issue_token(groups=["settings"], sensitive_read=True, access="control")
        allowed = self.secure_client.post(
            "/api/v1/admin/observability/integrations", headers=self.bearer(settings["token"]), json=payload,
        )
        self.assertEqual(allowed.status_code, 201, allowed.text)
        self.assertNotIn("secret_ref", allowed.json())

    def test_forwarded_ip_header_is_ignored_unless_direct_peer_is_trusted(self):
        request = main.Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [(b"cf-connecting-ip", b"203.0.113.9")], "client": ("198.51.100.10", 1234), "server": ("127.0.0.1", 8000), "scheme": "http"})
        with patch.dict(os.environ, {"VAOS_TRUSTED_PROXY_CIDRS": ""}):
            self.assertEqual(main._effective_client_ip(request), "198.51.100.10")
        with patch.dict(os.environ, {"VAOS_TRUSTED_PROXY_CIDRS": "198.51.100.0/24"}):
            self.assertEqual(main._effective_client_ip(request), "203.0.113.9")

    def test_api_request_telemetry_is_durable_before_background_flush(self):
        before = observability_store.overview(main.DB_PATH)["kpis"]["api_requests"]["value"] or 0
        main.queue_observability_api_request("/api/admin/observability/overview", "GET", 200)
        with observability_store._connection(main.DB_PATH) as db:
            buffered = db.execute("SELECT delta,dimensions_json FROM observability_counter_buffer WHERE metric_key='api.requests'").fetchall()
        self.assertTrue(any(json.loads(row["dimensions_json"]).get("route") == "/api/admin/observability/overview" and row["delta"] >= 1 for row in buffered))
        persisted = main.flush_observability_api_requests()
        self.assertGreaterEqual(persisted, 1)
        after = observability_store.overview(main.DB_PATH)["kpis"]["api_requests"]["value"]
        self.assertGreaterEqual(after, before + 1)

    def test_api_latency_is_measured_by_existing_versioned_api_middleware(self):
        with patch.object(main, "record_observability_api_latency") as capture:
            response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200, response.text)
        capture.assert_called_once()
        self.assertGreaterEqual(float(capture.call_args.args[0]), 0.0)

    def test_api_latency_quantiles_are_bounded_durable_and_requeued_on_failure(self):
        with tempfile.TemporaryDirectory(prefix="vaos-api-latency-") as temporary:
            database = Path(temporary) / "app.db"
            observability_store.initialize_database(database)
            session_id, _ = observability_store.start_session(database, component="admin", app_version="1.1.0")
            samples: deque[float] = deque(maxlen=4)
            with patch.object(main, "DB_PATH", database), \
                 patch.object(main, "OBSERVABILITY_API_LATENCY_SAMPLE_LIMIT", 4), \
                 patch.object(main, "observability_api_latency_samples", samples), \
                 patch.object(main, "observability_api_latency_dropped", 0):
                current = datetime.now(timezone.utc)
                range_start = (current - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
                range_end = (current + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
                for duration in (1.0, 2.0, 3.0, 4.0, 5.0):
                    main.record_observability_api_latency(duration)
                self.assertEqual(list(samples), [2.0, 3.0, 4.0, 5.0])
                self.assertEqual(main.observability_api_latency_dropped, 1)
                with patch.object(observability_store, "record_event", side_effect=sqlite3.OperationalError("simulated telemetry write failure")):
                    main._record_observability_api_latency_sample(session_id)
                self.assertEqual(list(samples), [2.0, 3.0, 4.0, 5.0])
                self.assertEqual(main.observability_api_latency_dropped, 1)

                after = main.utc_now()
                main._record_observability_api_latency_sample(session_id)
                p50 = observability_store.query_metric_series(
                    database, metric_key="api.request_latency_p50_ms", after=range_start,
                    before=range_end,
                )
                p95 = observability_store.query_metric_series(
                    database, metric_key="api.request_latency_p95_ms", after=range_start,
                    before=range_end,
                )
                self.assertAlmostEqual(p50[-1]["value"], 3.5)
                self.assertAlmostEqual(p95[-1]["value"], 4.85)
                events = observability_store.query_events(
                    database, after=range_start, before=range_end,
                    event_type="api.latency_sample", limit=10,
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["session_id"], session_id)
                self.assertEqual(events[0]["details"]["sample_count"], 4)
                self.assertEqual(events[0]["details"]["dropped_sample_count"], 1)
                self.assertGreaterEqual(events[0]["timestamp"], after)

    def test_successful_camera_frame_records_server_processing_duration_only(self):
        camera_token = f"latency-camera-{secrets.token_hex(8)}"
        with main.db_connection() as db:
            db.execute(
                "INSERT INTO cameras(token,name,created_at,active,source_type) VALUES(?,?,?,?,?)",
                (camera_token, "Latency fixture", main.utc_now(), 1, "camera"),
            )
        with patch.object(main, "process_frame", return_value={"objects": [], "faces": []}), \
             patch.object(main, "queue_observability_inference_frame"), \
             patch.object(main, "record_observability_inference_latency") as capture:
            response = self.client.post(
                f"/api/camera/{camera_token}/frame",
                content=b"fixture image bytes",
                headers={"content-type": "image/jpeg"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        capture.assert_called_once()
        self.assertGreaterEqual(float(capture.call_args.args[0]), 0.0)

        with patch.object(main, "process_frame", side_effect=RuntimeError("fixture failure")), \
             patch.object(main, "record_observability_inference_latency") as failed_capture:
            failed = self.client.post(
                f"/api/camera/{camera_token}/frame",
                content=b"fixture image bytes",
                headers={"content-type": "image/jpeg"},
            )
        self.assertEqual(failed.status_code, 503)
        failed_capture.assert_not_called()

    def test_camera_processing_latency_quantiles_are_bounded_durable_and_requeued(self):
        with tempfile.TemporaryDirectory(prefix="vaos-frame-latency-") as temporary:
            database = Path(temporary) / "app.db"
            observability_store.initialize_database(database)
            session_id, _ = observability_store.start_session(database, component="camera", app_version="1.1.0")
            samples: deque[float] = deque(maxlen=4)
            with patch.object(main, "DB_PATH", database), \
                 patch.object(main, "OBSERVABILITY_INFERENCE_LATENCY_SAMPLE_LIMIT", 4), \
                 patch.object(main, "observability_inference_latency_samples", samples), \
                 patch.object(main, "observability_inference_latency_dropped", 0):
                for duration in (1.0, 2.0, 3.0, 4.0, 5.0):
                    main.record_observability_inference_latency(duration)
                self.assertEqual(list(samples), [2.0, 3.0, 4.0, 5.0])
                self.assertEqual(main.observability_inference_latency_dropped, 1)
                with patch.object(observability_store, "record_event", side_effect=sqlite3.OperationalError("simulated telemetry write failure")):
                    main._record_observability_inference_latency_sample(session_id)
                self.assertEqual(list(samples), [2.0, 3.0, 4.0, 5.0])
                self.assertEqual(main.observability_inference_latency_dropped, 1)

                range_start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
                range_end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
                main._record_observability_inference_latency_sample(session_id)
                p50 = observability_store.query_metric_series(database, metric_key="inference.camera_frame_processing_latency_p50_ms", after=range_start, before=range_end)
                p95 = observability_store.query_metric_series(database, metric_key="inference.camera_frame_processing_latency_p95_ms", after=range_start, before=range_end)
                self.assertAlmostEqual(p50[-1]["value"], 3.5)
                self.assertAlmostEqual(p95[-1]["value"], 4.85)
                events = observability_store.query_events(database, after=range_start, before=range_end, event_type="inference.camera_frame_latency_sample", limit=10)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["session_id"], session_id)
                self.assertEqual(events[0]["details"]["sample_count"], 4)
                self.assertEqual(events[0]["details"]["dropped_sample_count"], 1)

    def test_training_run_persists_dataset_counts_and_terminal_duration_metrics(self):
        with tempfile.TemporaryDirectory(prefix="vaos-training-metrics-") as temporary:
            database = Path(temporary) / "app.db"
            observability_store.initialize_database(database)
            session_id, _ = observability_store.start_session(database, component="admin", app_version="1.1.0")
            with patch.object(main, "DB_PATH", database), patch.object(main, "observability_session_id", session_id):
                common = {"source": "dataset:indian-roads", "initial_model": "best.pt"}
                self.assertTrue(main._persist_observability_training_event({
                    "timestamp": "2026-11-12T10:00:00Z", "message": "Training run initialized",
                    "run_id": "run-complete", "images": 240, "classes": 5, **common,
                }))
                self.assertTrue(main._persist_observability_training_event({
                    "timestamp": "2026-11-12T10:05:00Z", "message": "Training finished",
                    "run_id": "run-complete", "status": "complete", "elapsed_seconds": 300,
                    "annotations": 1840, **common,
                }))
                self.assertTrue(main._persist_observability_training_event({
                    "timestamp": "2026-11-12T11:00:00Z", "message": "Training failed",
                    "run_id": "run-failed", "elapsed_seconds": 45, "error_type": "RuntimeError",
                    "error": "worker stopped", **common,
                }))

            bounds = {"after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z"}
            image_points = observability_store.query_metric_series(database, metric_key="training.dataset_images", dimensions={"training_job_id": "run-complete"}, **bounds)
            annotation_points = observability_store.query_metric_series(database, metric_key="training.dataset_annotations", dimensions={"training_job_id": "run-complete"}, **bounds)
            completed_duration = observability_store.query_metric_series(database, metric_key="training.job_duration_seconds", dimensions={"training_job_id": "run-complete"}, **bounds)
            failed_duration = observability_store.query_metric_series(database, metric_key="training.job_duration_seconds", dimensions={"training_job_id": "run-failed"}, **bounds)
            failed_annotations = observability_store.query_metric_series(database, metric_key="training.dataset_annotations", dimensions={"training_job_id": "run-failed"}, **bounds)
            self.assertEqual(image_points[-1]["value"], 240)
            self.assertEqual(annotation_points[-1]["value"], 1840)
            self.assertEqual(completed_duration[-1]["value"], 300)
            self.assertEqual(failed_duration[-1]["value"], 45)
            self.assertEqual(failed_annotations, [], "annotation totals should remain unavailable when the final label scan did not succeed")

    def test_local_dataset_validation_records_counts_without_source_paths(self):
        with tempfile.TemporaryDirectory(prefix="vaos-dataset-observability-") as temporary:
            root = Path(temporary)
            database = root / "app.db"
            observability_store.initialize_database(database)
            source_root = r"D:\Private\Road Dataset"
            report = {
                "data_yaml": source_root + r"\dataset.yaml", "source_root": source_root, "data_root": source_root,
                "classes": ["truck"], "train_count": 3, "val_count": 1, "image_count": 4,
                "labeled_images": 4, "missing_label_files": 0, "invalid_images": 0, "annotations": 6,
            }
            state = {}
            with patch.object(main, "DB_PATH", database), \
                 patch.object(main, "validate_local_yolo_dataset", return_value=report), \
                 patch.object(main, "_dataset_conflicts_with_training", return_value=False), \
                 patch.object(main, "load_public_dataset_imports", return_value={}), \
                 patch.object(main, "write_public_dataset_imports"), \
                 patch.object(main, "write_training_log"), \
                 patch.object(main, "ml_dataset_prepare_state", state):
                self.assertTrue(main.ml_dataset_prepare_lock.acquire(blocking=False))
                main._prepare_local_dataset_worker("Private road set", root / "source", "local_test")
                self.assertFalse(main.ml_dataset_prepare_lock.locked())

            events = observability_store.query_events(database, component="datasets", event_type="dataset.validation_succeeded", limit=5)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["details"], {
                "dataset_kind": "managed_local", "prepared_count": 4, "train_count": 3,
                "validation_count": 1, "class_count": 1, "box_count": 6,
            })
            self.assertNotIn("Private road", json.dumps(events[0]))
            self.assertNotIn("D:\\Private", json.dumps(events[0]))
            with closing(sqlite3.connect(database)) as db:
                counters = dict(db.execute("SELECT metric_key,value FROM observability_lifetime_counters"))
            self.assertEqual(counters["datasets.preparations_completed"], 1)
            self.assertEqual(counters["datasets.images_prepared"], 4)
            self.assertEqual(counters["datasets.annotations_validated"], 6)

    def test_local_dataset_validation_failure_does_not_export_raw_error_or_path(self):
        with tempfile.TemporaryDirectory(prefix="vaos-dataset-failure-observability-") as temporary:
            database = Path(temporary) / "app.db"
            observability_store.initialize_database(database)
            state = {}
            with patch.object(main, "DB_PATH", database), \
                 patch.object(main, "validate_local_yolo_dataset", side_effect=ValueError(r"Invalid file at D:\Private\Road Dataset\labels\secret.txt")), \
                 patch.object(main, "_dataset_conflicts_with_training", return_value=False), \
                 patch.object(main, "write_training_log"), \
                 patch.object(main, "ml_dataset_prepare_state", state):
                self.assertTrue(main.ml_dataset_prepare_lock.acquire(blocking=False))
                main._prepare_local_dataset_worker("Private road set", Path(r"D:\Private\Road Dataset"), "local_failed")
                self.assertFalse(main.ml_dataset_prepare_lock.locked())

            events = observability_store.query_events(database, component="datasets", event_type="dataset.validation_failed", limit=5)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["details"], {"dataset_kind": "managed_local"})
            self.assertNotIn("secret.txt", json.dumps(events[0]))
            self.assertNotIn("Private road", json.dumps(events[0]))
            with closing(sqlite3.connect(database)) as db:
                counters = dict(db.execute("SELECT metric_key,value FROM observability_lifetime_counters"))
            self.assertEqual(counters["datasets.preparations_failed"], 1)

    def test_api_counter_uses_durable_sidecar_during_sqlite_write_lock(self):
        route = "/api/v1/observability/lock-fixture"
        with closing(sqlite3.connect(main.DB_PATH, timeout=1)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            main.queue_observability_api_request(route, "GET", 200)
            with main.observability_api_lock:
                self.assertNotIn((route, "GET", 200), main.observability_api_aggregates)
            self.assertTrue(main.observability_counter_fallback_pending())
            blocker.commit()
        self.assertGreaterEqual(main.flush_observability_api_requests(max_events=5000), 1)
        with observability_store._connection(main.DB_PATH) as db:
            rows = db.execute("SELECT dimensions_json FROM observability_counter_events WHERE metric_key='api.requests'").fetchall()
        self.assertTrue(any(json.loads(row["dimensions_json"]).get("route") == route for row in rows))

    def test_api_and_frame_counters_recover_after_transient_storage_errors_without_duplicates(self):
        route = "/api/v1/observability/transient-storage-fixture"
        camera_token = "transient-storage-camera-secret"
        camera_ref = hashlib.sha256(camera_token.encode()).hexdigest()[:20]
        main.flush_observability_api_requests(max_events=5000)
        before_api = observability_store.overview(main.DB_PATH)["kpis"]["api_requests"]["value"] or 0
        before_frames = observability_store.overview(main.DB_PATH)["kpis"]["inference_frames"]["value"] or 0
        api_fallback: dict[tuple[str, str, int], int] = {}
        frame_fallback: dict[str, int] = {}
        original_buffer = observability_store.buffer_counter_delta
        calls = 0

        def fail_first_two_writes(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise sqlite3.OperationalError("simulated temporary database write failure")
            return original_buffer(*args, **kwargs)

        with patch.object(main, "observability_api_aggregates", api_fallback), \
             patch.object(main, "observability_frame_aggregates", frame_fallback), \
             patch.object(observability_store, "buffer_counter_delta", side_effect=fail_first_two_writes):
            main.queue_observability_api_request(route, "POST", 202)
            main.queue_observability_inference_frame(camera_token)
            self.assertEqual(api_fallback, {})
            self.assertEqual(frame_fallback, {})
            self.assertTrue(main.observability_counter_fallback_pending())
            self.assertGreaterEqual(main.flush_observability_api_requests(max_events=5000), 2)
            main.flush_observability_api_requests(max_events=5000)

        after = observability_store.overview(main.DB_PATH)["kpis"]
        self.assertEqual(after["api_requests"]["value"], before_api + 1)
        self.assertEqual(after["inference_frames"]["value"], before_frames + 1)
        with observability_store._connection(main.DB_PATH) as db:
            api_rows = db.execute(
                "SELECT dimensions_json,delta FROM observability_counter_events WHERE metric_key='api.requests'"
            ).fetchall()
            frame_rows = db.execute(
                "SELECT dimensions_json,delta FROM observability_counter_events WHERE metric_key='inference.frames_processed'"
            ).fetchall()
        self.assertEqual(sum(int(row["delta"]) for row in api_rows if json.loads(row["dimensions_json"]).get("route") == route), 1)
        self.assertEqual(sum(int(row["delta"]) for row in frame_rows if json.loads(row["dimensions_json"]).get("camera_ref") == camera_ref), 1)
        self.assertTrue(all(camera_token not in row["dimensions_json"] for row in frame_rows))

    def test_api_and_frame_memory_fallbacks_enforce_key_cap_and_preserve_totals(self):
        suffix = hashlib.sha256(os.urandom(16)).hexdigest()[:10]
        api_fallback: dict[tuple[str, str, int], int] = {}
        frame_fallback: dict[str, int] = {}

        def raise_sqlite_full(*_args, **_kwargs):
            error = sqlite3.OperationalError("database or disk is full")
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise error

        expected_camera_refs = {
            hashlib.sha256(f"bounded-camera-{suffix}-{index}".encode()).hexdigest()[:20]
            for index in range(12)
        }
        before = observability_store.overview(main.DB_PATH)["kpis"]
        with patch.object(main, "OBSERVABILITY_API_MAX_KEYS", 3), \
             patch.object(main, "observability_api_aggregates", api_fallback), \
             patch.object(main, "observability_frame_aggregates", frame_fallback):
            with patch.object(observability_store, "buffer_counter_fallback", side_effect=raise_sqlite_full), \
                 patch.object(observability_store, "buffer_counter_delta", side_effect=raise_sqlite_full):
                for index in range(12):
                    main.queue_observability_api_request(f"/api/v1/cardinality/{suffix}/{index}", "GET", 200)
                    main.queue_observability_inference_frame(f"bounded-camera-{suffix}-{index}")
            self.assertLessEqual(len(api_fallback), 3)
            self.assertLessEqual(len(frame_fallback), 3)
            self.assertEqual(sum(api_fallback.values()), 12)
            self.assertEqual(sum(frame_fallback.values()), 12)
            self.assertIn(("other", "OTHER", 0), api_fallback)
            self.assertIn("other", frame_fallback)
            self.assertGreaterEqual(main.flush_observability_api_requests(max_events=5000), 24)
        after = observability_store.overview(main.DB_PATH)["kpis"]
        self.assertEqual(after["api_requests"]["value"], (before["api_requests"]["value"] or 0) + 12)
        self.assertEqual(after["inference_frames"]["value"], (before["inference_frames"]["value"] or 0) + 12)
        with observability_store._connection(main.DB_PATH) as db:
            api_rows = db.execute(
                "SELECT dimensions_json,delta FROM observability_counter_events WHERE metric_key='api.requests'"
            ).fetchall()
            frame_rows = db.execute(
                "SELECT dimensions_json,delta FROM observability_counter_events WHERE metric_key='inference.frames_processed'"
            ).fetchall()
        self.assertTrue(any(
            json.loads(row["dimensions_json"]) == {"route": "other", "method": "OTHER", "status": 0}
            for row in api_rows
        ))
        self.assertEqual(sum(
            int(row["delta"]) for row in api_rows
            if json.loads(row["dimensions_json"]) == {"route": "other", "method": "OTHER", "status": 0}
        ), 10)
        self.assertEqual(sum(
            int(row["delta"]) for row in frame_rows
            if json.loads(row["dimensions_json"]).get("camera_ref") == "other"
        ), 10)
        persisted_camera_refs = {
            json.loads(row["dimensions_json"]).get("camera_ref")
            for row in frame_rows
            if json.loads(row["dimensions_json"]).get("camera_ref") in expected_camera_refs | {"other"}
        }
        self.assertTrue(persisted_camera_refs)

    def test_frame_counter_uses_one_way_camera_reference(self):
        token = "private-camera-capability-value"
        before = observability_store.overview(main.DB_PATH)["kpis"]["inference_frames"]["value"] or 0
        with main.db_connection() as db:
            db.execute("INSERT INTO cameras(token,name,created_at,active,source_type) VALUES(?,?,?,?,?)", (token, "Fixture camera", main.utc_now(), 1, "camera"))
        with patch("main.process_frame", return_value={"ok": True}):
            response = self.client.post(f"/api/camera/{token}/frame", content=b"fixture-jpeg", headers={"Content-Type": "image/jpeg"})
        self.assertEqual(response.status_code, 200, response.text)
        camera_ref = hashlib.sha256(token.encode()).hexdigest()[:20]
        with observability_store._connection(main.DB_PATH) as db:
            buffered = db.execute("SELECT dimensions_json FROM observability_counter_buffer WHERE metric_key='inference.frames_processed'").fetchall()
        self.assertTrue(any(json.loads(row["dimensions_json"]).get("camera_ref") == camera_ref for row in buffered))
        self.assertTrue(all(token not in row["dimensions_json"] for row in buffered))
        persisted = main.flush_observability_api_requests()
        self.assertGreaterEqual(persisted, 1)
        self.assertGreaterEqual(observability_store.overview(main.DB_PATH)["kpis"]["inference_frames"]["value"], before + 1)
        with main.observability_store._connection(main.DB_PATH) as db:
            dimensions_rows = db.execute("SELECT dimensions_json FROM observability_counter_events WHERE metric_key='inference.frames_processed'").fetchall()
            dimensions = next((row["dimensions_json"] for row in dimensions_rows if json.loads(row["dimensions_json"]).get("camera_ref") == camera_ref), None)
        self.assertIsNotNone(dimensions)
        self.assertNotIn(token, dimensions)
        self.assertIn('"camera_ref"', dimensions)
        self.assertIn(camera_ref, dimensions)
        after, before = self.recent_metric_window()
        series = self.client.get("/api/admin/observability/series", params={
            "metric": "inference.frames_processed", "after": after, "before": before,
            "dimensions": json.dumps({"camera_ref": camera_ref}),
        }, auth=self.basic_auth)
        self.assertEqual(series.status_code, 200, series.text)
        self.assertTrue(any(item["dimensions"].get("camera_ref") == camera_ref for item in series.json()["items"]))
        invalid = self.client.get("/api/admin/observability/series", params={
            "metric": "inference.frames_processed", "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z",
            "dimensions": json.dumps({"camera_id": camera_ref}),
        }, auth=self.basic_auth)
        self.assertEqual(invalid.status_code, 400)

    def test_video_cpu_selection_and_invalid_device(self):
        response = self.client.post('/api/admin/video-review/sessions', auth=self.basic_auth,
                                    json={'source_type':'local_video','file_name':'test.mp4'})
        url = '/api/admin/video-review/sessions/' + response.json()['session_id'] + '/frame'
        with patch('main.process_video_review_frame', return_value={'objects':[]}) as process:
            result = self.client.post(url, auth=self.basic_auth, content=b'frame',
                                     headers={'Content-Type':'image/jpeg','X-Analysis-Device':'cpu'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(process.call_args.args[-1], 'cpu')
            process.reset_mock()
            result = self.client.post(url, auth=self.basic_auth, content=b'frame',
                                     headers={'Content-Type':'image/jpeg','X-Analysis-Device':'invalid'})
            self.assertEqual(result.status_code, 400)
            process.assert_not_called()

    def test_local_video_review_frames_are_counted_without_video_identity(self):
        session = self.client.post(
            "/api/admin/video-review/sessions",
            auth=self.basic_auth,
            json={"source_type": "local_video", "file_name": "private-road-clip.mp4"},
        )
        self.assertEqual(session.status_code, 200, session.text)
        session_id = session.json()["session_id"]
        result = {"objects": [], "frame_size": [640, 480], "samples_saved": 0, "sample_limit_reached": False, "samples_enabled": False}
        with patch("main.process_video_review_frame", return_value=result), \
             patch("main.record_observability_video_review_latency") as latency_capture:
            response = self.client.post(
                f"/api/admin/video-review/sessions/{session_id}/frame",
                auth=self.basic_auth,
                content=b"fixture-jpeg",
                headers={"Content-Type": "image/jpeg", "X-Video-Time": "1.5", "X-Save-Samples": "0"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        latency_capture.assert_called_once()
        self.assertGreaterEqual(float(latency_capture.call_args.args[0]), 0.0)
        with patch("main.process_video_review_frame", side_effect=RuntimeError("fixture processing error")), \
             patch("main.record_observability_video_review_latency") as failed_latency_capture:
            failed = self.client.post(
                f"/api/admin/video-review/sessions/{session_id}/frame",
                auth=self.basic_auth,
                content=b"fixture-jpeg",
                headers={"Content-Type": "image/jpeg", "X-Video-Time": "1.5", "X-Save-Samples": "0"},
            )
        self.assertEqual(failed.status_code, 409)
        failed_latency_capture.assert_not_called()
        with observability_store._connection(main.DB_PATH) as db:
            rows = db.execute(
                "SELECT dimensions_json,delta FROM observability_counter_buffer WHERE metric_key='inference.frames_processed'"
            ).fetchall()
        self.assertTrue(any(json.loads(row["dimensions_json"]) == {"source_type": "local_video"} and row["delta"] == 1 for row in rows))
        self.assertTrue(all("private-road-clip.mp4" not in row["dimensions_json"] and session_id not in row["dimensions_json"] for row in rows))

        fallback: dict[str, int] = {}
        with patch.object(main, "observability_frame_aggregates", fallback), \
             patch.object(observability_store, "buffer_counter_delta", side_effect=sqlite3.OperationalError("temporary write failure")), \
             patch.object(observability_store, "buffer_counter_fallback", side_effect=sqlite3.OperationalError("temporary sidecar failure")):
            main.queue_observability_video_review_frame()
        self.assertEqual(fallback, {"source:local_video": 1})
        self.assertGreaterEqual(main.flush_observability_api_requests(max_events=5000), 1)
        with observability_store._connection(main.DB_PATH) as db:
            recovered = db.execute(
                "SELECT dimensions_json,delta FROM observability_counter_events WHERE metric_key='inference.frames_processed'"
            ).fetchall()
        self.assertTrue(any(json.loads(row["dimensions_json"]) == {"source_type": "local_video"} and row["delta"] == 1 for row in recovered))
        after, before = self.recent_metric_window()
        series = self.client.get("/api/admin/observability/series", params={
            "metric": "inference.frames_processed", "after": after, "before": before,
            "dimensions": json.dumps({"source_type": "local_video"}),
        }, auth=self.basic_auth)
        self.assertEqual(series.status_code, 200, series.text)
        self.assertTrue(any(item["dimensions"].get("source_type") == "local_video" for item in series.json()["items"]))

    def test_local_video_latency_quantiles_are_bounded_durable_and_requeued(self):
        with tempfile.TemporaryDirectory(prefix="vaos-local-video-latency-") as temporary:
            database = Path(temporary) / "app.db"
            observability_store.initialize_database(database)
            session_id, _ = observability_store.start_session(database, component="admin", app_version="1.1.0")
            samples: deque[float] = deque(maxlen=4)
            with patch.object(main, "DB_PATH", database), \
                 patch.object(main, "OBSERVABILITY_INFERENCE_LATENCY_SAMPLE_LIMIT", 4), \
                 patch.object(main, "observability_video_review_latency_samples", samples), \
                 patch.object(main, "observability_video_review_latency_dropped", 0):
                for duration in (1.0, 2.0, 3.0, 4.0, 5.0):
                    main.record_observability_video_review_latency(duration)
                self.assertEqual(list(samples), [2.0, 3.0, 4.0, 5.0])
                self.assertEqual(main.observability_video_review_latency_dropped, 1)
                with patch.object(observability_store, "record_event", side_effect=sqlite3.OperationalError("simulated telemetry write failure")):
                    main._record_observability_video_review_latency_sample(session_id)
                self.assertEqual(list(samples), [2.0, 3.0, 4.0, 5.0])
                self.assertEqual(main.observability_video_review_latency_dropped, 1)

                range_start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
                range_end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
                main._record_observability_video_review_latency_sample(session_id)
                p50 = observability_store.query_metric_series(database, metric_key="inference.local_video_frame_processing_latency_p50_ms", after=range_start, before=range_end)
                p95 = observability_store.query_metric_series(database, metric_key="inference.local_video_frame_processing_latency_p95_ms", after=range_start, before=range_end)
                self.assertAlmostEqual(p50[-1]["value"], 3.5)
                self.assertAlmostEqual(p95[-1]["value"], 4.85)
                self.assertEqual(p50[-1]["dimensions"], {})
                events = observability_store.query_events(database, after=range_start, before=range_end, event_type="inference.local_video_frame_latency_sample", limit=10)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["details"]["sample_count"], 4)
                self.assertEqual(events[0]["details"]["dropped_sample_count"], 1)

    def test_mcp_integration_destination_updates_are_sensitive(self):
        self.assertTrue(main._api_mcp_sensitive_action("PUT", "/api/admin/observability/integrations/destination-fixture"))
        self.assertTrue(main._api_mcp_sensitive_action("DELETE", "/api/admin/observability/integrations/destination-fixture"))

    def test_host_samples_reuse_existing_collector_and_keep_multiple_gpus(self):
        measured = {
            "cpu_percent": 37.5, "memory_used_bytes": 12_345_678,
            "disk_used_bytes": 8_765_432, "disk_used_percent": 61.25,
            "gpu_devices": [
                {"index": 0, "device_id": "a" * 32, "name": "Fixture GPU A", "utilization_percent": 55, "power_draw_w": 72.25},
                {"index": 1, "device_id": "b" * 32, "name": "Fixture GPU B", "utilization_percent": 12, "power_draw_w": 120.0},
            ],
        }
        session_id, _ = observability_store.start_session(
            main.DB_PATH, component="admin", app_version=main.APP_VERSION,
        )
        with patch("main.get_system_metrics", return_value=measured), patch("main.observability_installation_id", "test-installation"):
            main._record_observability_host_sample(session_id)
        after, before = self.recent_metric_window()
        cpu = observability_store.query_metric_series(main.DB_PATH, metric_key="hardware.cpu_utilization_percent", after=after, before=before)
        disk_used = observability_store.query_metric_series(main.DB_PATH, metric_key="hardware.disk_used_bytes", after=after, before=before)
        disk_percent = observability_store.query_metric_series(main.DB_PATH, metric_key="hardware.disk_used_percent", after=after, before=before)
        gpu = observability_store.query_metric_series(main.DB_PATH, metric_key="hardware.gpu_utilization_percent", after=after, before=before)
        self.assertEqual(cpu[-1]["value"], 37.5)
        self.assertEqual(disk_used[-1]["value"], 8_765_432)
        self.assertEqual(disk_percent[-1]["value"], 61.25)
        self.assertEqual(disk_percent[-1]["dimensions"]["volume_id"], "vaos-data")
        self.assertEqual(len(gpu), 2)
        self.assertEqual({item["dimensions"]["device_id"] for item in gpu}, {"a" * 32, "b" * 32})
        power = observability_store.query_metric_series(main.DB_PATH, metric_key="hardware.gpu_power_draw_w", after=after, before=before)
        self.assertEqual({item["value"] for item in power}, {72.25, 120.0})
        self.assertFalse(any("GPU-" in json.dumps(item) for item in power))
        events = observability_store.query_events(
            main.DB_PATH, limit=20, event_type="hardware.metric_sample", session_id=session_id,
        )
        self.assertEqual(len(events), 6)
        self.assertTrue(all(item["session_id"] == session_id for item in events))

    def test_host_sample_event_and_metric_roll_back_together_on_metric_storage_failure(self):
        with tempfile.TemporaryDirectory(prefix="vaos-host-sample-atomic-") as temporary:
            database = Path(temporary) / "app.db"
            observability_store.initialize_database(database)
            timestamp = "2026-11-13T12:00:00.123456Z"
            measured = {
                "cpu_percent": 42.0, "memory_used_bytes": None,
                "disk_used_bytes": None, "disk_used_percent": None,
                "gpu_devices": [],
            }
            session_id, _ = observability_store.start_session(
                database, component="admin", app_version=main.APP_VERSION,
            )
            event_id = hashlib.sha256(
                f"{session_id}|{timestamp}|hardware.cpu_utilization_percent|hardware.cpu_utilization_percent".encode()
            ).hexdigest()
            with patch.object(main, "DB_PATH", database), \
                 patch.object(main, "utc_now", return_value=timestamp), \
                 patch.object(main, "get_system_metrics", return_value=measured), \
                 patch.object(main, "observability_installation_id", "host-sample-installation"), \
                 patch.object(observability_store, "_insert_metric_observation", side_effect=sqlite3.OperationalError("fixture metric insert failure")), \
                 self.assertLogs(main.logger, level="WARNING") as captured:
                    main._record_observability_host_sample(session_id)

            self.assertIn("hardware.cpu_utilization_percent", " ".join(captured.output))

            with closing(sqlite3.connect(database)) as db:
                event_count = db.execute(
                    "SELECT COUNT(*) FROM observability_events WHERE event_id=?", (event_id,),
                ).fetchone()[0]
                metric_count = db.execute(
                    "SELECT COUNT(*) FROM observability_metric_observations WHERE source_event_id=?", (event_id,),
                ).fetchone()[0]
            self.assertEqual(event_count, 0, "a failed metric insert must not leave an event claiming the sample was recorded")
            self.assertEqual(metric_count, 0)

    def test_host_sample_persistence_failure_does_not_skip_later_metrics_or_gpu_power(self):
        with tempfile.TemporaryDirectory(prefix="vaos-host-sample-continue-") as temporary:
            database = Path(temporary) / "app.db"
            observability_store.initialize_database(database)
            session_id, _ = observability_store.start_session(
                database, component="admin", app_version=main.APP_VERSION,
            )
            timestamp = "2026-11-13T12:00:00.123456Z"
            measured = {
                "cpu_percent": 42.0, "memory_used_bytes": 1234,
                "disk_used_bytes": 5678, "disk_used_percent": 61.0,
                "gpu_devices": [{
                    "index": 0, "device_id": "c" * 32, "name": "Fixture GPU",
                    "utilization_percent": 73.0, "power_draw_w": 88.0,
                }],
            }
            insert_metric = observability_store._insert_metric_observation

            def fail_cpu_metric(db, **kwargs):
                if kwargs.get("metric_key") == "hardware.cpu_utilization_percent":
                    raise sqlite3.OperationalError("fixture CPU metric failure")
                return insert_metric(db, **kwargs)

            with patch.object(main, "DB_PATH", database), \
                 patch.object(main, "utc_now", return_value=timestamp), \
                 patch.object(main, "get_system_metrics", return_value=measured), \
                 patch.object(main, "observability_installation_id", "host-sample-continue-installation"), \
                 patch.object(observability_store, "_insert_metric_observation", side_effect=fail_cpu_metric), \
                 self.assertLogs(main.logger, level="WARNING") as captured:
                main._record_observability_host_sample(session_id)

            self.assertIn("hardware.cpu_utilization_percent", " ".join(captured.output))
            after, before = "2026-11-13T11:59:00Z", "2026-11-13T12:01:00Z"
            cpu = observability_store.query_metric_series(database, metric_key="hardware.cpu_utilization_percent", after=after, before=before)
            memory = observability_store.query_metric_series(database, metric_key="hardware.ram_used_bytes", after=after, before=before)
            disk = observability_store.query_metric_series(database, metric_key="hardware.disk_used_bytes", after=after, before=before)
            gpu = observability_store.query_metric_series(database, metric_key="hardware.gpu_utilization_percent", after=after, before=before)
            power = observability_store.query_metric_series(database, metric_key="hardware.gpu_power_draw_w", after=after, before=before)
            self.assertEqual(cpu, [])
            self.assertEqual(memory[-1]["value"], 1234)
            self.assertEqual(disk[-1]["value"], 5678)
            self.assertEqual(gpu[-1]["value"], 73.0)
            self.assertEqual(power[-1]["value"], 88.0)

    def test_host_sample_gap_is_retried_and_saved_after_storage_recovers(self):
        with tempfile.TemporaryDirectory(prefix="vaos-host-sample-gap-") as temporary:
            database = Path(temporary) / "app.db"
            observability_store.initialize_database(database)
            session_id, _ = observability_store.start_session(
                database, component="admin", app_version=main.APP_VERSION,
            )
            measured = {
                "cpu_percent": 42.0, "memory_used_bytes": 1234,
                "disk_used_bytes": 5678, "disk_used_percent": 61.0,
                "gpu_devices": [],
            }
            insert_metric = observability_store._insert_metric_observation
            cpu_failures_remaining = 1
            gap_write_failures_remaining = 1
            record_event = observability_store.record_event

            def fail_cpu_once(db, **kwargs):
                nonlocal cpu_failures_remaining
                if kwargs.get("metric_key") == "hardware.cpu_utilization_percent" and cpu_failures_remaining:
                    cpu_failures_remaining -= 1
                    raise sqlite3.OperationalError("fixture CPU metric write failure")
                return insert_metric(db, **kwargs)

            def fail_gap_once(db_path, **kwargs):
                nonlocal gap_write_failures_remaining
                if kwargs.get("event_type") == "hardware.metric_sample_gap" and gap_write_failures_remaining:
                    gap_write_failures_remaining -= 1
                    raise sqlite3.OperationalError("fixture gap summary write failure")
                return record_event(db_path, **kwargs)

            with patch.object(main, "DB_PATH", database), \
                 patch.object(main, "utc_now", side_effect=["2026-11-14T12:00:00Z", "2026-11-14T12:00:30Z"]), \
                 patch.object(main, "get_system_metrics", return_value=measured), \
                 patch.object(main, "observability_host_sample_lock", threading.Lock()), \
                 patch.object(main, "observability_host_sample_loss_count", 0), \
                 patch.object(main, "observability_host_sample_loss_metric_counts", {}), \
                 patch.object(main, "observability_host_sample_loss_event_id", None), \
                 patch.object(observability_store, "_insert_metric_observation", side_effect=fail_cpu_once), \
                 patch.object(observability_store, "record_event", side_effect=fail_gap_once), \
                 self.assertLogs(main.logger, level="WARNING"):
                main._record_observability_host_sample(session_id)
                self.assertEqual(main.observability_host_sample_loss_count, 1)
                self.assertEqual(main.observability_host_sample_loss_metric_counts, {"hardware.cpu_utilization_percent": 1})
                main._record_observability_host_sample(session_id)
                self.assertEqual(main.observability_host_sample_loss_count, 0)
                self.assertEqual(main.observability_host_sample_loss_metric_counts, {})
                self.assertIsNone(main.observability_host_sample_loss_event_id)

            gaps = observability_store.query_events(
                database, limit=10, event_type="hardware.metric_sample_gap", session_id=session_id,
            )
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["details"]["dropped_sample_count"], 1)
            self.assertEqual(
                gaps[0]["details"]["failed_metric_counts"],
                {"hardware.cpu_utilization_percent": 1},
            )
            with patch.object(main, "DB_PATH", database):
                response = self.client.get(
                    "/api/admin/observability/events",
                    params={"event_type": "hardware.metric_sample_gap", "session_id": session_id},
                    auth=self.basic_auth,
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()["items"]), 1)
            self.assertEqual(response.json()["items"][0]["details"]["dropped_sample_count"], 1)

    def test_system_metric_collector_reads_measured_gpu_power_without_exposing_uuid(self):
        previous_cache = dict(main.system_metrics_cache)
        main.system_metrics_cache.update({"sampled_at": 0.0, "data": {}})
        output = "0, GPU-639586ec-9f47-74b0-05e0-92a7b75e7144, Fixture GPU, 55, 2048, 12288, 48, 125.75\n"
        try:
            with patch("main.shutil.which", return_value="nvidia-smi"), patch("main.subprocess.run", return_value=SimpleNamespace(stdout=output)):
                metrics = main.get_system_metrics()
        finally:
            main.system_metrics_cache.update(previous_cache)
        self.assertEqual(metrics["gpu_power_draw_w"], 125.75)
        self.assertEqual(metrics["gpu_devices"][0]["power_draw_w"], 125.75)
        device_id = metrics["gpu_devices"][0]["device_id"]
        self.assertRegex(device_id, r"^[a-f0-9]{32}$")
        self.assertNotIn("GPU-639586ec-9f47-74b0-05e0-92a7b75e7144", json.dumps(metrics))

    def test_gpu_power_query_falls_back_without_dropping_legacy_gpu_metrics(self):
        previous_cache = dict(main.system_metrics_cache)
        main.system_metrics_cache.update({"sampled_at": 0.0, "data": {}})
        output = "0, GPU-639586ec-9f47-74b0-05e0-92a7b75e7144, Fixture GPU, 55, 2048, 12288, 48\n"
        power_query_error = subprocess.CalledProcessError(returncode=1, cmd="nvidia-smi power.draw")
        try:
            with patch("main.shutil.which", return_value="nvidia-smi"), patch(
                "main.subprocess.run",
                side_effect=[power_query_error, SimpleNamespace(stdout=output)],
            ) as run:
                metrics = main.get_system_metrics()
        finally:
            main.system_metrics_cache.update(previous_cache)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(metrics["gpu_available"])
        self.assertEqual(metrics["gpu_utilization_percent"], 55)
        self.assertIsNone(metrics["gpu_power_draw_w"])

    def test_training_epoch_event_and_metrics_commit_atomically_from_queue(self):
        record = {
            "timestamp": "2026-11-12T08:00:00Z", "level": "INFO", "message": "Training epoch completed",
            "run_id": "fixture-run", "source": "idd-lite", "initial_model": "best.pt", "epoch": 4,
            "stats": {"box_loss": 0.4, "class_loss": 0.3, "dfl_loss": 0.2, "precision": 0.8, "recall": 0.7, "map50": 0.75, "map50_95": 0.6, "learning_rate": 0.001},
        }
        self.assertTrue(main._persist_observability_training_event(record))
        series = observability_store.query_metric_series(main.DB_PATH, metric_key="training.dfl_loss", after="2026-11-12T00:00:00Z", before="2026-11-13T00:00:00Z")
        self.assertEqual(series[-1]["value"], 0.2)
        self.assertEqual(series[-1]["dimensions"]["dataset_id"], "idd-lite")
        self.assertEqual(observability_store.overview(main.DB_PATH)["kpis"]["training_epochs_completed"]["value"], 1)

    def test_training_log_recovery_restores_rotated_records_once_without_reassigning_session(self):
        with tempfile.TemporaryDirectory(prefix="vaos-training-log-recovery-") as folder:
            active_log = Path(folder) / "training.log"
            rotated_log = active_log.with_suffix(".log.1")
            older_rotated_log = active_log.with_suffix(".log.2")
            records = [
                {"timestamp": "2026-11-12T08:19:00Z", "level": "INFO", "message": "Training epoch completed", "run_id": "recovery-run", "source": "idd-lite", "initial_model": "best.pt", "epoch": 0, "stats": {"box_loss": 0.9, "class_loss": 0.8, "dfl_loss": 0.5}},
                {"timestamp": "2026-11-12T08:20:00Z", "level": "INFO", "message": "Training epoch completed", "run_id": "recovery-run", "source": "idd-lite", "initial_model": "best.pt", "epoch": 1, "stats": {"box_loss": 0.7, "class_loss": 0.6, "dfl_loss": 0.4}},
                {"timestamp": "2026-11-12T08:21:00Z", "level": "INFO", "message": "Training epoch completed", "run_id": "recovery-run", "source": "idd-lite", "initial_model": "best.pt", "epoch": 2, "stats": {"box_loss": 0.5, "class_loss": 0.4, "dfl_loss": 0.3}},
            ]
            older_rotated_log.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
            rotated_log.write_text(json.dumps(records[1]) + "\n", encoding="utf-8")
            active_log.write_text(json.dumps(records[2]) + "\n{\"timestamp\":\n", encoding="utf-8")
            with observability_store._connection(main.DB_PATH) as db:
                before = int(db.execute("SELECT COALESCE((SELECT value FROM observability_lifetime_counters WHERE metric_key='training.epochs_completed'),0)").fetchone()[0])
            with patch.object(main, "ML_TRAINING_LOG_PATH", active_log), patch.object(main, "observability_session_id", "current-admin-session"):
                first = main.reconcile_training_log_observability()
                second = main.reconcile_training_log_observability()
            with observability_store._connection(main.DB_PATH) as db:
                after = int(db.execute("SELECT value FROM observability_lifetime_counters WHERE metric_key='training.epochs_completed'").fetchone()[0])
                sessions = db.execute("SELECT session_id FROM observability_events WHERE event_type='training.training_epoch_completed' AND instr(details_json,?)>0", ('"run_id":"recovery-run"',)).fetchall()
            self.assertEqual((first["parsed"], first["processed"], first["invalid"], first["failed"]), (3, 3, 1, 0))
            self.assertEqual(second["processed"], 3)
            self.assertEqual(second["invalid"], 1)
            self.assertEqual(after, before + 3)
            self.assertEqual(len(sessions), 3)
            self.assertTrue(all(row["session_id"] is None for row in sessions))
            series = observability_store.query_metric_series(main.DB_PATH, metric_key="training.box_loss", after="2026-11-12T08:00:00Z", before="2026-11-12T09:00:00Z", dimensions={"training_job_id": "recovery-run"})
            self.assertEqual([item["value"] for item in series], [0.9, 0.7, 0.5])

    def test_training_log_recovers_event_after_simulated_process_exit_before_queue_flush(self):
        with tempfile.TemporaryDirectory(prefix="vaos-training-hard-exit-") as folder:
            data_dir = Path(folder) / "data"
            env = os.environ.copy()
            env["VAOS_DATA_DIR"] = str(data_dir)
            env["MODEL_DIR"] = str(Path(folder) / "models")
            child_script = (
                "import os, main; "
                "main.write_training_log('INFO', 'Training epoch completed', "
                "run_id='hard-exit-recovery-run', source='idd-lite', initial_model='best.pt', "
                "epoch=1, stats={'box_loss': 0.4, 'class_loss': 0.3, 'dfl_loss': 0.2}); "
                "os._exit(0)"
            )
            child = subprocess.run(
                [sys.executable, "-c", child_script], cwd=str(main.APP_DIR), env=env,
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(child.returncode, 0, child.stderr[-2000:])
            crash_log = data_dir / "ml_training" / "training.log"
            self.assertTrue(crash_log.is_file())
            with patch.object(main, "DB_PATH", data_dir / "app.db"), \
                 patch.object(main, "ML_TRAINING_LOG_PATH", crash_log):
                first = main.reconcile_training_log_observability()
                second = main.reconcile_training_log_observability()
            with observability_store._connection(data_dir / "app.db") as db:
                counter = db.execute(
                    "SELECT value FROM observability_lifetime_counters WHERE metric_key='training.epochs_completed'"
                ).fetchone()
                count = db.execute(
                    "SELECT COUNT(*) FROM observability_events WHERE event_type='training.training_epoch_completed' AND instr(details_json,?)>0",
                    ('"run_id":"hard-exit-recovery-run"',),
                ).fetchone()[0]
            self.assertEqual(first["processed"], 1)
            self.assertEqual(second["processed"], 1)
            self.assertEqual(counter[0], 1)
            self.assertEqual(count, 1)

    def test_training_log_rotation_keeps_twenty_segments_and_discards_oldest(self):
        with tempfile.TemporaryDirectory(prefix="vaos-training-log-rotation-") as folder:
            active_log = Path(folder) / "training.log"
            with patch.object(main, "ML_TRAINING_LOG_PATH", active_log), \
                 patch.object(main, "OBSERVABILITY_TRAINING_LOG_MAX_BYTES", 1), \
                 patch.object(main, "queue_observability_training_event"):
                for index in range(1, 25):
                    main.write_training_log("INFO", f"event-{index}", run_id="rotation-run")
                segments = [
                    active_log.with_suffix(f".log.{index}")
                    for index in range(main.OBSERVABILITY_TRAINING_LOG_ROTATIONS, 0, -1)
                ]
                segments.append(active_log)
                lines = [path.read_text(encoding="utf-8").splitlines()[0] for path in segments]
            self.assertEqual(len(segments), 21)
            self.assertEqual([json.loads(line)["message"] for line in lines], [f"event-{index}" for index in range(4, 25)])

    def test_training_log_view_skips_bad_lines_without_hiding_valid_records(self):
        with tempfile.TemporaryDirectory(prefix="vaos-training-log-view-") as folder:
            active_log = Path(folder) / "training.log"
            active_log.write_text(
                json.dumps({"level": "INFO", "message": "before-corruption"})
                + "\n{partial-json\n"
                + json.dumps({"level": "INFO", "message": "after-corruption"})
                + "\n[]\n",
                encoding="utf-8",
            )
            with patch.object(main, "ML_TRAINING_LOG_PATH", active_log):
                records = main.read_training_log()
            self.assertEqual([record["message"] for record in records], ["before-corruption", "after-corruption"])

    def test_training_log_concurrent_processes_preserve_records_during_rotation(self):
        with tempfile.TemporaryDirectory(prefix="vaos-training-log-process-lock-") as folder:
            shared_log = Path(folder) / "training.log"
            processes = []
            try:
                for worker in range(4):
                    worker_data = Path(folder) / f"worker-{worker}" / "data"
                    env = os.environ.copy()
                    env["VAOS_DATA_DIR"] = str(worker_data)
                    env["MODEL_DIR"] = str(Path(folder) / f"worker-{worker}" / "models")
                    child_script = (
                        "import sys; from pathlib import Path; import main; "
                        "main.ML_TRAINING_LOG_PATH=Path(sys.argv[1]); "
                        "main.OBSERVABILITY_TRAINING_LOG_MAX_BYTES=1; "
                        "[main.write_training_log('INFO', f'worker-{sys.argv[2]}-event-{index}', "
                        "worker=int(sys.argv[2]), sequence=index) for index in range(5)]"
                    )
                    processes.append(subprocess.Popen(
                        [sys.executable, "-c", child_script, str(shared_log), str(worker)],
                        cwd=str(main.APP_DIR), env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                    ))
                for process in processes:
                    stdout, stderr = process.communicate(timeout=60)
                    self.assertEqual(process.returncode, 0, f"child output: {stdout}\n{stderr[-2000:]}")

                retained_paths = [
                    shared_log.with_suffix(f".log.{index}")
                    for index in range(main.OBSERVABILITY_TRAINING_LOG_ROTATIONS, 0, -1)
                ] + [shared_log]
                records = [
                    json.loads(line)
                    for path in retained_paths if path.is_file()
                    for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
                ]
                observed = {record["message"] for record in records}
                expected = {f"worker-{worker}-event-{sequence}" for worker in range(4) for sequence in range(5)}
                self.assertEqual(observed, expected)
                self.assertEqual(len(records), len(expected))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=10)

    def test_training_log_append_failure_uses_durable_observability_fallback(self):
        with tempfile.TemporaryDirectory(prefix="vaos-training-log-fallback-") as folder:
            blocked_log = Path(folder) / "training.log"
            blocked_log.mkdir()
            with patch.object(main, "ML_TRAINING_LOG_PATH", blocked_log), \
                 patch.object(main, "queue_observability_training_event"):
                main.write_training_log("ERROR", "Training failed", run_id="disk-error-fallback", status="failed")
            with closing(sqlite3.connect(main.DB_PATH)) as db:
                rows = db.execute(
                    "SELECT event_type,level FROM observability_events WHERE instr(details_json,?)>0",
                    ('"run_id":"disk-error-fallback"',),
                ).fetchall()
            self.assertEqual(rows, [("training.training_failed", "ERROR")])

    def test_training_event_flush_requeues_unwritten_batch_in_original_order(self):
        events = [
            {"timestamp": f"2026-11-12T08:00:0{index}Z", "level": "INFO", "message": f"training-event-{index}"}
            for index in range(3)
        ]
        isolated_queue = deque(events)
        concurrent_event = {"timestamp": "2026-11-12T08:01:00Z", "level": "INFO", "message": "concurrent-event"}

        def fail_after_concurrent_enqueue(record):
            if record == events[0]:
                main.queue_observability_training_event(concurrent_event)
            return False

        with patch.object(main, "observability_training_queue", isolated_queue), \
             patch.object(main, "observability_training_dropped", 0), \
             patch.object(main, "_persist_observability_training_event", side_effect=fail_after_concurrent_enqueue):
            persisted = main.flush_observability_training_events(max_events=3)
            self.assertEqual(persisted, 0)
            self.assertEqual(list(isolated_queue), events + [concurrent_event])

    def test_training_event_queue_recovers_after_sqlite_page_limit_is_restored(self):
        with tempfile.TemporaryDirectory(prefix="vaos-sqlite-full-recovery-") as folder:
            database = Path(folder) / "observability.db"
            observability_store.initialize_database(database)
            with closing(sqlite3.connect(database)) as db:
                db.execute("VACUUM")
                page_limit = db.execute("PRAGMA page_count").fetchone()[0]

            original_connection = observability_store._connection
            capacity = {"limited": True}

            @contextmanager
            def capped_connection(path, *, timeout_seconds=15):
                with original_connection(path, timeout_seconds=timeout_seconds) as db:
                    if capacity["limited"] and Path(path).resolve() == database.resolve():
                        db.execute(f"PRAGMA max_page_count={page_limit}")
                    yield db

            details = {
                "run_id": "sqlite-full-recovery-fixture",
                "epoch": 1,
                "stats": {"box_loss": 1.2, "class_loss": 1.3, "dfl_loss": 0.9},
                **{f"payload_{index}": "x" * 650 for index in range(18)},
            }
            record = {
                "timestamp": "2026-11-12T10:00:00Z",
                "level": "INFO",
                "message": "Training epoch completed",
                **details,
            }
            isolated_queue = deque()

            with patch.object(main, "DB_PATH", database), \
                 patch.object(main, "observability_training_queue", isolated_queue), \
                 patch.object(main, "observability_training_lock", threading.RLock()), \
                 patch.object(main, "observability_session_id", None), \
                 patch.object(observability_store, "_connection", capped_connection):
                with self.assertRaisesRegex(sqlite3.OperationalError, "full"):
                    observability_store.record_event(
                        database,
                        event_id="sqlite-full-probe",
                        event_type="observability.capacity_probe",
                        component="test",
                        message="Temporary SQLite page-limit probe",
                        details=details,
                    )

                main.queue_observability_training_event(record)
                self.assertEqual(main.flush_observability_training_events(max_events=1), 0)
                self.assertEqual(len(isolated_queue), 1)

                capacity["limited"] = False
                self.assertEqual(main.flush_observability_training_events(max_events=1), 1)

            with closing(sqlite3.connect(database)) as db:
                saved = db.execute(
                    "SELECT COUNT(*) FROM observability_events WHERE instr(details_json, ?) > 0",
                    ("sqlite-full-recovery-fixture",),
                ).fetchone()[0]
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(saved, 1)
            self.assertEqual(integrity, "ok")

    def test_training_event_retry_keeps_queue_bounded_and_counts_replaced_updates(self):
        events = [
            {"timestamp": f"2026-11-12T08:00:0{index}Z", "level": "INFO", "message": "Training progress update", "batch": index}
            for index in range(2)
        ]
        isolated_queue = deque(events)
        arriving = [
            {"timestamp": f"2026-11-12T08:01:0{index}Z", "level": "INFO", "message": "Training progress update", "batch": index + 2}
            for index in range(2)
        ]

        def fail_after_queue_fills(record):
            if record == events[0]:
                for new_event in arriving:
                    main.queue_observability_training_event(new_event)
            return False

        with patch.object(main, "observability_training_queue", isolated_queue), \
             patch.object(main, "observability_training_dropped", 0), \
             patch.object(main, "OBSERVABILITY_TRAINING_QUEUE_LIMIT", 2), \
             patch.object(main, "_persist_observability_training_event", side_effect=fail_after_queue_fills):
            persisted = main.flush_observability_training_events(max_events=2)
            self.assertEqual(persisted, 0)
            self.assertEqual(len(isolated_queue), 2)
            self.assertEqual(list(isolated_queue), [events[0], arriving[1]])
            self.assertEqual(main.observability_training_dropped, 2)

    def test_hardware_endpoint_uses_existing_diagnostics_scope_and_redacts_path(self):
        self.save_integrations()
        token = self.issue_token(groups=["diagnostics"])
        measured = {
            "memory_percent": 41.0,
            "memory_used_gb": 13.1,
            "memory_total_gb": 32.0,
            "gpu_devices": [
                {"index": 0, "name": "Fixture GPU A", "utilization_percent": 60, "memory_used_mb": 3000, "memory_total_mb": 12000, "temperature_c": 61},
                {"index": 1, "name": "Fixture GPU B", "utilization_percent": 10, "memory_used_mb": 1000, "memory_total_mb": 8000, "temperature_c": 45},
            ],
        }
        with patch("main.get_system_metrics", return_value=measured), \
             patch("main.training_device_inventory", return_value={"cpu_threads": 12, "gpus": [{"index": 0, "name": "Fixture GPU A"}, {"index": 1, "name": "Fixture GPU B"}]}), \
             patch("main.model_runtime_status", return_value={"object_model_loaded": False, "cuda_available": False}), \
             patch("main.ml_training_state", {"status": "idle"}):
            response = self.secure_client.get("/api/v1/admin/hardware", headers=self.bearer(token["token"]))
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(len(report["facts"]["gpus"]), 2)
        self.assertEqual(report["compatibility"]["cuda_inference_status"], "unknown")
        self.assertFalse(report["recommendations"]["applied_automatically"])
        self.assertNotIn(str(Path(_TEST_HOME.name)), response.text)

    def test_tracking_rules_use_event_scope_sensitive_reads_and_mcp_approval(self):
        self.save_integrations(api_allow_sensitive_reads=True)
        camera_token = "test-camera-rule-token-0001"
        with main.db_connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO cameras(token,name,created_at,active) VALUES(?,?,?,1)",
                (camera_token, "Rule test camera", main.utc_now()),
            )
        payload = {
            "name": "Entrance count", "rule_type": "count_in_region", "class_name": "person",
            "threshold_count": 2, "min_consecutive_frames": 3, "cooldown_seconds": 60,
            "camera_token": camera_token, "region": {"x": 0, "y": 0, "width": 1, "height": 1},
        }
        created = self.client.post("/api/admin/tracking/rules", auth=self.basic_auth, json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        rule_id = created.json()["rule"]["id"]
        self.assertEqual(created.json()["rule"]["camera_name"], "Rule test camera")

        denied_read = self.issue_token(groups=["events"])
        denied = self.secure_client.get("/api/v1/admin/tracking/rules", headers=self.bearer(denied_read["token"]))
        self.assertEqual(denied.status_code, 403)
        allowed_read = self.issue_token(groups=["events"], sensitive_read=True)
        allowed = self.secure_client.get("/api/v1/admin/tracking/rules", headers=self.bearer(allowed_read["token"]))
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["rules"][0]["id"], rule_id)

        agent = self.issue_token(groups=["events"], mcp_agent=True, access="control")
        headers = self.bearer(agent["token"])
        self.save_integrations(mcp_enabled=True, mcp_read_only=False, mcp_allowed_groups=["events"], mcp_allow_sensitive=False)
        blocked = self.secure_client.post("/api/v1/admin/tracking/rules", headers=headers, json=payload)
        self.assertEqual(blocked.status_code, 403)
        self.save_integrations(mcp_enabled=True, mcp_read_only=False, mcp_allowed_groups=["events"], mcp_allow_sensitive=True)
        approval = self.secure_client.post("/api/v1/admin/tracking/rules", headers=headers, json=payload)
        self.assertEqual(approval.status_code, 428, approval.text)
        self.assertTrue(approval.json()["detail"]["approval_required"])

        updated = self.client.patch(f"/api/admin/tracking/rules/{rule_id}", auth=self.basic_auth, json={"enabled": False})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertFalse(updated.json()["rule"]["enabled"])
        removed = self.client.delete(f"/api/admin/tracking/rules/{rule_id}", auth=self.basic_auth)
        self.assertEqual(removed.status_code, 200, removed.text)

    def test_encrypted_project_export_import(self):
        response = self.client.post("/api/admin/projects/export", auth=self.basic_auth, json={"name":"Encrypted test", "passphrase":"Ab12Cd34"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        upload = {"file": ("test.vaosproject", response.content, "application/octet-stream")}
        wrong = self.client.post("/api/admin/projects/import", auth=self.basic_auth, files=upload, data={"passphrase":"Ab12Cd35"})
        self.assertEqual(wrong.status_code, 400)
        preview = self.client.post("/api/admin/projects/import?dry_run=true", auth=self.basic_auth, files=upload, data={"passphrase":"Ab12Cd34"})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertFalse(preview.json()["saved"])
        saved = self.client.post("/api/admin/projects/import?dry_run=false", auth=self.basic_auth, files=upload, data={"passphrase":"Ab12Cd34"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["saved"])
        review = self.client.get("/api/admin/projects/"+saved.json()["bundle_id"], auth=self.basic_auth)
        self.assertEqual(review.status_code, 200, review.text)
        invalid = self.client.post("/api/admin/projects/export", auth=self.basic_auth, json={"passphrase":"bad"})
        self.assertEqual(invalid.status_code, 400)

    def test_training_status_refresh_setting_is_persisted_and_bounded(self):
        for value in (1, 5, 10):
            response = self.client.post("/api/admin/engine-settings", auth=self.basic_auth, json={"training_status_refresh_seconds":value})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["settings"]["training_status_refresh_seconds"], value)
        for value in (0, 11):
            response = self.client.post("/api/admin/engine-settings", auth=self.basic_auth, json={"training_status_refresh_seconds":value})
            self.assertEqual(response.status_code, 400)

    def test_project_bundle_group_and_mcp_import_approval(self):
        self.save_integrations(api_enabled=True, mcp_enabled=True, mcp_read_only=False,
                               mcp_allowed_groups=["projects"], mcp_allow_sensitive=False)
        read_token = self.issue_token(groups=["projects"])
        listed = self.secure_client.get("/api/v1/admin/projects", headers=self.bearer(read_token["token"]))
        self.assertEqual(listed.status_code, 200, listed.text)
        wrong_group = self.issue_token(groups=["diagnostics"])
        denied = self.secure_client.get("/api/v1/admin/projects", headers=self.bearer(wrong_group["token"]))
        self.assertEqual(denied.status_code, 403)

        project = {
            "name": "Test bundle", "description": "", "engine_settings": {"object_detection_enabled": True},
            "dataset_refs": [], "model_refs": [],
        }
        bundle, _ = build_bundle(project, app_version=main.APP_VERSION, created_at=main.utc_now())
        agent = self.issue_token(groups=["projects"], mcp_agent=True, access="control")
        headers = self.bearer(agent["token"])
        upload = {"file": ("test.vaosproject", bundle, "application/vnd.vaos.project+zip")}
        blocked = self.secure_client.post("/api/v1/admin/projects/import?dry_run=true", headers=headers, files=upload)
        self.assertEqual(blocked.status_code, 403)
        self.save_integrations(api_enabled=True, mcp_enabled=True, mcp_read_only=False,
                               mcp_allowed_groups=["projects"], mcp_allow_sensitive=True)
        approval = self.secure_client.post("/api/v1/admin/projects/import?dry_run=true", headers=headers, files=upload)
        self.assertEqual(approval.status_code, 428, approval.text)
        self.assertTrue(approval.json()["detail"]["approval_required"])

    def test_sensitive_reads_require_both_global_and_token_grants(self):
        observability_store.record_event(main.DB_PATH, event_id="sensitive-log-fixture", event_type="observability.fixture", component="test", message="redacted fixture")
        self.save_integrations(api_allow_sensitive_reads=True)
        log_private_token = self.issue_token(groups=["diagnostics"], sensitive_read=True)
        token = self.issue_token(groups=["people"])
        self.save_integrations(api_allow_sensitive_reads=False)
        denied_global = self.secure_client.get("/api/v1/admin/participants", headers=self.bearer(token["token"]))
        self.assertEqual(denied_global.status_code, 403)

        denied_log_global = self.secure_client.get("/api/v1/admin/observability/events/sensitive-log-fixture", headers=self.bearer(log_private_token["token"]))
        self.assertEqual(denied_log_global.status_code, 403)

        self.save_integrations(api_allow_sensitive_reads=True)
        denied_token = self.secure_client.get("/api/v1/admin/participants", headers=self.bearer(token["token"]))
        self.assertEqual(denied_token.status_code, 403)
        log_diagnostics = self.issue_token(groups=["diagnostics"], sensitive_read=False)
        denied_log_token = self.secure_client.get("/api/v1/admin/observability/events/sensitive-log-fixture", headers=self.bearer(log_diagnostics["token"]))
        self.assertEqual(denied_log_token.status_code, 403)

        private_token = self.issue_token(groups=["people"], sensitive_read=True)
        allowed = self.secure_client.get("/api/v1/admin/participants", headers=self.bearer(private_token["token"]))
        self.assertEqual(allowed.status_code, 200, allowed.text)
        allowed_log = self.secure_client.get("/api/v1/admin/observability/events/sensitive-log-fixture", headers=self.bearer(log_private_token["token"]))
        self.assertEqual(allowed_log.status_code, 200, allowed_log.text)

    def test_default_token_is_least_privilege_and_writes_need_control(self):
        self.save_integrations(api_allow_sensitive_reads=True)
        token = self.issue_token(groups=["diagnostics"])
        blocked_people = self.secure_client.get("/api/v1/admin/participants", headers=self.bearer(token["token"]))
        self.assertEqual(blocked_people.status_code, 403)
        blocked_write = self.secure_client.delete("/api/v1/admin/events", headers=self.bearer(token["token"]))
        self.assertEqual(blocked_write.status_code, 403)

    def test_mcp_capability_groups_default_to_diagnostics_only(self):
        with main.db_connection() as db:
            db.execute("DELETE FROM app_settings WHERE key='integration_mcp_allowed_groups'")
        self.assertEqual(main.get_api_settings()["mcp_allowed_groups"], ["diagnostics"])

        response = self.client.post(
            "/api/admin/integrations", auth=self.basic_auth,
            json={"api_enabled": True, "mcp_enabled": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["settings"]["mcp_allowed_groups"], ["diagnostics"])

    def test_token_is_hashed_and_revocation_is_immediate(self):
        self.save_integrations()
        token = self.issue_token(groups=["diagnostics"])
        digest = hashlib.sha256(token["token"].encode()).hexdigest()
        with main.db_connection() as db:
            row = db.execute("SELECT token_hash,scopes FROM api_tokens WHERE token_prefix=?", (token["prefix"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["token_hash"], digest)
        self.assertNotIn(token["token"], row["scopes"])
        revoked = self.client.delete(f"/api/admin/api-tokens/{token['prefix']}", auth=self.basic_auth)
        self.assertEqual(revoked.status_code, 200)
        rejected = self.secure_client.get("/api/v1/admin/status", headers=self.bearer(token["token"]))
        self.assertEqual(rejected.status_code, 401)

    def test_sensitive_path_inventory_covers_capability_and_personal_data(self):
        paths = (
            "/api/v1/admin/cameras", "/api/v1/admin/cameras/secret/qr",
            "/api/v1/admin/enrollment-links/secret/qr", "/api/v1/admin/tunnel",
            "/api/v1/admin/admin-tunnel", "/api/v1/admin/participants/1/photos/2",
            "/api/v1/admin/events", "/api/v1/admin/ml/samples/1/image",
            "/api/v1/admin/observability/events/opaque-event-id",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(main._api_read_is_sensitive(path))
        self.assertFalse(main._api_read_is_sensitive("/api/v1/admin/status"))

    def enable_mcp_control(self):
        self.save_integrations(
            mcp_enabled=True,
            mcp_read_only=False,
            mcp_allow_sensitive=True,
            mcp_allow_deletes=True,
            mcp_allowed_groups=["settings", "diagnostics"],
        )

    def test_mcp_invocation_telemetry_is_limited_to_mcp_tokens_and_minimal_fields(self):
        self.save_integrations(mcp_enabled=True, mcp_read_only=True, mcp_allowed_groups=["settings"])
        token = self.issue_token(groups=["diagnostics"], mcp_agent=True, access="read")
        headers = self.bearer(token["token"])
        payload = {
            "tool": "vaos_get_system_health", "outcome": "success", "duration_ms": 12.5,
            "invocation_id": "0123456789abcdef0123456789abcdef",
        }

        basic = self.client.post("/api/admin/observability/mcp/invocations", auth=self.basic_auth, json=payload)
        self.assertEqual(basic.status_code, 403)
        non_mcp = self.issue_token(groups=["diagnostics"], access="read")
        rejected = self.secure_client.post("/api/v1/admin/observability/mcp/invocations", headers=self.bearer(non_mcp["token"]), json=payload)
        self.assertEqual(rejected.status_code, 403)

        accepted = self.secure_client.post("/api/v1/admin/observability/mcp/invocations", headers=headers, json=payload)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertTrue(accepted.json()["ok"])
        duplicate = self.secure_client.post("/api/v1/admin/observability/mcp/invocations", headers=headers, json=payload)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertTrue(duplicate.json()["duplicate"])
        event = observability_store.query_events(
            main.DB_PATH, limit=5, component="mcp", event_type="mcp.tool_invoked",
        )[0]
        self.assertEqual(event["details"], {"tool": payload["tool"], "outcome": "success", "duration_ms": 12.5})
        self.assertNotIn("arguments", json.dumps(event["details"]).casefold())
        self.assertNotIn("result", json.dumps(event["details"]).casefold())
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            count = db.execute(
                "SELECT delta,dimensions_json FROM observability_counter_events WHERE metric_key='mcp.tool_invocations' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            duration = db.execute(
                "SELECT value,dimensions_json FROM observability_metric_observations WHERE metric_key='mcp.tool_duration_ms' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(count[0], 1)
        self.assertEqual(json.loads(count[1]), {"tool": payload["tool"], "outcome": "success"})
        self.assertEqual(duration[0], 12.5)

        with closing(sqlite3.connect(main.DB_PATH)) as db:
            persisted_invocations = db.execute(
                "SELECT COUNT(*) FROM observability_events WHERE event_id=?",
                ("mcp-invocation-" + payload["invocation_id"],),
            ).fetchone()[0]
            persisted_counters = db.execute(
                "SELECT COUNT(*) FROM observability_counter_events WHERE event_id=? AND metric_key='mcp.tool_invocations'",
                ("mcp-invocation-" + payload["invocation_id"],),
            ).fetchone()[0]
            persisted_durations = db.execute(
                "SELECT COUNT(*) FROM observability_metric_observations WHERE source_event_id=? AND metric_key='mcp.tool_duration_ms'",
                ("mcp-invocation-" + payload["invocation_id"],),
            ).fetchone()[0]
        self.assertEqual(persisted_invocations, 1)
        self.assertEqual(persisted_counters, 1)
        self.assertEqual(persisted_durations, 1)

        invalid = self.secure_client.post(
            "/api/v1/admin/observability/mcp/invocations", headers=headers,
            json={"tool": "invented_tool", "outcome": "success", "duration_ms": 1},
        )
        self.assertEqual(invalid.status_code, 400)

        invalid_id = self.secure_client.post(
            "/api/v1/admin/observability/mcp/invocations", headers=headers,
            json={**payload, "invocation_id": "z" * 32},
        )
        self.assertEqual(invalid_id.status_code, 400)

        legacy_payload = {key: value for key, value in payload.items() if key != "invocation_id"}
        legacy = self.secure_client.post(
            "/api/v1/admin/observability/mcp/invocations", headers=headers, json=legacy_payload,
        )
        self.assertEqual(legacy.status_code, 200, legacy.text)

    def test_mcp_write_requires_approval_bound_to_exact_request_and_is_single_use(self):
        self.enable_mcp_control()
        token = self.issue_token(groups=["settings"], mcp_agent=True, access="control")
        headers = self.bearer(token["token"])
        agent_client = self.secure_client
        payload = {"object_confidence": 0.31, "note": "test-private-value"}
        first = agent_client.post("/api/v1/admin/engine-settings", headers=headers, json=payload)
        self.assertEqual(first.status_code, 428, first.text)
        pending = first.json()["detail"]
        self.assertTrue(pending["approval_required"])
        request_id = pending["request_id"]
        self.assertIn("0.31", json.dumps(pending["preview"]))
        self.assertNotIn("test-private-value", json.dumps(pending["preview"]))

        self.assertEqual(agent_client.get("/api/admin/mcp-approvals", headers=headers).status_code, 401)
        approved = self.client.post(
            f"/api/admin/mcp-approvals/{request_id}/decision",
            auth=self.basic_auth,
            json={"decision": "approve"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)

        mismatch = agent_client.post(
            "/api/v1/admin/engine-settings",
            headers={**headers, "X-VAOS-MCP-Approval-ID": request_id},
            json={"object_confidence": 0.32},
        )
        self.assertEqual(mismatch.status_code, 409)

        retried = agent_client.post(
            "/api/v1/admin/engine-settings",
            headers={**headers, "X-VAOS-MCP-Approval-ID": request_id},
            json=payload,
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        status = agent_client.get(f"/api/v1/admin/mcp-approvals/{request_id}", headers=headers)
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["status"], "used")
        replay = agent_client.post(
            "/api/v1/admin/engine-settings",
            headers={**headers, "X-VAOS-MCP-Approval-ID": request_id},
            json=payload,
        )
        self.assertEqual(replay.status_code, 409)

    def test_mcp_denial_expiry_and_token_binding(self):
        self.enable_mcp_control()
        token = self.issue_token(groups=["settings"], mcp_agent=True, access="control")
        second_token = self.issue_token(groups=["settings"], mcp_agent=True, access="control")
        headers = self.bearer(token["token"])
        agent_client = self.secure_client
        request = agent_client.post("/api/v1/admin/engine-settings", headers=headers, json={"object_confidence": 0.41})
        self.assertEqual(request.status_code, 428, request.text)
        request_id = request.json()["detail"]["request_id"]

        foreign = agent_client.post(
            "/api/v1/admin/engine-settings",
            headers={**self.bearer(second_token["token"]), "X-VAOS-MCP-Approval-ID": request_id},
            json={"object_confidence": 0.41},
        )
        self.assertEqual(foreign.status_code, 404)

        denied = self.client.post(
            f"/api/admin/mcp-approvals/{request_id}/decision",
            auth=self.basic_auth,
            json={"decision": "deny"},
        )
        self.assertEqual(denied.status_code, 200, denied.text)
        rejected = agent_client.post(
            "/api/v1/admin/engine-settings",
            headers={**headers, "X-VAOS-MCP-Approval-ID": request_id},
            json={"object_confidence": 0.41},
        )
        self.assertEqual(rejected.status_code, 409)

        expired = agent_client.post("/api/v1/admin/engine-settings", headers=headers, json={"object_confidence": 0.42})
        self.assertEqual(expired.status_code, 428, expired.text)
        expired_id = expired.json()["detail"]["request_id"]
        with main.db_connection() as db:
            db.execute("UPDATE mcp_approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE request_id=?", (expired_id,))
        expired_retry = agent_client.post(
            "/api/v1/admin/engine-settings",
            headers={**headers, "X-VAOS-MCP-Approval-ID": expired_id},
            json={"object_confidence": 0.42},
        )
        self.assertEqual(expired_retry.status_code, 409)
        check = agent_client.get(f"/api/v1/admin/mcp-approvals/{expired_id}", headers=headers)
        self.assertEqual(check.json()["status"], "expired")

    def test_multipart_upload_approval_ignores_generated_boundary_and_hides_filename(self):
        self.save_integrations(
            mcp_enabled=True,
            mcp_read_only=False,
            mcp_allow_sensitive=True,
            mcp_allowed_groups=["models", "settings"],
        )
        token = self.issue_token(groups=["models", "settings"], mcp_agent=True, access="control")
        headers = self.bearer(token["token"])
        files = {"file": ("private-model-name.pt", b"too small to be a checkpoint", "application/octet-stream")}
        first = self.secure_client.post("/api/v1/admin/ml/model-sources", headers=headers, files=files)
        self.assertEqual(first.status_code, 428, first.text)
        pending = first.json()["detail"]
        self.assertNotIn("private-model-name.pt", json.dumps(pending["preview"]))

        approved = self.client.post(
            f"/api/admin/mcp-approvals/{pending['request_id']}/decision",
            auth=self.basic_auth,
            json={"decision": "approve"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        retry = self.secure_client.post(
            "/api/v1/admin/ml/model-sources",
            headers={**headers, "X-VAOS-MCP-Approval-ID": pending["request_id"]},
            files=files,
        )
        self.assertEqual(retry.status_code, 400, retry.text)
        self.assertIn("too small", retry.text)
        status = self.secure_client.get(f"/api/v1/admin/mcp-approvals/{pending['request_id']}", headers=headers)
        self.assertEqual(status.json()["status"], "used")


if __name__ == "__main__":
    unittest.main()
