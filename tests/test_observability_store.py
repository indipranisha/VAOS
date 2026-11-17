"""Isolated tests for observability schema, lifecycle, privacy, and counters."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import observability_siem as siem
import observability_store as store

from observability_store import (
    METRIC_CATALOGUE,
    end_session,
    heartbeat_session,
    initialize_database,
    list_metrics,
    list_sessions,
    overview,
    query_events,
    query_security_audit,
    count_metric_series,
    record_event,
    record_metric,
    record_gpu_power_samples,
    list_saved_log_filters,
    save_log_filter,
    delete_saved_log_filter,
    apply_retention,
    buffer_counter_delta,
    flush_counter_buffer,
    query_metric_series,
    record_security_event,
    start_session,
    verify_security_chain,
)


def _write_counter_batch_from_process(db_path: str, worker_id: int, ready, start, result) -> None:
    """Exercise the durable counter upsert from an independent process."""
    try:
        ready.put(worker_id)
        if not start.wait(10):
            raise TimeoutError("counter writer start gate timed out")
        for _ in range(30):
            buffer_counter_delta(
                db_path,
                metric_key="api.requests",
                dimensions={"route": "/api/v1/status", "method": "GET", "status": 200},
                timestamp="2026-11-12T10:00:03Z",
            )
        result.put((worker_id, "ok", ""))
    except Exception as exc:  # reported back to the parent test process
        result.put((worker_id, "error", f"{type(exc).__name__}: {exc}"))


def _write_fallback_counter_from_process(db_path: str, worker_id: int, ready, start, result) -> None:
    """Exercise cross-process writes to the private observability sidecar."""
    try:
        ready.put(worker_id)
        if not start.wait(10):
            raise TimeoutError("fallback counter writer start gate timed out")
        for _ in range(20):
            store.buffer_counter_fallback(
                db_path,
                metric_key="api.requests",
                dimensions={"route": "/api/v1/status", "method": "GET", "status": 200},
                timestamp="2026-11-13T10:00:03Z",
            )
        result.put((worker_id, "ok", ""))
    except Exception as exc:  # reported back to the parent test process
        result.put((worker_id, "error", f"{type(exc).__name__}: {exc}"))


def _write_security_fallback_from_process(db_path: str, worker_id: int, ready, start, result) -> None:
    """Exercise concurrent auth-event sidecar initialization and writes across processes."""
    try:
        ready.put(worker_id)
        if not start.wait(10):
            raise TimeoutError("security fallback writer start gate timed out")
        for index in range(8):
            siem.queue_security_event_fallback(
                db_path,
                event_type="authz.request_denied",
                outcome="denied",
                component="authorization",
                actor_ref="unauthenticated-client",
                details={"http_status": 401, "route": f"/api/admin/test-{worker_id}"},
                correlation_id=None,
                event_id=f"{worker_id + 1:02x}{index:030x}",
                timestamp="2026-11-14T01:00:00Z",
            )
        result.put((worker_id, "ok", ""))
    except Exception as exc:  # reported back to the parent test process
        result.put((worker_id, "error", f"{type(exc).__name__}: {exc}"))


class ObservabilityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vaos-observability-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "app.db"

    def tearDown(self):
        self.temporary.cleanup()

    def test_metrics_dictionary_matches_catalogue_and_provenance(self):
        self.assertEqual(
            len(store._METRICS),
            len(METRIC_CATALOGUE),
            "metric definitions must have unique keys so registry entries are not silently overwritten",
        )
        documentation = Path(__file__).resolve().parents[1] / "docs" / "observability" / "metrics-dictionary.md"
        rows = {}
        for line in documentation.read_text(encoding="utf-8").splitlines():
            if not line.startswith("| `"):
                continue
            columns = [part.strip().replace("`", "") for part in line.strip("|").split("|")]
            metric_key = columns[0]
            if metric_key in METRIC_CATALOGUE:
                rows[metric_key] = columns

        self.assertEqual(set(rows), set(METRIC_CATALOGUE))
        for metric_key, metric in METRIC_CATALOGUE.items():
            row = rows[metric_key]
            self.assertIn(f"{metric['unit']} / {metric['type']}".casefold(), row[1].casefold(), metric_key)
            dimensions = ", ".join(metric["dimensions"]) or "no dimensions"
            self.assertIn(dimensions.casefold(), row[3].casefold(), metric_key)
            self.assertIn(str(metric["retention"]).casefold(), row[5].casefold(), metric_key)
            self.assertIn(str(metric["privacy"]).casefold(), row[6].casefold(), metric_key)
            if metric["sampling_seconds"]:
                self.assertIn(f"{metric['sampling_seconds']} seconds", row[4].casefold(), metric_key)
        self.assertEqual(
            {key for key, metric in METRIC_CATALOGUE.items() if metric["estimated"]},
            {"hardware.gpu_energy_interval_wh", "hardware.gpu_energy_wh"},
        )

    def test_gpu_power_energy_integrates_covered_samples_and_skips_gaps(self):
        initialize_database(self.database)
        device_id = "a" * 32
        start = "2026-11-13T00:00:00Z"
        baseline = record_gpu_power_samples(self.database, samples=[{"device_id": device_id, "model_id": "Fixture GPU", "power_draw_w": 80}], timestamp=start)
        self.assertEqual((baseline["power_samples"], baseline["energy_intervals"]), (1, 0))
        self.assertFalse(overview(self.database)["kpis"]["energy_wh"]["available"])
        measured_power = query_metric_series(self.database, metric_key="hardware.gpu_power_draw_w", after=start, before="2026-11-13T00:00:01Z")
        self.assertEqual([item["value"] for item in measured_power], [80])
        self.assertFalse(measured_power[0]["estimated"])

        first = record_gpu_power_samples(self.database, samples=[{"device_id": device_id, "model_id": "Fixture GPU", "power_draw_w": 100}], timestamp="2026-11-13T00:00:30Z")
        self.assertEqual(first["energy_intervals"], 1)
        second = record_gpu_power_samples(self.database, samples=[{"device_id": device_id, "model_id": "Fixture GPU", "power_draw_w": 120}], timestamp="2026-11-13T00:01:00Z")
        self.assertEqual(second["energy_intervals"], 1)
        exact_duplicate = record_gpu_power_samples(self.database, samples=[{"device_id": device_id, "model_id": "Fixture GPU", "power_draw_w": 120}], timestamp="2026-11-13T00:01:00Z")
        self.assertEqual(exact_duplicate["ignored_samples"], 1)
        too_soon = record_gpu_power_samples(self.database, samples=[{"device_id": device_id, "model_id": "Fixture GPU", "power_draw_w": 999}], timestamp="2026-11-13T00:01:10Z")
        self.assertEqual(too_soon["ignored_samples"], 1)

        long_gap = record_gpu_power_samples(self.database, samples=[{"device_id": device_id, "model_id": "Fixture GPU", "power_draw_w": 120}], timestamp="2026-11-13T00:03:20Z")
        self.assertEqual(long_gap["energy_gaps"], 1)
        after_gap = record_gpu_power_samples(self.database, samples=[{"device_id": device_id, "model_id": "Fixture GPU", "power_draw_w": 180}], timestamp="2026-11-13T00:03:50Z")
        self.assertEqual(after_gap["energy_intervals"], 1)

        lifetime = overview(self.database)["kpis"]["energy_wh"]
        expected_total = ((80 + 100) / 2 * 30 + (100 + 120) / 2 * 30 + (120 + 180) / 2 * 30) / 3600
        self.assertTrue(lifetime["available"])
        self.assertTrue(lifetime["estimated"])
        self.assertAlmostEqual(lifetime["value"], expected_total, places=8)
        self.assertEqual(lifetime["coverage_seconds"], 90)
        self.assertEqual(lifetime["skipped_gap_count"], 1)
        start_range, end_range = "2026-11-13T00:00:00Z", "2026-11-14T00:00:00Z"
        interval_series = query_metric_series(self.database, metric_key="hardware.gpu_energy_interval_wh", after=start_range, before=end_range)
        self.assertEqual(len(interval_series), 3)
        self.assertTrue(all(item["estimated"] for item in interval_series))
        period_energy = overview(self.database, period="custom", after=start_range, before=end_range)["kpis"]["energy_wh"]
        self.assertAlmostEqual(period_energy["value"], expected_total, places=8)
        self.assertEqual(period_energy["skipped_gap_count"], 1)
        whole_wh = query_metric_series(self.database, metric_key="hardware.gpu_energy_wh", after=start_range, before=end_range)
        self.assertEqual([item["value"] for item in whole_wh], [1, 1])
        self.assertTrue(all(item["estimated"] for item in whole_wh))

        invalid = record_gpu_power_samples(self.database, samples=[{"device_id": device_id, "model_id": "Fixture GPU", "power_draw_w": float("nan")}], timestamp="2026-11-13T00:04:20Z")
        self.assertEqual(invalid["ignored_samples"], 1)

    def test_gpu_power_schema_upgrade_preserves_existing_event_history(self):
        initialize_database(self.database)
        start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-13T00:00:00Z", process_id=1234)
        record_event(self.database, event_id="pre-energy-event", timestamp="2026-11-13T00:00:00Z", event_type="api.request_batch", component="api", message="Preserve existing event.")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP TABLE observability_gpu_energy_state")
            db.execute("DELETE FROM observability_metric_catalogue WHERE metric_key IN ('hardware.gpu_power_draw_w','hardware.gpu_energy_interval_wh')")
            db.execute("DELETE FROM vaos_schema_migrations WHERE version>8")
            db.execute("UPDATE observability_installation SET schema_version=8 WHERE singleton_id=1")
            db.commit()
        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], store.SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT schema_version FROM observability_installation WHERE singleton_id=1").fetchone()[0], store.SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT message FROM observability_events WHERE event_id='pre-energy-event'").fetchone()[0], "Preserve existing event.")
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observability_gpu_energy_state'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_metric_catalogue WHERE metric_key IN ('hardware.gpu_power_draw_w','hardware.gpu_energy_interval_wh')").fetchone()[0], 2)

    def test_v9_upgrade_adds_default_retention_policy_without_losing_events(self):
        initialize_database(self.database)
        start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-13T00:00:00Z", process_id=9876)
        record_event(self.database, event_id="pre-retention-policy", event_type="observability.fixture", component="test", message="Preserve this event.")
        store.set_retention_policy(
            self.database, event_details_days=180, metric_observations_max_days=365,
            delivery_history_days=180, confirm_shorter=False,
        )
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP TABLE observability_retention_policy")
            db.execute("DELETE FROM vaos_schema_migrations WHERE version>9")
            db.execute("UPDATE observability_installation SET schema_version=9 WHERE singleton_id=1")
            db.commit()
        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        self.assertEqual(store.get_retention_policy(self.database)["event_details_days"], 90)
        self.assertEqual(store.get_retention_policy(self.database)["metric_observations_max_days"], 365)
        self.assertEqual(store.get_retention_policy(self.database)["delivery_history_days"], 90)
        self.assertEqual(query_events(self.database, event_id="pre-retention-policy")[0]["message"], "Preserve this event.")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], store.SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT schema_version FROM observability_installation WHERE singleton_id=1").fetchone()[0], store.SCHEMA_VERSION)

    def test_retention_policy_and_events_survive_process_restart(self):
        repository = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(repository), environment.get("PYTHONPATH", "")) if part
        )
        write_state = (
            "import sys\n"
            "import observability_store as store\n"
            "path = sys.argv[1]\n"
            "store.initialize_database(path)\n"
            "store.record_event(path, event_id='restart-retention-event', "
            "event_type='observability.restart_fixture', component='qa', message='preserve-me')\n"
            "store.set_retention_policy(path, event_details_days=180, "
            "metric_observations_max_days=365, delivery_history_days=365, confirm_shorter=False)\n"
        )
        written = subprocess.run(
            [sys.executable, "-c", write_state, str(self.database)],
            check=True, capture_output=True, text=True, env=environment, timeout=30,
        )
        self.assertEqual(written.returncode, 0)

        read_state = (
            "import json, sys\n"
            "import observability_store as store\n"
            "path = sys.argv[1]\n"
            "store.initialize_database(path)\n"
            "print(json.dumps({'policy': store.get_retention_policy(path), "
            "'event': store.query_events(path, event_id='restart-retention-event')[0]['message']}, sort_keys=True))\n"
        )
        reopened = subprocess.run(
            [sys.executable, "-c", read_state, str(self.database)],
            check=True, capture_output=True, text=True, env=environment, timeout=30,
        )
        state = json.loads(reopened.stdout)
        self.assertEqual(state["event"], "preserve-me")
        self.assertEqual(
            {key: state["policy"][key] for key in (
                "delivery_history_days", "event_details_days", "metric_observations_max_days",
            )},
            {
                "delivery_history_days": 365,
                "event_details_days": 180,
                "metric_observations_max_days": 365,
            },
        )

    def test_migration_preserves_original_data_in_verified_online_backup(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("BEGIN")
            db.execute("CREATE TABLE existing_user_data(value TEXT NOT NULL)")
            db.execute("INSERT INTO existing_user_data(value) VALUES('preserve-me')")
            db.commit()
        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        manifest = json.loads(backup.with_suffix(backup.suffix + ".manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["integrity_check"], "ok")
        self.assertEqual(manifest["size_bytes"], backup.stat().st_size)
        self.assertEqual(manifest["sha256"], hashlib.sha256(backup.read_bytes()).hexdigest())
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT value FROM existing_user_data").fetchone()[0], "preserve-me")
        with closing(sqlite3.connect(self.database)) as db:
            event = db.execute(
                "SELECT event_id,event_type,details_json FROM observability_events "
                "WHERE event_type='storage.schema_migration_backup_verified'"
            ).fetchone()
            self.assertIsNotNone(event)
            self.assertEqual(event[0], f"observability-migration-backup-v{store.SCHEMA_VERSION}")
            self.assertEqual(json.loads(event[2]), {
                "backup_bytes": backup.stat().st_size,
                "integrity_check": "ok",
                "schema_version": store.SCHEMA_VERSION,
            })
            self.assertEqual(
                db.execute(
                    "SELECT value FROM observability_lifetime_counters "
                    "WHERE metric_key='storage.schema_migration_backups'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT available FROM observability_metric_catalogue "
                    "WHERE metric_key='storage.schema_migration_backups'"
                ).fetchone()[0],
                1,
            )
        restored = self.root / "restored-before-migration.db"
        shutil.copy2(backup, restored)
        with closing(sqlite3.connect(f"file:{restored.as_posix()}?mode=ro", uri=True)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT value FROM existing_user_data").fetchone()[0], "preserve-me")
            self.assertIsNone(
                db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vaos_schema_migrations'").fetchone(),
                "the copied backup must represent the database before the observability migration",
            )
        self.assertIsNone(initialize_database(self.database), "a repeated startup must not make another backup")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM observability_events "
                    "WHERE event_type='storage.schema_migration_backup_verified'"
                ).fetchone()[0],
                1,
                "repeat initialization must not duplicate the migration backup event",
            )

    def test_fresh_database_does_not_claim_a_pre_migration_backup(self):
        initialize_database(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM observability_events "
                    "WHERE event_type='storage.schema_migration_backup_verified'"
                ).fetchone()[0],
                0,
            )
            self.assertIsNone(
                db.execute(
                    "SELECT value FROM observability_lifetime_counters "
                    "WHERE metric_key='storage.schema_migration_backups'"
                ).fetchone(),
            )

    def test_v11_upgrade_preserves_observability_history_and_audits_v12_backup(self):
        initialize_database(self.database)
        record_event(
            self.database,
            event_id="pre-v12-observability-event",
            event_type="observability.pre_v12_fixture",
            component="test",
            message="preserve this history",
        )
        with closing(sqlite3.connect(self.database)) as db:
            # Reconstruct the committed v11 state: v12 introduced no data tables,
            # only its registry entry and the migration-time event/counter.
            db.execute(
                "DELETE FROM observability_metric_catalogue "
                "WHERE metric_key='storage.schema_migration_backups'"
            )
            db.execute(
                "DELETE FROM vaos_schema_migrations WHERE version>?", (11,)
            )
            db.execute(
                "INSERT INTO vaos_schema_migrations(version,applied_at,backup_name) "
                "VALUES(11,'2026-11-12T00:00:00Z',NULL)"
            )
            db.execute("UPDATE observability_installation SET schema_version=11 WHERE singleton_id=1")
            db.commit()

        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        assert backup is not None
        manifest = json.loads(backup.with_suffix(backup.suffix + ".manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["integrity_check"], "ok")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(
                db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0],
                12,
            )
            self.assertEqual(
                db.execute(
                    "SELECT message FROM observability_events WHERE event_id='pre-v12-observability-event'"
                ).fetchone()[0],
                "preserve this history",
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM observability_events "
                    "WHERE event_type='storage.schema_migration_backup_verified'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT value FROM observability_lifetime_counters "
                    "WHERE metric_key='storage.schema_migration_backups'"
                ).fetchone()[0],
                1,
            )
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], 11)
            self.assertEqual(
                db.execute(
                    "SELECT message FROM observability_events WHERE event_id='pre-v12-observability-event'"
                ).fetchone()[0],
                "preserve this history",
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM observability_events "
                    "WHERE event_type='storage.schema_migration_backup_verified'"
                ).fetchone()[0],
                0,
            )

    def test_migration_backup_audit_write_failure_rolls_back_schema_upgrade(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE existing_user_data(value TEXT NOT NULL)")
            db.execute("INSERT INTO existing_user_data(value) VALUES('preserve-me')")
            db.commit()
        with patch.object(store, "_insert_event", side_effect=sqlite3.OperationalError("audit write failed")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "audit write failed"):
                initialize_database(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT value FROM existing_user_data").fetchone()[0], "preserve-me")
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observability_events'"
                ).fetchone(),
                "the schema and backup audit must commit together",
            )

    def test_nonempty_database_migration_fails_closed_if_backup_is_missing(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE existing_user_data(value TEXT NOT NULL)")
            db.execute("INSERT INTO existing_user_data(value) VALUES('preserve-me')")
            db.commit()
        with patch.object(store, "_verified_backup", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "without a verified backup"):
                initialize_database(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT value FROM existing_user_data").fetchone()[0], "preserve-me")
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observability_events'"
                ).fetchone(),
            )

    def test_representative_legacy_vaos_backup_restores_feature_records(self):
        """Exercise the additive migration backup with synthetic pre-feature VAOS data."""
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(
                """
                CREATE TABLE cameras (
                    token TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, map_x REAL NOT NULL DEFAULT 0.5,
                    map_y REAL NOT NULL DEFAULT 0.5, heading REAL NOT NULL DEFAULT 0,
                    source_type TEXT NOT NULL DEFAULT 'camera', source_ref TEXT
                );
                CREATE TABLE participants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    embedding TEXT NOT NULL, profile_photo BLOB, created_at TEXT NOT NULL
                );
                CREATE TABLE participant_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL,
                    photo BLOB NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(person_id) REFERENCES participants(id) ON DELETE CASCADE
                );
                CREATE TABLE sightings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                    camera_token TEXT NOT NULL, person_id INTEGER NOT NULL,
                    score REAL NOT NULL, kind TEXT NOT NULL,
                    FOREIGN KEY(camera_token) REFERENCES cameras(token),
                    FOREIGN KEY(person_id) REFERENCES participants(id) ON DELETE CASCADE
                );
                CREATE TABLE camera_status (
                    camera_token TEXT PRIMARY KEY, last_frame_at TEXT NOT NULL,
                    faces_detected INTEGER NOT NULL DEFAULT 0, matched_count INTEGER NOT NULL DEFAULT 0,
                    objects_json TEXT NOT NULL DEFAULT '[]', error TEXT,
                    FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE
                );
                CREATE TABLE object_sightings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                    camera_token TEXT NOT NULL, class_name TEXT NOT NULL, count INTEGER NOT NULL,
                    confidence REAL NOT NULL,
                    FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE
                );
                CREATE TABLE object_event_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, rule_type TEXT NOT NULL,
                    class_name TEXT NOT NULL, min_confidence REAL NOT NULL DEFAULT 0.35,
                    threshold_count INTEGER NOT NULL DEFAULT 1,
                    min_consecutive_frames INTEGER NOT NULL DEFAULT 3,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 60, dwell_seconds REAL,
                    camera_token TEXT,
                    region_json TEXT NOT NULL DEFAULT '{"x":0,"y":0,"width":1,"height":1}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE
                );
                CREATE TABLE object_rule_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                    camera_token TEXT NOT NULL, rule_id INTEGER, rule_name TEXT NOT NULL,
                    rule_type TEXT NOT NULL, class_name TEXT NOT NULL,
                    object_count INTEGER NOT NULL, message TEXT NOT NULL,
                    FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE,
                    FOREIGN KEY(rule_id) REFERENCES object_event_rules(id) ON DELETE SET NULL
                );
                CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE api_tokens (
                    token_hash TEXT PRIMARY KEY, token_prefix TEXT NOT NULL, label TEXT NOT NULL,
                    scopes TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT,
                    last_used_at TEXT, revoked_at TEXT
                );
                CREATE TABLE api_access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                    token_prefix TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL
                );
                CREATE TABLE mcp_approvals (
                    request_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
                    token_prefix TEXT NOT NULL, method TEXT NOT NULL, request_path TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL, preview TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','expired','used')),
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    decided_at TEXT, decided_by TEXT, decision_note TEXT NOT NULL DEFAULT '', used_at TEXT
                );
                CREATE TABLE ml_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    camera_token TEXT NOT NULL, suggested_label TEXT NOT NULL,
                    image BLOB NOT NULL, bbox TEXT NOT NULL DEFAULT '0.5,0.5,0.9,0.9',
                    confidence REAL NOT NULL DEFAULT 0, review_state TEXT NOT NULL DEFAULT 'pending',
                    source_time_seconds REAL, confirmed_label TEXT,
                    FOREIGN KEY(camera_token) REFERENCES cameras(token) ON DELETE CASCADE
                );
                CREATE INDEX idx_sightings_timestamp ON sightings(timestamp DESC);
                CREATE INDEX idx_ml_samples_review ON ml_samples(review_state, id DESC);
                """
            )
            db.execute("INSERT INTO cameras(token,name,created_at) VALUES(?,?,?)", ("synthetic-camera-token", "Fixture camera", "2026-11-12T10:00:00Z"))
            db.execute("INSERT INTO participants(id,name,embedding,profile_photo,created_at) VALUES(?,?,?,?,?)", (7, "Synthetic participant", "synthetic-vector", b"synthetic-profile-image", "2026-11-12T10:00:00Z"))
            db.execute("INSERT INTO participant_photos(person_id,photo,created_at) VALUES(?,?,?)", (7, b"synthetic-enrollment-photo", "2026-11-12T10:01:00Z"))
            db.execute("INSERT INTO sightings(timestamp,camera_token,person_id,score,kind) VALUES(?,?,?,?,?)", ("2026-11-12T10:02:00Z", "synthetic-camera-token", 7, 0.91, "match"))
            db.execute("INSERT INTO camera_status(camera_token,last_frame_at,faces_detected,matched_count,objects_json,error) VALUES(?,?,?,?,?,?)", ("synthetic-camera-token", "2026-11-12T10:02:00Z", 1, 1, '[{"class":"truck","count":1}]', None))
            db.execute("INSERT INTO object_sightings(timestamp,camera_token,class_name,count,confidence) VALUES(?,?,?,?,?)", ("2026-11-12T10:02:00Z", "synthetic-camera-token", "truck", 1, 0.88))
            db.execute("INSERT INTO object_event_rules(id,name,rule_type,class_name,camera_token,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (3, "Synthetic truck rule", "count", "truck", "synthetic-camera-token", "2026-11-12T10:00:00Z", "2026-11-12T10:00:00Z"))
            db.execute("INSERT INTO object_rule_events(timestamp,camera_token,rule_id,rule_name,rule_type,class_name,object_count,message) VALUES(?,?,?,?,?,?,?,?)", ("2026-11-12T10:03:00Z", "synthetic-camera-token", 3, "Synthetic truck rule", "count", "truck", 1, "synthetic event"))
            db.execute("INSERT INTO app_settings(key,value) VALUES(?,?)", ("object_confidence", "0.25"))
            db.execute("INSERT INTO api_tokens(token_hash,token_prefix,label,scopes,created_at) VALUES(?,?,?,?,?)", ("synthetic-token-hash", "synthetic-prefix", "fixture read token", '["read"]', "2026-11-12T10:00:00Z"))
            db.execute("INSERT INTO api_access_log(timestamp,token_prefix,method,path) VALUES(?,?,?,?)", ("2026-11-12T10:04:00Z", "synthetic-prefix", "GET", "/api/v1/status"))
            db.execute("INSERT INTO mcp_approvals(request_id,token_hash,token_prefix,method,request_path,request_fingerprint,preview,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("synthetic-mcp-request", "synthetic-token-hash", "synthetic-prefix", "POST", "/api/admin/test", "synthetic-fingerprint", "synthetic preview", "pending", "2026-11-12T10:05:00Z", "2026-11-12T11:05:00Z"))
            db.execute("INSERT INTO ml_samples(id,created_at,camera_token,suggested_label,image,bbox,confidence,review_state,confirmed_label) VALUES(?,?,?,?,?,?,?,?,?)", (11, "2026-11-12T10:06:00Z", "synthetic-camera-token", "truck", b"synthetic-reviewed-image", "0.5,0.5,0.8,0.7", 0.89, "approved", "truck"))
            db.commit()

        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        assert backup is not None
        manifest = json.loads(backup.with_suffix(backup.suffix + ".manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["integrity_check"], "ok")
        self.assertEqual(manifest["size_bytes"], backup.stat().st_size)
        self.assertEqual(manifest["sha256"], hashlib.sha256(backup.read_bytes()).hexdigest())

        restored = self.root / "restored-representative-vaos.db"
        shutil.copy2(backup, restored)
        expected_counts = {
            "cameras": 1, "participants": 1, "participant_photos": 1, "sightings": 1,
            "camera_status": 1, "object_sightings": 1, "object_event_rules": 1,
            "object_rule_events": 1, "app_settings": 1, "api_tokens": 1,
            "api_access_log": 1, "mcp_approvals": 1, "ml_samples": 1,
        }
        for candidate in (backup, restored, self.database):
            with closing(sqlite3.connect(f"file:{candidate.as_posix()}?mode=ro", uri=True)) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                for table, count in expected_counts.items():
                    with self.subTest(database=candidate.name, table=table):
                        self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count)
                self.assertEqual(db.execute("SELECT image FROM ml_samples WHERE id=11").fetchone()[0], b"synthetic-reviewed-image")
                self.assertEqual(db.execute("SELECT value FROM app_settings WHERE key='object_confidence'").fetchone()[0], "0.25")
                self.assertEqual(db.execute("SELECT status FROM mcp_approvals WHERE request_id='synthetic-mcp-request'").fetchone()[0], "pending")

        with closing(sqlite3.connect(f"file:{restored.as_posix()}?mode=ro", uri=True)) as db:
            self.assertIsNone(
                db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vaos_schema_migrations'").fetchone(),
                "the separate recovery copy must remain a pre-migration database",
            )
        with closing(sqlite3.connect(f"file:{self.database.as_posix()}?mode=ro", uri=True)) as db:
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], store.SCHEMA_VERSION)

    def test_verified_migration_backup_uses_private_directory_and_file_permissions(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE private_fixture (value TEXT NOT NULL)")
            db.execute("INSERT INTO private_fixture VALUES ('sensitive')")
            db.commit()
        backup = store._verified_backup(self.database)
        self.assertIsNotNone(backup)
        assert backup is not None
        manifest = backup.with_suffix(backup.suffix + ".manifest.json")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        else:
            identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, timeout=5)
            import csv
            identity_row = next(row for row in csv.reader(identity.stdout.splitlines()) if len(row) >= 2 and row[-1].strip().startswith("S-1-"))
            account = identity_row[0].strip()
            for path in (backup.parent, backup, manifest):
                acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True, timeout=5).stdout
                self.assertIn(account.casefold(), acl.casefold())
                self.assertIn("SYSTEM", acl)

    def test_v2_upgrade_adds_event_expiry_and_updates_schema_version(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE vaos_schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL,backup_name TEXT)")
            db.execute("INSERT INTO vaos_schema_migrations(version,applied_at,backup_name) VALUES(2,'2026-01-01T00:00:00Z',NULL)")
            db.execute("CREATE TABLE observability_events(event_id TEXT PRIMARY KEY,timestamp TEXT NOT NULL,event_type TEXT NOT NULL,component TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL,session_id TEXT,correlation_id TEXT,details_json TEXT NOT NULL)")
            db.execute("INSERT INTO observability_events VALUES('legacy','2026-01-01T00:00:00Z','legacy.event','admin','INFO','preserve',NULL,NULL,'{}')")
            db.commit()
        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-12T00:00:00Z", process_id=123)
        with closing(sqlite3.connect(self.database)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(observability_events)")}
            self.assertIn("expires_at", columns)
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], store.SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT message FROM observability_events WHERE event_id='legacy'").fetchone()[0], "preserve")
            self.assertEqual(db.execute("SELECT schema_version FROM observability_installation WHERE singleton_id=1").fetchone()[0], store.SCHEMA_VERSION)
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observability_counter_buffer'").fetchone())
            self.assertIn("event_format", {row[1] for row in db.execute("PRAGMA table_info(observability_siem_destinations)")})

    def test_v4_upgrade_adds_default_siem_event_format_without_losing_destination(self):
        initialize_database(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("INSERT INTO observability_siem_destinations(destination_id,display_name,transport,endpoint,enabled,secret_ref,redaction_profile,event_format,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("a" * 32, "Legacy", "https_json", "https://siem.example.com/hook", 0, "VAOS_TEST_KEY", "default", "json", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            db.execute("ALTER TABLE observability_siem_destinations DROP COLUMN event_format")
            db.execute("DELETE FROM vaos_schema_migrations WHERE version>4")
            db.execute("UPDATE observability_installation SET schema_version=4 WHERE singleton_id=1")
            db.commit()
        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        with closing(sqlite3.connect(self.database)) as db:
            migrated = db.execute("SELECT display_name,event_format FROM observability_siem_destinations WHERE destination_id=?", ("a" * 32,)).fetchone()
            self.assertEqual((migrated[0], migrated[1]), ("Legacy", "json"))
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], store.SCHEMA_VERSION)

    def test_v5_upgrade_adds_soft_removal_and_delivery_capacity_fields(self):
        initialize_database(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("INSERT INTO observability_siem_destinations(destination_id,display_name,transport,endpoint,enabled,secret_ref,redaction_profile,event_format,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("b" * 32, "Legacy", "https_json", "https://siem.example.com/hook", 0, "VAOS_TEST_KEY", "default", "json", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            db.execute("ALTER TABLE observability_siem_destinations DROP COLUMN archived_at")
            db.execute("ALTER TABLE observability_siem_destinations DROP COLUMN dropped_events")
            db.execute("DELETE FROM vaos_schema_migrations WHERE version>5")
            db.execute("UPDATE observability_installation SET schema_version=5 WHERE singleton_id=1")
            db.commit()
        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        with closing(sqlite3.connect(self.database)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(observability_siem_destinations)")}
            self.assertIn("archived_at", columns)
            self.assertIn("dropped_events", columns)
            row = db.execute("SELECT display_name,endpoint,archived_at,dropped_events FROM observability_siem_destinations WHERE destination_id=?", ("b" * 32,)).fetchone()
            self.assertEqual(tuple(row), ("Legacy", "https://siem.example.com/hook", None, 0))
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], store.SCHEMA_VERSION)

    def test_v6_upgrade_adds_account_scoped_saved_log_filters(self):
        initialize_database(self.database)
        start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-12T00:00:00Z", process_id=456)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP TABLE observability_saved_log_filters")
            db.execute("DELETE FROM vaos_schema_migrations WHERE version>6")
            db.execute("UPDATE observability_installation SET schema_version=6 WHERE singleton_id=1")
            db.commit()
        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], store.SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT schema_version FROM observability_installation WHERE singleton_id=1").fetchone()[0], store.SCHEMA_VERSION)
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observability_saved_log_filters'").fetchone())

    def test_v7_upgrade_adds_actor_reference_audit_index_and_preserves_rows(self):
        initialize_database(self.database)
        record_security_event(
            self.database, event_id="v7-audit-row", event_type="auth.login_failed", outcome="denied",
            component="auth", actor_ref="api-token:fixture", timestamp="2026-11-12T00:00:00Z",
        )
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP INDEX idx_obs_security_actor_time")
            db.execute("DELETE FROM vaos_schema_migrations WHERE version>7")
            db.execute("UPDATE observability_installation SET schema_version=7 WHERE singleton_id=1")
            db.commit()
        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], store.SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT actor_ref FROM observability_security_audit WHERE event_id='v7-audit-row'").fetchone()[0], "api-token:fixture")
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_obs_security_actor_time'").fetchone())

    def test_saved_log_filters_are_private_to_owner_and_bounded(self):
        initialize_database(self.database)
        owner = "a" * 64
        other_owner = "b" * 64
        saved = save_log_filter(self.database, owner_hash=owner, name="Training errors", filters={"period": "7d", "level": "ERROR", "component": "training"})
        self.assertEqual(saved["filters"]["component"], "training")
        self.assertEqual([item["filter_id"] for item in list_saved_log_filters(self.database, owner_hash=owner)], [saved["filter_id"]])
        self.assertEqual(list_saved_log_filters(self.database, owner_hash=other_owner), [])
        with self.assertRaisesRegex(ValueError, "already exists"):
            save_log_filter(self.database, owner_hash=owner, name="training ERRORS", filters={"period": "24h"})
        self.assertFalse(delete_saved_log_filter(self.database, owner_hash=other_owner, filter_id=saved["filter_id"]))
        self.assertTrue(delete_saved_log_filter(self.database, owner_hash=owner, filter_id=saved["filter_id"]))
        self.assertEqual(list_saved_log_filters(self.database, owner_hash=owner), [])

        for index in range(20):
            save_log_filter(self.database, owner_hash=other_owner, name=f"Filter {index:02d}", filters={"period": "24h"})
        with self.assertRaisesRegex(ValueError, "up to 20"):
            save_log_filter(self.database, owner_hash=other_owner, name="Filter 20", filters={"period": "24h"})

    def test_counter_buffer_is_durable_compact_and_flushes_atomically(self):
        initialize_database(self.database)
        dimensions = {"route": "/api/v1/status", "method": "GET", "status": 200}
        buffer_counter_delta(self.database, metric_key="api.requests", dimensions=dimensions, timestamp="2026-11-12T10:00:03Z")
        buffer_counter_delta(self.database, metric_key="api.requests", dimensions=dimensions, timestamp="2026-11-12T10:00:08Z")
        buffer_counter_delta(self.database, metric_key="api.requests", dimensions=dimensions, timestamp="2026-11-12T10:00:12Z")
        with closing(sqlite3.connect(self.database)) as db:
            rows = db.execute("SELECT bucket_start,delta FROM observability_counter_buffer ORDER BY bucket_start").fetchall()
            self.assertEqual([(row[0], row[1]) for row in rows], [("2026-11-12T10:00:00Z", 2), ("2026-11-12T10:00:10Z", 1)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_counter_events").fetchone()[0], 0)

        # Re-opening the store represents a process restart before the worker flushes.
        self.assertIsNone(initialize_database(self.database))
        self.assertEqual(flush_counter_buffer(self.database, maximum=1), 2)
        self.assertEqual(flush_counter_buffer(self.database, maximum=10), 1)
        self.assertEqual(flush_counter_buffer(self.database, maximum=10), 0)
        series = query_metric_series(self.database, metric_key="api.requests", after="2026-11-12T10:00:00Z", before="2026-11-12T10:01:00Z", dimensions={"route": "/api/v1/status"})
        self.assertEqual([item["value"] for item in series], [2, 1])
        self.assertEqual(overview(self.database)["kpis"]["api_requests"]["value"], 3)

    def test_durable_counter_upsert_preserves_concurrent_process_writes(self):
        initialize_database(self.database)
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        result = context.Queue()
        start = context.Event()
        worker_count = 4
        increments_per_worker = 30
        workers = [
            context.Process(
                target=_write_counter_batch_from_process,
                args=(str(self.database), worker_id, ready, start, result),
            )
            for worker_id in range(worker_count)
        ]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                ready.get(timeout=15)
            start.set()
            outcomes = [result.get(timeout=20) for _ in workers]
            for worker in workers:
                worker.join(timeout=5)
            self.assertTrue(all(not worker.is_alive() for worker in workers), "counter writer process did not exit")
            self.assertTrue(all(worker.exitcode == 0 for worker in workers), [worker.exitcode for worker in workers])
            self.assertEqual([item for item in outcomes if item[1] != "ok"], [])

            with closing(sqlite3.connect(self.database)) as db:
                rows = db.execute("SELECT COUNT(*),SUM(delta) FROM observability_counter_buffer").fetchone()
            self.assertEqual(rows[0], 1, "same bucket and dimensions should compact into one durable row")
            self.assertEqual(rows[1], worker_count * increments_per_worker)
            self.assertEqual(flush_counter_buffer(self.database), worker_count * increments_per_worker)
            self.assertEqual(overview(self.database)["kpis"]["api_requests"]["value"], worker_count * increments_per_worker)
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            ready.close()
            result.close()
            ready.join_thread()
            result.join_thread()

    def test_failed_counter_buffer_flush_keeps_pending_data(self):
        initialize_database(self.database)
        buffer_counter_delta(self.database, metric_key="api.requests", dimensions={"route": "/api/v1/status", "method": "GET", "status": 200}, timestamp="2026-11-12T10:00:03Z")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE observability_counter_buffer SET delta=?", (2**53 + 1,))
            db.commit()
        with self.assertRaisesRegex(ValueError, "bounded"):
            flush_counter_buffer(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_counter_buffer").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_counter_events").fetchone()[0], 0)

    def test_fallback_counter_sidecar_survives_restart_and_ack_crash_without_double_counting(self):
        initialize_database(self.database)
        fallback = store.initialize_counter_fallback_store(self.database)
        dimensions = {"route": "/api/v1/status", "method": "GET", "status": 200}
        store.buffer_counter_fallback(self.database, metric_key="api.requests", dimensions=dimensions, timestamp="2026-11-13T10:00:03Z")
        store.buffer_counter_fallback(self.database, metric_key="api.requests", dimensions=dimensions, timestamp="2026-11-13T10:00:08Z")
        store.buffer_counter_fallback(self.database, metric_key="api.requests", dimensions=dimensions, timestamp="2026-11-13T10:00:12Z")
        self.assertTrue(store.counter_fallback_pending(self.database))

        # Reopening the sidecar models a process restart. Persist one primary
        # batch but deliberately skip its sidecar acknowledgement, then retry.
        self.assertEqual(store.stage_fallback_counter_batches(self.database, maximum=10), 2)
        with closing(sqlite3.connect(fallback)) as db:
            db.row_factory = sqlite3.Row
            batch = db.execute("SELECT * FROM fallback_counter_batches ORDER BY bucket_start LIMIT 1").fetchone()
        self.assertIsNotNone(batch)
        committed = store._persist_fallback_counter_batch(self.database, batch)
        self.assertEqual(committed, 2)
        self.assertTrue(store.counter_fallback_pending(self.database))

        # This retry sees the same deterministic event ID and exact payload.
        self.assertEqual(store.flush_counter_fallback(self.database, maximum=10), 3)
        self.assertFalse(store.counter_fallback_pending(self.database))
        self.assertEqual(store.flush_counter_fallback(self.database, maximum=10), 0)
        series = query_metric_series(
            self.database,
            metric_key="api.requests",
            after="2026-11-13T10:00:00Z",
            before="2026-11-13T10:01:00Z",
            dimensions={"route": "/api/v1/status"},
        )
        self.assertEqual([item["value"] for item in series], [2, 1])
        self.assertEqual(overview(self.database)["kpis"]["api_requests"]["value"], 3)

    def test_stopped_data_directory_restore_replays_fallback_sidecar_once(self):
        """A relocated, stopped-app backup must include and replay both fallback sidecars."""
        source_data = self.root / "stopped-source-data"
        source_data.mkdir()
        source_database = source_data / "app.db"
        initialize_database(source_database)
        store.initialize_counter_fallback_store(source_database)
        siem.initialize_security_event_fallback_store(source_database)
        record_event(
            source_database,
            event_id="restore-fixture-event",
            timestamp="2026-11-13T09:59:00Z",
            event_type="system.restore_fixture",
            component="test",
            message="Synthetic record retained in a complete data-directory recovery copy.",
        )
        security_event = {
            "event_type": "authz.request_denied", "outcome": "denied", "component": "authorization",
            "actor_ref": "unauthenticated-client", "details": {"http_status": 401, "route": "/api/admin/status"},
            "correlation_id": "restore-fixture-auth:request-1", "event_id": "1" * 32,
            "timestamp": "2026-11-13T09:59:30Z",
        }
        self.assertTrue(siem.queue_security_event_fallback(source_database, **security_event))
        dimensions = {"route": "/api/v1/status", "method": "GET", "status": 200}
        for timestamp in ("2026-11-13T10:00:03Z", "2026-11-13T10:00:08Z", "2026-11-13T10:01:12Z"):
            store.buffer_counter_fallback(
                source_database,
                metric_key="api.requests",
                dimensions=dimensions,
                timestamp=timestamp,
            )

        # Reproduce the crash window: one compacted batch reaches app.db but
        # its sidecar acknowledgement does not, while another minute remains
        # buffered in the sidecar.
        self.assertEqual(store.stage_fallback_counter_batches(source_database, maximum=1), 1)
        source_fallback = store.counter_fallback_database_path(source_database)
        with closing(sqlite3.connect(source_fallback)) as db:
            db.row_factory = sqlite3.Row
            committed_batch = db.execute("SELECT * FROM fallback_counter_batches").fetchone()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fallback_counter_buffer").fetchone()[0], 1)
        self.assertIsNotNone(committed_batch)
        self.assertEqual(store._persist_fallback_counter_batch(source_database, committed_batch), 2)

        # All writers have closed their SQLite connections. Copy the whole
        # application data directory, including the private fallback folder,
        # to model an offline backup restored at a different path.
        source_fallback = store.counter_fallback_database_path(source_database)
        source_security_fallback = siem.security_event_fallback_database_path(source_database)
        source_hashes = {
            source_database.name: hashlib.sha256(source_database.read_bytes()).hexdigest(),
            source_fallback.name: hashlib.sha256(source_fallback.read_bytes()).hexdigest(),
            source_security_fallback.name: hashlib.sha256(source_security_fallback.read_bytes()).hexdigest(),
        }
        restored_data = self.root / "relocated-recovery-data"
        shutil.copytree(source_data, restored_data)
        restored_database = restored_data / "app.db"
        restored_fallback = store.counter_fallback_database_path(restored_database)
        restored_security_fallback = siem.security_event_fallback_database_path(restored_database)
        self.assertTrue(restored_database.is_file())
        self.assertTrue(restored_fallback.is_file())
        self.assertTrue(restored_security_fallback.is_file())
        self.assertEqual(hashlib.sha256(restored_database.read_bytes()).hexdigest(), source_hashes["app.db"])
        self.assertEqual(hashlib.sha256(restored_fallback.read_bytes()).hexdigest(), source_hashes["counters.sqlite3"])
        self.assertEqual(hashlib.sha256(restored_security_fallback.read_bytes()).hexdigest(), source_hashes["security-events.sqlite3"])

        for candidate in (restored_database, restored_fallback, restored_security_fallback):
            with closing(sqlite3.connect(f"file:{candidate.as_posix()}?mode=ro", uri=True)) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                if candidate == restored_database:
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        with closing(sqlite3.connect(f"file:{restored_database.as_posix()}?mode=ro", uri=True)) as db:
            self.assertEqual(
                db.execute("SELECT message FROM observability_events WHERE event_id='restore-fixture-event'").fetchone()[0],
                "Synthetic record retained in a complete data-directory recovery copy.",
            )
        self.assertTrue(store.counter_fallback_pending(restored_database))
        self.assertEqual(store.counter_fallback_status(restored_database)["pending_increments"], 3)
        self.assertTrue(siem.security_event_fallback_pending(restored_database))

        # Start VAOS itself in a separate Python process against only the
        # relocated synthetic directory. Its startup hook should drain the
        # paired sidecar without opening a listener or using live data.
        startup_smoke = """
import json
from fastapi.testclient import TestClient
import main
import observability_siem
import observability_store

with TestClient(main.app):
    state = {
        "fallback_pending": observability_store.counter_fallback_pending(main.DB_PATH),
        "api_requests": observability_store.overview(main.DB_PATH)["kpis"]["api_requests"]["value"],
        "security_fallback_pending": observability_siem.security_event_fallback_pending(main.DB_PATH),
        "security_audits": len(observability_store.query_security_audit(main.DB_PATH, event_type="authz.request_denied")),
        "security_chain_valid": observability_store.verify_security_chain(main.DB_PATH)["valid"],
    }
print("VAOS_RESTORE_STATE=" + json.dumps(state, sort_keys=True))
"""
        environment = os.environ.copy()
        environment["VAOS_DATA_DIR"] = str(restored_data)
        environment["MODEL_DIR"] = str(restored_data / "models")
        environment["VAOS_ENV"] = "development"
        startup = subprocess.run(
            [sys.executable, "-c", startup_smoke],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(startup.returncode, 0, f"stdout:\n{startup.stdout}\nstderr:\n{startup.stderr[-4000:]}")
        state_line = next(
            (line.removeprefix("VAOS_RESTORE_STATE=") for line in startup.stdout.splitlines() if line.startswith("VAOS_RESTORE_STATE=")),
            None,
        )
        self.assertIsNotNone(state_line, startup.stdout)
        state = json.loads(state_line)
        self.assertFalse(state["fallback_pending"])
        self.assertEqual(state["api_requests"], 3)
        self.assertFalse(state["security_fallback_pending"])
        self.assertEqual(state["security_audits"], 1)
        self.assertTrue(state["security_chain_valid"])
        if os.name == "nt":
            account = subprocess.run(["whoami"], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
            for private_path in (restored_fallback.parent, restored_fallback, restored_security_fallback):
                acl = subprocess.run(
                    ["icacls", str(private_path)], capture_output=True, text=True, check=True, timeout=5
                ).stdout
                ace_lines = [line for line in acl.splitlines() if "(" in line and ")" in line]
                self.assertEqual(len(ace_lines), 2, f"restored fallback ACL must contain only the account and SYSTEM:\n{acl}")
                self.assertTrue(any(account.casefold() in line.casefold() for line in ace_lines), acl)
                self.assertTrue(any("SYSTEM" in line.upper() for line in ace_lines), acl)
                self.assertFalse(any("(I)" in line.upper() for line in ace_lines), acl)
        else:
            self.assertEqual(stat.S_IMODE(restored_fallback.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(restored_fallback.stat().st_mode), 0o600)
        self.assertEqual(store.flush_counter_fallback(restored_database, maximum=10), 0)

        recovered_series = query_metric_series(
            restored_database,
            metric_key="api.requests",
            after="2026-11-13T10:00:00Z",
            before="2026-11-13T10:02:00Z",
            dimensions={"route": "/api/v1/status"},
        )
        self.assertEqual([item["value"] for item in recovered_series], [2, 1])
        self.assertEqual(overview(restored_database)["kpis"]["api_requests"]["value"], 3)

        # Recovery operates only on the relocated copy; the original remains
        # available and its unflushed sidecar remains unchanged.
        self.assertTrue(store.counter_fallback_pending(source_database))
        self.assertEqual(overview(source_database)["kpis"]["api_requests"]["value"], 2)
        self.assertTrue(siem.security_event_fallback_pending(source_database))

    def test_security_event_fallback_preserves_concurrent_process_writes(self):
        initialize_database(self.database)
        siem.initialize_security_event_fallback_store(self.database)
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        result = context.Queue()
        start = context.Event()
        worker_count = 4
        workers = [
            context.Process(
                target=_write_security_fallback_from_process,
                args=(str(self.database), worker_id, ready, start, result),
            )
            for worker_id in range(worker_count)
        ]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                ready.get(timeout=15)
            start.set()
            outcomes = [result.get(timeout=45) for _ in workers]
            for worker in workers:
                worker.join(timeout=5)
            self.assertTrue(all(not worker.is_alive() for worker in workers), "security fallback writer process did not exit")
            self.assertTrue(all(worker.exitcode == 0 for worker in workers), [worker.exitcode for worker in workers])
            self.assertEqual([item for item in outcomes if item[1] != "ok"], [])
            self.assertEqual(siem.security_event_fallback_status(self.database)["pending_rows"], worker_count * 8)
            self.assertEqual(siem.security_event_fallback_status(self.database)["dropped_events"], 0)
            self.assertEqual(siem.flush_security_event_fallback(self.database, maximum=100), worker_count * 8)
            audits = query_security_audit(self.database, limit=100, event_type="authz.request_denied")
            self.assertEqual(len(audits), worker_count * 8)
            self.assertTrue(verify_security_chain(self.database)["valid"])
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            ready.close()
            result.close()
            ready.join_thread()
            result.join_thread()

    def test_fallback_counter_sidecar_coarsens_overflow_and_preserves_exact_total(self):
        initialize_database(self.database)
        store.initialize_counter_fallback_store(self.database)
        with patch.object(store, "FALLBACK_COUNTER_BUFFER_LIMIT", 3):
            for index in range(5):
                store.buffer_counter_fallback(
                    self.database,
                    metric_key="api.requests",
                    dimensions={"route": f"/api/v1/fixture-{index}", "method": "GET", "status": 200},
                    timestamp="2026-11-13T11:00:03Z",
                )
        fallback = store.counter_fallback_database_path(self.database)
        with closing(sqlite3.connect(fallback)) as db:
            count = db.execute("SELECT COUNT(*) FROM fallback_counter_buffer").fetchone()[0]
            overflow = db.execute(
                "SELECT delta FROM fallback_counter_buffer WHERE metric_key='api.requests' AND dimensions_json=?",
                (store._json({"route": "other", "method": "OTHER", "status": 0}),),
            ).fetchone()
        self.assertEqual(count, 4)
        self.assertIsNotNone(overflow)
        self.assertEqual(overflow[0], 2)
        self.assertEqual(store.flush_counter_fallback(self.database, maximum=10), 5)
        self.assertEqual(overview(self.database)["kpis"]["api_requests"]["value"], 5)

    def test_fallback_counter_conflicting_replay_stays_pending(self):
        initialize_database(self.database)
        store.initialize_counter_fallback_store(self.database)
        store.buffer_counter_fallback(
            self.database,
            metric_key="api.requests",
            dimensions={"route": "/api/v1/status", "method": "GET", "status": 200},
            timestamp="2026-11-13T12:00:03Z",
        )
        store.stage_fallback_counter_batches(self.database, maximum=1)
        fallback = store.counter_fallback_database_path(self.database)
        with closing(sqlite3.connect(fallback)) as db:
            batch_id = db.execute("SELECT batch_id FROM fallback_counter_batches").fetchone()[0]
        record_event(
            self.database,
            event_type="api.request_batch",
            component="api",
            message="Conflicting fixture payload.",
            details={"request_count": 1},
            event_id=f"counter-fallback-{batch_id}",
            timestamp="2026-11-13T12:00:00Z",
        )
        with self.assertRaisesRegex(RuntimeError, "conflicts"):
            store.flush_counter_fallback(self.database, maximum=1)
        self.assertTrue(store.counter_fallback_pending(self.database))
        with closing(sqlite3.connect(fallback)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fallback_counter_batches").fetchone()[0], 1)

    def test_fallback_counter_replay_accepts_only_retention_redacted_committed_event(self):
        initialize_database(self.database)
        store.initialize_counter_fallback_store(self.database)
        dimensions = {"route": "/api/v1/status", "method": "GET", "status": 200}
        store.buffer_counter_fallback(
            self.database,
            metric_key="api.requests",
            dimensions=dimensions,
            timestamp="2025-01-01T10:00:03Z",
        )
        store.stage_fallback_counter_batches(self.database, maximum=1)
        fallback = store.counter_fallback_database_path(self.database)
        with closing(sqlite3.connect(fallback)) as db:
            db.row_factory = sqlite3.Row
            batch = db.execute("SELECT * FROM fallback_counter_batches").fetchone()

        # Model the primary transaction completing before the process exits,
        # followed by retention and a crash before the sidecar acknowledgement.
        self.assertEqual(store._persist_fallback_counter_batch(self.database, batch), 1)
        store.apply_retention(self.database, now="2026-11-13T00:00:00Z")
        with closing(sqlite3.connect(self.database)) as db:
            event = db.execute(
                "SELECT message,details_json FROM observability_events WHERE event_id=?",
                (f"counter-fallback-{batch['batch_id']}",),
            ).fetchone()
            self.assertEqual(event[0], "Expired event details redacted by VAOS retention policy.")
            self.assertEqual(event[1], "{}")

        self.assertEqual(store.flush_counter_fallback(self.database, maximum=1), 1)
        self.assertFalse(store.counter_fallback_pending(self.database))
        self.assertEqual(overview(self.database)["kpis"]["api_requests"]["value"], 1)

    def test_fallback_counter_rejects_unapproved_route_and_camera_identity(self):
        initialize_database(self.database)
        store.initialize_counter_fallback_store(self.database)
        with self.assertRaisesRegex(ValueError, "route template"):
            store.buffer_counter_fallback(
                self.database,
                metric_key="api.requests",
                dimensions={"route": "/api/v1/status?secret=private", "method": "GET", "status": 200},
            )
        with self.assertRaisesRegex(ValueError, "one-way camera reference"):
            store.buffer_counter_fallback(
                self.database,
                metric_key="inference.frames_processed",
                dimensions={"camera_ref": "raw-camera-capability-token", "source_type": "camera"},
            )
        store.buffer_counter_fallback(
            self.database,
            metric_key="inference.frames_processed",
            dimensions={"camera_ref": hashlib.sha256(b"raw-camera-capability-token").hexdigest()[:20], "source_type": "camera"},
        )
        fallback = store.counter_fallback_database_path(self.database)
        self.assertNotIn(b"raw-camera-capability-token", fallback.read_bytes())

    def test_unknown_fallback_payload_version_is_retained_for_recovery(self):
        initialize_database(self.database)
        store.initialize_counter_fallback_store(self.database)
        store.buffer_counter_fallback(
            self.database,
            metric_key="api.requests",
            dimensions={"route": "/api/v1/status", "method": "GET", "status": 200},
        )
        store.stage_fallback_counter_batches(self.database, maximum=1)
        fallback = store.counter_fallback_database_path(self.database)
        with closing(sqlite3.connect(fallback)) as db:
            db.execute("UPDATE fallback_counter_batches SET payload_version=99")
            db.commit()
        with self.assertRaisesRegex(ValueError, "payload version"):
            store.flush_counter_fallback(self.database, maximum=1)
        self.assertTrue(store.counter_fallback_pending(self.database))

    def test_fallback_sidecar_repeated_fill_and_drain_respects_disk_bound(self):
        initialize_database(self.database)
        store.initialize_counter_fallback_store(self.database)
        with patch.object(store, "FALLBACK_COUNTER_BUFFER_LIMIT", 3):
            for cycle in range(8):
                for index in range(10):
                    store.buffer_counter_fallback(
                        self.database,
                        metric_key="api.requests",
                        dimensions={"route": f"/api/v1/cycle-{cycle}-route-{index}", "method": "GET", "status": 200},
                        timestamp=f"2026-11-13T13:{cycle:02d}:03Z",
                    )
                self.assertEqual(store.flush_counter_fallback(self.database, maximum=20), 10)
        fallback = store.counter_fallback_database_path(self.database)
        with closing(sqlite3.connect(fallback)) as db:
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            page_count = db.execute("PRAGMA page_count").fetchone()[0]
        self.assertLessEqual(page_size * page_count, store.FALLBACK_COUNTER_MAX_BYTES)
        self.assertLessEqual(sum(path.stat().st_size for path in fallback.parent.iterdir() if path.is_file()), store.FALLBACK_COUNTER_MAX_BYTES)
        self.assertEqual(overview(self.database)["kpis"]["api_requests"]["value"], 80)

    def test_fallback_sidecar_busy_wait_is_bounded(self):
        initialize_database(self.database)
        store.initialize_counter_fallback_store(self.database)
        fallback = store.counter_fallback_database_path(self.database)
        with closing(sqlite3.connect(fallback, timeout=1)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = datetime.now(timezone.utc)
            with self.assertRaises(sqlite3.OperationalError):
                store.buffer_counter_fallback(
                    self.database,
                    metric_key="api.requests",
                    dimensions={"route": "/api/v1/status", "method": "GET", "status": 200},
                )
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            blocker.rollback()
        self.assertLess(elapsed, 1.5)

    def test_fallback_counter_sidecar_preserves_concurrent_process_writes(self):
        initialize_database(self.database)
        store.initialize_counter_fallback_store(self.database)
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        result = context.Queue()
        start = context.Event()
        worker_count = 4
        workers = [
            context.Process(
                target=_write_fallback_counter_from_process,
                args=(str(self.database), worker_id, ready, start, result),
            )
            for worker_id in range(worker_count)
        ]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                ready.get(timeout=15)
            start.set()
            outcomes = [result.get(timeout=30) for _ in workers]
            for worker in workers:
                worker.join(timeout=5)
            self.assertTrue(all(not worker.is_alive() for worker in workers), "fallback writer process did not exit")
            self.assertTrue(all(worker.exitcode == 0 for worker in workers), [worker.exitcode for worker in workers])
            self.assertEqual([item for item in outcomes if item[1] != "ok"], [])
            fallback = store.counter_fallback_database_path(self.database)
            with closing(sqlite3.connect(fallback)) as db:
                rows = db.execute("SELECT COUNT(*),SUM(delta) FROM fallback_counter_buffer").fetchone()
            self.assertEqual(rows, (1, worker_count * 20))
            self.assertEqual(store.flush_counter_fallback(self.database, maximum=10), worker_count * 20)
            self.assertEqual(overview(self.database)["kpis"]["api_requests"]["value"], worker_count * 20)
        finally:
            start.set()
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            ready.close()
            result.close()
            ready.join_thread()
            result.join_thread()

    def test_lifecycle_counts_are_idempotent_and_unknown_is_not_called_a_crash(self):
        initialize_database(self.database)
        session_id, install_id = start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-12T00:00:00Z", process_id=111)
        same_id, same_install = start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-12T00:00:30Z", process_id=111)
        self.assertEqual((session_id, install_id), (same_id, same_install))
        self.assertTrue(heartbeat_session(self.database, session_id, now="2026-11-12T00:00:45Z"))
        self.assertTrue(end_session(self.database, session_id, now="2026-11-12T00:01:00Z"))
        self.assertFalse(end_session(self.database, session_id, now="2026-11-12T00:01:01Z"))
        stale_id, _ = start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-12T01:00:00Z", process_id=222)
        current_id, _ = start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-12T01:03:00Z", process_id=333)
        reused_pid_old_id, _ = start_session(self.database, component="camera", app_version="1.1.0", now="2026-11-12T02:00:00Z", process_id=444)
        reused_pid_new_id, _ = start_session(self.database, component="camera", app_version="1.1.0", now="2026-11-12T02:03:00Z", process_id=444)
        sessions = list_sessions(self.database)
        self.assertEqual(len(sessions), 5)
        self.assertEqual(next(row["exit_reason"] for row in sessions if row["session_id"] == session_id), "clean")
        self.assertEqual(next(row["exit_reason"] for row in sessions if row["session_id"] == stale_id), "unknown")
        self.assertNotEqual(reused_pid_old_id, reused_pid_new_id)
        self.assertEqual(next(row["exit_reason"] for row in sessions if row["session_id"] == reused_pid_old_id), "unknown")
        self.assertEqual(next(row["exit_reason"] for row in sessions if row["session_id"] == current_id), "unknown")
        metrics = overview(self.database)
        self.assertEqual(metrics["kpis"]["application_starts"]["value"], 5)
        self.assertEqual(metrics["kpis"]["clean_stops"]["value"], 1)
        self.assertEqual(metrics["kpis"]["uptime"]["quality"], "lower_bound_unknown_sessions")
        self.assertEqual(metrics["kpis"]["uptime"]["unknown_sessions"], 3)
        interval = overview(self.database, period="custom", after="2026-11-12T00:00:00Z", before="2026-11-12T00:02:00Z")
        self.assertEqual(interval["kpis"]["uptime"]["value"], 60)

    def test_fast_restart_with_reused_pid_starts_a_new_process_incarnation(self):
        initialize_database(self.database)
        first_id, install_id = start_session(
            self.database, component="admin", app_version="1.1.0",
            now="2026-11-12T00:00:00Z", process_id=515, process_instance_id="incarnation-a",
        )
        replayed_id, replayed_install = start_session(
            self.database, component="admin", app_version="1.1.0",
            now="2026-11-12T00:00:20Z", process_id=515, process_instance_id="incarnation-a",
        )
        self.assertEqual((first_id, install_id), (replayed_id, replayed_install))

        second_id, second_install = start_session(
            self.database, component="admin", app_version="1.1.0",
            now="2026-11-12T00:00:30Z", process_id=515, process_instance_id="incarnation-b",
        )
        replayed_second_id, _ = start_session(
            self.database, component="admin", app_version="1.1.0",
            now="2026-11-12T00:00:45Z", process_id=515, process_instance_id="incarnation-b",
        )
        sessions = {row["session_id"]: row for row in list_sessions(self.database)}
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(install_id, second_install)
        self.assertEqual(second_id, replayed_second_id)
        self.assertEqual(sessions[first_id]["exit_reason"], "unknown")
        self.assertEqual(sessions[second_id]["exit_reason"], "running")
        self.assertEqual(overview(self.database)["kpis"]["application_starts"]["value"], 2)

    def test_separate_python_launches_with_reused_pid_get_distinct_incarnations(self):
        initialize_database(self.database)
        script = (
            "import sys; from observability_store import start_session; "
            "start_session(sys.argv[1], component='admin', app_version='1.1.0', "
            "now=sys.argv[2], process_id=717)"
        )
        for timestamp in ("2026-11-12T00:00:00Z", "2026-11-12T00:00:30Z"):
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.database), timestamp],
                cwd=str(Path(__file__).resolve().parents[1]),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        sessions = list_sessions(self.database)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[1]["exit_reason"], "unknown")
        self.assertEqual(sessions[0]["exit_reason"], "running")
        self.assertEqual(overview(self.database)["kpis"]["application_starts"]["value"], 2)

    def test_v10_migration_preserves_legacy_session_and_adds_process_identity(self):
        initialize_database(self.database)
        legacy_id, _ = start_session(
            self.database, component="camera", app_version="1.1.0",
            now="2026-11-12T00:00:00Z", process_id=616, process_instance_id="pre-v11",
        )
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DROP INDEX idx_obs_sessions_process_instance")
            db.execute("ALTER TABLE observability_runtime_sessions DROP COLUMN process_instance_id")
            db.execute("DELETE FROM vaos_schema_migrations WHERE version>10")
            db.execute("UPDATE observability_installation SET schema_version=10 WHERE singleton_id=1")
            db.commit()

        backup = initialize_database(self.database)
        self.assertIsNotNone(backup)
        with closing(sqlite3.connect(self.database)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(observability_runtime_sessions)")}
            self.assertIn("process_instance_id", columns)
            self.assertEqual(db.execute("SELECT process_instance_id FROM observability_runtime_sessions WHERE session_id=?", (legacy_id,)).fetchone()[0], None)
            self.assertEqual(db.execute("SELECT MAX(version) FROM vaos_schema_migrations").fetchone()[0], store.SCHEMA_VERSION)

        replacement_id, _ = start_session(
            self.database, component="camera", app_version="1.1.0",
            now="2026-11-12T00:00:30Z", process_id=616, process_instance_id="post-v11",
        )
        sessions = {row["session_id"]: row for row in list_sessions(self.database)}
        self.assertEqual(sessions[legacy_id]["exit_reason"], "unknown")
        self.assertEqual(sessions[replacement_id]["exit_reason"], "running")

    def test_installation_uptime_merges_overlapping_services_and_counters_keep_components(self):
        initialize_database(self.database)
        admin_id, _ = start_session(self.database, component="admin", app_version="1.1.0", now="2026-11-12T00:00:00Z", process_id=1001)
        heartbeat_session(self.database, admin_id, now="2026-11-12T00:30:00Z")
        camera_id, _ = start_session(self.database, component="camera", app_version="1.1.0", now="2026-11-12T00:30:00Z", process_id=1002)
        end_session(self.database, admin_id, now="2026-11-12T01:00:00Z")
        end_session(self.database, camera_id, now="2026-11-12T01:30:00Z")
        result = overview(self.database, period="custom", after="2026-11-12T00:00:00Z", before="2026-11-12T02:00:00Z")
        self.assertEqual(result["kpis"]["uptime"]["value"], 5400)
        starts = query_metric_series(self.database, metric_key="app.starts", after="2026-11-12T00:00:00Z", before="2026-11-13T00:00:00Z", dimensions={"component": "camera"})
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["dimensions"]["component"], "camera")

    def test_counter_series_exposes_exact_dimensions_and_counts_filters(self):
        initialize_database(self.database)
        for event_id, timestamp, route, status, delta in (
            ("counter-a", "2026-11-12T10:00:00Z", "/api/v1/cameras", 200, 2),
            ("counter-b", "2026-11-12T11:00:00Z", "/api/v1/cameras", 500, 1),
            ("counter-c", "2026-11-12T12:00:00Z", "/api/v1/status", 200, 4),
        ):
            record_event(self.database, event_id=event_id, timestamp=timestamp, event_type="api.request_batch", component="api", message="requests", counter_deltas={"api.requests": delta}, counter_dimensions={"api.requests": {"route": route, "method": "GET", "status": status}})
        dimensions = {"route": "/api/v1/cameras"}
        series = query_metric_series(self.database, metric_key="api.requests", after="2026-11-12T00:00:00Z", before="2026-11-13T00:00:00Z", dimensions=dimensions)
        self.assertEqual([item["value"] for item in series], [2, 1])
        self.assertEqual(count_metric_series(self.database, metric_key="api.requests", after="2026-11-12T00:00:00Z", before="2026-11-13T00:00:00Z", dimensions=dimensions), 2)
        with self.assertRaisesRegex(ValueError, "Unsupported dimension"):
            query_metric_series(self.database, metric_key="api.requests", after="2026-11-12T00:00:00Z", before="2026-11-13T00:00:00Z", dimensions={"not_a_dimension": "x"})

    def test_replayed_event_does_not_double_count_and_secrets_are_redacted(self):
        initialize_database(self.database)
        session_id, _ = start_session(self.database, component="training", app_version="1.1.0", now="2026-11-12T00:00:00Z", process_id=987654321)
        arguments = {
            "event_type": "training.job_started",
            "component": "training",
            "session_id": session_id,
            "correlation_id": "request-epoch-42",
            "message": "started with Bearer abc.def-123 at C:\\private\\weights\\best.pt /home/aduit/VAOS_Datasets/IDD-Lite/data.yaml /api/camera/capability-secret/frame",
            "details": {"dataset_id": "idd-lite", "source": "uvh26", "initial_model": "best.pt", "run_id": "train-run-42", "api_token": "secret-value", "epoch": 2},
            "event_id": "fixed-event-id",
            "counter_deltas": {"training.jobs_started": 1},
            "timestamp": "2026-11-12T00:00:00Z",
        }
        self.assertTrue(record_event(self.database, **arguments))
        self.assertFalse(record_event(self.database, **arguments))
        result = overview(self.database)
        self.assertEqual(result["kpis"]["training_jobs_started"]["value"], 1)
        self.assertTrue(result["kpis"]["training_jobs_started"]["available"])
        entry = query_events(self.database, search="training.job_started", limit=10)
        self.assertEqual(len(entry), 1)
        self.assertEqual(len(query_events(self.database, correlation_id="request-epoch-42")), 1)
        self.assertEqual(query_events(self.database, correlation_id="request-other"), [])
        self.assertEqual({item["event_id"] for item in query_events(self.database, session_id=session_id)}, {"fixed-event-id", f"{session_id}:started"})
        self.assertEqual(query_events(self.database, session_id="00000000-0000-0000-0000-000000000000"), [])
        self.assertEqual([item["event_id"] for item in query_events(self.database, training_job_id="train-run-42")], ["fixed-event-id"])
        self.assertEqual([item["event_id"] for item in query_events(self.database, dataset_id="idd-lite")], ["fixed-event-id"])
        self.assertEqual([item["event_id"] for item in query_events(self.database, model_id="best.pt")], ["fixed-event-id"])
        self.assertEqual(query_events(self.database, training_job_id="another-run"), [])
        for field in ("training_job_id", "dataset_id", "model_id"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "120 characters or fewer"):
                query_events(self.database, **{field: "x" * 121})
        serialized = json.dumps(entry[0])
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("abc.def-123", serialized)
        self.assertNotIn("C:\\\\private", serialized)
        self.assertNotIn("/home/aduit", serialized)
        self.assertIn("/api/camera/[REDACTED]/frame", serialized)
        self.assertNotIn("capability-secret", serialized)
        self.assertNotIn("api_token", serialized)

    def test_metrics_validate_dimensions_and_are_idempotent(self):
        initialize_database(self.database)
        record_event(self.database, event_id="epoch-event", event_type="training.epoch_completed", component="training", message="epoch done")
        params = {"metric_key": "training.box_loss", "value": 0.42, "source_event_id": "epoch-event", "dimensions": {"epoch": 1}, "provenance": "trainer", "timestamp": "2026-11-12T00:00:00Z"}
        self.assertTrue(record_metric(self.database, **params))
        self.assertFalse(record_metric(self.database, **params))
        self.assertTrue(list_metrics(self.database, key="training.box_loss")[0]["available"])
        with self.assertRaisesRegex(ValueError, "Unsupported dimensions"):
            record_metric(self.database, **{**params, "source_event_id": "epoch-event", "metric_key": "training.box_loss", "dimensions": {"camera_id": "cam-1"}})
        with self.assertRaisesRegex(ValueError, "finite"):
            record_metric(self.database, **{**params, "value": float("nan")})
        with self.assertRaisesRegex(ValueError, "only finite numbers"):
            record_metric(self.database, **{**params, "dimensions": {"epoch": [1, 2, 3]}})
        series = query_metric_series(self.database, metric_key="training.box_loss", after="2026-11-12T00:00:00Z", before="2026-11-13T00:00:00Z")
        self.assertEqual(series[0]["value"], 0.42)
        self.assertEqual(series[0]["dimensions"]["epoch"], 1)

    def test_metric_series_aggregation_groups_and_sums_counter_buckets(self):
        initialize_database(self.database)
        record_event(
            self.database, event_id="series-counter-a", event_type="api.request_recorded", component="api",
            message="request", timestamp="2026-11-12T10:00:10Z",
            counter_deltas={"api.requests": 3},
            counter_dimensions={"api.requests": {"route": "/api/v1/status", "method": "GET", "status": 200}},
        )
        record_event(
            self.database, event_id="series-counter-b", event_type="api.request_recorded", component="api",
            message="request", timestamp="2026-11-12T10:00:40Z",
            counter_deltas={"api.requests": 2},
            counter_dimensions={"api.requests": {"route": "/api/v1/status", "method": "GET", "status": 200}},
        )
        record_event(
            self.database, event_id="series-counter-c", event_type="api.request_recorded", component="api",
            message="request", timestamp="2026-11-12T10:01:05Z",
            counter_deltas={"api.requests": 4},
            counter_dimensions={"api.requests": {"route": "/api/v1/status", "method": "GET", "status": 500}},
        )

        result = store.query_metric_series_aggregated(
            self.database, metric_key="api.requests", after="2026-11-12T10:00:00Z",
            before="2026-11-12T10:02:00Z", aggregation="sum", interval="minute",
            group_by="status", dimensions={"route": "/api/v1/status"},
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["group_count"], 2)
        self.assertEqual(
            [(item["timestamp"], item["value"], item["dimensions"]["status"], item["record_count"]) for item in result["items"]],
            [("2026-11-12T10:00:00Z", 5, "200", 2), ("2026-11-12T10:01:00Z", 4, "500", 1)],
        )
        self.assertIn("sum of 2 retained counter", result["items"][0]["provenance"])
        combined = store.query_metric_series_aggregated(
            self.database, metric_key="api.requests", after="2026-11-12T10:00:00Z",
            before="2026-11-12T10:02:00Z", aggregation="sum", interval="minute",
        )
        self.assertEqual(combined["group_count"], 1)
        self.assertEqual([item["value"] for item in combined["items"]], [5, 4])

    def test_metric_series_aggregation_averages_gauges_and_rejects_invalid_choices(self):
        initialize_database(self.database)
        for suffix, value, timestamp in (("a", 0.4, "2026-11-12T10:00:10Z"), ("b", 0.6, "2026-11-12T10:00:40Z")):
            event_id = f"series-gauge-{suffix}"
            record_event(self.database, event_id=event_id, event_type="training.epoch_completed", component="training", message="epoch", timestamp=timestamp)
            record_metric(
                self.database, metric_key="training.box_loss", value=value, source_event_id=event_id,
                dimensions={"training_job_id": "job-a", "dataset_id": "dataset-a", "model_id": "model-a", "epoch": 1},
                provenance="trainer", timestamp=timestamp,
            )
        result = store.query_metric_series_aggregated(
            self.database, metric_key="training.box_loss", after="2026-11-12T10:00:00Z",
            before="2026-11-12T10:01:00Z", aggregation="average", interval="minute",
            group_by="dataset_id",
        )
        self.assertEqual(result["total"], 1)
        self.assertAlmostEqual(result["items"][0]["value"], 0.5)
        self.assertEqual(result["items"][0]["dimensions"], {"dataset_id": "dataset-a"})
        with self.assertRaisesRegex(ValueError, "not supported"):
            store.query_metric_series_aggregated(
                self.database, metric_key="api.requests", after="2026-11-12T10:00:00Z",
                before="2026-11-12T10:01:00Z", aggregation="average", interval="minute",
            )
        with self.assertRaisesRegex(ValueError, "Unsupported grouping dimension"):
            store.query_metric_series_aggregated(
                self.database, metric_key="training.box_loss", after="2026-11-12T10:00:00Z",
                before="2026-11-12T10:01:00Z", aggregation="average", interval="minute",
                group_by="route",
            )
        with self.assertRaisesRegex(ValueError, "supported metric time interval"):
            store.query_metric_series_aggregated(
                self.database, metric_key="training.box_loss", after="2026-11-12T10:00:00Z",
                before="2026-11-12T10:01:00Z", aggregation="average", interval="week",
            )

    def test_metric_series_aggregation_sums_declared_energy_gauge_and_keeps_estimate_flag(self):
        initialize_database(self.database)
        for suffix, value, timestamp in (("a", 0.25, "2026-11-12T10:00:10Z"), ("b", 0.75, "2026-11-12T10:00:40Z")):
            event_id = f"series-energy-{suffix}"
            record_event(self.database, event_id=event_id, event_type="hardware.gpu_power_sample", component="hardware", message="sample", timestamp=timestamp)
            record_metric(
                self.database, metric_key="hardware.gpu_energy_interval_wh", value=value,
                source_event_id=event_id, dimensions={"device_id": "gpu-a", "model_id": "RTX"},
                provenance="sampled power integration", estimated=True, timestamp=timestamp,
            )
        result = store.query_metric_series_aggregated(
            self.database, metric_key="hardware.gpu_energy_interval_wh",
            after="2026-11-12T10:00:00Z", before="2026-11-12T10:01:00Z",
            aggregation="sum", interval="minute", group_by="device_id",
        )
        self.assertEqual(result["total"], 1)
        self.assertAlmostEqual(result["items"][0]["value"], 1.0)
        self.assertEqual(result["items"][0]["dimensions"], {"device_id": "gpu-a"})
        self.assertTrue(result["items"][0]["estimated"])

    def test_metric_series_grouping_matches_text_normalized_dimension_filters(self):
        initialize_database(self.database)
        for suffix, epoch, value in (("number", 1, 0.4), ("text", "1", 0.6)):
            event_id = f"series-dimension-{suffix}"
            record_event(self.database, event_id=event_id, event_type="training.epoch_completed", component="training", message="epoch")
            record_metric(
                self.database, metric_key="training.box_loss", value=value, source_event_id=event_id,
                dimensions={"epoch": epoch}, provenance="trainer", timestamp="2026-11-12T10:00:30Z",
            )
        result = store.query_metric_series_aggregated(
            self.database, metric_key="training.box_loss", after="2026-11-12T10:00:00Z",
            before="2026-11-12T10:01:00Z", aggregation="average", interval="minute", group_by="epoch",
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["group_count"], 1)
        self.assertAlmostEqual(result["items"][0]["value"], 0.5)
        self.assertEqual(result["items"][0]["dimensions"], {"epoch": "1"})
        self.assertEqual(store.count_metric_series_groups(
            self.database, metric_key="training.box_loss", group_by="epoch",
            after="2026-11-12T10:00:00Z", before="2026-11-12T10:01:00Z",
        ), 1)
        self.assertEqual(store.count_metric_series(
            self.database, metric_key="training.box_loss",
            after="2026-11-12T10:00:00Z", before="2026-11-12T10:01:00Z",
            dimensions={"epoch": "1"},
        ), 2)

    def test_retention_redacts_events_and_prunes_only_expiring_metrics(self):
        initialize_database(self.database)
        record_event(
            self.database, event_id="old-epoch", event_type="training.epoch_completed", component="training",
            message="old training event with /api/camera/capability-secret/frame",
            details={"summary": "expired detail"}, timestamp="2025-01-01T00:00:00Z",
            counter_deltas={"training.epochs_completed": 1},
        )
        record_metric(self.database, metric_key="training.box_loss", value=0.5, source_event_id="old-epoch", dimensions={"epoch": 3}, provenance="trainer", timestamp="2025-01-01T00:00:00Z")
        record_security_event(self.database, event_id="durable-audit", event_type="auth.login_failed", outcome="denied", component="auth", timestamp="2025-01-01T00:00:00Z")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("INSERT INTO observability_siem_destinations(destination_id,display_name,transport,endpoint,enabled,secret_ref,redaction_profile,event_format,archived_at,dropped_events,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("c" * 32, "Old receiver", "syslog_tls", "syslog-tls://logs.example.com", 0, "", "default", "json", None, 0, "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"))
            db.execute("INSERT INTO observability_delivery_events(delivery_id,destination_id,event_id,status,attempt_count,next_attempt_at,last_status_code,last_error_code,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", ("delivery-old", "c" * 32, "old-epoch", "sent", 1, None, None, None, "2025-01-01T00:00:00Z"))
            db.commit()
        result = apply_retention(self.database, now="2026-11-12T00:00:00Z")
        self.assertEqual(result["metric_observations_removed"], 1)
        self.assertGreaterEqual(result["event_details_redacted"], 1)
        self.assertEqual(result["delivery_events_removed"], 1)
        self.assertEqual(result["security_audit_rows_changed"], 0)
        self.assertEqual(query_events(self.database, event_type="training.epoch_completed"), [])
        self.assertEqual(overview(self.database)["kpis"]["training_epochs_completed"]["value"], 1)
        with closing(sqlite3.connect(self.database)) as db:
            event = db.execute("SELECT message,details_json FROM observability_events WHERE event_id='old-epoch'").fetchone()
            self.assertEqual(event[0], "Expired event details redacted by VAOS retention policy.")
            self.assertEqual(event[1], "{}")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_id='durable-audit'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_delivery_events WHERE delivery_id='delivery-old'").fetchone()[0], 0)
        repeated = apply_retention(self.database, now="2026-11-12T00:00:00Z")
        self.assertEqual(repeated, {
            "metric_observations_removed": 0,
            "event_details_redacted": 0,
            "delivery_events_removed": 0,
            "security_audit_rows_changed": 0,
        })
        self.assertEqual(overview(self.database)["kpis"]["training_epochs_completed"]["value"], 1)

    def test_retention_policy_is_configurable_and_shortening_requires_confirmation(self):
        initialize_database(self.database)
        defaults = store.get_retention_policy(self.database)
        self.assertEqual(
            (defaults["event_details_days"], defaults["metric_observations_max_days"], defaults["delivery_history_days"]),
            (90, 365, 90),
        )
        self.assertEqual(defaults["options"], [30, 90, 180, 365])
        with self.assertRaisesRegex(ValueError, "Shortening retention"):
            store.set_retention_policy(
                self.database, event_details_days=30, metric_observations_max_days=365,
                delivery_history_days=90,
            )
        with self.assertRaisesRegex(ValueError, "one of 30, 90, 180, or 365"):
            store.set_retention_policy(
                self.database, event_details_days=31, metric_observations_max_days=365,
                delivery_history_days=90, confirm_shorter=True,
            )
        saved = store.set_retention_policy(
            self.database, event_details_days=180, metric_observations_max_days=365,
            delivery_history_days=180,
        )
        self.assertEqual((saved["event_details_days"], saved["delivery_history_days"]), (180, 180))
        self.assertFalse(saved["shortened"])

    def test_shortened_policy_hides_then_cleans_expired_details_and_preserves_counters_audit(self):
        initialize_database(self.database)
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds").replace("+00:00", "Z")
        record_event(
            self.database, event_id="sixty-day-event", event_type="training.epoch_completed", component="training",
            message="Retained under the original policy", details={"summary": "private detail"}, timestamp=old_time,
            counter_deltas={"training.epochs_completed": 1},
        )
        record_metric(self.database, metric_key="training.box_loss", value=0.4, source_event_id="sixty-day-event", dimensions={"epoch": 1}, provenance="test", timestamp=old_time)
        record_security_event(self.database, event_id="sixty-day-audit", event_type="test.retention", outcome="allowed", component="test", timestamp=old_time)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("INSERT INTO observability_siem_destinations(destination_id,display_name,transport,endpoint,enabled,secret_ref,redaction_profile,event_format,archived_at,dropped_events,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("d" * 32, "Fixture", "syslog_tls", "syslog-tls://logs.example.com", 0, "", "default", "json", None, 0, old_time, old_time))
            db.execute("INSERT INTO observability_delivery_events(delivery_id,destination_id,event_id,status,attempt_count,next_attempt_at,last_status_code,last_error_code,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", ("delivery-sixty-day", "d" * 32, "sixty-day-event", "sent", 1, None, None, None, old_time))
            db.commit()
        self.assertEqual(len(query_events(self.database, event_id="sixty-day-event")), 1)
        store.set_retention_policy(
            self.database, event_details_days=30, metric_observations_max_days=30,
            delivery_history_days=30, confirm_shorter=True,
        )
        self.assertEqual(query_events(self.database, event_id="sixty-day-event"), [], "new policy hides expired details immediately")
        before = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.assertEqual(
            query_metric_series(self.database, metric_key="training.box_loss", after=old_time, before=before),
            [], "new metric retention takes effect before the physical cleanup pass",
        )
        result = apply_retention(self.database)
        self.assertGreaterEqual(result["event_details_redacted"], 1)
        self.assertGreaterEqual(result["metric_observations_removed"], 1)
        self.assertEqual(result["delivery_events_removed"], 1)
        self.assertEqual(result["security_audit_rows_changed"], 0)
        self.assertEqual(overview(self.database)["kpis"]["training_epochs_completed"]["value"], 1)
        with closing(sqlite3.connect(self.database)) as db:
            event = db.execute("SELECT message,details_json FROM observability_events WHERE event_id='sixty-day-event'").fetchone()
            self.assertEqual(tuple(event), ("Expired event details redacted by VAOS retention policy.", "{}"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_security_audit WHERE event_id='sixty-day-audit'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM observability_delivery_events WHERE delivery_id='delivery-sixty-day'").fetchone()[0], 0)
        self.assertEqual(query_metric_series(self.database, metric_key="training.box_loss", after=old_time, before=before), [])
    def test_security_audit_chain_is_append_only_and_detects_tampering(self):
        initialize_database(self.database)
        params = {"event_type": "auth.login_failed", "outcome": "denied", "component": "auth", "actor_ref": "user-7", "details": {"reason": "bad password", "password": "do-not-save"}, "event_id": "audit-1", "timestamp": "2026-11-12T00:00:00Z"}
        self.assertTrue(record_security_event(self.database, **params))
        self.assertFalse(record_security_event(self.database, **params))
        self.assertTrue(verify_security_chain(self.database)["valid"])
        with closing(sqlite3.connect(self.database)) as db:
            details = db.execute("SELECT details_json FROM observability_security_audit").fetchone()[0]
            self.assertNotIn("do-not-save", details)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE observability_security_audit SET outcome='allowed' WHERE event_id='audit-1'")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("BEGIN")
            db.execute("DROP TRIGGER trg_obs_security_no_update")
            db.execute("UPDATE observability_security_audit SET outcome='allowed' WHERE event_id='audit-1'")
            db.commit()
        self.assertFalse(verify_security_chain(self.database)["valid"])

    def test_security_audit_actor_reference_filter_is_exact_and_bounded(self):
        initialize_database(self.database)
        for event_id, actor_ref in (("actor-a", "api-token:abc"), ("actor-b", "api-token:abcd"), ("actor-c", "local-admin")):
            record_security_event(
                self.database, event_id=event_id, event_type="auth.login_failed", outcome="denied",
                component="auth", actor_ref=actor_ref, timestamp="2026-11-12T00:00:00Z",
            )
        matches = query_security_audit(self.database, actor_ref="api-token:abc")
        self.assertEqual([item["event_id"] for item in matches], ["actor-a"])
        with self.assertRaisesRegex(ValueError, "120-character limit"):
            query_security_audit(self.database, actor_ref="a" * 121)


if __name__ == "__main__":
    unittest.main()
