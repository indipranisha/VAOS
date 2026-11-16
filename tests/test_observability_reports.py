"""Isolated tests for safe asynchronous local observability reports."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import observability_reports as reports
import observability_store as store
from observability_store import initialize_database, record_event, record_metric, record_security_event, start_session


def _pdf_text(content: bytes) -> str:
    hex_strings = re.findall(rb"<([0-9A-F]+)> Tj", content)
    return "\n".join(bytes.fromhex(value.decode("ascii")).decode("cp1252", errors="replace") for value in hex_strings)


class ObservabilityReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vaos-report-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "app.db"
        self.output = self.root / "reports"
        initialize_database(self.database)
        self.session_id, _ = start_session(self.database, component="training", app_version="1.1.0", now="2026-11-12T09:00:00Z", process_id=987654321)
        record_event(
            self.database, event_id="report-event-1", timestamp="2026-11-12T10:00:00Z",
            event_type="training.epoch_completed", component="training", level="INFO",
            message="epoch completed", details={"epoch": 1, "run_id": "training-run-42", "source": "idd-lite", "initial_model": "best.pt"}, session_id=self.session_id, correlation_id="run-abc123", counter_deltas={"training.epochs_completed": 1},
        )
        record_metric(
            self.database, metric_key="training.box_loss", value=0.42, source_event_id="report-event-1",
            dimensions={"epoch": 1}, provenance="unit test", timestamp="2026-11-12T10:00:00Z",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _create(self, report_type="events", output_format="csv", parameters=None, now=None):
        return reports.create_report_job(
            self.database, report_type=report_type, output_format=output_format,
            requested_by="admin", parameters=parameters if parameters is not None else {
                "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z",
            }, now=now or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )

    def test_host_sample_gap_is_in_operational_event_report(self):
        record_event(
            self.database,
            event_id="host-sample-gap-report-fixture",
            timestamp="2026-11-12T11:00:00Z",
            event_type="hardware.metric_sample_gap",
            component="hardware",
            level="WARNING",
            message="Host or GPU metric samples were temporarily unavailable.",
            details={"dropped_sample_count": 2, "failed_metric_counts": {"hardware.cpu_utilization_percent": 2}},
            session_id=self.session_id,
        )
        job = self._create(parameters={
            "after": "2026-11-12T00:00:00Z",
            "before": "2026-11-13T00:00:00Z",
            "event_type": "hardware.metric_sample_gap",
        })
        completed = reports.process_report_job(self.database, self.output, job["job_id"])
        report_path = reports.report_file_path(self.output, completed)
        self.assertIsNotNone(report_path)
        with report_path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "hardware.metric_sample_gap")
        self.assertIn('"dropped_sample_count":2', rows[0]["details_json"])

    def test_source_retention_cleanup_keeps_report_copy_until_its_separate_expiry(self):
        requested_at = datetime.now(timezone.utc).replace(microsecond=0)
        old_event_time = requested_at - timedelta(days=60)
        old_event = old_event_time.isoformat(timespec="seconds").replace("+00:00", "Z")
        day_start = old_event_time.replace(hour=0, minute=0, second=0, microsecond=0)
        record_event(
            self.database, event_id="retention-export-copy", timestamp=old_event,
            event_type="observability.retention_fixture", component="qa",
            message="source event older than the new policy",
        )
        job = self._create(
            parameters={
                "after": day_start.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "before": (day_start + timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
            now=requested_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        completed = reports.process_report_job(self.database, self.output, job["job_id"])
        report_path = reports.report_file_path(self.output, completed)
        self.assertIsNotNone(report_path)
        self.assertIn("source event older than the new policy", report_path.read_text(encoding="utf-8-sig"))

        store.set_retention_policy(
            self.database, event_details_days=30, metric_observations_max_days=365,
            delivery_history_days=90, confirm_shorter=True,
        )
        cleaned_at = (requested_at + timedelta(minutes=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        cleanup = store.apply_retention(self.database, now=cleaned_at)
        self.assertGreaterEqual(cleanup["event_details_redacted"], 1)
        with closing(sqlite3.connect(self.database)) as db:
            source_message = db.execute(
                "SELECT message FROM observability_events WHERE event_id='retention-export-copy'"
            ).fetchone()[0]
        self.assertEqual(source_message, "Expired event details redacted by VAOS retention policy.")
        self.assertTrue(reports.record_download(self.database, job_id=job["job_id"], requested_by="admin"))
        self.assertEqual(
            reports.expire_reports(self.database, self.output, now=(requested_at + timedelta(hours=23)).isoformat()),
            0,
        )
        self.assertIsNotNone(reports.report_file_path(self.output, reports.get_report_job(self.database, job["job_id"])))

        self.assertEqual(
            reports.expire_reports(self.database, self.output, now=(requested_at + timedelta(hours=25)).isoformat()),
            1,
        )
        self.assertIsNone(reports.report_file_path(self.output, reports.get_report_job(self.database, job["job_id"])))

    def test_async_csv_job_has_integrity_and_download_audit(self):
        job = self._create()
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["progress_percent"], 100)
        path = reports.report_file_path(self.output, result)
        self.assertIsNotNone(path)
        content = path.read_text(encoding="utf-8-sig")
        self.assertIn("training.epoch_completed", content)
        self.assertTrue(reports.record_download(self.database, job_id=job["job_id"], requested_by="admin"))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_export_audit WHERE report_job_id=?", (job["job_id"],)).fetchone()[0], 1)

    def test_event_report_filters_by_exact_correlation_id(self):
        record_event(
            self.database, event_id="report-event-other", timestamp="2026-11-12T10:05:00Z",
            event_type="training.epoch_completed", component="training", level="INFO",
            message="different run", correlation_id="run-other",
        )
        job = self._create(parameters={
            "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z",
            "correlation_id": "run-abc123",
        })
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        content = reports.report_file_path(self.output, result).read_text(encoding="utf-8-sig")
        self.assertIn("epoch completed", content)
        self.assertNotIn("different run", content)

    def test_event_report_filters_by_runtime_session_and_includes_session_column(self):
        record_event(
            self.database, event_id="report-event-other-session", timestamp="2026-11-12T10:05:00Z",
            event_type="training.epoch_completed", component="training", level="INFO",
            message="another session", session_id=start_session(self.database, component="training-other", app_version="1.1.0", now="2026-11-12T10:05:00Z", process_id=987654322)[0],
        )
        job = self._create(parameters={
            "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z",
            "event_type": "training.epoch_completed", "session_id": self.session_id,
        })
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        report_path = reports.report_file_path(self.output, result)
        with report_path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["session_id"] for row in rows], [self.session_id])
        self.assertEqual([row["message"] for row in rows], ["epoch completed"])

    def test_event_report_filters_by_recorded_training_job_dataset_and_model(self):
        record_event(
            self.database, event_id="report-event-other-model", timestamp="2026-11-12T10:05:00Z",
            event_type="training.epoch_completed", component="training", level="INFO",
            message="different dataset/model", details={"run_id": "other-run", "source": "coco2017", "initial_model": "yolo.pt"},
        )
        job = self._create(parameters={
            "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z",
            "training_job_id": "training-run-42", "dataset_id": "idd-lite", "model_id": "best.pt",
        })
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        content = reports.report_file_path(self.output, result).read_text(encoding="utf-8-sig")
        self.assertIn("epoch completed", content)
        self.assertNotIn("different dataset/model", content)

    def test_event_report_rejects_oversized_training_identifier_filters(self):
        for field in ("training_job_id", "dataset_id", "model_id"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "exceeds its length limit"):
                self._create(parameters={
                    "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z", field: "x" * 121,
                })

    def test_csv_formula_injection_is_neutralized(self):
        payload = reports._render_csv(
            ["negative_integer", "negative_float", "negative_text", "message"],
            [[-3, -0.25, "-3", "  =HYPERLINK('https://invalid')"]],
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows[1], ["-3", "-0.25", "'-3", "'  =HYPERLINK('https://invalid')"])

    def test_report_catalogue_covers_requested_observability_domains(self):
        available = {item["report_type"]: item for item in reports.report_types()}
        self.assertTrue({"training", "datasets", "models", "inference", "cameras", "tracking-events", "hardware-energy", "api-activity", "mcp-activity"}.issubset(available))
        self.assertEqual(len(available), 14)
        self.assertNotIn("model-evaluations", available)
        self.assertTrue(all(item["formats"] == ["csv", "pdf"] and item["coverage"] and item["source"] for item in available.values()))
        with self.assertRaisesRegex(ValueError, "Unsupported report type"):
            self._create("model-evaluations")

    def test_every_registered_report_generates_csv_and_pdf(self):
        for definition in reports.report_types():
            report_type = definition["report_type"]
            parameters = {} if report_type == "lifetime-statistics" else {
                "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z",
            }
            if report_type == "metric-series":
                parameters["metric"] = "training.box_loss"
            for output_format in definition["formats"]:
                with self.subTest(report_type=report_type, output_format=output_format):
                    job = self._create(report_type, output_format, parameters)
                    result = reports.process_report_job(self.database, self.output, job["job_id"])
                    self.assertEqual(result["status"], "complete")
                    self.assertEqual(result["progress_percent"], 100)
                    report_path = reports.report_file_path(self.output, result)
                    self.assertIsNotNone(report_path)
                    content = report_path.read_bytes()
                    self.assertGreater(len(content), 100)
                    if output_format == "csv":
                        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
                    else:
                        self.assertTrue(content.startswith(b"%PDF-1.4"))

    def test_empty_model_report_is_explicit_and_csv_keeps_provenance(self):
        job = self._create("models")
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        self.assertEqual(result["status"], "complete")
        content = reports.report_file_path(self.output, result).read_text(encoding="utf-8-sig")
        self.assertIn("record_kind", content.splitlines()[0])
        self.assertIn("No matching source records were recorded", content)
        self.assertIn(job["job_id"], content)
        self.assertIn("model event records", content)

    def test_camera_inventory_report_includes_latest_status_but_never_private_token(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript(
                """
                CREATE TABLE cameras (
                    token TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, source_type TEXT NOT NULL DEFAULT 'camera'
                );
                CREATE TABLE camera_status (
                    camera_token TEXT PRIMARY KEY, last_frame_at TEXT NOT NULL,
                    faces_detected INTEGER NOT NULL DEFAULT 0,
                    matched_count INTEGER NOT NULL DEFAULT 0,
                    objects_json TEXT NOT NULL DEFAULT '[]', error TEXT
                );
                """
            )
            db.execute(
                "INSERT INTO cameras(token,name,created_at,active,source_type) VALUES(?,?,?,?,?)",
                ("qa-private-camera-token", "QA Entry Camera", "2026-11-12T10:00:00Z", 1, "rtsp"),
            )
            db.execute(
                "INSERT INTO camera_status(camera_token,last_frame_at,faces_detected,matched_count,objects_json,error) VALUES(?,?,?,?,?,?)",
                ("qa-private-camera-token", "2026-11-12T10:05:00Z", 2, 1, "[]", None),
            )
            db.commit()

        job = self._create("cameras", parameters={
            "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z",
            "search": "entry",
        })
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        self.assertEqual(result["status"], "complete")
        with reports.report_file_path(self.output, result).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_type"], "camera-inventory")
        self.assertEqual(json.loads(rows[0]["dimensions_json"]), {"name": "QA Entry Camera", "source_type": "rtsp"})
        self.assertEqual(
            json.loads(rows[0]["details_json"]),
            {"last_frame_at": "2026-11-12T10:05:00Z", "faces_detected": 2, "matched_count": 1},
        )
        self.assertNotIn("qa-private-camera-token", reports.report_file_path(self.output, result).read_text(encoding="utf-8-sig"))

    def test_review_sample_report_exports_aggregates_without_image_bytes(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE ml_samples (created_at TEXT, suggested_label TEXT, confirmed_label TEXT, review_state TEXT, image BLOB)")
            db.execute("INSERT INTO ml_samples VALUES(?,?,?,?,?)", ("2026-11-12T10:00:00Z", "truck", "lorry", "approved", b"PRIVATE_IMAGE_BYTES"))
            db.commit()
        record_event(
            self.database, event_id="dataset-success-event", timestamp="2026-11-12T10:05:00Z",
            event_type="dataset.validation_succeeded", component="datasets", level="INFO",
            message="Dataset preparation completed (uvh26).",
            details={"dataset_kind": "uvh26", "prepared_count": 300, "train_count": 240, "validation_count": 60, "class_count": 26, "box_count": 780},
            counter_deltas={"datasets.preparations_completed": 1, "datasets.images_prepared": 300, "datasets.annotations_validated": 780},
            counter_dimensions={
                "datasets.preparations_completed": {"dataset_kind": "uvh26"},
                "datasets.images_prepared": {"dataset_kind": "uvh26"},
                "datasets.annotations_validated": {"dataset_kind": "uvh26"},
            },
        )
        job = self._create("datasets", parameters={"after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z"})
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        content = reports.report_file_path(self.output, result).read_bytes()
        self.assertIn(b"lorry", content)
        self.assertIn(b"dataset-preparation", content)
        self.assertIn(b"uvh26", content)
        self.assertIn(b"780", content)
        self.assertIn(b"image data excluded", content)
        self.assertNotIn(b"PRIVATE_IMAGE_BYTES", content)

    def test_api_activity_report_includes_safe_counter_dimensions(self):
        record_event(
            self.database, event_id="api-dimension-event", timestamp="2026-11-12T10:10:00Z",
            event_type="api.request_batch", component="api", level="INFO", message="aggregated",
            details={"request_count": 3}, counter_deltas={"api.requests": 3},
            counter_dimensions={"api.requests": {"route": "/api/v1/status", "method": "GET", "status": 200}},
        )
        job = self._create("api-activity")
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        content = reports.report_file_path(self.output, result).read_text(encoding="utf-8-sig")
        self.assertIn("/api/v1/status", content)
        self.assertIn("GET", content)
        self.assertIn("200", content)

    def test_mcp_activity_report_includes_tool_name_outcome_and_duration_without_payloads(self):
        record_event(
            self.database, event_id="mcp-tool-event", timestamp="2026-11-12T10:20:00Z",
            event_type="mcp.tool_invoked", component="mcp", level="INFO",
            message="MCP bridge tool completed.",
            details={"tool": "vaos_get_system_health", "outcome": "success", "duration_ms": 12.5},
            counter_deltas={"mcp.tool_invocations": 1},
            counter_dimensions={"mcp.tool_invocations": {"tool": "vaos_get_system_health", "outcome": "success"}},
            metric_observations=[{
                "metric_key": "mcp.tool_duration_ms", "value": 12.5,
                "dimensions": {"tool": "vaos_get_system_health", "outcome": "success"},
                "provenance": "test", "measurement_scope": "MCP tool execution", "estimated": False,
            }],
        )
        record_security_event(
            self.database, event_id="mcp-audit-event", event_type="mcp.approval_decided",
            outcome="approved", component="mcp", timestamp="2026-11-12T10:19:00Z",
        )
        job = self._create("mcp-activity")
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        content = reports.report_file_path(self.output, result).read_text(encoding="utf-8-sig")
        self.assertIn("vaos_get_system_health", content)
        self.assertIn("12.5", content)
        self.assertIn("mcp.approval_decided", content)
        records = list(csv.DictReader(io.StringIO(content)))
        invocation = next(item for item in records if item["event_type"] == "mcp.tool_invoked")
        details = json.loads(invocation["details_json"])
        self.assertEqual(set(details), {"tool", "outcome", "duration_ms"})
        self.assertNotIn("secret-marker", content)

    def test_row_limit_fails_instead_of_marking_a_partial_report_complete(self):
        job = self._create()
        with patch.object(reports, "_report_rows", return_value=(["column"], [["row"]] * (reports.MAX_REPORT_ROWS + 1))):
            result = reports.process_report_job(self.database, self.output, job["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "report_limit_exceeded")

    def test_orphan_cleanup_only_removes_generated_job_artifacts(self):
        job = self._create()
        completed = reports.process_report_job(self.database, self.output, job["job_id"])
        orphan = self.output / ("a" * 32 + ".pdf")
        partial = self.output / ("b" * 32 + ".csv.part")
        user_file = self.output / "notes.csv"
        orphan.write_bytes(b"orphan")
        partial.write_bytes(b"partial")
        user_file.write_text("keep", encoding="utf-8")
        removed = reports.cleanup_orphan_report_artifacts(self.database, self.output)
        self.assertEqual(removed, 2)
        self.assertIsNotNone(reports.report_file_path(self.output, completed))
        self.assertTrue(user_file.exists())

    def test_overlong_filter_is_rejected_without_silent_truncation(self):
        with self.assertRaisesRegex(ValueError, "search exceeds"):
            self._create("events", parameters={"after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z", "search": "x" * 201})

    def test_pdf_has_valid_xref_offsets_and_fails_integrity_after_tamper(self):
        job = self._create(output_format="pdf")
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        path = reports.report_file_path(self.output, result)
        self.assertIsNotNone(path)
        content = path.read_bytes()
        self.assertTrue(content.startswith(b"%PDF-1.4"))
        marker = b"startxref\n"
        offset = int(content.split(marker, 1)[1].splitlines()[0])
        self.assertEqual(content[offset:offset + 5], b"xref\n")
        path.write_bytes(content + b"tampered")
        self.assertIsNone(reports.report_file_path(self.output, result))

    def test_pdf_uses_summary_cards_and_paginated_pages_for_large_reports(self):
        content = reports._render_pdf(
            "VAOS Observability - Fixture report",
            ["timestamp_utc", "message"],
            [[f"2026-11-12T10:{index % 60:02d}:00Z", f"Synthetic event {index} " + ("detail " * 30)] for index in range(150)],
            metadata=["Report ID: qa-report", "Generated UTC: 2026-11-12T11:00:00Z", "Rows included: 150", "Applied filters: component=qa"],
            summary_cards=[("Rows included", "150"), ("Period", "Synthetic QA"), ("Source", "Test fixture"), ("Coverage", "Synthetic records only")],
        )
        self.assertGreater(content.count(b"/Type /Page /Parent"), 1)
        self.assertIn("Page 2 of".encode("cp1252").hex().upper().encode("ascii"), content)
        self.assertIn("Timestamp (UTC)".encode("cp1252").hex().upper().encode("ascii"), content)
        self.assertIn("Rows included".encode("cp1252").hex().upper().encode("ascii"), content)

    def test_empty_pdf_report_counts_zero_source_rows_and_keeps_empty_state(self):
        job = self._create("events", "pdf", parameters={
            "after": "2026-11-14T00:00:00Z", "before": "2026-11-15T00:00:00Z",
        })
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        self.assertEqual(result["status"], "complete")
        content = reports.report_file_path(self.output, result).read_bytes()
        text = _pdf_text(content)
        self.assertIn("Rows included\n0", text)
        self.assertIn("No matching records in the selected range", text)

    def test_metric_series_report_exports_only_the_selected_metric(self):
        job = self._create("metric-series", parameters={
            "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z", "metric": "training.box_loss",
        })
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        self.assertEqual(result["status"], "complete")
        content = reports.report_file_path(self.output, result).read_text(encoding="utf-8-sig")
        self.assertIn("training.box_loss", content)
        self.assertIn("0.42", content)

    def test_metric_series_report_exports_the_selected_group_and_aggregation(self):
        job = self._create("metric-series", parameters={
            "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z",
            "metric": "training.box_loss", "aggregation": "average", "interval": "hour", "group_by": "epoch",
        })
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        self.assertEqual(result["status"], "complete")
        with reports.report_file_path(self.output, result).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["aggregation"], "average")
        self.assertEqual(rows[0]["interval"], "hour")
        self.assertEqual(rows[0]["record_count"], "1")
        self.assertEqual(json.loads(rows[0]["dimensions_json"]), {"epoch": "1"})

    def test_lifetime_security_and_lifecycle_report_types_use_their_source_tables(self):
        lifetime = self._create("lifetime-statistics", parameters={})
        lifetime_result = reports.process_report_job(self.database, self.output, lifetime["job_id"])
        lifetime_csv = reports.report_file_path(self.output, lifetime_result).read_text(encoding="utf-8-sig")
        self.assertIn("training_epochs_completed", lifetime_csv)
        record_security_event(self.database, event_id="report-audit-1", event_type="auth.login_failed", outcome="denied", component="auth", timestamp="2026-11-12T10:00:00Z")
        security = self._create("security-events", parameters={"after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z"})
        security_result = reports.process_report_job(self.database, self.output, security["job_id"])
        security_csv = reports.report_file_path(self.output, security_result).read_text(encoding="utf-8-sig")
        self.assertIn("auth.login_failed", security_csv)
        start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-12T11:00:00Z", process_id=812)
        lifecycle = self._create("lifecycle")
        lifecycle_result = reports.process_report_job(self.database, self.output, lifecycle["job_id"])
        lifecycle_csv = reports.report_file_path(self.output, lifecycle_result).read_text(encoding="utf-8-sig")
        self.assertIn("admin,1.1.0,2026-11-12T11:00:00", lifecycle_csv)

    def test_lifetime_report_uses_human_readable_metric_labels_and_title(self):
        csv_job = self._create("lifetime-statistics", "csv", parameters={})
        csv_result = reports.process_report_job(self.database, self.output, csv_job["job_id"])
        with reports.report_file_path(self.output, csv_result).open(encoding="utf-8-sig", newline="") as stream:
            rows = {row["metric_key"]: row for row in csv.DictReader(stream)}
        self.assertEqual(rows["application_starts"]["display_name"], "Application starts")
        self.assertEqual(rows["energy_wh"]["display_name"], "Estimated GPU board energy")

        pdf_job = self._create("lifetime-statistics", "pdf", parameters={})
        pdf_result = reports.process_report_job(self.database, self.output, pdf_job["job_id"])
        pdf_content = reports.report_file_path(self.output, pdf_result).read_bytes()
        self.assertIn("VAOS Observability - Installation lifetime statistics".encode("cp1252").hex().upper().encode("ascii"), pdf_content)
        self.assertIn("Application starts".encode("cp1252").hex().upper().encode("ascii"), pdf_content)
        self.assertNotIn("VAOS Observability - lifetime-statistics".encode("cp1252").hex().upper().encode("ascii"), pdf_content)
        extracted = _pdf_text(pdf_content)
        self.assertIn("VAOS Observability - Installation lifetime statistics", extracted)
        self.assertIn("Available", extracted)
        self.assertIn("Unavailable", extracted)
        self.assertIn("Not recorded\njobs\nUnavailable", extracted)
        self.assertNotIn("Not recorded | jobs | Unavailable", extracted)

    def test_wide_pdf_report_splits_columns_into_labeled_sections(self):
        header = [
            "timestamp_utc", "event_type", "component", "level", "outcome",
            "actor_ref", "correlation_id", "message", "details_json",
        ]
        groups = reports._pdf_column_groups(header, 720)
        self.assertGreater(len(groups), 1)

        content = reports._render_pdf(
            "VAOS Observability - Wide report",
            header,
            [["2026-11-12T10:00:00Z", "training.epoch_completed", "training", "INFO", "ok", "operator", "run-123", "Synthetic row", '{"epoch":1}']],
            metadata=["Report ID: qa-wide", "Generated UTC: 2026-11-12T11:00:00Z"],
        )
        extracted = _pdf_text(content)
        self.assertIn(f"Table section 1 of {len(groups)}", extracted)
        self.assertIn(f"Table section {len(groups)} of {len(groups)}", extracted)
        for column in header:
            self.assertIn(reports._pdf_header_label(column), extracted)

    def test_wide_pdf_repeats_table_headers_across_paginated_sections(self):
        header = [
            "timestamp_utc", "event_type", "component", "level", "outcome",
            "actor_ref", "correlation_id", "message", "details_json",
        ]
        rows = [
            [
                f"2026-11-12T10:{index % 60:02d}:00Z", "training.epoch_completed",
                "training", "INFO", "ok", f"operator-{index % 4}", f"run-{index:03d}",
                f"Synthetic event {index}", f'{{"epoch":{index % 50 + 1}}}',
            ]
            for index in range(150)
        ]
        content = reports._render_pdf(
            "VAOS Observability - Wide paginated report", header, rows,
            metadata=["Report ID: qa-wide-pages", "Generated UTC: 2026-11-12T11:00:00Z"],
        )
        page_count = content.count(b"/Type /Page /Parent")
        groups = reports._pdf_column_groups(header, 720)
        self.assertGreater(page_count, len(groups))
        streams = re.findall(rb"stream\n(.*?)\nendstream", content, re.S)
        self.assertEqual(len(streams), page_count)
        for stream in streams:
            page_text = _pdf_text(stream)
            section_match = re.search(r"Table section (\d+) of (\d+)", page_text)
            self.assertIsNotNone(section_match)
            section_index = int(section_match.group(1)) - 1
            self.assertEqual(int(section_match.group(2)), len(groups))
            expected_headers = ["Record", *(reports._pdf_header_label(header[index]) for index in groups[section_index])]
            for label in expected_headers:
                self.assertIn(label, page_text)
            self.assertRegex(page_text, rf"Page \d+ of {page_count}")

    def test_pdf_continues_an_overheight_wrapped_row_on_a_new_page(self):
        header = ["value", "record_count", "unit", "level", "outcome", "epoch", "interval"]
        content = reports._render_pdf(
            "VAOS Observability - Long row",
            header,
            [["x" * 2600, "42", "rows", "INFO", "ok", "3", "hour"]],
            metadata=["Report ID: qa-long-row", "Generated UTC: 2026-11-12T11:00:00Z"],
        )
        extracted = _pdf_text(content)
        self.assertGreater(content.count(b"/Type /Page /Parent"), 1)
        self.assertIn("1 cont", extracted)
        self.assertIn("Page 2 of 2", extracted)
        self.assertIn("Omitted (section text limit)", extracted.replace("\n", " "))
        text_ops = re.findall(
            rb"BT /F[12] ([0-9.]+) Tf [^\n]*? 1 0 0 1 ([0-9.]+) ([0-9.]+) Tm <([0-9A-F]+)> Tj ET",
            content,
        )
        row_lines = []
        for size_raw, _x_raw, baseline_raw, encoded in text_ops:
            text = bytes.fromhex(encoded.decode("ascii")).decode("cp1252", errors="replace")
            if text and set(text) == {"x"}:
                size = float(size_raw)
                baseline = float(baseline_raw)
                top = 612 - baseline - size
                self.assertGreaterEqual(top, 36)
                self.assertLessEqual(top + size, 612 - 36)
                row_lines.append(text)
        self.assertEqual(sum(map(len, row_lines)), 1197)

    def test_wide_pdf_text_limit_is_per_section_and_marks_omitted_values(self):
        header = [
            "timestamp_utc", "event_type", "component", "level", "outcome",
            "actor_ref", "correlation_id", "message", "details_json",
        ]
        content = reports._render_pdf(
            "VAOS Observability - Section text limits",
            header,
            [["2026-11-12T10:00:00Z", "x" * 2600, "training", "INFO", "ok", "operator", "run-1", "Visible later-section message", '{"epoch":1}']],
            metadata=["Report ID: qa-section-limits", "Generated UTC: 2026-11-12T11:00:00Z"],
        )
        extracted = _pdf_text(content)
        self.assertIn("Omitted (section text limit)", extracted)
        self.assertIn("Visible later-section message", extracted)
        self.assertIn('{"epoch":1}', extracted)

    def test_security_report_filters_by_exact_actor_reference(self):
        record_security_event(
            self.database, event_id="report-actor-target", event_type="auth.login_failed", outcome="denied",
            component="auth", actor_ref="api-token:abc", timestamp="2026-11-12T10:01:00Z",
        )
        record_security_event(
            self.database, event_id="report-actor-neighbor", event_type="auth.login_failed", outcome="denied",
            component="auth", actor_ref="api-token:abcd", timestamp="2026-11-12T10:02:00Z",
        )
        job = self._create("security-events", parameters={
            "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z", "actor_ref": "api-token:abc",
        })
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        content = reports.report_file_path(self.output, result).read_text(encoding="utf-8-sig")
        self.assertIn("api-token:abc", content)
        self.assertNotIn("api-token:abcd", content)
        with self.assertRaisesRegex(ValueError, "actor_ref exceeds its length limit"):
            self._create("security-events", parameters={
                "after": "2026-11-12T00:00:00Z", "before": "2026-11-13T00:00:00Z", "actor_ref": "a" * 121,
            })

    def test_cancellation_and_interrupted_job_recovery_are_explicit(self):
        queued = self._create()
        self.assertTrue(reports.cancel_report_job(self.database, queued["job_id"], requested_by="admin"))
        self.assertEqual(reports.get_report_job(self.database, queued["job_id"])["status"], "cancelled")
        interrupted = self._create()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE observability_report_jobs SET status='running' WHERE job_id=?", (interrupted["job_id"],))
            db.commit()
        self.assertEqual(reports.recover_report_jobs(self.database), [])
        recovered = reports.get_report_job(self.database, interrupted["job_id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error_code"], "process_interrupted")

    def test_queued_job_cap_and_range_validation(self):
        for _ in range(reports.MAX_ACTIVE_JOBS_PER_ACTOR):
            self._create()
        with self.assertRaisesRegex(ValueError, "five reports"):
            self._create()
        with self.assertRaisesRegex(ValueError, "ordered"):
            self._create(parameters={"after": "2026-11-13T00:00:00Z", "before": "2026-11-12T00:00:00Z"})

    def test_expiry_deletes_only_generated_files_and_marks_expired(self):
        requested_at = datetime.now(timezone.utc).replace(microsecond=0)
        job = self._create(now=requested_at.isoformat(timespec="seconds").replace("+00:00", "Z"))
        result = reports.process_report_job(self.database, self.output, job["job_id"])
        unrelated = self.output / "keep-me.csv"
        unrelated.write_text("user file", encoding="utf-8")
        expired_at = (requested_at + timedelta(hours=24, seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        expired = reports.expire_reports(self.database, self.output, now=expired_at)
        self.assertEqual(expired, 1)
        self.assertEqual(reports.get_report_job(self.database, job["job_id"])["status"], "expired")
        self.assertTrue(unrelated.exists())
        self.assertIsNone(reports.report_file_path(self.output, result))


if __name__ == "__main__":
    unittest.main()
